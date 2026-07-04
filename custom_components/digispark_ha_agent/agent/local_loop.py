"""Local-backend agent loop: one envelope per turn, guarded execution.

Copyright (c) 2026 DigiSpark Advisory LLC. All rights reserved.
Clean-room implementation authored from SPEC.md 2.1/3/5 and the PUBLIC
Apache-2.0 Selora AI model card (v0.4.7) - see PROVENANCE.md.

The local specialists emit an envelope IR instead of using the generic tool
protocol, so this loop replaces the iterate-until-answer loop for the local
provider (design settled 2026-07-03): classify the request to a specialist
with a deterministic heuristic router, make one chat call, parse the
envelope, and execute any proposed actions through HomeToolRunner.run_tool -
the same guard, per-turn action budget, and pending-confirmation queue as
the cloud path. safety/policy.py stays the single choke point; a routing
mistake can cost answer quality, never safety.

The model expects an AVAILABLE ENTITIES context block (exact line format
documented on the model card); it is built from the runner's own exposed
listing and capped so a big home cannot blow the local context window. The
answer specialist may reply with a query_state envelope; that gets exactly
one bounded follow-up call carrying the requested states.

Committed history matches AgentLoop semantics: user messages and final
assistant text only, committed only on success.
"""

from __future__ import annotations

import json
from collections.abc import Callable

from ..const import LOCAL_HISTORY_TURNS, MAX_LOCAL_ENTITY_BLOCK_CHARS
from ..providers.envelope import (
    INTENT_AUTOMATION,
    INTENT_CLARIFICATION,
    INTENT_COMMAND,
    INTENT_QUERY_STATE,
    Envelope,
    parse_envelope,
)
from ..providers.local import DEFAULT_MAX_TOKENS, SPECIALIST_MAX_TOKENS
from .loop import TurnCancelled, TurnResult

CancelCheck = Callable[[], bool]

_ENTITY_BLOCK_HEADER = "AVAILABLE ENTITIES:"
_TRUNCATION_LINE = "  - (entity list truncated; ask about a specific device)"
_NO_ANSWER = "The local model returned no answer."
_MAX_QUERY_ENTITIES = 5

# Deterministic router (design settled 2026-07-03): ordered rules, first
# match wins, unmatched requests fall through to the answer specialist.
# Automation phrasing is checked before command verbs so "turn on the porch
# light when the door opens" drafts an automation instead of acting now.
_AUTOMATION_PATTERNS: tuple[str, ...] = (
    "automate",
    "automation",
    "routine",
    "whenever",
    "every time",
    "each time",
    "every day",
    "every morning",
    "every evening",
    "every night",
    "at sunset",
    "at sunrise",
    "when i ",
    "when the ",
)
_COMMAND_PATTERNS: tuple[str, ...] = (
    "turn on",
    "turn off",
    "switch on",
    "switch off",
    "toggle",
    "set ",
    "dim ",
    "brighten",
    "lock ",
    "unlock",
    "open ",
    "close ",
    "start ",
    "stop ",
    "play ",
    "pause ",
    "activate ",
)


def classify_specialist(message: str, *, continue_specialist: str | None = None) -> str:
    """Pick the specialist for one request (deterministic, unit-testable).

    A reply to a clarification question continues with the specialist that
    asked it; otherwise ordered pattern rules decide, with the answer
    specialist as the safe fallback.
    """
    if continue_specialist:
        return continue_specialist
    text = f" {str(message).strip().lower()} "
    if any(pattern in text for pattern in _AUTOMATION_PATTERNS):
        return "automation"
    if any(pattern in text for pattern in _COMMAND_PATTERNS):
        return "command"
    return "answer"


