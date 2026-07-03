"""Authenticated WebSocket command handlers.

Copyright (c) 2026 DigiSpark Advisory LLC. All rights reserved.
Clean-room implementation authored from SPEC.md §8 — see PROVENANCE.md.

The frontend talks to the backend over Home Assistant's authenticated WebSocket
API. Every command here is admin-gated (``require_admin``). Version/manifest
lookups use the async integration loader (no blocking I/O on the event loop).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.loader import async_get_integration

from ..automations.writer import AutomationWriteError
from ..const import DOMAIN
from ..ha_bridge import (
    async_accept_draft,
    async_accept_suggestion,
    async_discard_draft,
    async_dismiss_suggestion,
    async_get_version,
    async_list_drafts,
    async_list_suggestions,
    async_list_versions,
    async_rollback,
    async_stale_advisories,
)
from ..patterns.suggestions import SuggestionStoreError
from ..runtime import chat_response, history_response
from ..versioning import VersionStoreError

if TYPE_CHECKING:
    from homeassistant.components.websocket_api import ActiveConnection

_AGENTS = "agents"
_RUNNERS = "runners"
_WS_REGISTERED = "ws_registered"


@callback
def async_register_ws_handlers(hass: HomeAssistant) -> None:
    """Register the integration's WebSocket commands exactly once."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    if domain_data.get(_WS_REGISTERED):
        return
    websocket_api.async_register_command(hass, websocket_chat)
    websocket_api.async_register_command(hass, websocket_history)
    websocket_api.async_register_command(hass, websocket_info)
    websocket_api.async_register_command(hass, websocket_pending_actions)
    websocket_api.async_register_command(hass, websocket_confirm_action)
    websocket_api.async_register_command(hass, websocket_deny_action)
    websocket_api.async_register_command(hass, websocket_list_drafts)
    websocket_api.async_register_command(hass, websocket_accept_draft)
    websocket_api.async_register_command(hass, websocket_discard_draft)
    websocket_api.async_register_command(hass, websocket_list_versions)
    websocket_api.async_register_command(hass, websocket_get_version)
    websocket_api.async_register_command(hass, websocket_rollback)
    websocket_api.async_register_command(hass, websocket_stale_advisories)
    websocket_api.async_register_command(hass, websocket_list_suggestions)
    websocket_api.async_register_command(hass, websocket_dismiss_suggestion)
    websocket_api.async_register_command(hass, websocket_accept_suggestion)
    domain_data[_WS_REGISTERED] = True


@callback
def _resolve(hass: HomeAssistant, key: str, entry_id: str | None) -> Any:
    """Return the stored object for entry_id, or the sole one if unambiguous."""
    store: dict = hass.data.get(DOMAIN, {}).get(key, {})
    if entry_id is not None:
        return store.get(entry_id)
    if len(store) == 1:
        return next(iter(store.values()))
    return None


@callback
def _resolve_agent(hass: HomeAssistant, entry_id: str | None) -> Any:
    """Return the agent for entry_id, or the sole agent if unambiguous."""
    return _resolve(hass, _AGENTS, entry_id)


def _author(connection: ActiveConnection) -> str:
    """Version-record author string for the requesting admin (SPEC.md §12)."""
    user = getattr(connection, "user", None)
    user_id = getattr(user, "id", None)
    return f"user:{user_id}" if user_id else "user"


