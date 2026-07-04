"""The agent loop: bounded iterate-until-answer over a provider.

Copyright (c) 2026 DigiSpark Advisory LLC. All rights reserved.
Clean-room implementation authored from SPEC.md §3–§4 — see PROVENANCE.md.

One user turn runs a bounded loop: assemble the prompt (system instructions +
a windowed slice of prior conversation + the user's message), call the provider,
service any data/action requests the model makes (subject to size caps), and
iterate until a final answer, the iteration cap, or cancellation. A failed or
cancelled turn rolls its messages back so it cannot poison later turns.

Tool exchanges (the assistant's tool_use message plus the tool results) are
intra-turn scratch: they are sent to the provider while the turn runs but are
not committed to history. Committed history holds only user messages and final
assistant text, so the context window can never slice a tool_use/tool_result
pair apart and session restore stays plain text (SPEC §3, §7).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from ..const import (
    MAX_CONTEXT_MESSAGES,
    MAX_DATA_MESSAGE_CHARS,
    MAX_LOOP_ITERATIONS,
)
from ..providers.base import ChatResult

_LOGGER = logging.getLogger(__name__)

DEFAULT_SYSTEM_PROMPT = (
    "You are the DigiSpark home agent. Answer questions about the home and, when "
    "asked to act, request only the data or actions you actually need. Be "
    "concise. If a data response is truncated, request narrower data rather than "
    "asking for a full dump."
)

TRUNCATION_NOTICE = (
    "\n\n[truncated: this response exceeded the size cap. Request narrower data "
    "(filter by area, domain, or entity) instead of a full dump.]"
)

# A tool runner services the model's data/action requests for one assistant turn
# and returns follow-up messages to append (already role-tagged), or an empty
# result when the turn is a final answer. Phase 2 wires HomeToolRunner
# (agent/tools.py) into this seam via runtime.build_agent. If the runner has a
# ``start_turn()`` method it is called at the top of every turn so per-turn
# state (e.g. the action budget) can reset.
ToolRunner = Callable[[ChatResult], Awaitable["list[dict] | None"]]
CancelCheck = Callable[[], bool]


class TurnCancelled(Exception):
    """Raised when a turn is cancelled cooperatively between steps (SPEC §3)."""


@dataclass(slots=True)
class TurnResult:
    """Outcome of a single user turn."""

    text: str
    iterations: int
    reason: str  # "final" | "max_iterations"


def cap_data(text: str, limit: int = MAX_DATA_MESSAGE_CHARS) -> str:
    """Hard-cap a single data payload, appending a narrowing notice (SPEC §4).

    The result never exceeds ``limit``. If ``limit`` is smaller than the notice
    itself (a degenerate tiny cap), a bare truncated preview is returned.
    """
    if len(text) <= limit:
        return text
    if limit <= len(TRUNCATION_NOTICE):
        return text[:limit]
    budget = limit - len(TRUNCATION_NOTICE)
    return text[:budget] + TRUNCATION_NOTICE


class AgentLoop:
    """Runs a single user turn to completion (SPEC §3–§4).

    Collaborators are injected so the control structure is testable in isolation:
    a ``provider`` whose ``chat`` matches the provider interface, the ``model``
    id, an optional ``tool_runner`` that services data/action requests, the
    ``tools`` schemas to advertise to the model, and an optional ``cancel``
    check consulted between steps.
    """

    def __init__(
        self,
        provider,
        *,
        model: str,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        tool_runner: ToolRunner | None = None,
        tools: list[dict] | None = None,
        max_iterations: int = MAX_LOOP_ITERATIONS,
        max_data_chars: int = MAX_DATA_MESSAGE_CHARS,
        max_context_messages: int = MAX_CONTEXT_MESSAGES,
    ) -> None:
        self._provider = provider
        self._model = model
        self._system_prompt = system_prompt
        self._tool_runner = tool_runner
        self._tools = tools
        self._max_iterations = max_iterations
        self._max_data_chars = max_data_chars
        self._max_context_messages = max_context_messages
        self._history: list[dict] = []

    @property
    def history(self) -> list[dict]:
        """A copy of the committed conversation history."""
        return list(self._history)

    def reset(self) -> None:
        """Clear committed history (e.g. on a new session)."""
        self._history = []

    def load_history(self, messages: list[dict]) -> None:
        """Replace committed history with one session's messages (SPEC §7).

        The caller owns session persistence (sessions/store.py); the loop
        only executes turns against whatever history was loaded.
        """
        self._history = [dict(message) for message in messages]

    async def run_turn(
        self, user_message: str, *, cancel: CancelCheck | None = None
    ) -> TurnResult:
        """Run one user turn to a final answer, the iteration cap, or cancel.

        History is committed only on success, so a failed or cancelled turn
        never leaves partial or oversized messages behind (SPEC §3). Tool
        exchanges live in a turn-local scratch buffer and are never committed.
        """
        working: list[dict] = [
            *self._history,
            {"role": "user", "content": user_message},
        ]
        scratch: list[dict] = []
        last_text = ""

        start_turn = getattr(self._tool_runner, "start_turn", None)
        if start_turn is not None:
            start_turn()

        for iteration in range(1, self._max_iterations + 1):
            _check_cancel(cancel)
            messages = self._assemble(working, scratch)
            result = await self._chat(messages)
            _check_cancel(cancel)
            last_text = result.text or last_text

            follow = None
            if self._tool_runner is not None:
                follow = await self._tool_runner(result)

            if not follow:
                working.append({"role": "assistant", "content": result.text})
                self._history = working
                return TurnResult(result.text, iteration, "final")

            scratch.append(self._assistant_message(result))
            scratch.extend(self._cap_message(msg) for msg in follow)

        # Iteration budget exhausted without a final answer (SPEC §3).
        text = last_text or (
            "The agent reached its step limit before producing an answer."
        )
        working.append({"role": "assistant", "content": text})
        self._history = working
        return TurnResult(text, self._max_iterations, "max_iterations")

    async def _chat(self, messages: list[dict]) -> ChatResult:
        """Call the provider, passing tool schemas only when configured.

        Omitting the ``tools`` keyword entirely keeps providers (and test
        fakes) with the tool-less Phase 1 signature working unchanged.
        """
        if self._tools is None:
            return await self._provider.chat(messages, model=self._model)
        return await self._provider.chat(messages, model=self._model, tools=self._tools)

    def _assistant_message(self, result: ChatResult) -> dict:
        """Provider-formatted assistant message for the scratch exchange.

        Providers that support tool use expose ``assistant_message`` so the
        follow-up request carries the original tool_use blocks; anything else
        falls back to plain text.
        """
        builder = getattr(self._provider, "assistant_message", None)
        if builder is not None:
            return builder(result)
        return {"role": "assistant", "content": result.text}

    def _assemble(self, working: list[dict], scratch: list[dict]) -> list[dict]:
        """System + windowed committed messages + the whole intra-turn exchange.

        The window applies to committed conversation only; scratch is always
        included in full so a tool_use/tool_result pair can never be sliced
        apart. Scratch stays bounded by the iteration cap and the per-item
        data caps (SPEC §3–§4).
        """
        window = working[-self._max_context_messages :]
        return [
            {"role": "system", "content": self._system_prompt},
            *window,
            *scratch,
        ]

    def _cap_message(self, msg: dict) -> dict:
        """Cap one appended data message per-item (SPEC §4).

        Structured (non-string) content passes through: the tool runner caps
        each tool result's text before wrapping it in content blocks.
        """
        content = msg.get("content", "")
        if isinstance(content, str):
            content = cap_data(content, self._max_data_chars)
        return {**msg, "content": content}


def _check_cancel(cancel: CancelCheck | None) -> None:
    if cancel is not None and cancel():
        raise TurnCancelled
