"""Home Assistant adapters for the tool runner (thin glue, SPEC.md §3–§5).

Copyright (c) 2026 DigiSpark Advisory LLC. All rights reserved.
Clean-room implementation — see PROVENANCE.md.

Supplies the HomeAdapters bundle backed by a running Home Assistant. Home
Assistant imports live inside the adapter functions so importing this module
in the pure-Python test suite needs no HA; the adapters themselves only run
inside HA.

"Exposed" here means: present in the state machine and not hidden or disabled
in the entity registry. Tightening this to Home Assistant's per-assistant
exposure settings is a candidate for a later phase.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from functools import partial
from typing import TYPE_CHECKING

from .agent.tools import HomeAdapters
from .automations.writer import (
    accept_draft,
    discard_draft,
    get_agent_automation,
    list_agent_automation_bodies,
    list_agent_automations,
    replace_agent_automation,
    write_draft_automation,
)
from .config_schema import merge_settings, redacted_settings
from .const import (
    CONF_MODEL,
    DOMAIN,
    MODEL_FETCH_TIMEOUT_SECONDS,
    PATTERN_LOOKBACK_DAYS,
    PROVIDER_ANTHROPIC,
    PROVIDER_LOCAL,
    SESSION_STORE_FILENAME,
    SUGGESTION_STORE_FILENAME,
    VERSION_STORE_FILENAME,
)
from .patterns import PatternEngine, StateEvent
from .patterns.suggestions import (
    STATUS_PENDING,
    SuggestionStore,
    SuggestionStoreError,
    synthesize_automation,
)
from .runtime import build_provider, friendly_hint, probe_provider
from .sessions import SessionStore
from .staleness import StaleDetector
from .versioning import VersionStore, utc_now_iso

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# Key within hass.data[DOMAIN] holding the shared version store (SPEC §12).
_VERSION_STORE = "version_store"
# Key within hass.data[DOMAIN] holding the latest stale-scan result (SPEC §13).
_STALE_ADVISORIES = "stale_advisories"
# Keys within hass.data[DOMAIN] for the suggestion store + last scan (SPEC §11).
_SUGGESTION_STORE = "suggestion_store"
_SUGGESTION_SCAN = "suggestion_scan"


def build_adapters(hass: HomeAssistant) -> HomeAdapters:
    """Bundle the HA-backed async callables the tool runner needs."""

    async def exposed_entities() -> set[str]:
        return _exposed_entity_ids(hass)

    async def get_state(entity_id: str) -> dict | None:
        state = hass.states.get(entity_id)
        if state is None:
            return None
        return {
            "entity_id": state.entity_id,
            "state": state.state,
            "attributes": dict(state.attributes),
            "last_changed": state.last_changed.isoformat(),
        }

    async def list_entities(domain: str | None, area: str | None) -> list[dict]:
        from homeassistant.helpers import area_registry as ar
        from homeassistant.helpers import device_registry as dr
        from homeassistant.helpers import entity_registry as er

        registry = er.async_get(hass)
        devices = dr.async_get(hass)

        area_id: str | None = None
        if area:
            wanted = area.strip().lower()
            for candidate in ar.async_get(hass).async_list_areas():
                if candidate.id == area or candidate.name.strip().lower() == wanted:
                    area_id = candidate.id
                    break
            if area_id is None:
                return []

        exposed = await exposed_entities()
        out: list[dict] = []
        for state in hass.states.async_all():
            entity_id = state.entity_id
            if entity_id not in exposed:
                continue
            if domain and not entity_id.startswith(f"{domain}."):
                continue
            if area_id:
                entry = registry.entities.get(entity_id)
                entity_area = entry.area_id if entry else None
                if entity_area is None and entry is not None and entry.device_id:
                    device = devices.devices.get(entry.device_id)
                    entity_area = device.area_id if device else None
                if entity_area != area_id:
                    continue
            out.append(
                {
                    "entity_id": entity_id,
                    "name": state.attributes.get("friendly_name", entity_id),
                    "state": state.state,
                }
            )
        return out

    async def list_areas() -> list[dict]:
        from homeassistant.helpers import area_registry as ar

        return [
            {"area_id": area.id, "name": area.name}
            for area in ar.async_get(hass).async_list_areas()
        ]

    async def get_history(entity_id: str, hours: int) -> list[dict]:
        from homeassistant.components.recorder import get_instance, history
        from homeassistant.util import dt as dt_util

        start = dt_util.utcnow() - timedelta(hours=hours)
        states = await get_instance(hass).async_add_executor_job(
            history.get_significant_states, hass, start, None, [entity_id]
        )
        return [
            {"state": s.state, "last_changed": s.last_changed.isoformat()}
            for s in states.get(entity_id, [])
        ]

    async def call_service(
        domain: str, service: str, entity_id: str, data: dict
    ) -> None:
        await hass.services.async_call(
            domain, service, {**data, "entity_id": entity_id}, blocking=True
        )

    async def draft_automation(automation: dict) -> dict:
        written = await hass.async_add_executor_job(
            write_draft_automation, hass.config.path("automations.yaml"), automation
        )
        # Reload so the disabled draft appears in the UI for review. This is
        # integration code, not a model-invokable service call; the agent's
        # own *.reload denylist (SPEC §5) is unaffected.
        await hass.services.async_call("automation", "reload", blocking=True)
        await _record_version(
            hass,
            written["id"],
            action="draft",
            body=written,
            author="agent",
            note="drafted by the agent",
        )
        return written

    async def device_class(entity_id: str) -> str | None:
        state = hass.states.get(entity_id)
        if state is None:
            return None
        value = state.attributes.get("device_class")
        return str(value) if value is not None else None

    return HomeAdapters(
        get_state=get_state,
        list_entities=list_entities,
        list_areas=list_areas,
        get_history=get_history,
        call_service=call_service,
        exposed_entities=exposed_entities,
        device_class=device_class,
        draft_automation=draft_automation,
    )


# --- Draft review (WS surface, not model-invokable) ---------------------------


async def async_list_drafts(hass: HomeAssistant) -> list[dict]:
    """Summarize agent-managed automations for the review surface."""
    return await hass.async_add_executor_job(
        list_agent_automations, hass.config.path("automations.yaml")
    )


async def async_accept_draft(
    hass: HomeAssistant, automation_id: str, *, author: str = "user"
) -> dict:
    """Accept a draft: unpin its forced-disabled flag, reload, enable it now.

    Removing ``initial_state: false`` makes acceptance survive restarts; the
    follow-up ``automation.turn_on`` enables it immediately for this run.
    The accepted body is recorded as a new version (SPEC §12).
    """
    result = await hass.async_add_executor_job(
        accept_draft, hass.config.path("automations.yaml"), automation_id
    )
    await hass.services.async_call("automation", "reload", blocking=True)

    from homeassistant.helpers import entity_registry as er

    entity_id = er.async_get(hass).async_get_entity_id(
        "automation", "automation", automation_id
    )
    if entity_id:
        await hass.services.async_call(
            "automation", "turn_on", {"entity_id": entity_id}, blocking=True
        )

    try:
        body = await hass.async_add_executor_job(
            get_agent_automation, hass.config.path("automations.yaml"), automation_id
        )
    except Exception:
        _LOGGER.exception("could not read %s for version recording", automation_id)
    else:
        await _record_version(
            hass,
            automation_id,
            action="accept",
            body=body,
            author=author,
            note="accepted by the user",
        )
    return {**result, "entity_id": entity_id}


async def async_discard_draft(
    hass: HomeAssistant, automation_id: str, *, author: str = "user"
) -> dict:
    """Remove one agent-managed automation (explicit user action) and reload.

    The discard is recorded as a new version carrying the last known body
    (SPEC §12), so a discarded automation's history remains inspectable.
    """
    result = await hass.async_add_executor_job(
        discard_draft, hass.config.path("automations.yaml"), automation_id
    )
    await hass.services.async_call("automation", "reload", blocking=True)

    store = _version_store(hass)
    try:
        body = await hass.async_add_executor_job(store.latest_body, automation_id)
    except Exception:
        _LOGGER.exception("could not read history of %s for discard", automation_id)
        body = None
    await _record_version(
        hass,
        automation_id,
        action="discard",
        body=body or {},
        author=author,
        note="discarded by the user",
    )
    return result


# --- Automation versioning (WS surface, not model-invokable; SPEC §12) --------


def _version_store(hass: HomeAssistant) -> VersionStore:
    """Return the shared version store, creating it on first use."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    store = domain_data.get(_VERSION_STORE)
    if store is None:
        store = VersionStore(hass.config.path(".storage", VERSION_STORE_FILENAME))
        domain_data[_VERSION_STORE] = store
    return store


