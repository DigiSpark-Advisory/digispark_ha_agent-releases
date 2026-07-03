"""Tool runner: services the model's data/action requests through the guard.

Copyright (c) 2026 DigiSpark Advisory LLC. All rights reserved.
Clean-room implementation authored from SPEC.md §3–§5 — see PROVENANCE.md.

HomeToolRunner plugs into the agent loop's tool_runner seam. It is
Home-Assistant-free: all home access goes through HomeAdapters, a bundle of
async callables the HA glue (ha_bridge.py) supplies. Read-only data tools
return capped JSON (SPEC §4); call_service passes through safety/policy.py
(SPEC §5); elevated actions are queued as PendingAction records for explicit
user confirmation (the WebSocket approve/deny surface is a later sub-unit).

Tool-result formatting currently uses the Anthropic message helpers; when a
second tool-capable provider lands (Phase 3), formatting moves behind the
provider interface.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable, Container
from dataclasses import dataclass
from uuid import uuid4

from ..const import MAX_DATA_MESSAGE_CHARS
from ..providers.anthropic import tool_result_message
from ..providers.base import ChatResult, ToolCall
from ..safety.policy import MAX_ACTIONS_PER_TURN, TurnActionBudget, evaluate_command
from .loop import cap_data

_LOGGER = logging.getLogger(__name__)

DEFAULT_HISTORY_HOURS = 24
MAX_HISTORY_HOURS = 168  # one week


# --- Tool schemas advertised to the model (Anthropic format, SPEC §3) ---------

TOOL_GET_ENTITY_STATE: dict = {
    "name": "get_entity_state",
    "description": (
        "Read the current state and attributes of one entity by exact entity "
        "id (e.g. light.kitchen)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "entity_id": {"type": "string", "description": "Exact entity id."}
        },
        "required": ["entity_id"],
    },
}

TOOL_LIST_ENTITIES: dict = {
    "name": "list_entities",
    "description": (
        "List exposed entities with name and current state, optionally "
        "filtered by domain and/or area. Always filter when you can; do not "
        "dump the whole home."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "domain": {"type": "string", "description": "e.g. light, switch."},
            "area": {"type": "string", "description": "Area name or id."},
        },
    },
}

TOOL_LIST_AREAS: dict = {
    "name": "list_areas",
    "description": "List the home's areas (id and name).",
    "input_schema": {"type": "object", "properties": {}},
}

TOOL_GET_HISTORY: dict = {
    "name": "get_history",
    "description": "Recent state changes for one entity.",
    "input_schema": {
        "type": "object",
        "properties": {
            "entity_id": {"type": "string", "description": "Exact entity id."},
            "hours": {
                "type": "integer",
                "description": "Lookback window in hours (1-168, default 24).",
            },
        },
        "required": ["entity_id"],
    },
}

TOOL_CALL_SERVICE: dict = {
    "name": "call_service",
    "description": (
        "Execute one Home Assistant service call on one entity (e.g. "
        "light.turn_on on light.kitchen). Calls are subject to a safety "
        "policy; elevated devices (locks, alarms, garage doors) require "
        "explicit user confirmation and will be queued, not executed."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "domain": {"type": "string", "description": "Service domain."},
            "service": {"type": "string", "description": "Service name."},
            "entity_id": {"type": "string", "description": "Target entity id."},
            "data": {
                "type": "object",
                "description": "Optional service data (e.g. brightness).",
            },
        },
        "required": ["domain", "service", "entity_id"],
    },
}

TOOL_DRAFT_AUTOMATION: dict = {
    "name": "draft_automation",
    "description": (
        "Draft a new Home Assistant automation for the user to review. The "
        "draft is written disabled and marked agent-generated; it never runs "
        "until the user reviews and enables it."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "alias": {"type": "string", "description": "Short, human-readable name."},
            "description": {
                "type": "string",
                "description": "What the automation does and why.",
            },
            "trigger": {"description": "Automation trigger (object or array)."},
            "condition": {"description": "Optional condition (object or array)."},
            "action": {"description": "Automation action (object or array)."},
        },
        "required": ["alias", "trigger", "action"],
    },
}


def tool_schemas() -> list[dict]:
    """The tool schemas the agent advertises to the model."""
    return [
        TOOL_GET_ENTITY_STATE,
        TOOL_LIST_ENTITIES,
        TOOL_LIST_AREAS,
        TOOL_GET_HISTORY,
        TOOL_CALL_SERVICE,
        TOOL_DRAFT_AUTOMATION,
    ]


@dataclass(slots=True)
class HomeAdapters:
    """Async home-access callables; the HA glue supplies real implementations."""

    get_state: Callable[[str], Awaitable[dict | None]]
    list_entities: Callable[[str | None, str | None], Awaitable[list[dict]]]
    list_areas: Callable[[], Awaitable[list[dict]]]
    get_history: Callable[[str, int], Awaitable[list[dict]]]
    call_service: Callable[[str, str, str, dict], Awaitable[None]]
    exposed_entities: Callable[[], Awaitable[Container[str]]]
    device_class: Callable[[str], Awaitable[str | None]]
    # Writes a sanitized draft to automations.yaml and returns what was
    # written (SPEC §6). Optional: when absent, the draft tool reports
    # itself unavailable instead of failing the turn.
    draft_automation: Callable[[dict], Awaitable[dict]] | None = None


@dataclass(frozen=True, slots=True)
class PendingAction:
    """An elevated service call awaiting explicit user confirmation (SPEC §5)."""

    id: str
    domain: str
    service: str
    entity_id: str
    data: dict
    reason: str


class HomeToolRunner:
    """Services the model's tool calls for the agent loop (SPEC §3–§5).

    Matches the loop's ToolRunner seam: awaiting the instance with a ChatResult
    returns follow-up messages (or None for a final answer), and start_turn()
    resets per-turn state such as the action budget.
    """

    def __init__(
        self,
        adapters: HomeAdapters,
        *,
        max_data_chars: int = MAX_DATA_MESSAGE_CHARS,
        actions_per_turn: int = MAX_ACTIONS_PER_TURN,
    ) -> None:
        self._adapters = adapters
        self._max_data_chars = max_data_chars
        self._actions_per_turn = actions_per_turn
        self._budget = TurnActionBudget(actions_per_turn)
        # Elevated actions awaiting user confirmation, keyed by id. The
        # WebSocket approve/deny surface consumes these in a later sub-unit.
        self.pending: dict[str, PendingAction] = {}

    def start_turn(self) -> None:
        """Reset per-turn state; the loop calls this at the top of every turn."""
        self._budget = TurnActionBudget(self._actions_per_turn)

    def pending_actions(self) -> list[PendingAction]:
        """Snapshot of elevated actions awaiting user confirmation."""
        return list(self.pending.values())

    async def confirm_pending(self, action_id: str) -> PendingAction:
        """Execute and clear one pending elevated action (the user approved)."""
        action = self.pending.pop(action_id, None)
        if action is None:
            raise KeyError(f"no pending action {action_id}")
        await self._adapters.call_service(
            action.domain, action.service, action.entity_id, action.data
        )
        return action

    def deny_pending(self, action_id: str) -> PendingAction:
        """Clear one pending elevated action without executing it."""
        action = self.pending.pop(action_id, None)
        if action is None:
            raise KeyError(f"no pending action {action_id}")
        return action

    async def run_tool(self, name: str, args: dict) -> tuple[str, bool]:
        """Run one named tool through the guarded dispatch; never raises.

        The local-backend path (SPEC §2.1) executes envelope-proposed
        actions here, so the guard, the per-turn action budget, and the
        pending-confirmation queue stay the single choke point regardless
        of provider. Returns (content, is_error).
        """
        return await self._dispatch(ToolCall(id=uuid4().hex, name=name, input=args))

    async def __call__(self, result: ChatResult) -> list[dict] | None:
        if not result.tool_calls:
            return None
        blocks: list[dict] = []
        for call in result.tool_calls:
            content, is_error = await self._dispatch(call)
            blocks.append(
                tool_result_message(call.id, content, is_error=is_error)["content"][0]
            )
        # All results ride in one user-role message so roles keep alternating.
        return [{"role": "user", "content": blocks}]

    async def _dispatch(self, call: ToolCall) -> tuple[str, bool]:
        """Run one tool call; returns (content, is_error). Never raises."""
        handlers = {
            "get_entity_state": self._get_entity_state,
            "list_entities": self._list_entities,
            "list_areas": self._list_areas,
            "get_history": self._get_history,
            "call_service": self._call_service,
            "draft_automation": self._draft_automation,
        }
        handler = handlers.get(call.name)
        if handler is None:
            return f"unknown tool: {call.name}", True
        try:
            return await handler(call.input)
        except Exception as err:  # noqa: S112 - surfaced to the model as an error
            _LOGGER.exception("tool %s failed", call.name)
            return f"tool {call.name} failed: {err}", True

    def _payload(self, data: object) -> str:
        """Serialize a data result with the SPEC §4 per-item cap applied."""
        return cap_data(json.dumps(data, default=str), self._max_data_chars)

    async def _require_exposed(self, entity_id: str) -> str | None:
        """Shared existence/exposure check for the read-only entity tools."""
        if not entity_id:
            return "entity_id is required"
        exposed = await self._adapters.exposed_entities()
        if entity_id not in exposed:
            return f"entity {entity_id} does not exist or is not exposed"
        return None

    async def _get_entity_state(self, args: dict) -> tuple[str, bool]:
        entity_id = str(args.get("entity_id", "")).strip().lower()
        problem = await self._require_exposed(entity_id)
        if problem:
            return problem, True
        state = await self._adapters.get_state(entity_id)
        if state is None:
            return f"entity {entity_id} does not exist or is not exposed", True
        return self._payload(state), False

    async def _list_entities(self, args: dict) -> tuple[str, bool]:
        domain = str(args.get("domain", "")).strip().lower() or None
        area = str(args.get("area", "")).strip() or None
        entities = await self._adapters.list_entities(domain, area)
        return self._payload(entities), False

    async def _list_areas(self, args: dict) -> tuple[str, bool]:
        return self._payload(await self._adapters.list_areas()), False

    async def _get_history(self, args: dict) -> tuple[str, bool]:
        entity_id = str(args.get("entity_id", "")).strip().lower()
        problem = await self._require_exposed(entity_id)
        if problem:
            return problem, True
        raw_hours = args.get("hours", DEFAULT_HISTORY_HOURS)
        try:
            hours = int(raw_hours)
        except (TypeError, ValueError):
            return "hours must be an integer", True
        hours = max(1, min(hours, MAX_HISTORY_HOURS))
        history = await self._adapters.get_history(entity_id, hours)
        return self._payload(history), False

    async def _call_service(self, args: dict) -> tuple[str, bool]:
        domain = str(args.get("domain", ""))
        service = str(args.get("service", ""))
        entity_id = str(args.get("entity_id", ""))
        raw_data = args.get("data")
        data = raw_data if isinstance(raw_data, dict) else {}

        exposed = await self._adapters.exposed_entities()
        device_class = await self._adapters.device_class(entity_id.strip().lower())
        decision = evaluate_command(
            domain,
            service,
            entity_id,
            exposed_entities=exposed,
            device_class=device_class,
        )
        if decision.denied:
            return f"refused: {decision.reason}", True
        if not self._budget.try_consume():
            return (
                "refused: the per-turn action limit was reached; ask the user "
                "before attempting more actions",
                True,
            )
        domain = domain.strip().lower()
        service = service.strip().lower()
        entity_id = entity_id.strip().lower()
        if decision.needs_confirmation:
            pending = PendingAction(
                id=uuid4().hex,
                domain=domain,
                service=service,
                entity_id=entity_id,
                data=data,
                reason=decision.reason,
            )
            self.pending[pending.id] = pending
            return (
                f"queued for user confirmation (id {pending.id}): "
                f"{decision.reason}. The action will not run until the user "
                "approves it.",
                False,
            )
        await self._adapters.call_service(domain, service, entity_id, data)
        return f"executed {domain}.{service} on {entity_id}", False

    async def _draft_automation(self, args: dict) -> tuple[str, bool]:
        if self._adapters.draft_automation is None:
            return "automation drafting is not available", True
        draft = {
            "alias": str(args.get("alias", "")).strip(),
            "description": str(args.get("description", "")).strip(),
            "trigger": args.get("trigger"),
            "condition": args.get("condition"),
            "action": args.get("action"),
        }
        draft = {key: value for key, value in draft.items() if value}
        written = await self._adapters.draft_automation(draft)
        return (
            self._payload(
                {
                    "status": (
                        "draft created disabled; it will not run until the "
                        "user reviews and enables it"
                    ),
                    "id": written.get("id"),
                    "alias": written.get("alias"),
                }
            ),
            False,
        )
