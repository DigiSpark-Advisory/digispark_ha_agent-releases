"""Scope + volume bounds for the pattern-detection scan (SPEC.md 11, perf).

Copyright (c) 2026 DigiSpark Advisory LLC. All rights reserved.
Clean-room implementation - see PROVENANCE.md.

The detection engine is superlinear in the number of state-change events, so a
14-day scan over every exposed entity in a large home (hundreds of chatty
numeric sensors) does not finish in interactive time. These pure helpers bound
the work before it reaches the engine:

- ``in_scan_scope`` keeps only domains that can actually become a suggestion -
  controllable action targets and discrete-state triggers - so continuous
  numerics (sensor / number / weather) never enter the scan at all;
- ``bound_events`` drops any single entity that emits more than a per-entity
  cap of changes (a flapping device is noise, not a routine) and caps the total
  event count fed to the engine, keeping the most recent when it must trim.

Home-Assistant-free by design; the bridge runs these in an executor.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence

from .engine import StateEvent


def in_scan_scope(entity_id: str, domains: Iterable[str]) -> bool:
    """True if ``entity_id``'s domain is one the scan should include."""
    domain, sep, _ = entity_id.partition(".")
    return bool(sep) and domain in domains


def bound_events(
    events_by_entity: Mapping[str, Sequence[StateEvent]],
    *,
    per_entity_cap: int,
    total_cap: int,
) -> list[StateEvent]:
    """Flatten per-entity event lists into a bounded, time-sorted event stream.

    Entities over ``per_entity_cap`` are dropped whole (treated as noise). The
    surviving events are sorted by time; if still over ``total_cap`` the most
    recent ``total_cap`` are kept. Deterministic for a given input.
    """
    kept: list[StateEvent] = []
    for events in events_by_entity.values():
        if len(events) > per_entity_cap:
            continue
        kept.extend(events)
    kept.sort(key=lambda event: (event.when, event.entity_id, event.state))
    if total_cap is not None and len(kept) > total_cap:
        kept = kept[-total_cap:]
    return kept
