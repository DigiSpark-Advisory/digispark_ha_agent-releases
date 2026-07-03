"""Stale automation detection.

Copyright (c) 2026 DigiSpark Advisory LLC. All rights reserved.
Clean-room implementation authored from SPEC.md 13 - see PROVENANCE.md.

Deterministic, advisory-only analysis of agent-managed automations. Two
signals (SPEC 13): dangling references (trigger/condition/action entity ids
that no longer exist or are unavailable) and long-idle (no trigger within the
configurable window; an automation's first recorded version stands in for
activity when it has never triggered, so a fresh draft is not flagged).

Home-Assistant-free by design: the caller supplies the automation bodies, the
known/unavailable entity sets, and the per-automation activity timestamps, so
the analysis is pure and unit-testable. Findings are advisories only - this
module (and its callers) never disable or delete anything; the user acts.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Container
from datetime import datetime

from ..automations.writer import AGENT_ID_PREFIX
from ..const import STALE_IDLE_DAYS

_SCANNED_SECTIONS = ("trigger", "condition", "action")
_SECONDS_PER_DAY = 24 * 60 * 60

KIND_DANGLING = "dangling_reference"
KIND_LONG_IDLE = "long_idle"

_ACTION_DANGLING = "fix the reference, or disable or discard the automation"
_ACTION_IDLE = "review the automation; disable or discard it if it is dead"


class StaleDetector:
    """Flags dead/idle agent-managed automations (SPEC 13, advisory-only).

    ``idle_days`` is the long-idle window; ``clock`` returns the current time
    as Unix seconds and is injectable for deterministic tests.
    """

    def __init__(
        self,
        *,
        idle_days: int = STALE_IDLE_DAYS,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if idle_days < 1:
            raise ValueError("idle_days must be >= 1")
        self._idle_days = idle_days
        self._idle_seconds = idle_days * _SECONDS_PER_DAY
        self._clock = clock or time.time

    def scan(
        self,
        automations: list[dict],
        *,
        known_entities: Container[str],
        unavailable_entities: Container[str],
        last_triggered: dict[str, str | None] | None = None,
        created_at: dict[str, str | None] | None = None,
    ) -> list[dict]:
        """Return advisories for the supplied agent automations.

        ``known_entities`` are the entity ids that exist; ``unavailable_entities``
        the subset currently unavailable. ``last_triggered`` and ``created_at``
        map automation ids to ISO 8601 timestamps (or None). Non-agent entries
        are skipped defensively - advisories are scoped like every other
        agent-only surface.
        """
        last_triggered = last_triggered or {}
        created_at = created_at or {}
        advisories: list[dict] = []
        for automation in automations:
            if not isinstance(automation, dict):
                continue
            automation_id = str(automation.get("id", ""))
            if not automation_id.startswith(AGENT_ID_PREFIX):
                continue
            alias = str(automation.get("alias", ""))
            dangling = self._dangling_advisory(
                automation, automation_id, alias, known_entities, unavailable_entities
            )
            if dangling is not None:
                advisories.append(dangling)
            idle = self._idle_advisory(
                automation_id,
                alias,
                last_triggered.get(automation_id),
                created_at.get(automation_id),
            )
            if idle is not None:
                advisories.append(idle)
        return advisories

    def _dangling_advisory(
        self,
        automation: dict,
        automation_id: str,
        alias: str,
        known_entities: Container[str],
        unavailable_entities: Container[str],
    ) -> dict | None:
        references = sorted(_entity_references(automation))
        missing = [e for e in references if e not in known_entities]
        unavailable = [
            e for e in references if e in known_entities and e in unavailable_entities
        ]
        if not missing and not unavailable:
            return None
        parts: list[str] = []
        if missing:
            parts.append(f"references entities that no longer exist: {missing}")
        if unavailable:
            parts.append(f"references unavailable entities: {unavailable}")
        return {
            "automation_id": automation_id,
            "alias": alias,
            "kind": KIND_DANGLING,
            "detail": "; ".join(parts),
            "missing": missing,
            "unavailable": unavailable,
            "suggested_action": _ACTION_DANGLING,
        }

    def _idle_advisory(
        self,
        automation_id: str,
        alias: str,
        last_triggered: str | None,
        created_at: str | None,
    ) -> dict | None:
        reference = last_triggered or created_at
        if reference is None:
            return {
                "automation_id": automation_id,
                "alias": alias,
                "kind": KIND_LONG_IDLE,
                "detail": "has never triggered and has no recorded history",
                "suggested_action": _ACTION_IDLE,
            }
        moment = _parse_iso(reference)
        if moment is None:
            # An unparseable timestamp is not evidence of staleness.
            return None
        age_seconds = self._clock() - moment.timestamp()
        if age_seconds < self._idle_seconds:
            return None
        days = int(age_seconds // _SECONDS_PER_DAY)
        if last_triggered is not None:
            what = f"last triggered {days} days ago"
        else:
            what = f"has never triggered since it was created {days} days ago"
        return {
            "automation_id": automation_id,
            "alias": alias,
            "kind": KIND_LONG_IDLE,
            "detail": f"{what} (idle window: {self._idle_days} days)",
            "suggested_action": _ACTION_IDLE,
        }


def _entity_references(automation: dict) -> set[str]:
    """Entity ids referenced by the trigger/condition/action sections only.

    Free text (alias, description) is deliberately not scanned; templated or
    malformed values are ignored rather than guessed at.
    """
    found: set[str] = set()
    for section in _SCANNED_SECTIONS:
        _collect(automation.get(section), found)
    return found


def _collect(node: object, found: set[str]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "entity_id":
                _collect_entity_ids(value, found)
            else:
                _collect(value, found)
    elif isinstance(node, list):
        for item in node:
            _collect(item, found)


def _collect_entity_ids(value: object, found: set[str]) -> None:
    if isinstance(value, str):
        candidates: list[str] = [value]
    elif isinstance(value, list):
        candidates = [item for item in value if isinstance(item, str)]
    else:
        return
    for candidate in candidates:
        entity_id = candidate.strip().lower()
        if "{" in entity_id or " " in entity_id:
            continue  # templated or malformed; not a literal reference
        domain, sep, object_id = entity_id.partition(".")
        if sep and domain and object_id:
            found.add(entity_id)


def _parse_iso(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
