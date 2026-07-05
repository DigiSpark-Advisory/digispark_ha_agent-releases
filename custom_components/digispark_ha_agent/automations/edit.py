"""Validation for user-edited agent automation bodies (SPEC.md §6, §12).

The structured panel editor reconstructs an automation body and sends it to the
``update_automation`` WS command. This gate rejects a body that is not a mapping
with at least one trigger and one action before it reaches the §6 writer, so a
half-edited automation can't be written and then fail to load. Pure logic — no
Home Assistant imports; the caller runs it in an executor alongside the writer.
"""

from __future__ import annotations

from .writer import AutomationWriteError


def validate_automation_body(body: object) -> None:
    """Raise AutomationWriteError unless ``body`` is an editable automation.

    Requires a mapping with a non-empty ``trigger`` and a non-empty ``action``
    (a single mapping or a list both count). Does not judge the *contents* of
    triggers/actions — Home Assistant's own config validation does that on
    reload; this only guards the shape the writer needs.
    """
    if not isinstance(body, dict):
        raise AutomationWriteError("automation body must be a mapping")
    if not body.get("trigger"):
        raise AutomationWriteError("automation must include at least one trigger")
    if not body.get("action"):
        raise AutomationWriteError("automation must include at least one action")