async def _record_version(
    hass: HomeAssistant,
    automation_id: str,
    *,
    action: str,
    body: dict,
    author: str,
    note: str,
) -> None:
    """Record one version; a store failure must not undo a completed write."""
    store = _version_store(hass)
    try:
        await hass.async_add_executor_job(
            partial(
                store.record,
                automation_id,
                action=action,
                body=body,
                author=author,
                note=note,
            )
        )
    except Exception:
        _LOGGER.exception("failed to record %s version for %s", action, automation_id)


async def async_list_versions(hass: HomeAssistant, automation_id: str) -> list[dict]:
    """Body-less version summaries for one agent automation."""
    store = _version_store(hass)
    return await hass.async_add_executor_job(store.list_versions, automation_id)


async def async_get_version(
    hass: HomeAssistant,
    automation_id: str,
    version: int,
    diff_against: int | None = None,
) -> dict:
    """One full version record, optionally with a diff from another version."""
    store = _version_store(hass)
    record = await hass.async_add_executor_job(
        store.get_version, automation_id, version
    )
    diff: list[str] | None = None
    if diff_against is not None:
        diff = await hass.async_add_executor_job(
            store.diff, automation_id, diff_against, version
        )
    return {"record": record, "diff": diff}


async def async_rollback(
    hass: HomeAssistant, automation_id: str, version: int, *, author: str = "user"
) -> dict:
    """Roll back an agent automation to a recorded version (SPEC §12).

    The restored body writes through the §6 writer (validated, backed up,
    atomic) and the rollback itself is recorded as a new version. Recording
    is not best-effort here: a rollback that cannot be recorded fails loudly.
    """
    store = _version_store(hass)
    record = await hass.async_add_executor_job(
        store.get_version, automation_id, version
    )
    result = await hass.async_add_executor_job(
        replace_agent_automation,
        hass.config.path("automations.yaml"),
        automation_id,
        record["body"],
    )
    await hass.services.async_call("automation", "reload", blocking=True)
    new_record = await hass.async_add_executor_job(
        partial(
            store.record,
            automation_id,
            action="rollback",
            body=record["body"],
            author=author,
            note=f"rollback to version {version}",
        )
    )
    return {**result, "version": new_record["version"]}


