"""Constants for the DigiSpark HA Agent integration.

Copyright (c) 2026 DigiSpark Advisory LLC. All rights reserved.
Clean-room implementation — see PROVENANCE.md.
"""

from __future__ import annotations

DOMAIN = "digispark_ha_agent"

# Config-entry keys.
CONF_PROVIDER = "provider"
CONF_API_KEY = "api_key"
CONF_MODEL = "model"
CONF_MAX_TOKENS = "max_tokens"

# Supported providers, selectable in the config flow (SPEC.md §8).
PROVIDER_ANTHROPIC = "anthropic"
# Local, privacy-first backend (OpenAI-compatible: llama-server / Ollama),
# recommended weights = the Apache-2.0 Selora AI model (SPEC.md §2.1).
PROVIDER_LOCAL = "local"
SUPPORTED_PROVIDERS: tuple[str, ...] = (PROVIDER_ANTHROPIC, PROVIDER_LOCAL)

# Custom Anthropic-compatible endpoint (SPEC.md §2.2, v0.4.1): gateways and
# proxies that speak the Messages API. Empty base URL = api.anthropic.com.
CONF_BASE_URL = "base_url"
CONF_CREDENTIAL_KIND = "credential_kind"
CONF_CREDENTIAL_HEADER = "credential_header"
# Raw "Name: value" lines; parsed by config_schema.parse_extra_headers.
# Stored in entry.data and treated as secret (may carry tenant/org tokens).
CONF_EXTRA_HEADERS = "extra_headers"
CREDENTIAL_KIND_X_API_KEY = "x-api-key"
CREDENTIAL_KIND_BEARER = "bearer"
CREDENTIAL_KIND_CUSTOM = "custom_header"
CREDENTIAL_KIND_NONE = "none"
CREDENTIAL_KINDS: tuple[str, ...] = (
    CREDENTIAL_KIND_X_API_KEY,
    CREDENTIAL_KIND_BEARER,
    CREDENTIAL_KIND_CUSTOM,
    CREDENTIAL_KIND_NONE,
)

# Local backend (SPEC.md §2.1). The host is stored in entry.data; cleartext
# http is only accepted for explicitly local hosts (providers/local.py).
CONF_HOST = "host"
DEFAULT_LOCAL_HOST = "http://localhost:8080"
# Conversation turns of history sent to the local model (model card).
LOCAL_HISTORY_TURNS = 3
# Cap on the AVAILABLE ENTITIES context block so a large home cannot exhaust
# the local model's context window.
MAX_LOCAL_ENTITY_BLOCK_CHARS = 4000

