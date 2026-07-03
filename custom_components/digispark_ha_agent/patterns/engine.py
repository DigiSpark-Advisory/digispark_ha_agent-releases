"""Pattern detection -> automation-suggestion engine.

Copyright (c) 2026 DigiSpark Advisory LLC. All rights reserved.
Clean-room implementation authored from SPEC.md §11 - see PROVENANCE.md.

Deterministic analytics over recorder history the bridge supplies (owner
decision 2026-07-03: 14 days of state changes for exposed entities only -
PATTERN_LOOKBACK_DAYS). Three signals (SPEC §11):

- time-of-day routines: an entity reliably reaches a state near a clock time;
- device correlations: B tends to follow A within a short window;
- recurring sequences: a three-step ordered chain that repeats.

Confidence is support/consistency - how often the pattern holds over how
often its precondition occurs (active days for routines, occurrences of the
first step for correlations/sequences). Candidates below the confidence
floor or the support floor are suppressed.

Home-Assistant-free by design: the caller supplies the state-change events,
so the analysis is pure and unit-testable. No raw history leaves the process;
the LLM only ever sees an already-detected, compact candidate summary.

Every candidate carries a stable ``signature`` so the suggestion pipeline can
remember dismissals: a dismissed signature never resurfaces unless the
underlying pattern materially changes (owner decision 2026-07-03).
"""

from __future__ import annotations

import statistics
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime

from ..const import (
    PATTERN_CORRELATION_WINDOW_SECONDS,
    PATTERN_MIN_CONFIDENCE,
    PATTERN_MIN_SUPPORT,
    PATTERN_TIME_TOLERANCE_MINUTES,
)

KIND_TIME_OF_DAY = "time_of_day"
KIND_CORRELATION = "correlation"
KIND_SEQUENCE = "sequence"

_IGNORED_STATES = ("", "unknown", "unavailable")
_SECONDS_PER_DAY = 24 * 60 * 60
# Signatures bucket the routine time to the nearest half hour so day-to-day
# jitter does not mint a "new" pattern that escapes the dismissal store.
_SIGNATURE_BUCKET_MINUTES = 30

# Timezone-aware epoch reference; avoids datetime.timezone.utc, which ruff's
# py313 UP rules rewrite to datetime.UTC - unavailable on the py3.10 sandbox.
_EPOCH = datetime.fromisoformat("1970-01-01T00:00:00+00:00")

# (entity_id, state) - the atom every detector works over.
_Key = tuple[str, str]


@dataclass(frozen=True)
class StateEvent:
    """One recorder state change: ``entity_id`` became ``state`` at ``when``.

    ``when`` is Unix seconds (UTC).
    """

    entity_id: str
    state: str
    when: float

    @classmethod
    def from_iso(cls, entity_id: str, state: str, when: str) -> StateEvent:
        """Build an event from an ISO 8601 timestamp (naive means UTC)."""
        moment = datetime.fromisoformat(str(when).replace("Z", "+00:00"))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=_EPOCH.tzinfo)
        when_seconds = (moment - _EPOCH).total_seconds()
        return cls(entity_id=entity_id, state=state, when=when_seconds)