# --- Stale detection (advisory-only, SPEC §13) ---------------------------------


async def async_scan_stale(hass: HomeAssistant) -> dict:
    """Run one advisory-only stale scan and cache the result.

    Gathers what the HA-free detector needs: agent automation bodies, the
    known and unavailable entity sets, each automation's ``last_triggered``
    (from its automation entity), and its first recorded version timestamp
    as the never-triggered fallback. Never disables or deletes anything.
    """
    from homeassistant.helpers import entity_registry as er

    automations = await hass.async_add_executor_job(
        list_agent_automation_bodies, hass.config.path("automations.yaml")
    )

    known: set[str] = set()
    unavailable: set[str] = set()
    for state in hass.states.async_all():
        known.add(state.entity_id)
        if state.state == "unavailable":
            unavailable.add(state.entity_id)

    registry = er.async_get(hass)
    store = _version_store(hass)
    last_triggered: dict[str, str | None] = {}
    created_at: dict[str, str | None] = {}
    for automation in automations:
        automation_id = str(automation.get("id", ""))
        last_triggered[automation_id] = _last_triggered(hass, registry, automation_id)
        try:
            versions = await hass.async_add_executor_job(
                store.list_versions, automation_id
            )
        except Exception:
            _LOGGER.exception("could not read history of %s for scan", automation_id)
            versions = []
        created_at[automation_id] = versions[0]["timestamp"] if versions else None

    advisories = StaleDetector().scan(
        automations,
        known_entities=known,
        unavailable_entities=unavailable,
        last_triggered=last_triggered,
        created_at=created_at,
    )
    result = {"advisories": advisories, "scanned_at": utc_now_iso()}
    hass.data.setdefault(DOMAIN, {})[_STALE_ADVISORIES] = result
    return result