# Pattern-detection / stale-detection tunables (SPEC.md §11, §13).
# Acceptance floor on a candidate's raw pass rate (consistency): how often the
# outcome follows its trigger. Unchanged role; see PATTERN_CONFIDENCE_Z.
PATTERN_MIN_CONFIDENCE = 0.7
# Displayed/ranked confidence is a Wilson score lower bound on that pass rate
# (owner decision 2026-07-05, label-not-hide): a thinly-evidenced pattern (5/5)
# scores well below a well-evidenced one (500/500) instead of both reading
# 100%. This changes labels + ranking only — acceptance still gates on the raw
# rate (PATTERN_MIN_CONFIDENCE) — so nothing that surfaces today disappears.
# z = standard-normal quantile (1.96 ≈ one-sided 97.5%); higher z = more
# conservative (lower) scores for thin evidence.
PATTERN_CONFIDENCE_Z = 1.96
# Pattern detection (SPEC.md §11). History lookback the bridge feeds the
# engine (owner decision, 2026-07-03: 14 days, exposed entities only).
PATTERN_LOOKBACK_DAYS = 14
# Distinct occurrences (days for routines, precondition events for
# correlations/sequences) required before a candidate is considered.
PATTERN_MIN_SUPPORT = 3
# Correlations/sequences must recur across at least this many DISTINCT calendar
# days (owner decision 2026-07-05, fix #3). Raw occurrence counts let a single
# busy session or a restart storm reach full support; requiring spread across
# days keeps a same-day fluke from ever becoming a suggestion.
PATTERN_MIN_DISTINCT_DAYS = 2
# Gap threshold when clustering time-of-day occurrences, minutes.
PATTERN_TIME_TOLERANCE_MINUTES = 30
# Window in which B must follow A for correlations/sequences, seconds.
PATTERN_CORRELATION_WINDOW_SECONDS = 300
# Startup-cascade exclusion (owner report 2026-07-05, fix #1). Every HA restart
# flips a large fraction of entities within a few seconds (add-on *_running
# sensors, restored helpers); those co-occurrences are boot artifacts, not
# behaviour. Any PATTERN_BURST_WINDOW_SECONDS window in which at least the burst
# threshold of distinct entities change is dropped. The threshold scales with
# the data — max(PATTERN_BURST_MIN_ENTITIES, fraction x distinct entities
# present) — so ordinary multi-device activity (a scene, one busy room) is never
# mistaken for a cascade.
PATTERN_BURST_WINDOW_SECONDS = 60
PATTERN_BURST_MIN_ENTITIES = 8
PATTERN_BURST_ENTITY_FRACTION = 0.5
# Periodic detection-scan cadence; the WS surface supports an on-demand
# rescan (owner decision, 2026-07-03: daily).
PATTERN_SCAN_INTERVAL_HOURS = 24
# Pattern-scan scope + volume bounds (SPEC.md §11 perf; owner decision
# 2026-07-05). Detection only ever yields suggestions for controllable action
# targets and discrete-state triggers, so restricting the recorder scan to
# these domains — excluding continuous numerics like sensor / number /
# weather — both bounds the (superlinear) scan and removes noise candidates.
PATTERN_SCAN_DOMAINS: frozenset[str] = frozenset(
    {
        "light",
        "switch",
        "fan",
        "cover",
        "media_player",
        "input_boolean",
        "binary_sensor",
        "device_tracker",
        "person",
    }
)
# A single entity contributing more state changes than this over the lookback
# window is treated as noise (a flapping/misbehaving device) and dropped whole.
PATTERN_MAX_EVENTS_PER_ENTITY = 5000
# Hard ceiling on the total events handed to the engine; if exceeded after
# per-entity filtering, the most recent are kept.
PATTERN_MAX_TOTAL_EVENTS = 50000
# Entities per recorder-history batch when scanning (SPEC.md §11 perf). Reading
# the lookback for every in-scope entity at once can spike memory on a large
# recorder DB; batching bounds peak memory to one batch.
PATTERN_SCAN_BATCH_SIZE = 25
STALE_IDLE_DAYS = 30
# Periodic advisory-only stale scan cadence (SPEC.md §13).
STALE_SCAN_INTERVAL_HOURS = 6

# Version-store sidecar for agent-managed automations (SPEC.md §12). Lives
# under .storage/ beside HA's own store files; plain JSON managed by the
# HA-free versioning/store.py via executor.
VERSION_STORE_FILENAME = f"{DOMAIN}.versions.json"

# Suggestions sidecar (SPEC.md §11): pending pattern suggestions plus the
# permanent dismissed/accepted signature memory (a decision never resurfaces).
SUGGESTION_STORE_FILENAME = f"{DOMAIN}.suggestions.json"

# Conversation-sessions sidecar (SPEC.md §7, v0.6.0): server-side sessions,
# persisted, user-managed lifecycle. The 30-minute auto-expiry was retired
# with multi-session support (owner decision, 2026-07-03); the session cap
# in sessions/store.py bounds the store instead.
SESSION_STORE_FILENAME = f"{DOMAIN}.sessions.json"

# Agent-loop limits (see SPEC.md §3–§4).
MAX_LOOP_ITERATIONS = 8
MAX_DATA_MESSAGE_CHARS = 50_000
# Per-request message-window cap so capped data messages cannot stack past the
# context limit or the per-minute token budget (SPEC.md §4).
MAX_CONTEXT_MESSAGES = 40

# Provider request settings (see SPEC.md §2).
PROVIDER_TIMEOUT_SECONDS = 300
# Anthropic requires an explicit output-token budget on every Messages call.
# User-configurable via the config flow; this is the default.
DEFAULT_MAX_TOKENS = 4096
# Fallback model offered when the provider's model list cannot be fetched
# (SPEC.md §8); with a live provider the config flow lists models dynamically.
DEFAULT_MODEL = "claude-fable-5"
# The flow queries list_models() with its own short timeout so a down backend
# cannot hang the form for the provider's generous chat timeout.
MODEL_FETCH_TIMEOUT_SECONDS = 10
