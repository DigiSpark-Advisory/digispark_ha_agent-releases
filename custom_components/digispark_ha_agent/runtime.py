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
from .config_schema import OPTION_DEFAULTS, parse_extra_headers
from .const import (
    CONF_API_KEY,
    CONF_BASE_URL,
    CONF_CREDENTIAL_HEADER,
    CONF_CREDENTIAL_KIND,
    CONF_EXTRA_HEADERS,
    CONF_HOST,
    CONF_MAX_TOKENS,
    CONF_MODEL,
    CONF_PROVIDER,
    CREDENTIAL_KIND_X_API_KEY,
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
    provider = build_provider(session, data, options)

    if provider_id == PROVIDER_LOCAL:
        # The local path executes envelope actions through the guarded
        # runner; the generic tool protocol is not used (SPEC §2.1).
        return LocalAgentLoop(provider, tool_runner=tool_runner)

    model = options.get(CONF_MODEL, OPTION_DEFAULTS[CONF_MODEL])
    return AgentLoop(provider, model=model, tool_runner=tool_runner, tools=tools)


def build_provider(
    session: aiohttp.ClientSession,
    data: dict,
    options: dict,
    *,
    max_tokens_override: int | None = None,
):
    """The configured provider for an entry (SPEC.md §2, §8).

    Shared by the agent loop and the WS connection-test path;
    ``max_tokens_override`` lets the probe cap a chat test at one token.
    """
    provider_id = data.get(CONF_PROVIDER, PROVIDER_ANTHROPIC)
    if provider_id == PROVIDER_LOCAL:
        return LocalProvider(session, data.get(CONF_HOST) or DEFAULT_LOCAL_HOST)
    if provider_id != PROVIDER_ANTHROPIC:
        raise UnsupportedProviderError(provider_id)
    model = options.get(CONF_MODEL, OPTION_DEFAULTS[CONF_MODEL])
    max_tokens = max_tokens_override or options.get(
        CONF_MAX_TOKENS, OPTION_DEFAULTS[CONF_MAX_TOKENS]
    )
    try:
        extra_headers = parse_extra_headers(data.get(CONF_EXTRA_HEADERS, ""))
    except ValueError:
        # Validated at config time; a corrupt stored value must not brick setup.
        extra_headers = {}
    return AnthropicProvider(
        session,
        data[CONF_API_KEY],
        model=model,
        max_tokens=max_tokens,
        base_url=str(data.get(CONF_BASE_URL, "") or ""),
        credential_kind=data.get(CONF_CREDENTIAL_KIND, CREDENTIAL_KIND_X_API_KEY),
        credential_header=str(data.get(CONF_CREDENTIAL_HEADER, "") or ""),
        extra_headers=extra_headers,
    )


# Friendly hints for the panel keyed on status codes in provider error text.
_HINTS: tuple[tuple[str, str], ...] = (
    (" 401", "The endpoint rejected the credentials (401). Check the API key."),
    (" 403", "The endpoint refused access (403). Check the key's permissions."),
    (" 404", "Not found (404). Check the base URL."),
    (" 429", "Rate limited (429). Try again shortly."),
)


def friendly_hint(message: str) -> str:
    """A short human hint for a provider error message, or an empty string."""
    for token, hint in _HINTS:
        if token in message:
            return hint
    return ""


async def probe_provider(provider, *, chat: bool = False, model: str = "") -> dict:
    """Never-throw connection probe (SPEC.md §8).

    Always lists models (reachability + auth); with ``chat`` also runs a
    one-token chat call against ``model`` to prove end-to-end access. Failures
    come back as ``success: False`` with the scrubbed provider message and a
    friendly hint — this function never raises.
    """
    result: dict = {"success": False, "list_models": None, "chat": None}
    try:
        models = await provider.list_models()
    except Exception as err:
        message = str(err)
        result["list_models"] = {
            "success": False,
            "message": message,
            "hint": friendly_hint(message),
            "models": [],
        }
        return result
    listed = [m for m in models if isinstance(m, str)]
    result["list_models"] = {
        "success": True,
        "message": f"Connected. {len(listed)} model(s) available.",
        "hint": "",
        "models": listed,
    }
    result["success"] = True
    if chat:
        try:
            await provider.chat([{"role": "user", "content": "ping"}], model=model)
        except Exception as err:
            message = str(err)
            result["chat"] = {
                "success": False,
                "message": message,
                "hint": friendly_hint(message),
            }
            result["success"] = False
        else:
            result["chat"] = {
                "success": True,
                "message": f"Chat OK: {model or 'default model'} responded.",
                "hint": "",
            }
    return result


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
