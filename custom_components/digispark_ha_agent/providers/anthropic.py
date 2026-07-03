"""Anthropic (Claude) provider.

Copyright (c) 2026 DigiSpark Advisory LLC. All rights reserved.
Clean-room implementation authored from SPEC.md §2 and Anthropic's public API
documentation — see PROVENANCE.md.

Talks to the Anthropic Messages API over its public HTTP interface. All
requests go through Home Assistant's shared aiohttp session (TLS verified) with
a uniform generous timeout. Error bodies are surfaced; deterministic 4xx errors
are not retried; 429 honors ``retry-after``; the API key is never logged.
"""

from __future__ import annotations

import asyncio
import logging

import aiohttp

from ..const import (
    CREDENTIAL_KIND_BEARER,
    CREDENTIAL_KIND_CUSTOM,
    CREDENTIAL_KIND_X_API_KEY,
    CREDENTIAL_KINDS,
    DEFAULT_MAX_TOKENS,
    PROVIDER_TIMEOUT_SECONDS,
)
from .base import ChatResult, NonRetryableError, Provider, ToolCall
from .local import host_problem

_LOGGER = logging.getLogger(__name__)

# Anthropic public HTTP API (see docs.anthropic.com). Authored from the public
# contract, not from any reference integration.
API_BASE = "https://api.anthropic.com"
MESSAGES_ENDPOINT = f"{API_BASE}/v1/messages"
MODELS_ENDPOINT = f"{API_BASE}/v1/models"
ANTHROPIC_VERSION = "2023-06-01"

# Headers the integration manages itself; free-form extra headers may never
# carry credentials or override the protocol headers (SPEC §2.2).
_RESERVED_HEADERS = frozenset(
    {"x-api-key", "authorization", "anthropic-version", "content-type"}
)

# Transient-failure retry policy. 4xx (except 429) never retries (SPEC §2).
DEFAULT_MAX_RETRIES = 2
_BACKOFF_BASE_SECONDS = 1.0
# Fallback pause when a 429 arrives without a parseable retry-after header.
_DEFAULT_RETRY_AFTER_SECONDS = 5.0


