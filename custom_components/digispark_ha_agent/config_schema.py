"""Home-Assistant-free config-flow field definitions and persistence helpers.

Copyright (c) 2026 DigiSpark Advisory LLC. All rights reserved.
Clean-room implementation authored from SPEC.md §8 — see PROVENANCE.md.

This module has no Home Assistant imports so the create/edit persistence logic
is unit-testable without a running HA. ``config_flow.py`` builds its voluptuous
schemas and flow classes on top of these definitions. Because the create path
and the options path both derive their persisted mappings from the same field
tuples here, a field can never be collected on one path yet dropped on the other
(the SPEC.md §8 regression).
"""

from __future__ import annotations

import re

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
    DEFAULT_MAX_TOKENS,
    DEFAULT_MODEL,
    PROVIDER_ANTHROPIC,
)

# Every field presented on the create form.
FORM_FIELDS: tuple[str, ...] = (
    CONF_PROVIDER,
    CONF_API_KEY,
    CONF_HOST,
    CONF_BASE_URL,
    CONF_CREDENTIAL_KIND,
    CONF_CREDENTIAL_HEADER,
    CONF_EXTRA_HEADERS,
    CONF_MODEL,
    CONF_MAX_TOKENS,
)
# Identity/secret fields — stored in entry.data, set once at create.
DATA_FIELDS: tuple[str, ...] = (
    CONF_PROVIDER,
    CONF_API_KEY,
    CONF_HOST,
    CONF_BASE_URL,
    CONF_CREDENTIAL_KIND,
    CONF_CREDENTIAL_HEADER,
    CONF_EXTRA_HEADERS,
)
# Tunable fields — stored in entry.options, set at create and editable later.
OPTION_FIELDS: tuple[str, ...] = (CONF_MODEL, CONF_MAX_TOKENS)

DATA_DEFAULTS: dict = {
    CONF_PROVIDER: PROVIDER_ANTHROPIC,
    # The API key is only required for cloud providers; the local backend
    # (SPEC.md §2.1) needs none, so the field defaults to empty.
    CONF_API_KEY: "",
    CONF_HOST: DEFAULT_LOCAL_HOST,
    # Custom Anthropic-compatible endpoint (SPEC.md §2.2): empty means the
    # public API; credential kind defaults to Anthropic's own header.
    CONF_BASE_URL: "",
    CONF_CREDENTIAL_KIND: CREDENTIAL_KIND_X_API_KEY,
    CONF_CREDENTIAL_HEADER: "",
    CONF_EXTRA_HEADERS: "",
}
OPTION_DEFAULTS: dict = {
    CONF_MODEL: DEFAULT_MODEL,
    CONF_MAX_TOKENS: DEFAULT_MAX_TOKENS,
}


def split_create_input(user_input: dict) -> tuple[dict, dict]:
    """Split submitted create-form input into (entry.data, entry.options).

    Every form field lands in exactly one bucket, so nothing read from the form
    can be silently dropped before it is persisted (SPEC.md §8).
    """
    data = {k: user_input.get(k, DATA_DEFAULTS.get(k)) for k in DATA_FIELDS}
    options = {k: user_input.get(k, OPTION_DEFAULTS.get(k)) for k in OPTION_FIELDS}
    return data, options


def build_options(user_input: dict) -> dict:
    """Build the options mapping from options-flow input.

    Uses the same OPTION_FIELDS as the create path so the two cannot drift.
    """
    return {k: user_input.get(k, OPTION_DEFAULTS.get(k)) for k in OPTION_FIELDS}


# Headers the integration manages itself; the free-form field may never carry
# credentials or override protocol headers (SPEC.md §2.2).
_RESERVED_EXTRA_HEADERS = frozenset(
    {"x-api-key", "authorization", "anthropic-version", "content-type"}
)
_HEADER_NAME_OK = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]+$")


def parse_extra_headers(text: str) -> dict[str, str]:
    """Parse the extra-headers field: one ``Name: value`` per non-empty line.

    Raises ValueError on a malformed line, an invalid header name, or a
    reserved name. Values may contain colons (only the first splits).
    """
    headers: dict[str, str] = {}
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        name, sep, value = line.partition(":")
        name = name.strip()
        value = value.strip()
        if not sep or not name or not value or not _HEADER_NAME_OK.match(name):
            raise ValueError(f"malformed header line: {line!r}")
        if name.lower() in _RESERVED_EXTRA_HEADERS:
            raise ValueError(f"header {name!r} is reserved")
        headers[name] = value
    return headers


def extra_headers_problem(text: str) -> str | None:
    """Error key for the config form, or None when the field parses."""
    try:
        parse_extra_headers(text)
    except ValueError:
        return "invalid_extra_headers"
    return None


def model_choices(fetched: list[str], current: str) -> list[str]:
    """Selector choices from a fetched model list (SPEC.md §8).

    Deduped in fetch order, with the currently-configured model appended so
    an existing entry never becomes unselectable when the provider stops
    listing it. Empty when nothing was fetched - the flow then falls back to
    a free-text field.
    """
    if not fetched:
        return []
    choices: list[str] = []
    for model in [*fetched, current]:
        model = str(model).strip()
        if model and model not in choices:
            choices.append(model)
    return choices
