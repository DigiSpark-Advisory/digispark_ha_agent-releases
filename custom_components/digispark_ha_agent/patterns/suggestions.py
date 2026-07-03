"""Suggestion store + deterministic automation synthesis.

Copyright (c) 2026 DigiSpark Advisory LLC. All rights reserved.
Clean-room implementation authored from SPEC.md 11 - see PROVENANCE.md.

Pattern candidates from the engine become reviewable suggestions here.
Nothing is written to Home Assistant until the user explicitly accepts a
suggestion; acceptance then runs the normal SPEC 6 draft path (disabled,
agent-prefixed, versioned). Owner decisions, 2026-07-03:

- Separate suggestions store with explicit accept-into-draft (fork c).
- Dismissal is permanent by signature: a dismissed (or accepted) signature
  never resurfaces unless the underlying pattern materially changes and so
  mints a new signature (fork b).
- Automation bodies are synthesized deterministically - no LLM involvement.
  Only candidates whose action targets map onto a small state->service table
  (aligned with the SPEC 5 allowlist domains) become suggestions at all, so
  an accept can never dead-end.

Pure filesystem logic - no Home Assistant imports; callers run it in an
executor. The store file is JSON:

    {"schema": 1, "suggestions": {"<signature>": <record>}}

Writes are atomic (temp file + fsync + os.replace) preserving the original
file's permission bits; a corrupt or wrong-shape file is rejected untouched,
exactly like the version store (SPEC 12) it is modeled on.
"""

from __future__ import annotations

import contextlib
import json
import os
import stat
import tempfile
from collections.abc import Callable
from pathlib import Path

from ..versioning import utc_now_iso

SCHEMA_VERSION = 1

STATUS_PENDING = "pending"
STATUS_DISMISSED = "dismissed"
STATUS_ACCEPTED = "accepted"

# Action targets the synthesizer can express, aligned with the SPEC 5
# service-allowlist domains. climate/scene are deliberately absent: their
# states do not map onto a service deterministically (backlog).
_STATE_SERVICES: dict[str, dict[str, str]] = {
    "light": {"on": "turn_on", "off": "turn_off"},
    "switch": {"on": "turn_on", "off": "turn_off"},
    "fan": {"on": "turn_on", "off": "turn_off"},
    "input_boolean": {"on": "turn_on", "off": "turn_off"},
    "media_player": {"on": "turn_on", "off": "turn_off"},
    "cover": {"open": "open_cover", "closed": "close_cover"},
}


class SuggestionStoreError(ValueError):
    """The suggestion store could not be read or written safely."""