class AnthropicProvider(Provider):
    """Talks to the Anthropic Messages API over its public HTTP interface."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        api_key: str,
        *,
        model: str,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        base_url: str = "",
        credential_kind: str = CREDENTIAL_KIND_X_API_KEY,
        credential_header: str = "",
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self._session = session
        self._api_key = api_key
        self._model = model
        self._max_tokens = max_tokens
        self._max_retries = max_retries
        base = str(base_url or "").strip().rstrip("/")
        if base:
            problem = host_problem(base)
            if problem:
                raise ValueError(f"invalid endpoint base URL: {problem}")
        self._base_url = base or API_BASE
        self._messages_url = f"{self._base_url}/v1/messages"
        self._models_url = f"{self._base_url}/v1/models"
        if credential_kind not in CREDENTIAL_KINDS:
            raise ValueError(f"unknown credential kind {credential_kind!r}")
        header_name = str(credential_header or "").strip()
        if credential_kind == CREDENTIAL_KIND_CUSTOM and not header_name:
            raise ValueError("the custom_header credential kind needs a header name")
        self._credential_kind = credential_kind
        self._credential_header = header_name
        extras = {str(k).strip(): str(v) for k, v in (extra_headers or {}).items()}
        for name in extras:
            if name.lower() in _RESERVED_HEADERS:
                raise ValueError(f"extra header {name!r} is reserved")
        self._extra_headers = extras

    @property
    def _headers(self) -> dict[str, str]:
        headers = {
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }
        headers.update(self._extra_headers)
        if self._credential_kind == CREDENTIAL_KIND_X_API_KEY:
            headers["x-api-key"] = self._api_key
        elif self._credential_kind == CREDENTIAL_KIND_BEARER:
            headers["Authorization"] = f"Bearer {self._api_key}"
        elif self._credential_kind == CREDENTIAL_KIND_CUSTOM:
            headers[self._credential_header] = self._api_key
        return headers

    async def chat(
        self,
        messages: list[dict],
        *,
        model: str,
        tools: list[dict] | None = None,
    ) -> ChatResult:
        """Send a conversation to the Messages API and return the reply.

        tools, when provided, is a list of Anthropic-format tool schemas the
        model may invoke. Requested invocations are surfaced as
        ChatResult.tool_calls; return their outputs on the next call via
        tool_result_message(), after the assistant_message() for this result.
        """
        system, api_messages = _split_system(messages)
        payload: dict = {
            "model": model or self._model,
            "max_tokens": self._max_tokens,
            "messages": api_messages,
        }
        if system:
            payload["system"] = system
        if tools:
            payload["tools"] = tools

        data = await self._request("POST", self._messages_url, json=payload)
        return ChatResult(
            text=_extract_text(data),
            raw=data,
            tool_calls=_extract_tool_calls(data),
            stop_reason=data.get("stop_reason") if isinstance(data, dict) else None,
        )

    def assistant_message(self, result: ChatResult) -> dict:
        """Provider-formatted assistant message (delegates to the helper)."""
        return assistant_message(result)

    async def list_models(self) -> list[str]:
        """Return selectable model IDs from the Anthropic models endpoint."""
        data = await self._request("GET", self._models_url)
        entries = data.get("data", []) if isinstance(data, dict) else []
        return [m["id"] for m in entries if isinstance(m, dict) and "id" in m]

    async def health_check(self) -> bool:
        """Return True if the backend is reachable and the key authenticates."""
        try:
            await self._request("GET", self._models_url)
        except NonRetryableError:
            # Auth failure / bad request: reachable but not usable.
            return False
        except (aiohttp.ClientError, TimeoutError):
            return False
        return True

    async def _request(
        self, method: str, url: str, *, json: dict | None = None
    ) -> dict:
        """Issue a request with retry on transient failures only.

        Raises ``NonRetryableError`` on deterministic 4xx (except 429) and
        surfaces the provider's human-readable error body. Retries network
        errors, 5xx, and 429 (honoring ``retry-after``) up to ``max_retries``.
        """
        timeout = aiohttp.ClientTimeout(total=PROVIDER_TIMEOUT_SECONDS)
        attempt = 0
        while True:
            try:
                async with self._session.request(
                    method,
                    url,
                    headers=self._headers,
                    json=json,
                    timeout=timeout,
                ) as resp:
                    if resp.status < 400:
                        return await resp.json()

                    body = await resp.text()
                    if resp.status == 429:
                        if attempt >= self._max_retries:
                            raise NonRetryableError(
                                _scrub(
                                    f"Anthropic rate limit (429) persisted after "
                                    f"{attempt + 1} attempts: {body}",
                                    self._api_key,
                                )
                            )
                        await asyncio.sleep(_retry_after(resp.headers))
                        attempt += 1
                        continue

                    if resp.status < 500:
                        # Deterministic client error — a bug to fix, not retried.
                        raise NonRetryableError(
                            _scrub(
                                f"Anthropic API error {resp.status}: {body}",
                                self._api_key,
                            )
                        )

                    # 5xx — transient server error.
                    if attempt >= self._max_retries:
                        raise NonRetryableError(
                            _scrub(
                                f"Anthropic server error {resp.status} persisted "
                                f"after {attempt + 1} attempts: {body}",
                                self._api_key,
                            )
                        )
            except (aiohttp.ClientError, TimeoutError) as err:
                if attempt >= self._max_retries:
                    raise NonRetryableError(
                        _scrub(f"Anthropic request failed: {err}", self._api_key)
                    ) from err

            await asyncio.sleep(_BACKOFF_BASE_SECONDS * (2**attempt))
            attempt += 1


def _split_system(messages: list[dict]) -> tuple[str, list[dict]]:
    """Split HA-style messages into an Anthropic system string + message list.

    Anthropic carries system instructions as a top-level ``system`` field rather
    than an in-band ``system`` role. Consecutive system parts are joined.
    """
    system_parts: list[str] = []
    api_messages: list[dict] = []
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content", "")
        if role == "system":
            if content:
                system_parts.append(
                    content if isinstance(content, str) else str(content)
                )
            continue
        api_messages.append({"role": role, "content": content})
    return "\n\n".join(system_parts), api_messages


def _extract_text(data: dict) -> str:
    """Concatenate text blocks from a Messages API response."""
    blocks = data.get("content", []) if isinstance(data, dict) else []
    return "".join(
        block.get("text", "")
        for block in blocks
        if isinstance(block, dict) and block.get("type") == "text"
    )


def _extract_tool_calls(data: dict) -> tuple[ToolCall, ...]:
    """Collect tool_use blocks from a Messages API response."""
    blocks = data.get("content", []) if isinstance(data, dict) else []
    calls: list[ToolCall] = []
    for block in blocks:
        if not (isinstance(block, dict) and block.get("type") == "tool_use"):
            continue
        raw_input = block.get("input")
        calls.append(
            ToolCall(
                id=str(block.get("id", "")),
                name=str(block.get("name", "")),
                input=raw_input if isinstance(raw_input, dict) else {},
            )
        )
    return tuple(calls)


def assistant_message(result: ChatResult) -> dict:
    """Rebuild the assistant turn (text and tool_use blocks) for the next call.

    When the model requests tools, the follow-up request must carry the
    assistant's original content blocks so tool_result messages can reference
    them. Falls back to plain text for text-only results.
    """
    raw = result.raw
    if isinstance(raw, dict) and isinstance(raw.get("content"), list):
        return {"role": "assistant", "content": raw["content"]}
    return {"role": "assistant", "content": result.text}


def tool_result_message(
    tool_use_id: str, content: str, *, is_error: bool = False
) -> dict:
    """Build the user-role message returning one tool result to the model."""
    block: dict = {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": content,
    }
    if is_error:
        block["is_error"] = True
    return {"role": "user", "content": [block]}


def _retry_after(headers) -> float:
    """Parse the ``retry-after`` header (seconds) with a safe fallback."""
    raw = headers.get("retry-after")
    if raw is None:
        return _DEFAULT_RETRY_AFTER_SECONDS
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return _DEFAULT_RETRY_AFTER_SECONDS


def _scrub(text: str, api_key: str) -> str:
    """Remove API-key material from any text before it can be logged/shown."""
    if api_key and api_key in text:
        text = text.replace(api_key, "***")
    return text