async def async_stale_advisories(hass: HomeAssistant, *, rescan: bool = False) -> dict:
    """The latest cached scan result, scanning first when asked or empty."""
    cached = hass.data.get(DOMAIN, {}).get(_STALE_ADVISORIES)
    if rescan or cached is None:
        return await async_scan_stale(hass)
    return cached


def _last_triggered(hass: HomeAssistant, registry, automation_id: str) -> str | None:
    """ISO ``last_triggered`` of the automation entity, if it has one."""
    entity_id = registry.async_get_entity_id("automation", "automation", automation_id)
    if not entity_id:
        return None
    state = hass.states.get(entity_id)
    if state is None:
        return None
    raw = state.attributes.get("last_triggered")
    if raw is None:
        return None
    if hasattr(raw, "isoformat"):
        return raw.isoformat()
    return str(raw)


# --- Pattern suggestions (WS surface, not model-invokable; SPEC §11) -----------


def _exposed_entity_ids(hass: HomeAssistant) -> set[str]:
    """Entity ids present in the state machine and not hidden or disabled."""
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    exposed: set[str] = set()
    for state in hass.states.async_all():
        entry = registry.entities.get(state.entity_id)
        if entry is not None and (entry.hidden_by or entry.disabled_by):
            continue
        exposed.add(state.entity_id)
    return exposed


def _suggestion_store(hass: HomeAssistant) -> SuggestionStore:
    """Return the shared suggestion store, creating it on first use."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    store = domain_data.get(_SUGGESTION_STORE)
    if store is None:
        store = SuggestionStore(hass.config.path(".storage", SUGGESTION_STORE_FILENAME))
        domain_data[_SUGGESTION_STORE] = store
    return store


async def _cache_pending(hass: HomeAssistant, scanned_at: str) -> dict:
    """Refresh the cached pending-suggestion list without a new detection run."""
    store = _suggestion_store(hass)
    result = {
        "suggestions": await hass.async_add_executor_job(store.list_suggestions),
        "scanned_at": scanned_at,
    }
    hass.data.setdefault(DOMAIN, {})[_SUGGESTION_SCAN] = result
    return result


async def async_scan_patterns(hass: HomeAssistant) -> dict:
    """Run one detection pass over recorder history and update the store.

    History (SPEC §11; owner decision 2026-07-03): the last
    ``PATTERN_LOOKBACK_DAYS`` days of state changes for exposed entities only,
    read from the local recorder — raw history never leaves the process.
    Candidates that cannot be synthesized deterministically are dropped before
    they become suggestions, so an accept can never dead-end.
    """
    from homeassistant.components.recorder import get_instance, history
    from homeassistant.util import dt as dt_util

    entity_ids = sorted(_exposed_entity_ids(hass))
    if entity_ids:
        start = dt_util.utcnow() - timedelta(days=PATTERN_LOOKBACK_DAYS)
        states = await get_instance(hass).async_add_executor_job(
            history.get_significant_states, hass, start, None, entity_ids
        )
        events = [
            StateEvent(entity_id, s.state, s.last_changed.timestamp())
            for entity_id, entity_states in states.items()
            for s in entity_states
        ]
        offset = dt_util.now().utcoffset()
        tz_offset_minutes = int(offset.total_seconds() // 60) if offset else 0
        engine = PatternEngine(tz_offset_minutes=tz_offset_minutes)
        candidates = await hass.async_add_executor_job(engine.detect, events)
        actionable = [c for c in candidates if synthesize_automation(c) is not None]
        store = _suggestion_store(hass)
        await hass.async_add_executor_job(store.upsert_candidates, actionable)
    return await _cache_pending(hass, utc_now_iso())


async def async_list_suggestions(hass: HomeAssistant, *, rescan: bool = False) -> dict:
    """The latest cached suggestions, scanning first when asked or empty."""
    cached = hass.data.get(DOMAIN, {}).get(_SUGGESTION_SCAN)
    if rescan or cached is None:
        return await async_scan_patterns(hass)
    return cached


async def async_dismiss_suggestion(
    hass: HomeAssistant, signature: str, *, author: str = "user"
) -> dict:
    """Permanently dismiss one pending suggestion (SPEC §11, fork b)."""
    store = _suggestion_store(hass)
    record = await hass.async_add_executor_job(
        partial(store.dismiss, signature, author=author)
    )
    cached = hass.data.get(DOMAIN, {}).get(_SUGGESTION_SCAN) or {}
    await _cache_pending(hass, cached.get("scanned_at") or utc_now_iso())
    return {"dismissed": record}


async def async_accept_suggestion(
    hass: HomeAssistant, signature: str, *, author: str = "user"
) -> dict:
    """Accept one pending suggestion into a §6 draft automation.

    The synthesized body writes through the normal draft path — disabled,
    agent-prefixed, version-recorded — and then shows up in the drafts inbox
    for the usual review/enable step. Nothing is enabled here.
    """
    store = _suggestion_store(hass)
    record = await hass.async_add_executor_job(store.get, signature)
    if record["status"] != STATUS_PENDING:
        raise SuggestionStoreError(
            f"suggestion {signature!r} is already {record['status']}"
        )
    body = synthesize_automation(record["candidate"])
    if body is None:
        raise SuggestionStoreError(
            f"suggestion {signature!r} can no longer be synthesized"
        )
    written = await hass.async_add_executor_job(
        write_draft_automation, hass.config.path("automations.yaml"), body
    )
    await hass.services.async_call("automation", "reload", blocking=True)
    await _record_version(
        hass,
        written["id"],
        action="draft",
        body=written,
        author=author,
        note=f"drafted from pattern suggestion {signature}",
    )
    accepted = await hass.async_add_executor_job(
        partial(
            store.mark_accepted,
            signature,
            author=author,
            automation_id=written["id"],
        )
    )
    cached = hass.data.get(DOMAIN, {}).get(_SUGGESTION_SCAN) or {}
    await _cache_pending(hass, cached.get("scanned_at") or utc_now_iso())
    return {"automation_id": written["id"], "suggestion": accepted}


# --- Provider settings (WS surface, not model-invokable; SPEC §8) ---------------


def _settings_entry(hass: HomeAssistant, entry_id: str | None):
    """The addressed config entry, or the sole one when unambiguous."""
    entries = hass.config_entries.async_entries(DOMAIN)
    if entry_id:
        for entry in entries:
            if entry.entry_id == entry_id:
                return entry
        return None
    return entries[0] if len(entries) == 1 else None


async def async_get_provider_settings(
    hass: HomeAssistant, *, entry_id: str | None = None
) -> dict | None:
    """The entry's provider settings, redacted for the panel (SPEC §8)."""
    entry = _settings_entry(hass, entry_id)
    if entry is None:
        return None
    return {"settings": redacted_settings(dict(entry.data), dict(entry.options))}


