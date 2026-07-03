"""DigiSpark HA Agent — integration setup.

Copyright (c) 2026 DigiSpark Advisory LLC. All rights reserved.
Clean-room implementation authored from SPEC.md §8–§9 — see PROVENANCE.md.

Home Assistant imports are done inside the setup/unload functions (not at module
level) so importing this package stays free of a Home Assistant dependency — the
pure-Python test suite imports sibling modules without a running HA. Only the
Home-Assistant-free ``runtime`` helper is imported at module level.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import TYPE_CHECKING

from .const import (
    DOMAIN,
    PATTERN_SCAN_INTERVAL_HOURS,
    STALE_SCAN_INTERVAL_HOURS,
)
from .runtime import build_agent

_LOGGER = logging.getLogger(__name__)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

PLATFORMS: list[str] = []

# Keys within hass.data[DOMAIN].
_AGENTS = "agents"
_RUNNERS = "runners"


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up DigiSpark HA Agent from a config entry (SPEC.md §8–§9)."""
    from homeassistant.helpers.aiohttp_client import async_get_clientsession
    from homeassistant.helpers.event import async_track_time_interval

    from .agent.tools import HomeToolRunner, tool_schemas
    from .ha_bridge import async_scan_patterns, async_scan_stale, build_adapters
    from .panel import async_register_panel
    from .ws import async_register_ws_handlers

    session = async_get_clientsession(hass)
    tool_runner = HomeToolRunner(build_adapters(hass))
    agent = build_agent(
        session,
        dict(entry.data),
        dict(entry.options),
        tool_runner=tool_runner,
        tools=tool_schemas(),
    )

    domain_data = hass.data.setdefault(DOMAIN, {})
    domain_data.setdefault(_AGENTS, {})[entry.entry_id] = agent
    # The WS layer needs the runner for pending-action confirm/deny.
    domain_data.setdefault(_RUNNERS, {})[entry.entry_id] = tool_runner

    async_register_ws_handlers(hass)
    await async_register_panel(hass)

    # Periodic advisory-only stale scan (SPEC §13); cancelled on unload.
    async def _scheduled_stale_scan(_now) -> None:
        try:
            await async_scan_stale(hass)
        except Exception:
            _LOGGER.exception("scheduled stale scan failed")

    entry.async_on_unload(
        async_track_time_interval(
            hass, _scheduled_stale_scan, timedelta(hours=STALE_SCAN_INTERVAL_HOURS)
        )
    )

    # Periodic pattern-detection scan (SPEC §11); cancelled on unload.
    async def _scheduled_pattern_scan(_now) -> None:
        try:
            await async_scan_patterns(hass)
        except Exception:
            _LOGGER.exception("scheduled pattern scan failed")

    entry.async_on_unload(
        async_track_time_interval(
            hass,
            _scheduled_pattern_scan,
            timedelta(hours=PATTERN_SCAN_INTERVAL_HOURS),
        )
    )

    # Rebuild the agent when the user edits model / max-tokens in the options flow.
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry; remove the panel when the last entry is gone."""
    domain_data = hass.data.get(DOMAIN, {})
    agents = domain_data.get(_AGENTS, {})
    agents.pop(entry.entry_id, None)
    domain_data.get(_RUNNERS, {}).pop(entry.entry_id, None)

    if not agents:
        from .panel import async_unregister_panel

        async_unregister_panel(hass)
    return True


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry so edited options take effect."""
    await hass.config_entries.async_reload(entry.entry_id)
