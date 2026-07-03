"""Provider interface.

Copyright (c) 2026 DigiSpark Advisory LLC. All rights reserved.
Clean-room implementation authored from SPEC.md 2 - see PROVENANCE.md.

A provider isolates one model backend's request shaping, response parsing,
authentication, timeouts, and error normalization behind a common interface so
one provider's quirks cannot affect another.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ToolCall:
    """One tool invocation requested by the model."""

    id: str
    name: str
    input: dict


@dataclass(slots=True)
class ChatResult:
    """Normalized result of a chat call."""

    text: str
    raw: object | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    stop_reason: str | None = None

    @property
    def wants_tools(self) -> bool:
        """True when the model paused to request tool execution."""
        return bool(self.tool_calls)


class NonRetryableError(Exception):
    """Deterministic provider error (e.g. 4xx) that must not be retried."""


class Provider(abc.ABC):
    """Common interface every provider implements (SPEC.md 2)."""

    def assistant_message(self, result: ChatResult) -> dict:
        """Rebuild the assistant message for a tool exchange.

        Text-only default; tool-capable providers override this so the
        follow-up request carries their tool_use content blocks.
        """
        return {"role": "assistant", "content": result.text}

    @abc.abstractmethod
    async def chat(
        self,
        messages: list[dict],
        *,
        model: str,
        tools: list[dict] | None = None,
    ) -> ChatResult:
        """Send a conversation and return the assistant's reply.

        tools, when provided, is a list of provider-format tool schemas the
        model may invoke; requested invocations are surfaced as
        ChatResult.tool_calls.

        Must: verify TLS for remote hosts, use a uniform generous timeout,
        surface provider error bodies, raise NonRetryableError on deterministic
        4xx, and honor retry-after on 429. Never log API keys.
        """

    @abc.abstractmethod
    async def list_models(self) -> list[str]:
        """Return selectable model IDs (may be static)."""

    @abc.abstractmethod
    async def health_check(self) -> bool:
        """Return True if the backend is reachable and authenticated."""