def _action_payload(action: Any) -> dict:
    return {
        "id": action.id,
        "domain": action.domain,
        "service": action.service,
        "entity_id": action.entity_id,
        "data": dict(action.data),
        "reason": action.reason,
    }


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): "digispark_ha_agent/chat",
        vol.Required("message"): str,
        vol.Optional("entry_id"): str,
    }
)
@websocket_api.async_response
async def websocket_chat(
    hass: HomeAssistant, connection: ActiveConnection, msg: dict
) -> None:
    """Run one agent turn and return the answer (admin-gated, SPEC.md §8)."""
    agent = _resolve_agent(hass, msg.get("entry_id"))
    if agent is None:
        connection.send_error(
            msg["id"], "not_found", "No configured DigiSpark HA Agent entry"
        )
        return
    try:
        result = await agent.run_turn(msg["message"])
    except Exception as err:  # surface provider/loop errors to the client
        connection.send_error(msg["id"], "chat_failed", str(err))
        return
    connection.send_result(msg["id"], chat_response(result))


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): "digispark_ha_agent/history",
        vol.Optional("entry_id"): str,
    }
)
@websocket_api.async_response
async def websocket_history(
    hass: HomeAssistant, connection: ActiveConnection, msg: dict
) -> None:
    """Return the committed conversation for session restore (SPEC.md §7, §9)."""
    agent = _resolve_agent(hass, msg.get("entry_id"))
    if agent is None:
        connection.send_error(
            msg["id"], "not_found", "No configured DigiSpark HA Agent entry"
        )
        return
    connection.send_result(msg["id"], {"messages": history_response(agent)})


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): "digispark_ha_agent/info"})
@websocket_api.async_response
async def websocket_info(
    hass: HomeAssistant, connection: ActiveConnection, msg: dict
) -> None:
    """Return the integration version and configured-entry count (async lookup)."""
    integration = await async_get_integration(hass, DOMAIN)
    agents: dict = hass.data.get(DOMAIN, {}).get(_AGENTS, {})
    connection.send_result(
        msg["id"],
        {"version": str(integration.version), "entries": len(agents)},
    )


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): "digispark_ha_agent/pending_actions",
        vol.Optional("entry_id"): str,
    }
)
@websocket_api.async_response
async def websocket_pending_actions(
    hass: HomeAssistant, connection: ActiveConnection, msg: dict
) -> None:
    """List elevated actions awaiting user confirmation (SPEC.md §5)."""
    runner = _resolve(hass, _RUNNERS, msg.get("entry_id"))
    if runner is None:
        connection.send_error(
            msg["id"], "not_found", "No configured DigiSpark HA Agent entry"
        )
        return
    connection.send_result(
        msg["id"],
        {"actions": [_action_payload(a) for a in runner.pending_actions()]},
    )


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): "digispark_ha_agent/confirm_action",
        vol.Required("action_id"): str,
        vol.Optional("entry_id"): str,
    }
)
@websocket_api.async_response
async def websocket_confirm_action(
    hass: HomeAssistant, connection: ActiveConnection, msg: dict
) -> None:
    """Execute one pending elevated action the user approved (SPEC.md §5)."""
    runner = _resolve(hass, _RUNNERS, msg.get("entry_id"))
    if runner is None:
        connection.send_error(
            msg["id"], "not_found", "No configured DigiSpark HA Agent entry"
        )
        return
    try:
        action = await runner.confirm_pending(msg["action_id"])
    except KeyError:
        connection.send_error(msg["id"], "not_found", "No such pending action")
        return
    except Exception as err:  # surface HA service errors to the client
        connection.send_error(msg["id"], "confirm_failed", str(err))
        return
    connection.send_result(msg["id"], {"executed": _action_payload(action)})


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): "digispark_ha_agent/deny_action",
        vol.Required("action_id"): str,
        vol.Optional("entry_id"): str,
    }
)
@websocket_api.async_response
async def websocket_deny_action(
    hass: HomeAssistant, connection: ActiveConnection, msg: dict
) -> None:
    """Discard one pending elevated action without executing it."""
    runner = _resolve(hass, _RUNNERS, msg.get("entry_id"))
    if runner is None:
        connection.send_error(
            msg["id"], "not_found", "No configured DigiSpark HA Agent entry"
        )
        return
    try:
        action = runner.deny_pending(msg["action_id"])
    except KeyError:
        connection.send_error(msg["id"], "not_found", "No such pending action")
        return
    connection.send_result(msg["id"], {"denied": _action_payload(action)})


