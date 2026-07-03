"""Safety guard: the allow/deny policy between the model and the home.

Copyright (c) 2026 DigiSpark Advisory LLC. All rights reserved.
Clean-room implementation authored from SPEC.md 5 - see PROVENANCE.md.

The lists below are configuration expressing DigiSpark's own safety policy.
Evaluation order (SPEC.md 5): the hard denylist always wins; then the domain
allowlist (elevated confirmation domains are conditionally permitted); then the
target entity must exist and be exposed; finally elevated domains and elevated
cover device-classes are routed to confirmation instead of auto-execution.

This module is Home-Assistant-free by design: callers supply the set of
exposed entity ids and the target's device class, so the evaluation is pure
and unit-testable.
"""

from __future__ import annotations

from collections.abc import Container
from dataclasses import dataclass
from enum import Enum

# Service domains an agent command may target. Anything not listed is refused.
ALLOWED_COMMAND_DOMAINS: frozenset[str] = frozenset(
    {
        "light",
        "switch",
        "fan",
        "media_player",
        "climate",
        "cover",
        "input_boolean",
        "scene",
    }
)

# Services that may never be called by the agent. The denylist always wins.
DENIED_SERVICES: frozenset[str] = frozenset(
    {
        "homeassistant.restart",
        "homeassistant.stop",
        "recorder.purge",
        "python_script.exec",
    }
)

# Service-name suffixes/patterns that are always denied (e.g. any *.reload).
DENIED_SERVICE_SUFFIXES: tuple[str, ...] = (".reload",)

# Domains/device-classes that require explicit user confirmation.
CONFIRMATION_REQUIRED_DOMAINS: frozenset[str] = frozenset(
    {"lock", "alarm_control_panel"}
)
CONFIRMATION_REQUIRED_COVER_CLASSES: frozenset[str] = frozenset(
    {"garage", "gate", "door"}
)

# Maximum service calls a single turn may execute.
MAX_ACTIONS_PER_TURN = 5


class Verdict(Enum):
    """Outcome of a guard evaluation."""

    ALLOW = "allow"
    DENY = "deny"
    CONFIRM = "confirm"


@dataclass(frozen=True, slots=True)
class GuardDecision:
    """The guard's decision for one proposed service call, with the reason."""

    verdict: Verdict
    reason: str

    @property
    def allowed(self) -> bool:
        """True when the call may execute immediately."""
        return self.verdict is Verdict.ALLOW

    @property
    def denied(self) -> bool:
        """True when the call must be refused."""
        return self.verdict is Verdict.DENY

    @property
    def needs_confirmation(self) -> bool:
        """True when the call requires explicit user confirmation first."""
        return self.verdict is Verdict.CONFIRM


class TurnActionBudget:
    """Per-turn service-call budget enforcing the SPEC.md 5 action cap.

    Create one per user turn; every executed (or confirmed) service call must
    consume one unit. When the budget is exhausted, further calls are refused.
    """

    def __init__(self, limit: int = MAX_ACTIONS_PER_TURN) -> None:
        if limit < 1:
            raise ValueError("action budget limit must be >= 1")
        self._limit = limit
        self._used = 0

    @property
    def limit(self) -> int:
        """The total number of actions this turn may execute."""
        return self._limit

    @property
    def remaining(self) -> int:
        """How many actions may still execute this turn."""
        return self._limit - self._used

    def try_consume(self) -> bool:
        """Consume one action from the budget; False when exhausted."""
        if self._used >= self._limit:
            return False
        self._used += 1
        return True


def _normalize(value: str) -> str:
    return value.strip().lower()


def _is_denied_service(domain: str, service: str) -> bool:
    qualified = f"{domain}.{service}"
    if qualified in DENIED_SERVICES:
        return True
    return any(qualified.endswith(suffix) for suffix in DENIED_SERVICE_SUFFIXES)


def is_command_allowed(domain: str, service: str) -> bool:
    """Return whether a service call is permitted by the policy tables.

    Denylist first (exact and suffix), then the domain allowlist. Elevated
    confirmation domains count as permitted here because they may execute
    after user confirmation; use evaluate_command for the full decision
    including entity membership and confirmation routing.
    """
    domain = _normalize(domain)
    service = _normalize(service)
    if not domain or not service:
        return False
    if _is_denied_service(domain, service):
        return False
    return domain in ALLOWED_COMMAND_DOMAINS or domain in CONFIRMATION_REQUIRED_DOMAINS


def evaluate_command(
    domain: str,
    service: str,
    entity_id: str,
    *,
    exposed_entities: Container[str],
    device_class: str | None = None,
) -> GuardDecision:
    """Evaluate one model-proposed service call against the safety policy.

    domain/service/entity_id are model-supplied strings; the caller supplies
    the home context: exposed_entities is the collection of entity ids that
    exist and are exposed to the agent, and device_class is the target
    entity's device class (used for cover confirmation routing).
    """
    domain = _normalize(domain)
    service = _normalize(service)
    entity_id = _normalize(entity_id)

    if not domain or not service:
        return GuardDecision(Verdict.DENY, "malformed service call")

    # 1. Hard denylist - always wins, regardless of any allowlist.
    if _is_denied_service(domain, service):
        return GuardDecision(
            Verdict.DENY, f"service {domain}.{service} is denied by policy"
        )

    # 2. Domain allowlist. Elevated confirmation domains are conditionally
    #    permitted: they proceed, but are routed to confirmation in step 4.
    elevated_domain = domain in CONFIRMATION_REQUIRED_DOMAINS
    if domain not in ALLOWED_COMMAND_DOMAINS and not elevated_domain:
        return GuardDecision(
            Verdict.DENY, f"domain {domain} is not allowed for agent commands"
        )

    # 3. Entity membership - the model cannot invent targets. The target must
    #    be a well-formed entity id in the service's own domain, and it must
    #    exist and be exposed to the agent.
    entity_domain, sep, object_id = entity_id.partition(".")
    if not sep or not entity_domain or not object_id:
        return GuardDecision(Verdict.DENY, f"malformed entity id {entity_id!r}")
    if entity_domain != domain:
        return GuardDecision(
            Verdict.DENY,
            f"entity {entity_id} does not belong to service domain {domain}",
        )
    if entity_id not in exposed_entities:
        return GuardDecision(
            Verdict.DENY, f"entity {entity_id} does not exist or is not exposed"
        )

    # 4. Confirmation routing for elevated domains and cover device-classes.
    if elevated_domain:
        return GuardDecision(
            Verdict.CONFIRM, f"domain {domain} requires user confirmation"
        )
    if (
        domain == "cover"
        and device_class is not None
        and _normalize(device_class) in CONFIRMATION_REQUIRED_COVER_CLASSES
    ):
        return GuardDecision(
            Verdict.CONFIRM,
            f"cover device class {_normalize(device_class)} requires user confirmation",
        )

    return GuardDecision(Verdict.ALLOW, "permitted by policy")
