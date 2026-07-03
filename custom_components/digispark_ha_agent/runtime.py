"""Runtime wiring for a config entry: build the agent, serialize WS responses.

Copyright (c) 2026 DigiSpark Advisory LLC. All rights reserved.
Clean-room implementation authored from SPEC.md §2–§4 and §8 — see PROVENANCE.md.

This module has no Home Assistant imports so the wiring is unit-testable without
a running HA. ``__init__.py`` (setup) and ``ws`` (WebSocket handlers) build on it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .agent.local_loop import LocalAgentLoop
from .agent.loop import AgentLoop
from .config_schema import OPTION_DEFAULTS
from .const import (
    CONF_API_KEY,
    CONF_HOST,
    CONF_MAX_TOKENS,
    CONF_MODEL,
    CONF_PROVIDER,
    DEFAULT_LOCAL_HOST,
    PROVIDER_ANTHROPIC,
    PROVIDER_LOCAL,
)
from .providers.anthropic import AnthropicProvider
from .providers.local import LocalProvider

if TYPE_CHECKING:
    import aiohttp

    from .agent.loop import TurnResult


class UnsupportedProviderError(ValueError):
    """Raised when a config entry names a provider that isn't supported yet."""


def build_agent(
    session: aiohttp.ClientSession,
    data: dict,
    options: dict,
    *,
    tool_runner=None,
    tools: list[dict] | None = None,
):
    """Build the agent loop for a config entry (SPEC.md §2–§4, §8).

    ``data`` holds the provider id and API key; ``options`` holds the model and
    max-tokens budget. Missing tunables fall back to the shared option defaults.
    ``tool_runner``/``tools`` (Phase 2) plug the guarded tool layer into the
    loop; both default to None so a tool-less agent still works.
    """
    provider_id = data.get(CONF_PROVIDER, PROVIDER_ANTHROPIC)

    if provider_id == PROVIDER_LOCAL:
        host = data.get(CONF_HOST) or DEFAULT_LOCAL_HOST
        provider = LocalProvider(session, host)
        # The local path executes envelope actions through the guarded
        # runner; the generic tool protocol is not used (SPEC §2.1).
        return LocalAgentLoop(provider, tool_runner=tool_runner)

    if provider_id != PROVIDER_ANTHROPIC:
        raise UnsupportedProviderError(provider_id)

    model = options.get(CONF_MODEL, OPTION_DEFAULTS[CONF_MODEL])
    max_tokens = options.get(CONF_MAX_TOKENS, OPTION_DEFAULTS[CONF_MAX_TOKENS])
    provider = AnthropicProvider(
        session, data[CONF_API_KEY], model=model, max_tokens=max_tokens
    )
    return AgentLoop(provider, model=model, tool_runner=tool_runner, tools=tools)


def chat_response(result: TurnResult) -> dict:
    """Serialize a completed turn for the WebSocket client."""
    return {
        "text": result.text,
        "iterations": result.iterations,
        "reason": result.reason,
    }


def history_response(agent: AgentLoop) -> list[dict]:
    """Return the agent's committed conversation for session restore.

    Conversation state lives server-side in the loop (SPEC.md §7, §9); this
    returns a copy so callers cannot mutate the stored history.
    """
    return list(agent.history)
