# DigiSpark HA Agent

A natural-language AI agent for Home Assistant, by [DigiSpark Advisory LLC](https://github.com/DigiSpark-Advisory). Ask about your home, run guarded commands, and get reviewable draft automations — including proactive suggestions learned from your home's own patterns.

> **This is the distribution repository.** It carries tagged releases of the integration for installation via HACS. Development happens in a private repository; issues and feature requests are welcome via the [issue tracker](https://github.com/DigiSpark-Advisory/digispark_ha_agent-releases/issues).

## What it does

- **Chat with your home** from a sidebar panel: ask about the state of any exposed entity.
- **Natural-language commands**, safety-guarded: a service allowlist and hard denylist, entity-membership checks, and a per-turn action cap. Locks, alarm panels, and garage/gate covers always require an explicit Approve step.
- **Draft automations**: the agent writes automations *disabled* and prefixed for your review — nothing runs until you accept it.
- **Pattern suggestions**: deterministic analytics over your recorder history (time-of-day routines, device correlations, recurring sequences) surface confidence-scored automation suggestions in a review inbox. No raw history ever leaves your Home Assistant instance.
- **Version history** for every agent automation, with a diff viewer and rollback, plus advisory-only stale-automation findings.
- **Choice of backend**: Anthropic (Claude), or a fully local, no-cloud backend via llama-server/Ollama (recommended weights: the Apache-2.0 Selora AI model) — with the local backend, nothing leaves your LAN.

## Installation (HACS)

1. In Home Assistant, open **HACS**, click the three-dot menu (top right) → **Custom repositories**.
2. Add `https://github.com/DigiSpark-Advisory/digispark_ha_agent-releases` with type **Integration**.
3. Find **DigiSpark HA Agent** in HACS and click **Download**.
4. Restart Home Assistant.
5. Go to **Settings → Devices & Services → Add Integration**, search for *DigiSpark HA Agent*, and follow the config flow (pick your provider, connect, choose a model).

Minimum Home Assistant version: **2025.1.0**.

## Safety model (summary)

Every model-proposed action passes a guard before execution: allowlist → hard denylist → entity checks → confirmation routing for elevated domains. Automations are created disabled and cannot be enabled by the model. All writes to `automations.yaml` are validated, backed up, and atomic. The panel talks to the backend only over Home Assistant's authenticated WebSocket API, admin-gated.

## License

Proprietary — © 2026 DigiSpark Advisory LLC. All rights reserved. See [LICENSE](LICENSE) and [NOTICE](NOTICE). Installation and use are permitted per the license terms; redistribution and derivative works are not.
