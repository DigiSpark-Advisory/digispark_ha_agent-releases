"""Envelope parser for local specialist output.

Copyright (c) 2026 DigiSpark Advisory LLC. All rights reserved.
Clean-room implementation authored from the PUBLIC Apache-2.0 Selora AI model
card (v0.4.7), which documents each envelope shape - NOT from any proprietary
integration source. Data formats are not copyrightable. See PROVENANCE.md.

The specialists emit a JSON envelope that is an intermediate representation,
not a finished action. Documented shapes (model card, v0.4.7):

    command:       {"intent": "command", "response": ..., "calls": [...]}
    automation:    {"intent": "automation", "automation": {...}}
    answer:        {"intent": "answer", "response": ...}
    clarification: {"intent": "clarification", "response": ...}

plus the answer adapter's ``query_state`` tool envelope for live state
queries. Older card revisions documented single-character keys (r=response,
q=query, c=calls, s=service, e=entity_id, d=data); those are accepted as
aliases so mixed-version deployments keep working.

This module turns an envelope into internal objects only. Every resulting
action still flows through the safety guard and the disabled-by-default
automation writer - an envelope is a proposal, never a direct execution.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

INTENT_COMMAND = "command"
INTENT_AUTOMATION = "automation"
INTENT_ANSWER = "answer"
INTENT_CLARIFICATION = "clarification"
INTENT_QUERY_STATE = "query_state"

_KNOWN_INTENTS = frozenset(
    {
        INTENT_COMMAND,
        INTENT_AUTOMATION,
        INTENT_ANSWER,
        INTENT_CLARIFICATION,
        INTENT_QUERY_STATE,
    }
)

# Qwen3 emits an optional reasoning block; it is never part of the envelope.
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
_FENCE_RE = re.compile(r"^```[a-zA-Z0-9_-]*\s*|\s*```$")

# The automation body may arrive in HA's plural style; the writer expects
# singular section names (it normalizes single mappings into lists itself).
_PLURAL_SECTIONS = (
    ("triggers", "trigger"),
    ("conditions", "condition"),
    ("actions", "action"),
)


@dataclass(frozen=True, slots=True)
class EnvelopeCall:
    """One proposed service call (still subject to the guard)."""

    domain: str
    service: str
    entity_id: str
    data: dict


@dataclass(frozen=True, slots=True)
class Envelope:
    """A parsed specialist envelope: a proposal for the integration to vet."""

    intent: str
    response: str = ""
    calls: tuple[EnvelopeCall, ...] = ()
    automation: dict | None = None
    query_entities: tuple[str, ...] = ()


def parse_envelope(specialist: str, raw: str) -> Envelope:
    """Parse a specialist's raw output into an Envelope.

    Never raises: output that is not a JSON envelope degrades gracefully to a
    plain answer carrying the cleaned text, so a malformed model reply cannot
    fail the turn (and can never execute anything).
    """
    text = _strip_wrappers(raw)
    data = _load_json_object(text)
    if data is None:
        return Envelope(intent=INTENT_ANSWER, response=text)

    intent = str(data.get("intent") or data.get("i") or "").strip().lower()
    if intent not in _KNOWN_INTENTS:
        intent = _default_intent(specialist)

    response = str(data.get("response") or data.get("r") or "").strip()

    if intent == INTENT_COMMAND:
        calls = _parse_calls(data.get("calls", data.get("c")))
        return Envelope(intent=intent, response=response, calls=calls)

    if intent == INTENT_AUTOMATION:
        automation = _parse_automation(data.get("automation", data.get("a")))
        if automation is None:
            return Envelope(
                intent=INTENT_ANSWER,
                response=response or "the model returned no usable automation",
            )
        return Envelope(intent=intent, response=response, automation=automation)

    if intent == INTENT_QUERY_STATE:
        entities = _parse_query_entities(data)
        return Envelope(intent=intent, response=response, query_entities=entities)

    return Envelope(intent=intent, response=response)


def _strip_wrappers(raw: str) -> str:
    """Drop reasoning blocks and markdown fences around the envelope."""
    text = _THINK_RE.sub("", str(raw)).strip()
    if text.startswith("```"):
        text = _FENCE_RE.sub("", text).strip()
    return text


def _load_json_object(text: str) -> dict | None:
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _default_intent(specialist: str) -> str:
    specialist = str(specialist).strip().lower()
    if specialist in _KNOWN_INTENTS:
        return specialist
    return INTENT_ANSWER


def _parse_calls(raw: object) -> tuple[EnvelopeCall, ...]:
    """Vet call entries structurally; malformed entries are dropped.

    Accepts the documented shape ({service, entity_id, data}) with the
    service either domain-qualified ("light.turn_on") or split into
    domain/service keys, plus the legacy single-character aliases.
    """
    if not isinstance(raw, list):
        return ()
    calls: list[EnvelopeCall] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        service = str(entry.get("service") or entry.get("s") or "").strip().lower()
        domain = str(entry.get("domain") or "").strip().lower()
        if not domain and "." in service:
            domain, _, service = service.partition(".")
        entity_id = str(entry.get("entity_id") or entry.get("e") or "").strip().lower()
        raw_data = entry.get("data", entry.get("d"))
        data = raw_data if isinstance(raw_data, dict) else {}
        if not domain or not service or not entity_id:
            continue
        calls.append(
            EnvelopeCall(domain=domain, service=service, entity_id=entity_id, data=data)
        )
    return tuple(calls)


def _parse_automation(raw: object) -> dict | None:
    """Normalize an automation body for the SPEC §6 writer."""
    if not isinstance(raw, dict):
        return None
    body = dict(raw)
    for plural, singular in _PLURAL_SECTIONS:
        if plural in body and singular not in body:
            body[singular] = body.pop(plural)
    return body


def _parse_query_entities(data: dict) -> tuple[str, ...]:
    raw = (
        data.get("entity_id")
        or data.get("entities")
        or data.get("q")
        or data.get("query")
    )
    if isinstance(raw, str):
        candidates = [raw]
    elif isinstance(raw, list):
        candidates = [item for item in raw if isinstance(item, str)]
    else:
        return ()
    out: list[str] = []
    for candidate in candidates:
        entity_id = candidate.strip().lower()
        if entity_id and "." in entity_id:
            out.append(entity_id)
    return tuple(out)