async def async_update_provider_settings(
    hass: HomeAssistant, updates: dict, *, entry_id: str | None = None
) -> dict | None:
    """Apply a partial settings update; validate, persist, reload (SPEC §8).

    Validation runs through the same connection_problem rules as the create
    form. On success the config entry is updated — the entry's update
    listener reloads the agent, so the change takes effect immediately.
    On a problem nothing is written and the current settings are returned.
    """
    entry = _settings_entry(hass, entry_id)
    if entry is None:
        return None
    data, options, problem = merge_settings(
        dict(entry.data), dict(entry.options), dict(updates)
    )
    if problem:
        return {
            "success": False,
            "error": problem,
            "settings": redacted_settings(dict(entry.data), dict(entry.options)),
        }
    hass.config_entries.async_update_entry(entry, data=data, options=options)
    return {
        "success": True,
        "error": None,
        "settings": redacted_settings(data, options),
    }


async def async_test_provider_connection(
    hass: HomeAssistant, *, entry_id: str | None = None, chat: bool = False
) -> dict | None:
    """Connection test: model-list probe + optional one-token chat (SPEC §8).

    The chat probe runs against the configured model with a one-token budget.
    It is only meaningful for the Anthropic-format provider; the local
    backend's chat path goes through the specialist router, so its test is
    the model-list probe alone.
    """
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    entry = _settings_entry(hass, entry_id)
    if entry is None:
        return None
    data = dict(entry.data)
    options = dict(entry.options)
    chat_supported = data.get("provider", PROVIDER_ANTHROPIC) != PROVIDER_LOCAL
    provider = build_provider(
        async_get_clientsession(hass), data, options, max_tokens_override=1
    )
    result = await probe_provider(
        provider,
        chat=chat and chat_supported,
        model=str(options.get(CONF_MODEL, "") or ""),
    )
    if chat and not chat_supported:
        result["chat"] = {
            "success": False,
            "message": "The chat probe is not supported for the local provider.",
            "hint": "",
        }
    return result


