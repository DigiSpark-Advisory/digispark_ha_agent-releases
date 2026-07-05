"""Pattern detection -> automation-suggestion engine.

Copyright (c) 2026 DigiSpark Advisory LLC. All rights reserved.
Clean-room implementation authored from SPEC.md 11 - see PROVENANCE.md.

Deterministic analytics over recorder history the bridge supplies (owner
decision 2026-07-03: 14 days of state changes for exposed entities only -
PATTERN_LOOKBACK_DAYS). Three signals (SPEC 11):

- time-of-day routines: an entity reliably reaches a state near a clock time;
- device correlations: B tends to follow A within a short window;
- recurring sequences: a three-step ordered chain that repeats.

A candidate carries two scores. ``consistency`` is the raw pass rate - how
often the pattern holds over how often its precondition occurs (active days
for routines, precondition occurrences for correlations/sequences).
``confidence`` is a Wilson score lower bound on that rate, so a thinly-
evidenced pattern scores below a well-evidenced one instead of both reading
1.0. The acceptance floors gate on consistency (plus support and distinct
days); confidence drives display and ranking. Candidates below the floors
are suppressed.

Home-Assistant-free by design: the caller supplies the state-change events,
so the analysis is pure and unit-testable. No raw history leaves the process;
the LLM only ever sees an already-detected, compact candidate summary.

Every candidate carries a stable ``signature`` so the suggestion pipeline can
remember dismissals: a dismissed signature never resurfaces unless the
underlying pattern materially changes (owner decision 2026-07-03).
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime

from ..const import (
    PATTERN_BURST_ENTITY_FRACTION,
    PATTERN_BURST_MIN_ENTITIES,
    PATTERN_BURST_WINDOW_SECONDS,
    PATTERN_CONFIDENCE_Z,
    PATTERN_CORRELATION_WINDOW_SECONDS,
    PATTERN_MIN_CONFIDENCE,
    PATTERN_MIN_DISTINCT_DAYS,
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
    """Detects routines/correlations/sequences and scores them (SPEC 11).

    ``tz_offset_minutes`` shifts UTC event times into the home's local clock
    for the time-of-day detector; the bridge supplies HA's configured offset.
    All thresholds are injectable for tests; defaults come from const.py.
    """

    def __init__(
        self,
        *,
        min_confidence: float = PATTERN_MIN_CONFIDENCE,
        min_support: int = PATTERN_MIN_SUPPORT,
        min_distinct_days: int = PATTERN_MIN_DISTINCT_DAYS,
        time_tolerance_minutes: int = PATTERN_TIME_TOLERANCE_MINUTES,
        correlation_window_seconds: int = PATTERN_CORRELATION_WINDOW_SECONDS,
        burst_window_seconds: int = PATTERN_BURST_WINDOW_SECONDS,
        burst_min_entities: int = PATTERN_BURST_MIN_ENTITIES,
        burst_entity_fraction: float = PATTERN_BURST_ENTITY_FRACTION,
        confidence_z: float = PATTERN_CONFIDENCE_Z,
        tz_offset_minutes: int = 0,
    ) -> None:
        if not 0 < min_confidence <= 1:
            raise ValueError("min_confidence must be in (0, 1]")
        if min_support < 1:
            raise ValueError("min_support must be >= 1")
        if min_distinct_days < 1:
            raise ValueError("min_distinct_days must be >= 1")
        if time_tolerance_minutes < 1:
            raise ValueError("time_tolerance_minutes must be >= 1")
        if correlation_window_seconds < 1:
            raise ValueError("correlation_window_seconds must be >= 1")
        if burst_window_seconds < 1:
            raise ValueError("burst_window_seconds must be >= 1")
        if burst_min_entities < 1:
            raise ValueError("burst_min_entities must be >= 1")
        if not 0 < burst_entity_fraction <= 1:
            raise ValueError("burst_entity_fraction must be in (0, 1]")
        if confidence_z <= 0:
            raise ValueError("confidence_z must be > 0")
        self._min_confidence = min_confidence
        self._min_support = min_support
        self._min_distinct_days = min_distinct_days
        self._tolerance = time_tolerance_minutes
        self._window = correlation_window_seconds
        self._burst_window = burst_window_seconds
        self._burst_min_entities = burst_min_entities
        self._burst_entity_fraction = burst_entity_fraction
        self._confidence_z = confidence_z
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
        cleaned = self._drop_bursts(cleaned)
        if not cleaned:
            return []
        sequences = [
            c
            for c in self._sequences(cleaned)
            if c["consistency"] >= self._min_confidence
        ]
        # A surviving three-step sequence subsumes its own first hop; the
        # bare (A, B) correlation would only restate it as noise.
        prefixes = {_steps_prefix(c) for c in sequences}
        candidates = sequences + [
            c
            for c in self._correlations(cleaned, suppress=prefixes)
            + self._time_of_day(cleaned)
            if c["consistency"] >= self._min_confidence
        ]
        return sorted(candidates, key=lambda c: (-c["confidence"], c["signature"]))

    # -- startup-cascade filter ----------------------------------------------

    def _drop_bursts(self, events: Sequence[StateEvent]) -> list[StateEvent]:
        """Remove events inside startup-cascade windows.

        On every Home Assistant restart a large fraction of entities change
        state within a few seconds - add-on ``*_running`` sensors flip on,
        helpers restore their last value. Those co-occurrences are boot
        artifacts, not behaviour, yet they inflated the correlation engine
        (owner report 2026-07-05). Any ``_burst_window``-second span in which
        at least ``threshold`` distinct entities change is treated as a
        cascade and dropped. The threshold scales with the dataset (a
        fraction of the distinct entities present) but never dips below
        ``_burst_min_entities``, so ordinary multi-device activity - a scene,
        one busy room - is never mistaken for a cascade.
        """
        total_entities = len({e.entity_id for e in events})
        if total_entities < self._burst_min_entities:
            return list(events)
        threshold = max(
            self._burst_min_entities,
            math.ceil(total_entities * self._burst_entity_fraction),
        )
        counts: dict[str, int] = {}
        distinct = 0
        left = 0
        burst = [False] * len(events)
        for right, event in enumerate(events):
            if counts.get(event.entity_id, 0) == 0:
                distinct += 1
            counts[event.entity_id] = counts.get(event.entity_id, 0) + 1
            while event.when - events[left].when > self._burst_window:
                left_id = events[left].entity_id
                counts[left_id] -= 1
                if counts[left_id] == 0:
                    distinct -= 1
                left += 1
            if distinct >= threshold:
                for index in range(left, right + 1):
                    burst[index] = True
        return [event for index, event in enumerate(events) if not burst[index]]

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
                        "confidence": round(
                            _confidence_score(days_hit, total_days, self._confidence_z),
                            3,
                        ),
                        "consistency": round(days_hit / total_days, 3),
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
        days: dict[tuple[_Key, _Key], set[int]] = {}
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
                days.setdefault(pair, set()).add(self._day_index(first.when))
        candidates: list[dict] = []
        for pair in sorted(hits):
            if pair in suppress:
                continue
            (entity_a, state_a), (entity_b, state_b) = pair
            support = hits[pair]
            occurrences = first_counts[pair[0]]
            if support < self._min_support:
                continue
            if len(days[pair]) < self._min_distinct_days:
                continue
            delay = round(statistics.median(delays[pair]))
            candidates.append(
                {
                    "kind": KIND_CORRELATION,
                    "entities": [entity_a, entity_b],
                    "confidence": round(
                        _confidence_score(support, occurrences, self._confidence_z),
                        3,
                    ),
                    "consistency": round(support / occurrences, 3),
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
        days: dict[tuple[_Key, _Key, _Key], set[int]] = {}
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
                    days.setdefault(triple, set()).add(self._day_index(first.when))
        candidates: list[dict] = []
        for triple in sorted(hits):
            support = hits[triple]
            occurrences = first_counts[triple[0]]
            if support < self._min_support:
                continue
            if len(days[triple]) < self._min_distinct_days:
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
                    "confidence": round(
                        _confidence_score(support, occurrences, self._confidence_z),
                        3,
                    ),
                    "consistency": round(support / occurrences, 3),
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


def _confidence_score(support: int, occurrences: int, z: float) -> float:
    """Wilson score lower bound on the pass rate ``support / occurrences``.

    Evidence-aware: for a perfect rate it reduces to ``n / (n + z**2)``, so a
    pattern seen 5/5 scores well below one seen 500/500. ``z`` is the
    standard-normal quantile; larger ``z`` is more conservative. Result is
    clamped to ``[0, 1]``.
    """
    if occurrences <= 0:
        return 0.0
    n = occurrences
    p = support / n
    z2 = z * z
    centre = p + z2 / (2 * n)
    margin = z * math.sqrt((p * (1 - p) + z2 / (4 * n)) / n)
    return max(0.0, min(1.0, (centre - margin) / (1 + z2 / n)))
