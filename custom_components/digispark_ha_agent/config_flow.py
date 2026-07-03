"""Config and options flow for DigiSpark HA Agent.

Copyright (c) 2026 DigiSpark Advisory LLC. All rights reserved.
Clean-room implementation authored from SPEC.md §8 — see PROVENANCE.md.

The create flow runs in two steps: connection (provider + API key or host,
stored in ``entry.data``), then model selection — the provider's live model
list when it can be fetched, a free-text field when it cannot (SPEC.md §8) —
plus the max-tokens budget (stored in ``entry.options``). The options flow
edits the model (same dynamic list) and max-tokens after setup. Both paths
derive their persisted mappings from ``config_schema`` so a field cannot be
collected but not persisted; fields are written on both create and edit.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .config_schema import (
    OPTION_DEFAULTS,
    build_options,
    extra_headers_problem,
    model_choices,
    parse_extra_headers,
    split_create_input,
)
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
    CREDENTIAL_KIND_CUSTOM,
    CREDENTIAL_KIND_NONE,
    CREDENTIAL_KIND_X_API_KEY,
    CREDENTIAL_KINDS,
    DEFAULT_LOCAL_HOST,
    DEFAULT_MODEL,
    DOMAIN,
    MODEL_FETCH_TIMEOUT_SECONDS,
    PROVIDER_ANTHROPIC,
    PROVIDER_LOCAL,
    SUPPORTED_PROVIDERS,
)
from .providers.anthropic import AnthropicProvider
from .providers.local import LocalProvider, host_problem

if TYPE_CHECKING:
    from homeassistant.data_entry_flow import FlowResult

_LOGGER = logging.getLogger(__name__)

_TITLE = "DigiSpark HA Agent"

# host_problem() speaks in local-backend error keys; the endpoint field has
# its own strings so the form message names the right field (SPEC.md §2.2).
_BASE_URL_ERRORS = {
    "invalid_host": "invalid_base_url",
    "cleartext_remote_host": "cleartext_remote_base_url",
}


async def _fetch_models(hass: HomeAssistant, connection: dict) -> list[str]:
    """The provider's model ids, or [] when they cannot be fetched.

    Failures fall back to a free-text model field rather than blocking setup
    (SPEC.md §8); the short timeout keeps a down backend from hanging the
    form for the provider's generous chat timeout.
    """
    session = async_get_clientsession(hass)
    try:
        if connection.get(CONF_PROVIDER, PROVIDER_ANTHROPIC) == PROVIDER_LOCAL:
            provider = LocalProvider(
                session, connection.get(CONF_HOST) or DEFAULT_LOCAL_HOST
            )
        else:
            provider = AnthropicProvider(
                session,
                str(connection.get(CONF_API_KEY, "")),
                model=DEFAULT_MODEL,
                base_url=str(connection.get(CONF_BASE_URL, "") or ""),
                credential_kind=connection.get(
                    CONF_CREDENTIAL_KIND, CREDENTIAL_KIND_X_API_KEY
                ),
                credential_header=str(connection.get(CONF_CREDENTIAL_HEADER, "") or ""),
                extra_headers=parse_extra_headers(
                    connection.get(CONF_EXTRA_HEADERS, "")
                ),
            )
        async with asyncio.timeout(MODEL_FETCH_TIMEOUT_SECONDS):
            models = await provider.list_models()
    except Exception:  # any fetch problem -> free-text fallback, never a block
        _LOGGER.warning(
            "could not fetch the provider's model list; offering free text",
            exc_info=True,
        )
        return []
    return [model for model in models if isinstance(model, str)]


def _model_schema(models: list[str], current: str) -> vol.Schema:
    """Model + max-tokens form: dynamic selector, free text on fetch failure."""
    choices = model_choices(models, current)
    if choices:
        default = current if current in choices else choices[0]
        model_field = (
            vol.Required(CONF_MODEL, default=default),
            vol.In(choices),
        )
    else:
        model_field = (vol.Required(CONF_MODEL, default=current), str)
    return vol.Schema(
        {
            model_field[0]: model_field[1],
            vol.Required(
                CONF_MAX_TOKENS, default=OPTION_DEFAULTS[CONF_MAX_TOKENS]
            ): vol.All(vol.Coerce(int), vol.Range(min=1)),
        }
    )


def _create_schema() -> vol.Schema:
    """Schema for the connection step of the setup flow."""
    return vol.Schema(
        {
            vol.Required(CONF_PROVIDER, default=PROVIDER_ANTHROPIC): vol.In(
                list(SUPPORTED_PROVIDERS)
            ),
            # Cloud providers need a key; the local backend (SPEC.md §2.1)
            # needs a host instead. Cross-field validation happens in the
            # step handler because voluptuous sees one field at a time.
            vol.Optional(CONF_API_KEY, default=""): str,
            vol.Optional(CONF_HOST, default=DEFAULT_LOCAL_HOST): str,
            # Custom Anthropic-compatible endpoint (SPEC.md §2.2): optional
            # gateway/proxy base URL, pluggable credential header, and extra
            # per-request headers ("Name: value" per line).
            vol.Optional(CONF_BASE_URL, default=""): str,
            vol.Optional(
                CONF_CREDENTIAL_KIND, default=CREDENTIAL_KIND_X_API_KEY
            ): vol.In(list(CREDENTIAL_KINDS)),
            vol.Optional(CONF_CREDENTIAL_HEADER, default=""): str,
            vol.Optional(CONF_EXTRA_HEADERS, default=""): str,
        }
    )


def _create_errors(user_input: dict) -> dict[str, str]:
    """Cross-field validation for the create form (SPEC.md §8)."""
    provider = user_input.get(CONF_PROVIDER, PROVIDER_ANTHROPIC)
    if provider == PROVIDER_LOCAL:
        problem = host_problem(str(user_input.get(CONF_HOST, "")))
        if problem:
            return {"base": problem}
        return {}
    base_url = str(user_input.get(CONF_BASE_URL, "")).strip()
    if base_url:
        problem = host_problem(base_url)
        if problem:
            return {"base": _BASE_URL_ERRORS.get(problem, problem)}
    kind = user_input.get(CONF_CREDENTIAL_KIND, CREDENTIAL_KIND_X_API_KEY)
    header_name = str(user_input.get(CONF_CREDENTIAL_HEADER, "")).strip()
    if kind == CREDENTIAL_KIND_CUSTOM and not header_name:
        return {"base": "credential_header_required"}
    headers_problem = extra_headers_problem(user_input.get(CONF_EXTRA_HEADERS, ""))
    if headers_problem:
        return {"base": headers_problem}
    if (
        kind != CREDENTIAL_KIND_NONE
        and not str(user_input.get(CONF_API_KEY, "")).strip()
    ):
        return {"base": "api_key_required"}
    return {}


def _options_schema(current: dict, models: list[str]) -> vol.Schema:
    """Schema for the options (edit) form, pre-filled from current options."""
    current_model = current.get(CONF_MODEL, DEFAULT_MODEL)
    choices = model_choices(models, current_model)
    if choices:
        model_field = (
            vol.Required(CONF_MODEL, default=current_model),
            vol.In(choices),
        )
    else:
        model_field = (vol.Required(CONF_MODEL, default=current_model), str)
    return vol.Schema(
        {
            model_field[0]: model_field[1],
            vol.Required(
                CONF_MAX_TOKENS,
                default=current.get(CONF_MAX_TOKENS, OPTION_DEFAULTS[CONF_MAX_TOKENS]),
            ): vol.All(vol.Coerce(int), vol.Range(min=1)),
        }
    )


class DigiSparkAgentConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the UI setup flow (SPEC.md §8)."""

    VERSION = 1

    def __init__(self) -> None:
        self._connection: dict = {}

    async def async_step_user(self, user_input: dict | None = None) -> FlowResult:
        """Step 1: collect and validate the provider connection."""
        if user_input is None:
            return self.async_show_form(step_id="user", data_schema=_create_schema())

        errors = _create_errors(user_input)
        if errors:
            return self.async_show_form(
                step_id="user", data_schema=_create_schema(), errors=errors
            )

        self._connection = dict(user_input)
        return await self.async_step_model()

    async def async_step_model(self, user_input: dict | None = None) -> FlowResult:
        """Step 2: pick the model (dynamic list when fetchable) + max tokens."""
        if user_input is None:
            models = await _fetch_models(self.hass, self._connection)
            return self.async_show_form(
                step_id="model", data_schema=_model_schema(models, DEFAULT_MODEL)
            )

        data, options = split_create_input({**self._connection, **user_input})
        return self.async_create_entry(title=_TITLE, data=data, options=options)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow handler."""
        return DigiSparkAgentOptionsFlow(config_entry)


class DigiSparkAgentOptionsFlow(OptionsFlow):
    """Edit model and max-tokens after setup (SPEC.md §8)."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._config_entry = config_entry

    async def async_step_init(self, user_input: dict | None = None) -> FlowResult:
        """Show and persist the editable options (dynamic model list, §8)."""
        if user_input is None:
            models = await _fetch_models(self.hass, dict(self._config_entry.data))
            return self.async_show_form(
                step_id="init",
                data_schema=_options_schema(dict(self._config_entry.options), models),
            )
        return self.async_create_entry(title="", data=build_options(user_input))