class PatternEngine:
    """Detects routines/correlations/sequences and scores them (SPEC §11).

    ``tz_offset_minutes`` shifts UTC event times into the home's local clock
    for the time-of-day detector; the bridge supplies HA's configured offset.
    All thresholds are injectable for tests; defaults come from const.py.
    """

    def __init__(
        self,
        *,
        min_confidence: float = PATTERN_MIN_CONFIDENCE,
        min_support: int = PATTERN_MIN_SUPPORT,
        time_tolerance_minutes: int = PATTERN_TIME_TOLERANCE_MINUTES,
        correlation_window_seconds: int = PATTERN_CORRELATION_WINDOW_SECONDS,
        tz_offset_minutes: int = 0,
    ) -> None:
        if not 0 < min_confidence <= 1:
            raise ValueError("min_confidence must be in (0, 1]")
        if min_support < 1:
            raise ValueError("min_support must be >= 1")
        if time_tolerance_minutes < 1:
            raise ValueError("time_tolerance_minutes must be >= 1")
        if correlation_window_seconds < 1:
            raise ValueError("correlation_window_seconds must be >= 1")
        self._min_confidence = min_confidence
        self._min_support = min_support
        self._tolerance = time_tolerance_minutes
        self._window = correlation_window_seconds
        self._tz_offset_seconds = tz_offset_minutes * 60

    def detect(self, events: Iterable[StateEvent]) -> list[dict]:
        """Return confidence-scored pattern candidates from raw events.

        Events may arrive unsorted; unknown/unavailable/blank states and
        malformed entity ids are dropped. Output is deterministic: sorted by
        confidence (descending) then signature.
        """
        cleaned = sorted(
            (
                e
                for e in events
                if e.state not in _IGNORED_STATES and "." in e.entity_id
            ),
            key=lambda e: (e.when, e.entity_id, e.state),
        )
        if not cleaned:
            return []
        sequences = [
            c
            for c in self._sequences(cleaned)
            if c["confidence"] >= self._min_confidence
        ]
        # A surviving three-step sequence subsumes its own first hop; the
        # bare (A, B) correlation would only restate it as noise.
        prefixes = {_steps_prefix(c) for c in sequences}
        candidates = sequences + [
            c
            for c in self._correlations(cleaned, suppress=prefixes)
            + self._time_of_day(cleaned)
            if c["confidence"] >= self._min_confidence
        ]
        return sorted(candidates, key=lambda c: (-c["confidence"], c["signature"]))

    # -- time-of-day routines ------------------------------------------------

    def _time_of_day(self, events: Sequence[StateEvent]) -> list[dict]:
        total_days = len({self._day_index(e.when) for e in events})
        by_key: dict[_Key, list[tuple[int, int]]] = {}
        for event in events:
            point = (self._day_index(event.when), self._minute_of_day(event.when))
            by_key.setdefault(_key(event), []).append(point)
        candidates: list[dict] = []
        for key in sorted(by_key):
            entity_id, state = key
            for cluster in _clusters(by_key[key], self._tolerance):
                days_hit = len({day for day, _ in cluster})
                if days_hit < self._min_support:
                    continue
                minutes = [minute for _, minute in cluster]
                center = round(statistics.median(minutes))
                bucket = (
                    round(center / _SIGNATURE_BUCKET_MINUTES)
                    * _SIGNATURE_BUCKET_MINUTES
                ) % (24 * 60)
                candidates.append(
                    {
                        "kind": KIND_TIME_OF_DAY,
                        "entities": [entity_id],
                        "confidence": round(days_hit / total_days, 3),
                        "support": days_hit,
                        "occurrences": total_days,
                        "description": (
                            f"{entity_id} is set to '{state}' around "
                            f"{_hhmm(center)} on {days_hit} of {total_days} "
                            "active days"
                        ),
                        "signature": (
                            f"time_of_day:{entity_id}:{state}:{_hhmm(bucket)}"
                        ),
                        "details": {
                            "state": state,
                            "minute_of_day": center,
                            "days_observed": total_days,
                        },
                    }
                )
        return candidates

    # -- device correlations -------------------------------------------------

    def _correlations(
        self, events: Sequence[StateEvent], *, suppress: set[tuple[_Key, _Key]]
    ) -> list[dict]:
        hits: dict[tuple[_Key, _Key], int] = {}
        delays: dict[tuple[_Key, _Key], list[float]] = {}
        first_counts: dict[_Key, int] = {}
        for i, first in enumerate(events):
            key_a = _key(first)
            first_counts[key_a] = first_counts.get(key_a, 0) + 1
            seen: set[_Key] = set()
            for follower in _within(events, i + 1, first.when + self._window):
                if follower.entity_id == first.entity_id:
                    continue
                key_b = _key(follower)
                if key_b in seen:
                    continue  # one hit per precondition occurrence
                seen.add(key_b)
                pair = (key_a, key_b)
                hits[pair] = hits.get(pair, 0) + 1
                delays.setdefault(pair, []).append(follower.when - first.when)
        candidates: list[dict] = []
        for pair in sorted(hits):
            if pair in suppress:
                continue
            (entity_a, state_a), (entity_b, state_b) = pair
            support = hits[pair]
            occurrences = first_counts[pair[0]]
            if support < self._min_support:
                continue
            delay = round(statistics.median(delays[pair]))
            candidates.append(
                {
                    "kind": KIND_CORRELATION,
                    "entities": [entity_a, entity_b],
                    "confidence": round(support / occurrences, 3),
                    "support": support,
                    "occurrences": occurrences,
                    "description": (
                        f"{entity_b} is set to '{state_b}' about {delay}s "
                        f"after {entity_a} is set to '{state_a}' "
                        f"({support} of {occurrences} times)"
                    ),
                    "signature": (
                        f"correlation:{entity_a}:{state_a}->{entity_b}:{state_b}"
                    ),
                    "details": {
                        "trigger": {"entity_id": entity_a, "state": state_a},
                        "result": {"entity_id": entity_b, "state": state_b},
                        "median_delay_seconds": delay,
                    },
                }
            )
        return candidates

    # -- recurring three-step sequences ---------------------------------------

    def _sequences(self, events: Sequence[StateEvent]) -> list[dict]:
        hits: dict[tuple[_Key, _Key, _Key], int] = {}
        first_counts: dict[_Key, int] = {}
        for i, first in enumerate(events):
            key_a = _key(first)
            first_counts[key_a] = first_counts.get(key_a, 0) + 1
            firsts_b: dict[_Key, float] = {}
            for second in _within(events, i + 1, first.when + self._window):
                if second.entity_id == first.entity_id:
                    continue
                firsts_b.setdefault(_key(second), second.when)
            for key_b, when_b in firsts_b.items():
                seen_c: set[_Key] = set()
                for third in _within(events, i + 1, when_b + self._window):
                    if third.when <= when_b:
                        continue
                    if third.entity_id in (first.entity_id, key_b[0]):
                        continue
                    key_c = _key(third)
                    if key_c in seen_c:
                        continue
                    seen_c.add(key_c)
                    triple = (key_a, key_b, key_c)
                    hits[triple] = hits.get(triple, 0) + 1
        candidates: list[dict] = []
        for triple in sorted(hits):
            support = hits[triple]
            occurrences = first_counts[triple[0]]
            if support < self._min_support:
                continue
            steps = [
                {"entity_id": entity_id, "state": state} for entity_id, state in triple
            ]
            chain = " then ".join(
                f"{step['entity_id']}='{step['state']}'" for step in steps
            )
            signature_body = ">".join(f"{e}:{s}" for e, s in triple)
            candidates.append(
                {
                    "kind": KIND_SEQUENCE,
                    "entities": [entity_id for entity_id, _ in triple],
                    "confidence": round(support / occurrences, 3),
                    "support": support,
                    "occurrences": occurrences,
                    "description": (
                        f"recurring sequence: {chain} "
                        f"({support} of {occurrences} times)"
                    ),
                    "signature": f"sequence:{signature_body}",
                    "details": {"steps": steps},
                }
            )
        return candidates

    # -- clock helpers ---------------------------------------------------------

    def _day_index(self, when: float) -> int:
        return int((when + self._tz_offset_seconds) // _SECONDS_PER_DAY)

    def _minute_of_day(self, when: float) -> int:
        return int((when + self._tz_offset_seconds) % _SECONDS_PER_DAY) // 60


def _key(event: StateEvent) -> _Key:
    return (event.entity_id, event.state)


def _within(
    events: Sequence[StateEvent], start: int, deadline: float
) -> Iterable[StateEvent]:
    """Yield events[start:] whose time is <= deadline (events are sorted)."""
    for index in range(start, len(events)):
        if events[index].when > deadline:
            return
        yield events[index]


def _clusters(
    points: list[tuple[int, int]], tolerance: int
) -> list[list[tuple[int, int]]]:
    """Group (day, minute) points whose consecutive minutes gap <= tolerance.

    Gap-based clustering over the sorted minutes; no midnight wraparound (a
    routine straddling 00:00 splits into two clusters and each must qualify
    on its own).
    """
    ordered = sorted(points, key=lambda p: (p[1], p[0]))
    grouped: list[list[tuple[int, int]]] = []
    for point in ordered:
        if grouped and point[1] - grouped[-1][-1][1] <= tolerance:
            grouped[-1].append(point)
        else:
            grouped.append([point])
    return grouped


def _steps_prefix(candidate: dict) -> tuple[_Key, _Key]:
    steps = candidate["details"]["steps"]
    return (
        (steps[0]["entity_id"], steps[0]["state"]),
        (steps[1]["entity_id"], steps[1]["state"]),
    )


def _hhmm(minute_of_day: int) -> str:
    return f"{minute_of_day // 60:02d}:{minute_of_day % 60:02d}"