class SuggestionStore:
    """Reviewable pattern suggestions with permanent signature memory.

    One store instance manages one JSON file. Methods are synchronous and do
    disk I/O; Home Assistant callers must run them in an executor. ``clock``
    is injectable for deterministic tests and must return an ISO 8601 string.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        clock: Callable[[], str] | None = None,
    ) -> None:
        self._path = Path(path)
        self._clock = clock or utc_now_iso

    def upsert_candidates(self, candidates: list[dict]) -> dict:
        """Merge one detection run into the store; return summary counts.

        New signatures become pending suggestions. Existing pending
        suggestions get the fresh candidate payload and ``last_seen``.
        Dismissed and accepted signatures are left untouched - they never
        resurface (owner decision, 2026-07-03). Pending suggestions absent
        from this run are kept: a pattern may dip below the confidence floor
        transiently, and the panel shows ``last_seen`` for staleness.
        """
        data = self._load()
        now = str(self._clock())
        counts = {"new": 0, "refreshed": 0, "suppressed": 0}
        for candidate in candidates:
            signature = _candidate_signature(candidate)
            record = data["suggestions"].get(signature)
            if record is None:
                data["suggestions"][signature] = {
                    "signature": signature,
                    "status": STATUS_PENDING,
                    "candidate": _plain_json_mapping(candidate),
                    "first_seen": now,
                    "last_seen": now,
                    "decided_at": None,
                    "decided_by": None,
                    "automation_id": None,
                }
                counts["new"] += 1
            elif record["status"] == STATUS_PENDING:
                record["candidate"] = _plain_json_mapping(candidate)
                record["last_seen"] = now
                counts["refreshed"] += 1
            else:
                counts["suppressed"] += 1
        self._write(data)
        return counts

    def list_suggestions(self, status: str | None = STATUS_PENDING) -> list[dict]:
        """Suggestions with the given status (None for all), best first.

        Sorted by candidate confidence (descending) then signature, matching
        the engine's deterministic ordering.
        """
        records = [
            json.loads(json.dumps(record))
            for record in self._load()["suggestions"].values()
            if status is None or record["status"] == status
        ]
        return sorted(
            records,
            key=lambda r: (-r["candidate"].get("confidence", 0), r["signature"]),
        )

    def get(self, signature: str) -> dict:
        """The full record for one signature."""
        record = self._load()["suggestions"].get(str(signature))
        if record is None:
            raise SuggestionStoreError(f"no suggestion with signature {signature!r}")
        return json.loads(json.dumps(record))

    def dismiss(self, signature: str, *, author: str) -> dict:
        """Permanently dismiss a pending suggestion (fork b: never resurfaces)."""
        return self._decide(
            signature, status=STATUS_DISMISSED, author=author, automation_id=None
        )

    def mark_accepted(self, signature: str, *, author: str, automation_id: str) -> dict:
        """Record that a pending suggestion became a draft automation."""
        return self._decide(
            signature,
            status=STATUS_ACCEPTED,
            author=author,
            automation_id=str(automation_id),
        )

    def _decide(
        self,
        signature: str,
        *,
        status: str,
        author: str,
        automation_id: str | None,
    ) -> dict:
        data = self._load()
        record = data["suggestions"].get(str(signature))
        if record is None:
            raise SuggestionStoreError(f"no suggestion with signature {signature!r}")
        if record["status"] != STATUS_PENDING:
            raise SuggestionStoreError(
                f"suggestion {signature!r} is already {record['status']}"
            )
        record["status"] = status
        record["decided_at"] = str(self._clock())
        record["decided_by"] = str(author)
        record["automation_id"] = automation_id
        self._write(data)
        return json.loads(json.dumps(record))

    def _load(self) -> dict:
        """Load and shape-check the store file; reject anything suspect."""
        if not self._path.exists():
            return {"schema": SCHEMA_VERSION, "suggestions": {}}
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as err:
            raise SuggestionStoreError(
                f"suggestion store could not be parsed: {err}"
            ) from err
        if (
            not isinstance(raw, dict)
            or raw.get("schema") != SCHEMA_VERSION
            or not isinstance(raw.get("suggestions"), dict)
            or not all(
                isinstance(record, dict) for record in raw["suggestions"].values()
            )
        ):
            raise SuggestionStoreError(
                "suggestion store file has an unexpected shape; "
                "refusing to overwrite it"
            )
        return raw

    def _write(self, data: dict) -> None:
        """Atomically replace the store file, preserving permission bits."""
        new_text = json.dumps(data, indent=2, ensure_ascii=False, sort_keys=False)
        mode: int | None = None
        if self._path.exists():
            mode = stat.S_IMODE(self._path.stat().st_mode)
        try:
            fd, tmp_name = tempfile.mkstemp(
                dir=str(self._path.parent), prefix=".suggestions_", suffix=".tmp"
            )
        except OSError as err:
            raise SuggestionStoreError(
                f"suggestion store could not be written: {err}"
            ) from err
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(new_text)
                handle.flush()
                os.fsync(handle.fileno())
            if mode is not None:
                os.chmod(tmp_name, mode)
            os.replace(tmp_name, self._path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
            raise


# -- deterministic automation synthesis ------------------------------------------


def synthesize_automation(candidate: dict) -> dict | None:
    """Build a draft-ready automation body from a pattern candidate, or None.

    Deterministic by design (owner decision, 2026-07-03): triggers come from
    the detected pattern, actions from the ``_STATE_SERVICES`` table. Returns
    None when any action target cannot be expressed - such candidates must
    not become suggestions. The result carries no id/enabled flags; the
    SPEC 6 writer assigns those (disabled, agent-prefixed) on accept.
    """
    if not isinstance(candidate, dict):
        return None
    kind = candidate.get("kind")
    details = candidate.get("details") or {}
    confidence = candidate.get("confidence", 0)

    if kind == "time_of_day":
        entities = candidate.get("entities") or []
        if len(entities) != 1:
            return None
        action = _action_for(entities[0], str(details.get("state", "")))
        minute = details.get("minute_of_day")
        if action is None or not isinstance(minute, int):
            return None
        at = f"{minute // 60:02d}:{minute % 60:02d}:00"
        trigger = [{"platform": "time", "at": at}]
        alias = f"{entities[0]} {details['state']} at {at[:5]}"
        actions = [action]
    elif kind == "correlation":
        source = details.get("trigger") or {}
        result = details.get("result") or {}
        action = _action_for(
            str(result.get("entity_id", "")), str(result.get("state", ""))
        )
        if action is None or not source.get("entity_id"):
            return None
        trigger = [
            {
                "platform": "state",
                "entity_id": str(source["entity_id"]),
                "to": str(source.get("state", "")),
            }
        ]
        alias = (
            f"{result.get('entity_id')} {result.get('state')} "
            f"when {source['entity_id']} {source.get('state')}"
        )
        actions = [action]
    elif kind == "sequence":
        steps = details.get("steps") or []
        if len(steps) != 3:
            return None
        actions = []
        for step in steps[1:]:
            action = _action_for(
                str(step.get("entity_id", "")), str(step.get("state", ""))
            )
            if action is None:
                return None
            actions.append(action)
        first = steps[0]
        if not first.get("entity_id"):
            return None
        trigger = [
            {
                "platform": "state",
                "entity_id": str(first["entity_id"]),
                "to": str(first.get("state", "")),
            }
        ]
        chain = " then ".join(
            f"{step.get('entity_id')} {step.get('state')}" for step in steps[1:]
        )
        alias = f"{chain} when {first['entity_id']} {first.get('state')}"
    else:
        return None

    description = str(candidate.get("description", "")).strip()
    percent = int(round(float(confidence) * 100))
    suffix = f"Detected pattern (confidence {percent}%)."
    return {
        "alias": alias,
        "description": f"{description}. {suffix}" if description else suffix,
        "trigger": trigger,
        "action": actions,
        "mode": "single",
    }


def _action_for(entity_id: str, state: str) -> dict | None:
    """One service-call action reaching ``state`` on ``entity_id``, or None."""
    domain, sep, object_id = str(entity_id).partition(".")
    if not sep or not domain or not object_id:
        return None
    service = _STATE_SERVICES.get(domain, {}).get(str(state))
    if service is None:
        return None
    return {
        "service": f"{domain}.{service}",
        "target": {"entity_id": entity_id},
    }


def _candidate_signature(candidate: dict) -> str:
    signature = str(candidate.get("signature", "")).strip()
    if not signature:
        raise SuggestionStoreError("candidate has no signature")
    return signature


def _plain_json_mapping(candidate: dict) -> dict:
    """Deep-normalize a candidate to plain JSON types; refuse anything else."""
    if not isinstance(candidate, dict):
        raise SuggestionStoreError("candidate must be a mapping")
    try:
        return json.loads(json.dumps(candidate))
    except (TypeError, ValueError) as err:
        raise SuggestionStoreError(
            f"candidate is not JSON-serializable: {err}"
        ) from err
