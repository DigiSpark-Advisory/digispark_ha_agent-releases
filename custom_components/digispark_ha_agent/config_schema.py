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
    CREDENTIAL_KIND_CUSTOM,
    CREDENTIAL_KIND_NONE,
    CREDENTIAL_KIND_X_API_KEY,
    CREDENTIAL_KINDS,
    DEFAULT_LOCAL_HOST,
    DEFAULT_MAX_TOKENS,
    DEFAULT_MODEL,
    PROVIDER_ANTHROPIC,
    PROVIDER_LOCAL,
)
from .providers.local import host_problem

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


# -- provider-settings editing (SPEC.md §8; shared by the create form and WS) ----

# host_problem() speaks in local-backend error keys; the endpoint field has its
# own keys so the message names the right field (SPEC.md §2.2).
_BASE_URL_ERRORS = {
    "invalid_host": "invalid_base_url",
    "cleartext_remote_host": "cleartext_remote_base_url",
}

# Connection fields the WS settings update may change. The provider id is
# fixed at create — switching provider families means a new entry.
UPDATABLE_DATA_FIELDS: tuple[str, ...] = (
    CONF_API_KEY,
    CONF_HOST,
    CONF_BASE_URL,
    CONF_CREDENTIAL_KIND,
    CONF_CREDENTIAL_HEADER,
    CONF_EXTRA_HEADERS,
)


def connection_problem(merged: dict) -> str | None:
    """Error key for a full connection mapping, or None when it is valid.

    Single source of truth: the create form and the WS settings update both
    validate through here, so the rules cannot drift between the two paths.
    """
    provider = merged.get(CONF_PROVIDER, PROVIDER_ANTHROPIC)
    if provider == PROVIDER_LOCAL:
        return host_problem(str(merged.get(CONF_HOST, "")))
    base_url = str(merged.get(CONF_BASE_URL, "") or "").strip()
    if base_url:
        problem = host_problem(base_url)
        if problem:
            return _BASE_URL_ERRORS.get(problem, problem)
    kind = merged.get(CONF_CREDENTIAL_KIND, CREDENTIAL_KIND_X_API_KEY)
    if kind not in CREDENTIAL_KINDS:
        return "invalid_credential_kind"
    header_name = str(merged.get(CONF_CREDENTIAL_HEADER, "") or "").strip()
    if kind == CREDENTIAL_KIND_CUSTOM and not header_name:
        return "credential_header_required"
    problem = extra_headers_problem(merged.get(CONF_EXTRA_HEADERS, ""))
    if problem:
        return problem
    if (
        kind != CREDENTIAL_KIND_NONE
        and not str(merged.get(CONF_API_KEY, "") or "").strip()
    ):
        return "api_key_required"
    return None


def redacted_settings(data: dict, options: dict) -> dict:
    """Provider settings safe to send to the panel: secrets never leave.

    The API key is reduced to ``has_api_key``. Extra-header values are masked
    (names stay visible so the admin can see what is configured); on update
    the whole field is replaced, never edited line-by-line.
    """
    try:
        parsed = parse_extra_headers(data.get(CONF_EXTRA_HEADERS, ""))
    except ValueError:
        parsed = {}
    masked = "\n".join(f"{name}: ***" for name in parsed)
    return {
        CONF_PROVIDER: data.get(CONF_PROVIDER, PROVIDER_ANTHROPIC),
        CONF_HOST: data.get(CONF_HOST, DEFAULT_LOCAL_HOST),
        CONF_BASE_URL: data.get(CONF_BASE_URL, ""),
        CONF_CREDENTIAL_KIND: data.get(CONF_CREDENTIAL_KIND, CREDENTIAL_KIND_X_API_KEY),
        CONF_CREDENTIAL_HEADER: data.get(CONF_CREDENTIAL_HEADER, ""),
        "extra_headers_masked": masked,
        "has_api_key": bool(str(data.get(CONF_API_KEY, "") or "").strip()),
        CONF_MODEL: options.get(CONF_MODEL, DEFAULT_MODEL),
        CONF_MAX_TOKENS: options.get(CONF_MAX_TOKENS, DEFAULT_MAX_TOKENS),
    }


def merge_settings(
    data: dict, options: dict, updates: dict
) -> tuple[dict, dict, str | None]:
    """Apply a partial settings update; returns (data, options, problem).

    Absent fields keep their current values. The API key follows
    leave-blank-keeps-current: absent, None, or an empty string means keep the
    stored key. On any problem the ORIGINAL mappings are returned unchanged.
    """
    new_data = dict(data)
    new_options = dict(options)
    for field in UPDATABLE_DATA_FIELDS:
        if field not in updates:
            continue
        value = updates[field]
        if field == CONF_API_KEY and not str(value or "").strip():
            continue  # leave blank to keep the stored key
        new_data[field] = "" if value is None else str(value)
    if CONF_MODEL in updates and str(updates[CONF_MODEL] or "").strip():
        new_options[CONF_MODEL] = str(updates[CONF_MODEL]).strip()
    if CONF_MAX_TOKENS in updates:
        try:
            tokens = int(updates[CONF_MAX_TOKENS])
        except (TypeError, ValueError):
            return data, options, "invalid_max_tokens"
        if tokens < 1:
            return data, options, "invalid_max_tokens"
        new_options[CONF_MAX_TOKENS] = tokens
    problem = connection_problem(new_data)
    if problem:
        return data, options, problem
    return new_data, new_options, None
