"""Automation authoring. See SPEC.md §6 and PROVENANCE.md."""

from __future__ import annotations

from .writer import (
    AutomationWriteError,
    accept_draft,
    discard_draft,
    get_agent_automation,
    list_agent_automation_bodies,
    list_agent_automations,
    replace_agent_automation,
    sanitize_draft,
    write_draft_automation,
)

__all__ = [
    "AutomationWriteError",
    "accept_draft",
    "discard_draft",
    "get_agent_automation",
    "list_agent_automation_bodies",
    "list_agent_automations",
    "replace_agent_automation",
    "sanitize_draft",
    "write_draft_automation",
]
