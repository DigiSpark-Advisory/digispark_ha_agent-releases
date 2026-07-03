"""Local, privacy-first backend (OpenAI-compatible: llama-server / Ollama).

Copyright (c) 2026 DigiSpark Advisory LLC. All rights reserved.
Clean-room implementation authored from SPEC.md 2.1 and the PUBLIC Apache-2.0
Selora AI model card (v0.4.7) - see PROVENANCE.md and NOTICE.

Runs entirely on the LAN with no API key. Recommended weights: the Apache-2.0
Selora AI model (Qwen3-1.7B + four LoRA specialists: command, automation,
answer, clarification; the recipe/utilities specialist was dropped in v0.4.7).

Two backends, both speaking the OpenAI-compatible chat API:

- ``llama-server`` (reference runtime): one base model; the specialist LoRA is
  hot-swapped in-process via the ``/lora-adapters`` endpoint before each call.
- ``Ollama``: no hot-swap; each specialist is its own named model
  (``selora-qwen-<specialist>``) selected via the request's ``model`` field.

Cleartext HTTP is permitted only for explicitly local hosts (loopback /
private-range / mDNS-style names); anything else must be https (SPEC 2).
Generation parameters follow the model card: temperature 0, the documented
stop sequences, and a larger token budget for automation output.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
from urllib.parse import urlparse

import aiohttp

from ..const import PROVIDER_TIMEOUT_SECONDS
from .base import ChatResult, NonRetryableError, Provider

_LOGGER = logging.getLogger(__name__)

# Specialist slots per the public model card (v0.4.7).
SPECIALISTS: tuple[str, ...] = (
    "command",
    "automation",
    "answer",
    "clarification",
)

BACKEND_LLAMA_SERVER = "llama-server"
BACKEND_OLLAMA = "ollama"
_OLLAMA_DEFAULT_PORT = 11434

# Ollama has no in-process LoRA hot-swap; each specialist is a named model
# created from the bundle's Modelfiles (model card, v0.4.7).
OLLAMA_MODEL_PREFIX = "selora-qwen-"

# Generation parameters from the public model card. Automation output is
# longer JSON and gets a larger budget.
STOP_SEQUENCES: tuple[str, ...] = ("<|im_end|>", "<|endoftext|>")
DEFAULT_MAX_TOKENS = 384
AUTOMATION_MAX_TOKENS = 1536
SPECIALIST_MAX_TOKENS: dict[str, int] = {"automation": AUTOMATION_MAX_TOKENS}

# Transient-failure retry policy, matching the Anthropic provider (SPEC 2).
DEFAULT_MAX_RETRIES = 2
_BACKOFF_BASE_SECONDS = 1.0

# Hostname suffixes that are explicitly local-network names.
_LOCAL_NAME_SUFFIXES = (".local", ".lan", ".home.arpa", ".internal")


def is_local_host(hostname: str) -> bool:
    """True when a hostname names an explicitly local backend (SPEC 2).

    Loopback/private/link-local IP literals, ``localhost``, mDNS-style
    suffixes, and single-label LAN hostnames count as local; everything else
    (public IPs, dotted public names) does not.
    """
    hostname = str(hostname or "").strip().lower().rstrip(".")
    if not hostname:
        return False
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        return address.is_loopback or address.is_private or address.is_link_local
    if hostname == "localhost":
        return True
    if any(hostname.endswith(suffix) for suffix in _LOCAL_NAME_SUFFIXES):
        return True
    return "." not in hostname  # single-label LAN name


def host_problem(host: str) -> str | None:
    """Validate a local-backend host URL; returns an error key or None.

    Shared with the config flow so the form can reject what the provider
    constructor would refuse, with the same rules (SPEC 2).
    """
    parsed = urlparse(str(host).strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return "invalid_host"
    if parsed.scheme == "http" and not is_local_host(parsed.hostname):
        return "cleartext_remote_host"
    return None


class LocalProvider(Provider):
    """Talks to a local OpenAI-compatible server; routes to a specialist."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        *,
        backend: str | None = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ) -> None:
        parsed = urlparse(str(host).strip())
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError(f"local backend host must be an http(s) URL, got {host!r}")
        if parsed.scheme == "http" and not is_local_host(parsed.hostname):
            raise ValueError(
                "cleartext http is only permitted for local backends "
                f"(loopback / private hosts); use https for {parsed.hostname!r}"
            )
        self._session = session
        self._base = f"{parsed.scheme}://{parsed.netloc}"
        if backend is None:
            backend = (
                BACKEND_OLLAMA
                if parsed.port == _OLLAMA_DEFAULT_PORT
                else BACKEND_LLAMA_SERVER
            )
        if backend not in (BACKEND_LLAMA_SERVER, BACKEND_OLLAMA):
            raise ValueError(f"unknown local backend {backend!r}")
        self._backend = backend
        self._max_retries = max_retries

    @property
    def backend(self) -> str:
        """Which local runtime this provider talks to."""
        return self._backend

    def model_for(self, specialist: str) -> str:
        """The ``model`` field value that selects a specialist."""
        if self._backend == BACKEND_OLLAMA:
            return f"{OLLAMA_MODEL_PREFIX}{specialist}"
        # llama-server serves one base model; the LoRA hot-swap picks the
        # specialist, so any non-empty model id is accepted.
        return "selora"

    async def activate_specialist(self, specialist: str) -> None:
        """Make ``specialist`` the active LoRA (llama-server only).

        llama-server exposes ``/lora-adapters``: GET lists the loaded
        adapters (id + file path), POST sets per-adapter scales. The adapter
        whose path names the specialist gets scale 1.0; all others 0. On
        Ollama this is a no-op - the model name does the selection.
        """
        if self._backend != BACKEND_LLAMA_SERVER:
            return
        adapters = await self._request("GET", "/lora-adapters")
        if not isinstance(adapters, list):
            raise NonRetryableError(
                "llama-server /lora-adapters returned an unexpected shape"
            )
        scales: list[dict] = []
        matched = False
        for adapter in adapters:
            if not isinstance(adapter, dict) or "id" not in adapter:
                continue
            path = str(adapter.get("path", "")).lower()
            active = specialist in path
            matched = matched or active
            scales.append({"id": adapter["id"], "scale": 1.0 if active else 0.0})
        if not matched:
            raise NonRetryableError(
                f"no LoRA adapter for specialist {specialist!r} is loaded; "
                "start llama-server with all four specialist adapters"
            )
        await self._request("POST", "/lora-adapters", json=scales)

    async def chat(
        self,
        messages: list[dict],
        *,
        model: str,
        tools: list[dict] | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> ChatResult:
        """Send one chat completion and return the raw specialist text.

        ``tools`` is accepted for interface compatibility but ignored: the
        local specialists emit their own envelope IR, which the local agent
        loop parses and routes through the safety guard (the generic tool
        protocol is not used on this path).
        """
        payload: dict = {
            "model": model,
            "messages": messages,
            "temperature": 0.0,
            "max_tokens": max_tokens,
            "stop": list(STOP_SEQUENCES),
        }
        data = await self._request("POST", "/v1/chat/completions", json=payload)
        return ChatResult(text=_extract_text(data), raw=data)

    async def list_models(self) -> list[str]:
        """Model ids from the OpenAI-compatible models endpoint."""
        data = await self._request("GET", "/v1/models")
        entries = data.get("data", []) if isinstance(data, dict) else []
        return [m["id"] for m in entries if isinstance(m, dict) and "id" in m]

    async def health_check(self) -> bool:
        """True when the backend answers its models endpoint."""
        try:
            await self._request("GET", "/v1/models")
        except NonRetryableError:
            return False
        except (aiohttp.ClientError, TimeoutError):
            return False
        return True

    async def _request(
        self, method: str, path: str, *, json: object | None = None
    ) -> object:
        """Issue a request with retry on transient failures only (SPEC 2).

        Deterministic 4xx raises NonRetryableError with the server's error
        body surfaced; network errors and 5xx retry with backoff.
        """
        url = f"{self._base}{path}"
        timeout = aiohttp.ClientTimeout(total=PROVIDER_TIMEOUT_SECONDS)
        attempt = 0
        while True:
            try:
                async with self._session.request(
                    method, url, json=json, timeout=timeout
                ) as resp:
                    if resp.status < 400:
                        return await resp.json()
                    body = await resp.text()
                    if resp.status < 500:
                        raise NonRetryableError(
                            f"local backend error {resp.status}: {body}"
                        )
                    if attempt >= self._max_retries:
                        raise NonRetryableError(
                            f"local backend server error {resp.status} persisted "
                            f"after {attempt + 1} attempts: {body}"
                        )
            except (aiohttp.ClientError, TimeoutError) as err:
                if attempt >= self._max_retries:
                    raise NonRetryableError(
                        f"local backend request failed: {err}"
                    ) from err
            await asyncio.sleep(_BACKOFF_BASE_SECONDS * (2**attempt))
            attempt += 1


def _extract_text(data: object) -> str:
    """First choice's message content from an OpenAI-compatible response."""
    if not isinstance(data, dict):
        return ""
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0]
    if not isinstance(first, dict):
        return ""
    message = first.get("message")
    if isinstance(message, dict):
        content = message.get("content")
        return content if isinstance(content, str) else ""
    return ""
