"""Sidebar panel registration (SPEC.md §9).

Copyright (c) 2026 DigiSpark Advisory LLC. All rights reserved.
Clean-room implementation authored from SPEC.md §9 — see PROVENANCE.md.

Serves the panel's ES module as a static asset and registers an admin-only
custom sidebar panel. Registration is global (once per Home Assistant) and
guarded so multiple config entries do not double-register.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from homeassistant.components import panel_custom
from homeassistant.components.frontend import async_remove_panel
from homeassistant.components.http import StaticPathConfig

from .const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_PANEL_URL_PATH = "digispark-ha-agent"
_PANEL_REGISTERED = "panel_registered"
_STATIC_URL = "/digispark_ha_agent_frontend"
_FRONTEND_DIR = Path(__file__).parent / "frontend"
_MODULE_URL = f"{_STATIC_URL}/digispark-panel.js"
_WEBCOMPONENT = "digispark-agent-panel"


async def async_register_panel(hass: HomeAssistant) -> None:
    """Register the static asset path and the sidebar panel exactly once."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    if domain_data.get(_PANEL_REGISTERED):
        return

    await hass.http.async_register_static_paths(
        [StaticPathConfig(_STATIC_URL, str(_FRONTEND_DIR), cache_headers=False)]
    )
    await panel_custom.async_register_panel(
        hass,
        frontend_url_path=_PANEL_URL_PATH,
        webcomponent_name=_WEBCOMPONENT,
        module_url=_MODULE_URL,
        sidebar_title="DigiSpark Agent",
        sidebar_icon="mdi:robot-happy-outline",
        require_admin=True,
    )
    domain_data[_PANEL_REGISTERED] = True


def async_unregister_panel(hass: HomeAssistant) -> None:
    """Remove the sidebar panel (called when the last entry unloads)."""
    domain_data = hass.data.get(DOMAIN, {})
    if not domain_data.get(_PANEL_REGISTERED):
        return
    async_remove_panel(hass, _PANEL_URL_PATH)
    domain_data[_PANEL_REGISTERED] = False