class LocalAgentLoop:
    """Runs one user turn against the local backend (SPEC 2.1).

    Presents the same surface the WS layer uses on AgentLoop: ``history``,
    ``reset()``, and ``run_turn()`` returning a TurnResult.
    """

    def __init__(
        self,
        provider,
        *,
        tool_runner,
        history_turns: int = LOCAL_HISTORY_TURNS,
        entity_block_chars: int = MAX_LOCAL_ENTITY_BLOCK_CHARS,
    ) -> None:
        if tool_runner is None:
            raise ValueError("the local backend requires the tool runner")
        self._provider = provider
        self._tool_runner = tool_runner
        self._history_turns = history_turns
        self._entity_block_chars = entity_block_chars
        self._history: list[dict] = []
        self._continue_with: str | None = None

    @property
    def history(self) -> list[dict]:
        """A copy of the committed conversation history."""
        return list(self._history)

    def reset(self) -> None:
        """Clear committed history (e.g. on a new session)."""
        self._history = []
        self._continue_with = None

    def load_history(self, messages: list[dict]) -> None:
        """Replace committed history with one session's messages (SPEC §7)."""
        self._history = [dict(message) for message in messages]
        self._continue_with = None

    async def run_turn(
        self, user_message: str, *, cancel: CancelCheck | None = None
    ) -> TurnResult:
        """One request -> one envelope -> guarded execution -> answer."""
        _check_cancel(cancel)
        start_turn = getattr(self._tool_runner, "start_turn", None)
        if start_turn is not None:
            start_turn()

        specialist = classify_specialist(
            user_message, continue_specialist=self._continue_with
        )
        messages = await self._assemble(user_message)
        await self._provider.activate_specialist(specialist)
        _check_cancel(cancel)

        result = await self._provider.chat(
            messages,
            model=self._provider.model_for(specialist),
            max_tokens=SPECIALIST_MAX_TOKENS.get(specialist, DEFAULT_MAX_TOKENS),
        )
        envelope = parse_envelope(specialist, result.text)
        iterations = 1

        if envelope.intent == INTENT_QUERY_STATE:
            _check_cancel(cancel)
            envelope, iterations = await self._answer_query_state(
                specialist, messages, result.text, envelope
            )

        _check_cancel(cancel)
        text = await self._execute(envelope) or _NO_ANSWER
        self._continue_with = (
            specialist if envelope.intent == INTENT_CLARIFICATION else None
        )
        self._history = [
            *self._history,
            {"role": "user", "content": user_message},
            {"role": "assistant", "content": text},
        ]
        return TurnResult(text, iterations, "final")

    async def _assemble(self, user_message: str) -> list[dict]:
        """System entity block + a bounded history window + the request."""
        block = await self._entity_block()
        window = self._history[-(2 * self._history_turns) :]
        return [
            {"role": "system", "content": block},
            *window,
            {"role": "user", "content": user_message},
        ]

    async def _entity_block(self) -> str:
        """The AVAILABLE ENTITIES block in the model card's line format.

        Without this block the model invents entity ids (model card,
        troubleshooting). Capped so a large home cannot exhaust the local
        context window; the guard rejects invented targets regardless.
        """
        content, is_error = await self._tool_runner.run_tool("list_entities", {})
        lines: list[str] = [_ENTITY_BLOCK_HEADER]
        used = len(_ENTITY_BLOCK_HEADER)
        if not is_error:
            try:
                entities = json.loads(content)
            except ValueError:
                entities = []
            for entity in entities if isinstance(entities, list) else []:
                if not isinstance(entity, dict):
                    continue
                line = (
                    f"  - entity_id={entity.get('entity_id', '')}; "
                    f"state={entity.get('state', '')}; "
                    f"friendly_name={entity.get('name', '')}"
                )
                if used + len(line) + 1 > self._entity_block_chars:
                    lines.append(_TRUNCATION_LINE)
                    break
                lines.append(line)
                used += len(line) + 1
        return "\n".join(lines)

    async def _answer_query_state(
        self,
        specialist: str,
        messages: list[dict],
        raw_reply: str,
        envelope: Envelope,
    ) -> tuple[Envelope, int]:
        """Service a query_state envelope with exactly one follow-up call."""
        results: list[str] = []
        for entity_id in envelope.query_entities[:_MAX_QUERY_ENTITIES]:
            content, _is_error = await self._tool_runner.run_tool(
                "get_entity_state", {"entity_id": entity_id}
            )
            results.append(f"{entity_id}: {content}")
        if not results:
            return Envelope(intent="answer", response=envelope.response), 1

        follow_up = [
            *messages,
            {"role": "assistant", "content": raw_reply},
            {"role": "user", "content": "STATE RESULTS:\n" + "\n".join(results)},
        ]
        result = await self._provider.chat(
            follow_up,
            model=self._provider.model_for(specialist),
            max_tokens=DEFAULT_MAX_TOKENS,
        )
        second = parse_envelope(specialist, result.text)
        if second.intent == INTENT_QUERY_STATE:
            # The model asked again; answer with the data we already have.
            second = Envelope(intent="answer", response="\n".join(results))
        return second, 2

    async def _execute(self, envelope: Envelope) -> str:
        """Turn one vetted envelope into effects + final text.

        Every action goes through HomeToolRunner.run_tool, i.e. the guard,
        the per-turn budget, and the pending-confirmation queue (SPEC 5).
        Outcome lines are appended so refused or queued actions are visible
        to the user, never silent.
        """
        if envelope.intent == INTENT_COMMAND:
            outcomes: list[str] = []
            for call in envelope.calls:
                content, _is_error = await self._tool_runner.run_tool(
                    "call_service",
                    {
                        "domain": call.domain,
                        "service": call.service,
                        "entity_id": call.entity_id,
                        "data": call.data,
                    },
                )
                outcomes.append(f"- {content}")
            parts = [envelope.response] if envelope.response else []
            parts.extend(outcomes)
            return "\n".join(parts)

        if envelope.intent == INTENT_AUTOMATION and envelope.automation:
            body = envelope.automation
            args = {
                "alias": str(body.get("alias", "")),
                "description": str(body.get("description", "")),
                "trigger": body.get("trigger"),
                "condition": body.get("condition"),
                "action": body.get("action"),
            }
            content, _is_error = await self._tool_runner.run_tool(
                "draft_automation", args
            )
            parts = [envelope.response] if envelope.response else []
            parts.append(content)
            return "\n".join(parts)

        return envelope.response


def _check_cancel(cancel: CancelCheck | None) -> None:
    if cancel is not None and cancel():
        raise TurnCancelled