@websocket_api.require_admin
@websocket_api.websocket_command(
    {vol.Required("type"): "digispark_ha_agent/list_drafts"}
)
@websocket_api.async_response
async def websocket_list_drafts(
    hass: HomeAssistant, connection: ActiveConnection, msg: dict
) -> None:
    """List agent-managed automations for review (SPEC.md §6)."""
    try:
        drafts = await async_list_drafts(hass)
    except AutomationWriteError as err:
        connection.send_error(msg["id"], "drafts_failed", str(err))
        return
    connection.send_result(msg["id"], {"drafts": drafts})


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): "digispark_ha_agent/accept_draft",
        vol.Required("automation_id"): str,
    }
)
@websocket_api.async_response
async def websocket_accept_draft(
    hass: HomeAssistant, connection: ActiveConnection, msg: dict
) -> None:
    """Accept a drafted automation: persist the enable and turn it on."""
    try:
        result = await async_accept_draft(
            hass, msg["automation_id"], author=_author(connection)
        )
    except AutomationWriteError as err:
        connection.send_error(msg["id"], "draft_failed", str(err))
        return
    connection.send_result(msg["id"], result)


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): "digispark_ha_agent/discard_draft",
        vol.Required("automation_id"): str,
    }
)
@websocket_api.async_response
async def websocket_discard_draft(
    hass: HomeAssistant, connection: ActiveConnection, msg: dict
) -> None:
    """Discard a drafted automation (explicit user action; agent-scoped)."""
    try:
        result = await async_discard_draft(
            hass, msg["automation_id"], author=_author(connection)
        )
    except AutomationWriteError as err:
        connection.send_error(msg["id"], "draft_failed", str(err))
        return
    connection.send_result(msg["id"], result)


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): "digispark_ha_agent/list_versions",
        vol.Required("automation_id"): str,
    }
)
@websocket_api.async_response
async def websocket_list_versions(
    hass: HomeAssistant, connection: ActiveConnection, msg: dict
) -> None:
    """List one agent automation's version history (SPEC.md §12)."""
    try:
        versions = await async_list_versions(hass, msg["automation_id"])
    except VersionStoreError as err:
        connection.send_error(msg["id"], "versions_failed", str(err))
        return
    connection.send_result(msg["id"], {"versions": versions})


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): "digispark_ha_agent/get_version",
        vol.Required("automation_id"): str,
        vol.Required("version"): int,
        vol.Optional("diff_against"): int,
    }
)
@websocket_api.async_response
async def websocket_get_version(
    hass: HomeAssistant, connection: ActiveConnection, msg: dict
) -> None:
    """Return one full version record, with an optional diff (SPEC.md §12)."""
    try:
        result = await async_get_version(
            hass, msg["automation_id"], msg["version"], msg.get("diff_against")
        )
    except VersionStoreError as err:
        connection.send_error(msg["id"], "versions_failed", str(err))
        return
    connection.send_result(msg["id"], result)


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): "digispark_ha_agent/rollback",
        vol.Required("automation_id"): str,
        vol.Required("version"): int,
    }
)
@websocket_api.async_response
async def websocket_rollback(
    hass: HomeAssistant, connection: ActiveConnection, msg: dict
) -> None:
    """Roll one agent automation back to a recorded version (SPEC.md §12)."""
    try:
        result = await async_rollback(
            hass, msg["automation_id"], msg["version"], author=_author(connection)
        )
    except (AutomationWriteError, VersionStoreError) as err:
        connection.send_error(msg["id"], "rollback_failed", str(err))
        return
    connection.send_result(msg["id"], {"restored": result})


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): "digispark_ha_agent/stale_advisories",
        vol.Optional("rescan"): bool,
    }
)
@websocket_api.async_response
async def websocket_stale_advisories(
    hass: HomeAssistant, connection: ActiveConnection, msg: dict
) -> None:
    """Return stale-automation advisories, rescanning on request (SPEC.md §13)."""
    try:
        result = await async_stale_advisories(hass, rescan=msg.get("rescan", False))
    except AutomationWriteError as err:
        connection.send_error(msg["id"], "stale_failed", str(err))
        return
    connection.send_result(msg["id"], result)


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): "digispark_ha_agent/list_suggestions",
        vol.Optional("rescan"): bool,
    }
)
@websocket_api.async_response
async def websocket_list_suggestions(
    hass: HomeAssistant, connection: ActiveConnection, msg: dict
) -> None:
    """Return pending pattern suggestions, rescanning on request (SPEC.md §11)."""
    try:
        result = await async_list_suggestions(hass, rescan=msg.get("rescan", False))
    except SuggestionStoreError as err:
        connection.send_error(msg["id"], "suggestions_failed", str(err))
        return
    connection.send_result(msg["id"], result)


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): "digispark_ha_agent/dismiss_suggestion",
        vol.Required("signature"): str,
    }
)
@websocket_api.async_response
async def websocket_dismiss_suggestion(
    hass: HomeAssistant, connection: ActiveConnection, msg: dict
) -> None:
    """Permanently dismiss one pending suggestion (SPEC.md §11)."""
    try:
        result = await async_dismiss_suggestion(
            hass, msg["signature"], author=_author(connection)
        )
    except SuggestionStoreError as err:
        connection.send_error(msg["id"], "suggestions_failed", str(err))
        return
    connection.send_result(msg["id"], result)


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): "digispark_ha_agent/accept_suggestion",
        vol.Required("signature"): str,
    }
)
@websocket_api.async_response
async def websocket_accept_suggestion(
    hass: HomeAssistant, connection: ActiveConnection, msg: dict
) -> None:
    """Accept one suggestion into a disabled §6 draft (SPEC.md §11)."""
    try:
        result = await async_accept_suggestion(
            hass, msg["signature"], author=_author(connection)
        )
    except (AutomationWriteError, SuggestionStoreError) as err:
        connection.send_error(msg["id"], "suggestions_failed", str(err))
        return
    connection.send_result(msg["id"], result)