async def async_list_provider_models(
    hass: HomeAssistant, *, entry_id: str | None = None
) -> dict | None:
    """Live model list using the entry's stored credentials (SPEC §8)."""
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    entry = _settings_entry(hass, entry_id)
    if entry is None:
        return None
    provider = build_provider(
        async_get_clientsession(hass), dict(entry.data), dict(entry.options)
    )
    try:
        async with asyncio.timeout(MODEL_FETCH_TIMEOUT_SECONDS):
            models = await provider.list_models()
    except Exception as err:
        message = str(err)
        return {
            "success": False,
            "models": [],
            "message": message,
            "hint": friendly_hint(message),
        }
    listed = [m for m in models if isinstance(m, str)]
    return {
        "success": True,
        "models": listed,
        "message": f"{len(listed)} model(s) available.",
        "hint": "",
    }


# --- Conversation sessions (SPEC §7) ---------------------------------------

# Keys within hass.data[DOMAIN] for the session store and the turn lock.
_SESSION_STORE = "session_store"
_SESSION_LOCK = "session_lock"


def _session_store(hass: HomeAssistant) -> SessionStore:
    """Return the shared session store, creating it on first use."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    store = domain_data.get(_SESSION_STORE)
    if store is None:
        store = SessionStore(hass.config.path(".storage", SESSION_STORE_FILENAME))
        domain_data[_SESSION_STORE] = store
    return store


def _session_lock(hass: HomeAssistant) -> asyncio.Lock:
    """The lock serializing turns so sessions cannot interleave in one loop."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    lock = domain_data.get(_SESSION_LOCK)
    if lock is None:
        lock = asyncio.Lock()
        domain_data[_SESSION_LOCK] = lock
    return lock


async def async_list_sessions(hass: HomeAssistant) -> dict:
    """Session summaries, most recently active first (SPEC §7)."""
    store = _session_store(hass)
    return {"sessions": await hass.async_add_executor_job(store.list_sessions)}


async def async_create_session(hass: HomeAssistant, title: str = "") -> dict:
    """Create one empty session and return its summary (SPEC §7)."""
    store = _session_store(hass)
    return await hass.async_add_executor_job(store.create_session, title)


async def async_rename_session(
    hass: HomeAssistant, session_id: str, title: str
) -> dict:
    """Rename one session and return its summary (SPEC §7)."""
    store = _session_store(hass)
    return await hass.async_add_executor_job(store.rename_session, session_id, title)


async def async_delete_session(hass: HomeAssistant, session_id: str) -> dict:
    """Delete one session (explicit user action) and return its summary."""
    store = _session_store(hass)
    return await hass.async_add_executor_job(store.delete_session, session_id)


async def async_chat_turn(
    hass: HomeAssistant, agent, message: str, *, session_id: str | None = None
) -> dict:
    """Run one agent turn against one session (SPEC §3, §7).

    Loads the session's committed history into the loop, runs the turn, and
    persists the new history back. The session lock serializes turns so two
    concurrent chats cannot interleave different sessions' histories through
    the entry's single loop. An absent session id targets the most recently
    active session, created on demand, so a session-unaware client keeps
    working unchanged.
    """
    store = _session_store(hass)
    async with _session_lock(hass):
        if session_id is None:
            session_id = await hass.async_add_executor_job(store.latest_session_id)
        if session_id is None:
            created = await hass.async_add_executor_job(store.create_session)
            session_id = created["id"]
        messages = await hass.async_add_executor_job(store.get_messages, session_id)
        agent.load_history(messages)
        result = await agent.run_turn(message)
        await hass.async_add_executor_job(
            store.commit_messages, session_id, agent.history
        )
    return {"session_id": session_id, "result": result}


async def async_session_history(
    hass: HomeAssistant, *, session_id: str | None = None
) -> dict:
    """One session's committed conversation for restore (SPEC §7, §9)."""
    store = _session_store(hass)
    if session_id is None:
        session_id = await hass.async_add_executor_job(store.latest_session_id)
        if session_id is None:
            return {"session_id": None, "messages": []}
    messages = await hass.async_add_executor_job(store.get_messages, session_id)
    return {"session_id": session_id, "messages": messages}
