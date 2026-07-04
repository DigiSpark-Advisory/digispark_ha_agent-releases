/*
 * DigiSpark HA Agent — in-panel SPA (v0.6.0 sub-unit 5).
 * Copyright (c) 2026 DigiSpark Advisory LLC. All rights reserved.
 * Clean-room implementation authored from SPEC.md §7, §8, §9, §11, §12, §13.
 *
 * Vanilla custom element (no build step). The panel is a small single-page
 * app over Home Assistant's panel routing contract: it reads HA's `route`
 * to select a view and navigates with history.pushState + a location-changed
 * event (deep-linkable, browser-back friendly, survives reloads). A header
 * tab strip switches between three views — Conversations, Automations and
 * Themes — and collapses into an overflow menu on narrow layouts. Provider
 * settings live behind a header gear as an overlay drawer.
 *
 *   Conversations — a multi-conversation workspace over the session backend
 *     (SPEC §7): a sidebar lists sessions (list_sessions), New starts one
 *     (create_session), and each row can be renamed inline (rename_session)
 *     or deleted (delete_session). Chat turns carry the active session_id
 *     (chat) and history loads per session (history); the panel remembers the
 *     last-open session in localStorage. Assistant messages get a small,
 *     escape-first markdown pass. Pending service calls confirm inline
 *     (pending_actions, confirm_action, deny_action).
 *   Automations — a card grid of agent-managed automations (list_drafts):
 *     Accept/Discard on drafts, with per-card Flow (structured
 *     trigger/condition/action outline), YAML (the current body rendered from
 *     the latest version), and History (list_versions + get_version diff,
 *     SPEC §12). A Suggestions sub-tab lists pattern suggestions
 *     (list_suggestions, accept_suggestion, dismiss_suggestion, SPEC §11) and
 *     stale advisories (stale_advisories, SPEC §13) surface as card badges.
 *   Themes — panel-local visual presets (DigiSpark identity: subdued
 *     professional blues, Crestron-style dark surfaces). Selecting a preset
 *     overrides the panel's CSS variables on the host element and persists in
 *     localStorage; "Follow Home Assistant" clears the overrides. This only
 *     restyles the DigiSpark panel — it never changes HA's global theme.
 *   Settings — provider settings drawer (provider_settings,
 *     update_provider_settings, test_connection, list_models, SPEC §8) — the
 *     stored API key and extra-header values are never sent to the browser.
 *
 * All text is escaped before rendering; the markdown/YAML passes run on
 * already-escaped text and only introduce a fixed whitelist of tags.
 */

const esc = (s) =>
  String(s == null ? "" : s).replace(
    /[&<>"']/g,
    (c) =>
      ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;",
      })[c],
  );

const diffClass = (line) => {
  if (line.startsWith("+")) return "add";
  if (line.startsWith("-")) return "del";
  if (line.startsWith("@@")) return "hunk";
  return "ctx";
};

// Inline markdown over ALREADY-ESCAPED text: code spans, links (http/https
// only), bold, then italic. Order matters so bold isn't eaten by italic.
const mdInline = (escaped) => {
  let s = escaped.replace(/`([^`]+)`/g, (_m, p1) => `<code>${p1}</code>`);
  s = s.replace(
    /\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
    (_m, text, url) =>
      `<a href="${url}" target="_blank" rel="noopener noreferrer">${text}</a>`,
  );
  s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  s = s.replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, "$1<em>$2</em>");
  s = s.replace(/(^|[^_])_([^_\n]+)_(?!_)/g, "$1<em>$2</em>");
  return s;
};

// Block markdown: split on fenced code, format the rest line-by-line with
// simple unordered lists and <br> line breaks. Runs on escaped input.
const md = (raw) => {
  const parts = esc(raw).split(/```/);
  let out = "";
  parts.forEach((seg, i) => {
    if (i % 2 === 1) {
      out += `<pre class="md-code">${seg.replace(/^\n/, "")}</pre>`;
      return;
    }
    const lines = seg.split(/\n/);
    let inList = false;
    lines.forEach((line) => {
      const item = line.match(/^\s*[-*]\s+(.*)$/);
      if (item) {
        if (!inList) {
          out += "<ul>";
          inList = true;
        }
        out += `<li>${mdInline(item[1])}</li>`;
      } else {
        if (inList) {
          out += "</ul>";
          inList = false;
        }
        if (line.trim() !== "") out += `${mdInline(line)}<br>`;
      }
    });
    if (inList) out += "</ul>";
  });
  return out.replace(/(?:<br>)+$/, "");
};

// Minimal, read-only YAML dumper for a structured automation body (plain
// JSON from get_version.record.body). Output is escaped by the caller.
const yamlScalar = (v) => {
  if (v === null || v === undefined) return "null";
  if (typeof v === "boolean") return v ? "true" : "false";
  if (typeof v === "number") return String(v);
  const s = String(v);
  if (
    s === "" ||
    /^[\s>|&*!%@`#?:\-[\]{},"']/.test(s) ||
    /[:#]\s|\s$|\n/.test(s) ||
    /^(?:true|false|yes|no|on|off|null|none|~)$/i.test(s) ||
    /^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$/.test(s)
  )
    return JSON.stringify(s);
  return s;
};

const yamlLines = (val, indent) => {
  const pad = "  ".repeat(indent);
  if (Array.isArray(val)) {
    if (!val.length) return [`${pad}[]`];
    const out = [];
    val.forEach((item) => {
      const nested =
        item !== null &&
        typeof item === "object" &&
        (Array.isArray(item) ? item.length : Object.keys(item).length);
      if (nested) {
        const inner = yamlLines(item, indent + 1);
        inner[0] = `${pad}- ${inner[0].slice((indent + 1) * 2)}`;
        out.push(...inner);
      } else {
        out.push(`${pad}- ${yamlScalar(item)}`);
      }
    });
    return out;
  }
  if (val !== null && typeof val === "object") {
    const keys = Object.keys(val);
    if (!keys.length) return [`${pad}{}`];
    const out = [];
    keys.forEach((k) => {
      const v = val[k];
      const nested =
        v !== null &&
        typeof v === "object" &&
        (Array.isArray(v) ? v.length : Object.keys(v).length);
      if (nested) {
        out.push(`${pad}${k}:`);
        out.push(...yamlLines(v, indent + 1));
      } else if (v !== null && typeof v === "object") {
        out.push(`${pad}${k}: ${Array.isArray(v) ? "[]" : "{}"}`);
      } else {
        out.push(`${pad}${k}: ${yamlScalar(v)}`);
      }
    });
    return out;
  }
  return [`${pad}${yamlScalar(val)}`];
};

const toYaml = (body) => yamlLines(body, 0).join("\n");

// Panel-side messages for the WS settings-update error keys (SPEC §8).
const SETTINGS_ERRORS = {
  invalid_base_url: "Enter the custom endpoint as an http(s) URL.",
  cleartext_remote_base_url:
    "Cleartext http is only allowed for local endpoints; use https.",
  credential_header_required:
    "The custom_header credential kind needs a header name.",
  invalid_extra_headers:
    'Extra headers must be one "Name: value" per line; credential and protocol headers are not allowed.',
  api_key_required: "An API key is required for this credential kind.",
  invalid_credential_kind: "Unknown credential kind.",
  invalid_max_tokens: "Max tokens must be a positive number.",
  invalid_host: "Enter the local backend as an http(s) URL.",
  cleartext_remote_host:
    "Cleartext http is only allowed for local hosts; use https.",
};

const CREDENTIAL_KINDS = ["x-api-key", "bearer", "custom_header", "none"];

// The three routable views, in tab order.
const VIEWS = [
  ["conversations", "Conversations"],
  ["automations", "Automations"],
  ["themes", "Themes"],
];

// Below this host width the tab strip and the session sidebar collapse.
const COMPACT_WIDTH = 520;

// localStorage key remembering the last-open conversation across reloads.
const ACTIVE_SESSION_KEY = "digispark_ha_agent.active_session";

// localStorage key remembering the selected panel theme.
const THEME_KEY = "digispark_ha_agent.theme";

// HA CSS custom properties the panel themes override (without the -- prefix).
const THEME_VARS = [
  "primary-color",
  "primary-background-color",
  "card-background-color",
  "primary-text-color",
  "secondary-text-color",
  "divider-color",
  "text-primary-color",
  "error-color",
];

// Panel-local theme presets — DigiSpark identity: subdued professional blues,
// Crestron-style dark surfaces. "follow" clears the overrides and inherits the
// active Home Assistant theme. Applied by setting the variables on the host
// element; never touches HA's global theme.
const THEMES = [
  { id: "follow", name: "Follow Home Assistant", vars: null },
  {
    id: "digispark-light",
    name: "DigiSpark Light",
    vars: {
      "primary-color": "#2e5a87",
      "primary-background-color": "#f4f6f8",
      "card-background-color": "#ffffff",
      "primary-text-color": "#1c2733",
      "secondary-text-color": "#5b6b7a",
      "divider-color": "#d9e0e6",
      "text-primary-color": "#ffffff",
      "error-color": "#b3402f",
    },
  },
  {
    id: "digispark-dark",
    name: "DigiSpark Dark",
    vars: {
      "primary-color": "#4a86b8",
      "primary-background-color": "#12171d",
      "card-background-color": "#1b232c",
      "primary-text-color": "#e4e9ee",
      "secondary-text-color": "#93a1ad",
      "divider-color": "#2c3843",
      "text-primary-color": "#ffffff",
      "error-color": "#d9695a",
    },
  },
  {
    id: "digispark-slate",
    name: "DigiSpark Slate",
    vars: {
      "primary-color": "#5a8fbf",
      "primary-background-color": "#0f1b2b",
      "card-background-color": "#172636",
      "primary-text-color": "#dce6f0",
      "secondary-text-color": "#8ba0b5",
      "divider-color": "#22364a",
      "text-primary-color": "#ffffff",
      "error-color": "#cf6a5c",
    },
  },
];

class DigiSparkAgentPanel extends HTMLElement {
  constructor() {
    super();
    this._messages = [];
    this._pending = [];
    this._drafts = [];
    this._advisories = [];
    this._scannedAt = "";
    this._suggestions = [];
    this._suggScannedAt = "";
    this._settings = null;
    this._settingsOpen = false;
    this._settingsModels = [];
    this._settingsError = "";
    this._settingsNotice = "";
    this._testResult = null;
    this._busy = false;
    this._restored = false;
    this._view = "conversations";
    this._route = null;
    this._narrow = false;
    this._compact = false;
    this._menuOpen = false;
    this._sessions = [];
    this._activeSession = null;
    this._sidebarOpen = false;
    this._renamingId = null;
    this._confirmDeleteId = null;
    this._autoTab = "automations";
    this._expandedCard = null;
    this._cardDetailTab = "flow";
    this._cardVersions = [];
    this._cardBody = null;
    this._cardDiff = null;
    this._draftsLoading = false;
    this._staleLoading = false;
    this._suggLoading = false;
    this._draftsError = "";
    this._staleError = "";
    this._suggError = "";
    this._theme = "follow";
    this.attachShadow({ mode: "open" });
  }

  set hass(hass) {
    this._hass = hass;
    if (!this.shadowRoot.firstChild) this._render();
    if (hass && !this._restored) {
      this._restored = true;
      this._restore();
    }
  }

  get hass() {
    return this._hass;
  }

  set route(route) {
    this._route = route;
    this._view = this._viewFromRoute();
    if (this.shadowRoot.firstChild) this._applyView();
  }

  get route() {
    return this._route;
  }

  set narrow(narrow) {
    this._narrow = !!narrow;
    if (this._topbar) this._renderNav();
    this._applySidebarMode();
  }

  get narrow() {
    return this._narrow;
  }

  set panel(panel) {
    this._panel = panel;
  }

  get panel() {
    return this._panel;
  }

  disconnectedCallback() {
    if (this._ro) {
      this._ro.disconnect();
      this._ro = null;
    }
  }

  _viewFromRoute() {
    const path = (this._route && this._route.path) || "";
    const seg = String(path)
      .replace(/^\/+/, "")
      .split("/")[0];
    return seg === "automations" || seg === "themes" ? seg : "conversations";
  }

  _navigate(view) {
    this._menuOpen = false;
    this._view = view;
    const prefix =
      (this._route && this._route.prefix) ||
      "/" + (window.location.pathname.split("/")[1] || "digispark-ha-agent");
    const url = view === "conversations" ? prefix : `${prefix}/${view}`;
    if (window.location.pathname !== url) {
      window.history.pushState(null, "", url);
      window.dispatchEvent(new Event("location-changed"));
    }
    this._applyView();
  }

  _applyView() {
    const map = {
      conversations: this._viewConversations,
      automations: this._viewAutomations,
      themes: this._viewThemes,
    };
    Object.keys(map).forEach((key) => {
      if (map[key]) map[key].hidden = key !== this._view;
    });
    this._renderNav();
    this._applySidebarMode();
    if (this._view === "automations") this._refreshReview();
    if (this._view === "conversations" && this._input && !this._busy)
      this._input.focus();
  }

  _hasAutoAlerts() {
    return this._drafts.some((d) => !d.accepted) || this._suggestions.length > 0;
  }

  _renderNav() {
    if (!this._topbar) return;
    const alert = this._hasAutoAlerts();
    const label = (id, text) =>
      esc(text) + (id === "automations" && alert ? ' <span class="dot">•</span>' : "");
    const compact = this._narrow || this._compact;
    let nav;
    if (compact) {
      const current = VIEWS.find((v) => v[0] === this._view) || VIEWS[0];
      const menu = this._menuOpen
        ? `<div class="menu">${VIEWS.map(
            (v) =>
              `<button class="menu-item${v[0] === this._view ? " active" : ""}" data-nav="${v[0]}">${label(v[0], v[1])}</button>`,
          ).join("")}</div>`
        : "";
      nav = `<button class="menu-btn" data-nav-menu aria-haspopup="true" aria-expanded="${this._menuOpen ? "true" : "false"}">&#9776; ${label(current[0], current[1])}</button>${menu}`;
    } else {
      nav = `<div class="tabs">${VIEWS.map(
        (v) =>
          `<button class="tab${v[0] === this._view ? " active" : ""}" data-nav="${v[0]}">${label(v[0], v[1])}</button>`,
      ).join("")}</div>`;
    }
    this._topbar.innerHTML =
      `<div class="brand">DigiSpark Agent</div>${nav}` +
      `<button class="gear" data-gear title="Settings" aria-label="Settings">&#9881;</button>`;
  }

  _applySidebarMode() {
    if (!this._convo) return;
    const compact = this._narrow || this._compact;
    this._convo.classList.toggle("compact", compact);
    if (!compact) this._sidebarOpen = false;
    if (this._sidebar)
      this._sidebar.classList.toggle("open", compact && this._sidebarOpen);
  }

  async _ws(msg) {
    return this._hass.connection.sendMessagePromise(msg);
  }

  _pushError(err, fallback) {
    this._messages.push({
      role: "error",
      content: (err && err.message) || fallback,
    });
    this._renderMessages();
  }

  _readActiveSession() {
    try {
      return window.localStorage.getItem(ACTIVE_SESSION_KEY);
    } catch (_err) {
      return null;
    }
  }

  _persistActiveSession() {
    try {
      if (this._activeSession)
        window.localStorage.setItem(ACTIVE_SESSION_KEY, this._activeSession);
      else window.localStorage.removeItem(ACTIVE_SESSION_KEY);
    } catch (_err) {
      /* storage unavailable (embedded context) — non-fatal */
    }
  }

  _readTheme() {
    try {
      return window.localStorage.getItem(THEME_KEY);
    } catch (_err) {
      return null;
    }
  }

  _persistTheme() {
    try {
      if (this._theme && this._theme !== "follow")
        window.localStorage.setItem(THEME_KEY, this._theme);
      else window.localStorage.removeItem(THEME_KEY);
    } catch (_err) {
      /* storage unavailable (embedded context) — non-fatal */
    }
  }

  _applyTheme(id) {
    const theme = THEMES.find((t) => t.id === id);
    if (!theme || !theme.vars) {
      THEME_VARS.forEach((v) => this.style.removeProperty(`--${v}`));
      return;
    }
    THEME_VARS.forEach((v) => {
      if (theme.vars[v]) this.style.setProperty(`--${v}`, theme.vars[v]);
      else this.style.removeProperty(`--${v}`);
    });
  }

  _selectTheme(id) {
    this._theme = id;
    this._persistTheme();
    this._applyTheme(id);
    this._renderThemes();
  }

  async _restore() {
    await this._refreshSessions();
    const stored = this._readActiveSession();
    let active = null;
    if (stored && this._sessions.some((s) => s.id === stored)) active = stored;
    else if (this._sessions.length) active = this._sessions[0].id;
    this._activeSession = active;
    this._persistActiveSession();
    if (active) await this._loadSessionHistory(active);
    else {
      this._messages = [];
      this._renderMessages();
    }
    this._renderSessions();
    this._refreshPending();
    this._refreshReview();
  }

  async _refreshSessions() {
    if (!this._hass) return;
    try {
      const res = await this._ws({ type: "digispark_ha_agent/list_sessions" });
      this._sessions = (res && res.sessions) || [];
    } catch (_err) {
      this._sessions = [];
    }
    this._renderSessions();
  }

  async _loadSessionHistory(sessionId) {
    try {
      const res = await this._ws({
        type: "digispark_ha_agent/history",
        session_id: sessionId,
      });
      this._messages = (res && res.messages) || [];
    } catch (_err) {
      this._messages = [];
    }
    this._renderMessages();
  }

  async _newSession() {
    if (!this._hass || this._busy) return;
    try {
      const res = await this._ws({ type: "digispark_ha_agent/create_session" });
      const s = res && res.session;
      if (s) {
        this._activeSession = s.id;
        this._messages = [];
        this._persistActiveSession();
      }
    } catch (err) {
      this._pushError(err, "Could not create conversation");
    }
    this._renamingId = null;
    this._confirmDeleteId = null;
    await this._refreshSessions();
    this._renderMessages();
    this._sidebarOpen = false;
    this._applySidebarMode();
    if (this._input) this._input.focus();
  }

  async _switchSession(sessionId) {
    if (!this._busy && sessionId !== this._activeSession) {
      this._activeSession = sessionId;
      this._persistActiveSession();
      await this._loadSessionHistory(sessionId);
    }
    this._renderSessions();
    this._sidebarOpen = false;
    this._applySidebarMode();
  }

  _startRename(sessionId) {
    this._renamingId = sessionId;
    this._confirmDeleteId = null;
    this._renderSessions();
    const el = this.shadowRoot.querySelector("input.sess-rename");
    if (el) {
      el.focus();
      el.select();
    }
  }

  _cancelRename() {
    this._renamingId = null;
    this._renderSessions();
  }

  async _commitRename(sessionId) {
    const el = this.shadowRoot.querySelector("input.sess-rename");
    const title = el ? el.value.trim() : "";
    if (!title) {
      this._cancelRename();
      return;
    }
    try {
      await this._ws({
        type: "digispark_ha_agent/rename_session",
        session_id: sessionId,
        title,
      });
    } catch (err) {
      this._pushError(err, "Rename failed");
    }
    this._renamingId = null;
    await this._refreshSessions();
  }

  _askDelete(sessionId) {
    this._confirmDeleteId = sessionId;
    this._renamingId = null;
    this._renderSessions();
  }

  _cancelDelete() {
    this._confirmDeleteId = null;
    this._renderSessions();
  }

  async _confirmDelete(sessionId) {
    try {
      await this._ws({
        type: "digispark_ha_agent/delete_session",
        session_id: sessionId,
      });
    } catch (err) {
      this._pushError(err, "Delete failed");
    }
    this._confirmDeleteId = null;
    const wasActive = sessionId === this._activeSession;
    await this._refreshSessions();
    if (wasActive) {
      const next = this._sessions.length ? this._sessions[0].id : null;
      this._activeSession = next;
      this._persistActiveSession();
      if (next) await this._loadSessionHistory(next);
      else {
        this._messages = [];
        this._renderMessages();
      }
    }
    this._renderSessions();
  }

  async _refreshPending() {
    if (!this._hass) return;
    try {
      const res = await this._ws({ type: "digispark_ha_agent/pending_actions" });
      this._pending = (res && res.actions) || [];
    } catch (_err) {
      this._pending = [];
    }
    this._renderPending();
  }

  // Paint the Automations shell immediately, then load each section
  // independently so a slow recorder scan can never blank the view or block
  // the drafts list (v0.6.1 fix).
  _refreshReview(rescan) {
    if (!this._hass) return;
    this._renderAutomations();
    this._loadDrafts();
    this._loadStale(rescan);
    this._loadSuggestions(rescan);
  }

  async _loadDrafts() {
    this._draftsLoading = true;
    this._draftsError = "";
    this._renderAutomations();
    try {
      const res = await this._ws({ type: "digispark_ha_agent/list_drafts" });
      this._drafts = (res && res.drafts) || [];
    } catch (err) {
      this._drafts = [];
      this._draftsError = (err && err.message) || "Could not load automations";
    }
    this._draftsLoading = false;
    this._renderAutomations();
  }

  async _loadStale(rescan) {
    this._staleLoading = true;
    this._staleError = "";
    this._renderAutomations();
    try {
      const msg = { type: "digispark_ha_agent/stale_advisories" };
      if (rescan) msg.rescan = true;
      const res = await this._ws(msg);
      this._advisories = (res && res.advisories) || [];
      this._scannedAt = (res && res.scanned_at) || "";
    } catch (err) {
      this._advisories = [];
      this._scannedAt = "";
      this._staleError =
        (err && err.message) || "Could not scan for stale automations";
    }
    this._staleLoading = false;
    this._renderAutomations();
  }

  async _loadSuggestions(rescan) {
    this._suggLoading = true;
    this._suggError = "";
    this._renderAutomations();
    try {
      const msg = { type: "digispark_ha_agent/list_suggestions" };
      if (rescan) msg.rescan = true;
      const res = await this._ws(msg);
      this._suggestions = (res && res.suggestions) || [];
      this._suggScannedAt = (res && res.scanned_at) || "";
    } catch (err) {
      this._suggestions = [];
      this._suggScannedAt = "";
      this._suggError = (err && err.message) || "Could not load suggestions";
    }
    this._suggLoading = false;
    this._renderAutomations();
  }

  _rescanSuggestions() {
    this._loadSuggestions(true);
  }

  async _actOnSuggestion(act, signature) {
    const type =
      act === "accept"
        ? "digispark_ha_agent/accept_suggestion"
        : "digispark_ha_agent/dismiss_suggestion";
    try {
      const res = await this._ws({ type, signature });
      if (act === "accept") {
        this._messages.push({
          role: "assistant",
          content: `Suggestion accepted as a disabled draft (${res.automation_id}). Review and enable it under Automations.`,
        });
      } else {
        this._messages.push({
          role: "assistant",
          content: "Suggestion dismissed — it will not be shown again.",
        });
      }
    } catch (err) {
      this._messages.push({
        role: "error",
        content: (err && err.message) || "Suggestion action failed",
      });
    }
    this._renderMessages();
    this._refreshReview();
  }

  async _loadSettings() {
    if (!this._hass) return;
    try {
      const res = await this._ws({ type: "digispark_ha_agent/provider_settings" });
      this._settings = (res && res.settings) || null;
    } catch (_err) {
      this._settings = null;
    }
    this._settingsModels = [];
    this._settingsError = "";
    this._settingsNotice = "";
    this._testResult = null;
    this._renderSettings(null);
  }

  _readSettingsForm() {
    const val = (id) => {
      const el = this.shadowRoot.getElementById(id);
      return el ? el.value : "";
    };
    const checked = (id) => {
      const el = this.shadowRoot.getElementById(id);
      return !!(el && el.checked);
    };
    return {
      base_url: val("set-base-url").trim(),
      credential_kind: val("set-cred-kind"),
      credential_header: val("set-cred-header").trim(),
      api_key: val("set-api-key"),
      extra_headers: val("set-headers"),
      clear_headers: checked("set-clear-headers"),
      model: val("set-model").trim(),
      max_tokens: val("set-max-tokens"),
      host: val("set-host").trim(),
      chat_probe: checked("set-test-chat"),
    };
  }

  async _saveSettings() {
    const form = this._readSettingsForm();
    const updates = {
      base_url: form.base_url,
      credential_kind: form.credential_kind,
      credential_header: form.credential_header,
      model: form.model,
      max_tokens: form.max_tokens,
    };
    if (this._settings && this._settings.provider === "local")
      updates.host = form.host;
    if (form.api_key.trim()) updates.api_key = form.api_key;
    if (form.clear_headers) updates.extra_headers = "";
    else if (form.extra_headers.trim()) updates.extra_headers = form.extra_headers;
    try {
      const res = await this._ws({
        type: "digispark_ha_agent/update_provider_settings",
        settings: updates,
      });
      if (res && res.success) {
        this._settings = res.settings;
        this._settingsError = "";
        this._settingsNotice = "Saved. The agent reloaded with the new settings.";
        this._testResult = null;
        this._renderSettings(null);
      } else {
        const key = (res && res.error) || "";
        this._settingsError = SETTINGS_ERRORS[key] || key || "Save failed";
        this._settingsNotice = "";
        this._renderSettings(form);
      }
    } catch (err) {
      this._settingsError = (err && err.message) || "Save failed";
      this._settingsNotice = "";
      this._renderSettings(form);
    }
  }

  async _fetchSettingsModels() {
    const form = this._readSettingsForm();
    try {
      const res = await this._ws({ type: "digispark_ha_agent/list_models" });
      if (res && res.success) {
        this._settingsModels = res.models || [];
        this._settingsError = "";
        this._settingsNotice = `Fetched ${this._settingsModels.length} model(s).`;
      } else {
        this._settingsError =
          (res && (res.hint || res.message)) || "Model fetch failed";
        this._settingsNotice = "";
      }
    } catch (err) {
      this._settingsError = (err && err.message) || "Model fetch failed";
      this._settingsNotice = "";
    }
    this._renderSettings(form);
  }

  async _testSettingsConnection() {
    const form = this._readSettingsForm();
    try {
      this._testResult = await this._ws({
        type: "digispark_ha_agent/test_connection",
        chat: form.chat_probe,
      });
    } catch (err) {
      this._testResult = {
        success: false,
        list_models: {
          success: false,
          message: (err && err.message) || "Test failed",
          hint: "",
        },
        chat: null,
      };
    }
    this._settingsError = "";
    this._settingsNotice = "";
    this._renderSettings(form);
  }

  _renderSettings(form) {
    if (!this._settingsBox) return;
    const s = this._settings;
    if (!s) {
      this._settingsBox.innerHTML = `<div class="none">No configured entry.</div>`;
      return;
    }
    const v = (formValue, saved) => esc(form ? formValue : saved);
    const kindNow = form ? form.credential_kind : s.credential_kind;
    const kindOptions = CREDENTIAL_KINDS.map(
      (k) =>
        `<option value="${esc(k)}"${k === kindNow ? " selected" : ""}>${esc(k)}</option>`,
    ).join("");
    const modelNow = String(form ? form.model : s.model);
    let modelField;
    if (this._settingsModels.length) {
      const opts = [...this._settingsModels];
      if (modelNow && !opts.includes(modelNow)) opts.push(modelNow);
      modelField = `<select id="set-model">${opts
        .map(
          (m) =>
            `<option value="${esc(m)}"${m === modelNow ? " selected" : ""}>${esc(m)}</option>`,
        )
        .join("")}</select>`;
    } else {
      modelField = `<input id="set-model" type="text" value="${esc(modelNow)}" />`;
    }
    const hostField =
      s.provider === "local"
        ? `<div class="field"><label>Local backend URL</label>
            <input id="set-host" type="text" value="${v(form && form.host, s.host)}" /></div>`
        : "";
    const keyStatus = s.has_api_key ? "configured" : "not set";
    const maskedNote = s.extra_headers_masked
      ? ` Current: ${esc(s.extra_headers_masked).replaceAll("\n", ", ")}.`
      : "";
    this._settingsBox.innerHTML = `
      <h3>Provider settings <span class="badge">${esc(s.provider)}</span></h3>
      ${hostField}
      <div class="field"><label>Custom endpoint base URL (Anthropic-compatible; empty = api.anthropic.com)</label>
        <input id="set-base-url" type="text" value="${v(form && form.base_url, s.base_url)}" /></div>
      <div class="field"><label>Credential kind</label>
        <select id="set-cred-kind">${kindOptions}</select></div>
      <div class="field"><label>Credential header name (custom_header kind only)</label>
        <input id="set-cred-header" type="text" value="${v(form && form.credential_header, s.credential_header)}" /></div>
      <div class="field"><label>API key (${esc(keyStatus)} — leave blank to keep)</label>
        <input id="set-api-key" type="password" value="" autocomplete="off" /></div>
      <div class="field"><label>Extra inference headers, one "Name: value" per line (blank = keep current).${maskedNote}</label>
        <textarea id="set-headers" rows="2">${form ? esc(form.extra_headers) : ""}</textarea>
        <label><input id="set-clear-headers" type="checkbox"${form && form.clear_headers ? " checked" : ""} /> Clear all extra headers</label></div>
      <div class="field"><label>Model</label>${modelField}</div>
      <div class="field"><label>Max output tokens</label>
        <input id="set-max-tokens" type="number" min="1" value="${v(form && form.max_tokens, s.max_tokens)}" /></div>
      <div class="row">
        <span class="what"><label><input id="set-test-chat" type="checkbox"${form && form.chat_probe ? " checked" : ""} /> Include one-token chat probe</label></span>
        <button class="ghost" data-settings="fetch-models">Fetch models</button>
        <button class="ghost" data-settings="test">Test connection</button>
        <button data-settings="save">Save</button>
      </div>
      <div class="none">Fetch and Test use the saved settings, not unsaved edits.</div>
      ${this._settingsResultHtml()}`;
  }

  _settingsResultHtml() {
    let html = "";
    if (this._settingsError)
      html += `<div class="advice"><span class="result-bad">${esc(this._settingsError)}</span></div>`;
    if (this._settingsNotice)
      html += `<div class="advice"><span class="result-ok">${esc(this._settingsNotice)}</span></div>`;
    const t = this._testResult;
    if (t && t.list_models) {
      const line = (r) =>
        `<span class="${r.success ? "result-ok" : "result-bad"}">${r.success ? "OK" : "FAILED"} — ${esc(r.message)}</span>${r.hint ? ` <span class="detail">${esc(r.hint)}</span>` : ""}`;
      html += `<div class="advice">Models: ${line(t.list_models)}</div>`;
      if (t.chat) html += `<div class="advice">Chat: ${line(t.chat)}</div>`;
    }
    return html;
  }

  async _toggleCard(automationId) {
    if (this._expandedCard === automationId) {
      this._expandedCard = null;
      this._cardVersions = [];
      this._cardBody = null;
      this._cardDiff = null;
      this._renderAutomations();
      return;
    }
    this._expandedCard = automationId;
    this._cardDetailTab = "flow";
    this._cardVersions = [];
    this._cardBody = null;
    this._cardDiff = null;
    await this._loadCard(automationId);
    this._renderAutomations();
  }

  async _loadCard(automationId) {
    try {
      const res = await this._ws({
        type: "digispark_ha_agent/list_versions",
        automation_id: automationId,
      });
      this._cardVersions = (res && res.versions) || [];
    } catch (_err) {
      this._cardVersions = [];
    }
    const latest = this._cardVersions.length
      ? this._cardVersions[this._cardVersions.length - 1].version
      : null;
    if (latest != null) {
      try {
        const res = await this._ws({
          type: "digispark_ha_agent/get_version",
          automation_id: automationId,
          version: latest,
        });
        this._cardBody = (res && res.record && res.record.body) || null;
      } catch (_err) {
        this._cardBody = null;
      }
    }
  }

  async _showCardDiff(automationId) {
    const from = parseInt(this.shadowRoot.getElementById("cdiff-from").value, 10);
    const to = parseInt(this.shadowRoot.getElementById("cdiff-to").value, 10);
    try {
      const res = await this._ws({
        type: "digispark_ha_agent/get_version",
        automation_id: automationId,
        version: to,
        diff_against: from,
      });
      this._cardDiff = (res && res.diff) || [];
      if (!this._cardDiff.length)
        this._cardDiff = ["(the two versions are identical)"];
    } catch (err) {
      this._cardDiff = [(err && err.message) || "diff failed"];
    }
    this._renderAutomations();
  }

  async _actOnDraft(act, automationId) {
    const type =
      act === "accept"
        ? "digispark_ha_agent/accept_draft"
        : "digispark_ha_agent/discard_draft";
    try {
      const res = await this._ws({ type, automation_id: automationId });
      const verb = act === "accept" ? "Accepted" : "Discarded";
      this._messages.push({
        role: "assistant",
        content: `${verb} automation: ${res.alias || automationId}`,
      });
    } catch (err) {
      this._messages.push({
        role: "error",
        content: (err && err.message) || "Draft action failed",
      });
    }
    if (this._expandedCard === automationId && act === "discard") {
      this._expandedCard = null;
      this._cardVersions = [];
      this._cardBody = null;
      this._cardDiff = null;
    }
    this._renderMessages();
    this._refreshReview();
  }

  async _actOnPending(act, actionId) {
    if (!this._hass) return;
    const type =
      act === "confirm"
        ? "digispark_ha_agent/confirm_action"
        : "digispark_ha_agent/deny_action";
    try {
      const res = await this._ws({ type, action_id: actionId });
      const a = res.executed || res.denied || {};
      const verb = act === "confirm" ? "Approved" : "Denied";
      this._messages.push({
        role: "assistant",
        content: `${verb}: ${a.domain}.${a.service} on ${a.entity_id}`,
      });
    } catch (err) {
      this._messages.push({
        role: "error",
        content: (err && err.message) || "Action failed",
      });
    }
    this._renderMessages();
    this._refreshPending();
  }

  async _send(text) {
    if (!text || this._busy || !this._hass) return;
    this._messages.push({ role: "user", content: text });
    this._busy = true;
    this._renderMessages();
    const sessionId = this._activeSession;
    try {
      const msg = { type: "digispark_ha_agent/chat", message: text };
      if (sessionId) msg.session_id = sessionId;
      const res = await this._ws(msg);
      if (res && res.session_id) {
        this._activeSession = res.session_id;
        this._persistActiveSession();
      }
      this._messages.push({ role: "assistant", content: res.text });
    } catch (err) {
      this._messages.push({
        role: "error",
        content: (err && err.message) || "Request failed",
      });
    }
    this._busy = false;
    this._renderMessages();
    this._refreshPending();
    this._refreshReview();
    this._refreshSessions();
  }

  _render() {
    this.shadowRoot.innerHTML = `
      <style>
        :host { display: block; height: 100%; background: var(--primary-background-color, #fafafa); }
        .wrap { position: relative; display: flex; flex-direction: column; height: 100%; box-sizing: border-box; }
        .topbar { display: flex; align-items: center; gap: 8px; padding: 8px 12px; border-bottom: 1px solid var(--divider-color, #e0e0e0); background: var(--card-background-color, #fff); position: relative; }
        .brand { font-weight: 600; font-size: 14px; color: var(--primary-text-color, #212121); white-space: nowrap; }
        .tabs { display: flex; gap: 4px; flex: 1; }
        .tab { background: transparent; color: var(--secondary-text-color, #727272); border: none; border-radius: 8px; padding: 6px 12px; font-size: 14px; cursor: pointer; }
        .tab.active { background: var(--primary-color, #03a9f4); color: var(--text-primary-color, #fff); }
        .menu-btn { flex: 1; text-align: left; background: transparent; color: var(--primary-text-color, #212121); border: 1px solid var(--divider-color, #e0e0e0); border-radius: 8px; padding: 6px 12px; font-size: 14px; cursor: pointer; }
        .menu { position: absolute; top: 46px; left: 12px; right: 12px; z-index: 20; background: var(--card-background-color, #fff); border: 1px solid var(--divider-color, #e0e0e0); border-radius: 8px; box-shadow: 0 4px 16px rgba(0,0,0,0.18); overflow: hidden; }
        .menu-item { display: block; width: 100%; text-align: left; background: transparent; color: var(--primary-text-color, #212121); border: none; border-radius: 0; padding: 10px 14px; font-size: 14px; cursor: pointer; }
        .menu-item.active { color: var(--primary-color, #03a9f4); font-weight: 600; }
        .gear { margin-left: auto; background: transparent; color: var(--secondary-text-color, #727272); border: none; font-size: 18px; padding: 4px 8px; cursor: pointer; }
        .dot { color: var(--primary-color, #03a9f4); }
        .view { flex: 1; min-height: 0; }
        .view[hidden] { display: none; }
        #view-conversations { height: 100%; }
        #view-automations { height: 100%; overflow-y: auto; }
        #view-themes { height: 100%; overflow-y: auto; }
        .placeholder { color: var(--secondary-text-color, #727272); text-align: center; padding: 24px; max-width: 360px; line-height: 1.5; }
        .convo { position: relative; display: flex; height: 100%; }
        .sidebar { width: 240px; flex: none; display: flex; flex-direction: column; border-right: 1px solid var(--divider-color, #e0e0e0); background: var(--card-background-color, #fff); overflow: hidden; }
        .sidebar-head { display: flex; align-items: center; justify-content: space-between; padding: 10px 12px; border-bottom: 1px solid var(--divider-color, #e0e0e0); font-size: 12px; text-transform: uppercase; letter-spacing: 0.05em; color: var(--secondary-text-color, #727272); }
        .sidebar-head button { padding: 4px 10px; font-size: 12px; }
        .sess-list { flex: 1; overflow-y: auto; }
        .sess { display: flex; align-items: center; gap: 2px; padding: 2px 6px 2px 10px; border-bottom: 1px solid var(--divider-color, #e0e0e0); }
        .sess.active { background: var(--primary-background-color, #fafafa); }
        .sess-open { flex: 1; min-width: 0; text-align: left; background: transparent; color: var(--primary-text-color, #212121); border: none; border-radius: 0; padding: 8px 4px; font-size: 13px; cursor: pointer; }
        .sess.active .sess-open { color: var(--primary-color, #03a9f4); font-weight: 600; }
        .sess-title { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
        .icon { background: transparent; border: none; color: var(--secondary-text-color, #727272); cursor: pointer; padding: 4px 6px; font-size: 15px; }
        .icon.sm { font-size: 13px; }
        .sess .what { flex: 1; min-width: 0; font-size: 13px; padding: 6px 4px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
        .sess input.sess-rename { flex: 1; min-width: 0; padding: 6px 8px; font-size: 13px; border-radius: 6px; border: 1px solid var(--divider-color, #e0e0e0); background: var(--primary-background-color, #fafafa); color: var(--primary-text-color, #212121); }
        .sess button.ghost, .sess button.discard { padding: 3px 8px; font-size: 12px; }
        .chat { flex: 1; min-width: 0; display: flex; flex-direction: column; }
        .chat-head { display: none; align-items: center; gap: 8px; padding: 8px 12px; border-bottom: 1px solid var(--divider-color, #e0e0e0); background: var(--card-background-color, #fff); }
        .chat-title { font-size: 13px; font-weight: 600; color: var(--primary-text-color, #212121); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
        .convo.compact .chat-head { display: flex; }
        .convo.compact .sidebar { position: absolute; top: 0; bottom: 0; left: 0; z-index: 15; transform: translateX(-100%); transition: transform 0.2s ease; box-shadow: 2px 0 8px rgba(0,0,0,0.15); }
        .convo.compact .sidebar.open { transform: translateX(0); }
        .log { flex: 1; overflow-y: auto; padding: 16px; display: flex; flex-direction: column; gap: 8px; }
        .msg { max-width: 80%; padding: 8px 12px; border-radius: 12px; white-space: pre-wrap; word-wrap: break-word; line-height: 1.4; }
        .msg.user { align-self: flex-end; background: var(--primary-color, #03a9f4); color: var(--text-primary-color, #fff); }
        .msg.assistant { align-self: flex-start; background: var(--card-background-color, #fff); color: var(--primary-text-color, #212121); border: 1px solid var(--divider-color, #e0e0e0); }
        .msg.error { align-self: flex-start; background: var(--error-color, #db4437); color: #fff; }
        .msg.thinking { align-self: flex-start; color: var(--secondary-text-color, #727272); font-style: italic; }
        .msg a { color: var(--primary-color, #03a9f4); }
        .msg code { background: rgba(0,0,0,0.06); padding: 1px 4px; border-radius: 4px; font-family: monospace; font-size: 0.92em; }
        .msg ul { margin: 4px 0; padding-left: 20px; }
        .msg li { margin: 2px 0; }
        pre.md-code { background: var(--primary-background-color, #fafafa); border: 1px solid var(--divider-color, #e0e0e0); border-radius: 6px; padding: 8px; overflow-x: auto; font-family: monospace; font-size: 12px; white-space: pre; margin: 6px 0; }
        pre.md-code code { background: none; padding: 0; }
        .bar { display: flex; gap: 8px; padding: 12px 16px; border-top: 1px solid var(--divider-color, #e0e0e0); background: var(--card-background-color, #fff); }
        input { flex: 1; padding: 10px 12px; border-radius: 8px; border: 1px solid var(--divider-color, #e0e0e0); background: var(--primary-background-color, #fafafa); color: var(--primary-text-color, #212121); font-size: 14px; }
        input:disabled { opacity: 0.6; }
        button { padding: 0 16px; border: none; border-radius: 8px; background: var(--primary-color, #03a9f4); color: var(--text-primary-color, #fff); cursor: pointer; font-size: 14px; }
        button:disabled { opacity: 0.6; cursor: default; }
        button.ghost { background: transparent; color: var(--primary-color, #03a9f4); border: 1px solid var(--divider-color, #e0e0e0); }
        .empty { color: var(--secondary-text-color, #727272); text-align: center; margin-top: 24px; }
        .pending { border-top: 1px solid var(--divider-color, #e0e0e0); background: var(--card-background-color, #fff); }
        .pend { display: flex; align-items: center; gap: 8px; padding: 8px 16px; font-size: 13px; color: var(--primary-text-color, #212121); }
        .pend .what { flex: 1; }
        .pend .why { color: var(--secondary-text-color, #727272); }
        .pend button { padding: 4px 12px; font-size: 13px; }
        .pend button.deny { background: var(--error-color, #db4437); }
        .section { font-size: 13px; color: var(--primary-text-color, #212121); }
        .section h3 { margin: 12px 16px 4px; font-size: 12px; text-transform: uppercase; letter-spacing: 0.05em; color: var(--secondary-text-color, #727272); display: flex; align-items: center; gap: 8px; }
        .section h3 button { padding: 2px 10px; font-size: 12px; }
        .auto-tabs { display: flex; gap: 6px; padding: 10px 16px 0; border-bottom: 1px solid var(--divider-color, #e0e0e0); }
        .subtab { background: transparent; color: var(--secondary-text-color, #727272); border: none; border-bottom: 2px solid transparent; border-radius: 0; padding: 6px 8px; font-size: 14px; cursor: pointer; }
        .subtab.active { color: var(--primary-text-color, #212121); border-bottom-color: var(--primary-color, #03a9f4); }
        .auto-scan { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 8px 16px 0; }
        .auto-scan button { padding: 3px 10px; font-size: 12px; }
        .cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap: 10px; padding: 10px 16px 16px; }
        .card { border: 1px solid var(--divider-color, #e0e0e0); border-radius: 10px; background: var(--card-background-color, #fff); padding: 10px 12px; display: flex; flex-direction: column; gap: 6px; }
        .card.open { grid-column: 1 / -1; }
        .card-title { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
        .card-desc { color: var(--secondary-text-color, #727272); font-size: 12px; }
        .card-actions { display: flex; gap: 6px; flex-wrap: wrap; }
        .card-actions button { padding: 4px 12px; font-size: 13px; }
        .detail { border-top: 1px solid var(--divider-color, #e0e0e0); margin-top: 4px; padding-top: 8px; }
        .detail-tabs { display: flex; gap: 4px; margin-bottom: 6px; }
        .dtab { background: transparent; color: var(--secondary-text-color, #727272); border: 1px solid var(--divider-color, #e0e0e0); border-radius: 6px; padding: 3px 10px; font-size: 12px; cursor: pointer; }
        .dtab.active { background: var(--primary-color, #03a9f4); color: var(--text-primary-color, #fff); border-color: transparent; }
        .flow-sec { margin: 4px 0; }
        .flow-h { font-size: 12px; font-weight: 600; color: var(--secondary-text-color, #727272); }
        .flow-sec ul { margin: 2px 0 6px; padding-left: 18px; }
        .flow-sec li { font-size: 12px; word-break: break-word; }
        .flow-sec li.muted { color: var(--secondary-text-color, #727272); }
        .row { display: flex; align-items: center; gap: 8px; padding: 6px 16px; }
        .row .what { flex: 1; }
        .row button { padding: 4px 12px; font-size: 13px; }
        .row button.discard { background: var(--error-color, #db4437); }
        .row button.ghost { background: transparent; color: var(--primary-color, #03a9f4); border: 1px solid var(--divider-color, #e0e0e0); }
        .badge { display: inline-block; padding: 1px 8px; border-radius: 10px; font-size: 11px; background: var(--divider-color, #e0e0e0); color: var(--primary-text-color, #212121); }
        .badge.ok { background: #c8e6c9; }
        .badge.warn { background: #ffe0b2; }
        .vrow { display: flex; gap: 8px; padding: 2px 0; color: var(--primary-text-color, #212121); }
        .vrow .meta { color: var(--secondary-text-color, #727272); }
        .diffbar { display: flex; align-items: center; gap: 8px; margin-top: 6px; }
        .diffbar select { padding: 4px; border-radius: 6px; border: 1px solid var(--divider-color, #e0e0e0); background: var(--primary-background-color, #fafafa); color: var(--primary-text-color, #212121); }
        pre.diff { margin: 6px 0 0; padding: 8px; border-radius: 6px; background: var(--primary-background-color, #fafafa); overflow-x: auto; font-size: 12px; line-height: 1.35; white-space: pre; }
        pre.diff .add { color: #2e7d32; }
        pre.diff .del { color: #c62828; }
        pre.diff .hunk { color: var(--secondary-text-color, #727272); }
        .advice { padding: 6px 16px; }
        .advice .detail { color: var(--secondary-text-color, #727272); }
        .none { padding: 8px 16px; color: var(--secondary-text-color, #727272); }
        .field { display: flex; flex-direction: column; gap: 2px; padding: 6px 16px; }
        .field label { font-size: 12px; color: var(--secondary-text-color, #727272); }
        .field input, .field select, .field textarea { padding: 8px 10px; border-radius: 8px; border: 1px solid var(--divider-color, #e0e0e0); background: var(--primary-background-color, #fafafa); color: var(--primary-text-color, #212121); font-size: 13px; }
        .field input[type="checkbox"] { flex: none; padding: 0; }
        .result-ok { color: #2e7d32; }
        .result-bad { color: #c62828; }
        .theme-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 10px; padding: 10px 16px 16px; }
        .theme-card { display: flex; flex-direction: column; gap: 6px; padding: 8px; background: var(--card-background-color, #fff); border: 1px solid var(--divider-color, #e0e0e0); border-radius: 10px; cursor: pointer; text-align: left; color: var(--primary-text-color, #212121); }
        .theme-card.active { border-color: var(--primary-color, #03a9f4); box-shadow: 0 0 0 1px var(--primary-color, #03a9f4); }
        .swatch { position: relative; display: flex; align-items: center; gap: 6px; height: 54px; border-radius: 8px; padding: 8px; overflow: hidden; border: 1px solid rgba(0,0,0,0.08); box-sizing: border-box; }
        .sw-card { display: inline-flex; align-items: center; justify-content: center; width: 34px; height: 34px; border-radius: 6px; font-size: 13px; font-weight: 600; }
        .sw-accent { width: 14px; height: 34px; border-radius: 6px; }
        .sw-line { position: absolute; right: 8px; bottom: 8px; width: 40%; height: 4px; border-radius: 2px; opacity: 0.85; }
        .theme-name { font-size: 13px; }
        .drawer { position: absolute; top: 0; right: 0; bottom: 0; left: 0; z-index: 30; background: var(--card-background-color, #fff); display: flex; flex-direction: column; }
        .drawer[hidden] { display: none; }
        .drawer-head { display: flex; align-items: center; justify-content: space-between; padding: 12px 16px; border-bottom: 1px solid var(--divider-color, #e0e0e0); font-weight: 600; color: var(--primary-text-color, #212121); }
        .drawer-head button { padding: 4px 12px; font-size: 13px; }
        .drawer-body { flex: 1; overflow-y: auto; }
      </style>
      <div class="wrap">
        <header class="topbar" id="topbar"></header>
        <section class="view" id="view-conversations">
          <div class="convo" id="convo">
            <aside class="sidebar" id="convo-sidebar">
              <div class="sidebar-head"><span>Conversations</span>
                <button class="ghost" data-sess-act="new">New</button></div>
              <div class="sess-list" id="sess-list"></div>
            </aside>
            <div class="chat">
              <div class="chat-head" id="chat-head">
                <button class="icon" id="sidebar-toggle" aria-label="Conversations">&#9776;</button>
                <span class="chat-title" id="chat-title"></span>
              </div>
              <div class="log" id="log"></div>
              <div class="pending" id="pending"></div>
              <form class="bar" id="form">
                <input id="input" type="text" placeholder="Ask your home…" autocomplete="off" />
                <button id="send" type="submit">Send</button>
              </form>
            </div>
          </div>
        </section>
        <section class="view" id="view-automations" hidden>
          <div class="section" id="auto-content"></div>
        </section>
        <section class="view" id="view-themes" hidden>
          <div class="section" id="themes-content"></div>
        </section>
        <div class="drawer" id="settings" hidden>
          <div class="drawer-head"><span>Settings</span>
            <button class="ghost" id="settings-close" type="button">Close</button></div>
          <div class="section drawer-body" id="settings-body"></div>
        </div>
      </div>`;
    this._topbar = this.shadowRoot.getElementById("topbar");
    this._viewConversations = this.shadowRoot.getElementById("view-conversations");
    this._viewAutomations = this.shadowRoot.getElementById("view-automations");
    this._viewThemes = this.shadowRoot.getElementById("view-themes");
    this._convo = this.shadowRoot.getElementById("convo");
    this._sidebar = this.shadowRoot.getElementById("convo-sidebar");
    this._sessList = this.shadowRoot.getElementById("sess-list");
    this._chatTitle = this.shadowRoot.getElementById("chat-title");
    this._log = this.shadowRoot.getElementById("log");
    this._pendingBox = this.shadowRoot.getElementById("pending");
    this._input = this.shadowRoot.getElementById("input");
    this._sendBtn = this.shadowRoot.getElementById("send");
    this._autoContent = this.shadowRoot.getElementById("auto-content");
    this._themesContent = this.shadowRoot.getElementById("themes-content");
    this._settingsDrawer = this.shadowRoot.getElementById("settings");
    this._settingsBox = this.shadowRoot.getElementById("settings-body");

    this._topbar.addEventListener("click", (ev) => {
      const nav = ev.target.closest("button[data-nav]");
      if (nav) {
        this._navigate(nav.dataset.nav);
        return;
      }
      if (ev.target.closest("[data-nav-menu]")) {
        this._menuOpen = !this._menuOpen;
        this._renderNav();
        return;
      }
      if (ev.target.closest("[data-gear]")) this._toggleSettings();
    });
    this._sidebar.addEventListener("click", (ev) => {
      const btn = ev.target.closest("button[data-sess-act]");
      if (!btn) return;
      const act = btn.dataset.sessAct;
      const id = btn.dataset.id;
      if (this._busy && (act === "new" || act === "switch")) return;
      if (act === "new") this._newSession();
      else if (act === "switch") this._switchSession(id);
      else if (act === "rename") this._startRename(id);
      else if (act === "rename-save") this._commitRename(id);
      else if (act === "rename-cancel") this._cancelRename();
      else if (act === "del") this._askDelete(id);
      else if (act === "del-yes") this._confirmDelete(id);
      else if (act === "del-no") this._cancelDelete();
    });
    this._sidebar.addEventListener("keydown", (ev) => {
      const el = ev.target;
      if (!el.classList || !el.classList.contains("sess-rename")) return;
      if (ev.key === "Enter") {
        ev.preventDefault();
        this._commitRename(el.dataset.id);
      } else if (ev.key === "Escape") {
        ev.preventDefault();
        this._cancelRename();
      }
    });
    this.shadowRoot
      .getElementById("sidebar-toggle")
      .addEventListener("click", () => {
        this._sidebarOpen = !this._sidebarOpen;
        this._applySidebarMode();
      });
    this._pendingBox.addEventListener("click", (ev) => {
      const btn = ev.target.closest("button[data-act]");
      if (btn) this._actOnPending(btn.dataset.act, btn.dataset.id);
    });
    this._autoContent.addEventListener("click", (ev) => {
      const tab = ev.target.closest("button[data-auto-tab]");
      if (tab) {
        this._autoTab = tab.dataset.autoTab;
        this._renderAutomations();
        return;
      }
      const dtab = ev.target.closest("button[data-detail-tab]");
      if (dtab) {
        this._cardDetailTab = dtab.dataset.detailTab;
        this._renderAutomations();
        return;
      }
      const act = ev.target.closest("button[data-auto-act]");
      if (act) {
        const a = act.dataset.autoAct;
        const id = act.dataset.id;
        if (a === "accept" || a === "discard") this._actOnDraft(a, id);
        else if (a === "expand") this._toggleCard(id);
        else if (a === "diff") this._showCardDiff(id);
        else if (a === "rescan-stale") this._loadStale(true);
        return;
      }
      const sugg = ev.target.closest("button[data-sugg-act]");
      if (sugg) {
        const a = sugg.dataset.suggAct;
        if (a === "accept" || a === "dismiss")
          this._actOnSuggestion(a, sugg.dataset.id);
        else if (a === "rescan") this._rescanSuggestions();
      }
    });
    this._viewThemes.addEventListener("click", (ev) => {
      const btn = ev.target.closest("button[data-theme-id]");
      if (btn) this._selectTheme(btn.dataset.themeId);
    });
    this._settingsBox.addEventListener("click", (ev) => {
      const btn = ev.target.closest("button[data-settings]");
      if (!btn) return;
      const act = btn.dataset.settings;
      if (act === "save") this._saveSettings();
      else if (act === "test") this._testSettingsConnection();
      else if (act === "fetch-models") this._fetchSettingsModels();
    });
    this.shadowRoot
      .getElementById("settings-close")
      .addEventListener("click", () => this._toggleSettings(false));
    this.shadowRoot.getElementById("form").addEventListener("submit", (ev) => {
      ev.preventDefault();
      const value = this._input.value.trim();
      this._input.value = "";
      this._send(value);
    });

    if (typeof ResizeObserver !== "undefined") {
      this._ro = new ResizeObserver((entries) => {
        const width = entries[0].contentRect.width;
        const compact = width > 0 && width < COMPACT_WIDTH;
        if (compact !== this._compact) {
          this._compact = compact;
          this._renderNav();
          this._applySidebarMode();
        }
      });
      this._ro.observe(this);
    }

    this._theme = this._readTheme() || "follow";
    this._applyTheme(this._theme);
    this._renderThemes();
    this._renderSessions();
    this._renderMessages();
    this._applyView();
  }

  _toggleSettings(open) {
    this._settingsOpen = open === undefined ? !this._settingsOpen : open;
    if (this._settingsDrawer) this._settingsDrawer.hidden = !this._settingsOpen;
    if (this._settingsOpen) this._loadSettings();
  }

  _renderSessions() {
    if (!this._sessList) return;
    if (!this._sessions.length) {
      this._sessList.innerHTML = `<div class="none">No conversations yet.</div>`;
    } else {
      this._sessList.innerHTML = this._sessions
        .map((s) => this._sessionRow(s))
        .join("");
    }
    if (this._chatTitle) {
      const active = this._sessions.find((s) => s.id === this._activeSession);
      this._chatTitle.textContent = active
        ? active.title || "New conversation"
        : "New conversation";
    }
  }

  _sessionRow(s) {
    const id = esc(s.id);
    if (this._renamingId === s.id) {
      return `<div class="sess">
          <input class="sess-rename" data-id="${id}" type="text" value="${esc(s.title || "")}" />
          <button class="ghost" data-sess-act="rename-save" data-id="${id}">Save</button>
          <button class="ghost" data-sess-act="rename-cancel" data-id="${id}">Cancel</button>
        </div>`;
    }
    if (this._confirmDeleteId === s.id) {
      return `<div class="sess">
          <span class="what">Delete “${esc(s.title || "New conversation")}”?</span>
          <button class="discard" data-sess-act="del-yes" data-id="${id}">Delete</button>
          <button class="ghost" data-sess-act="del-no" data-id="${id}">Cancel</button>
        </div>`;
    }
    const active = s.id === this._activeSession ? " active" : "";
    return `<div class="sess${active}">
        <button class="sess-open" data-sess-act="switch" data-id="${id}" title="${esc(s.title || "New conversation")}">
          <span class="sess-title">${esc(s.title || "New conversation")}</span>
        </button>
        <button class="icon sm" data-sess-act="rename" data-id="${id}" aria-label="Rename" title="Rename">&#9998;</button>
        <button class="icon sm" data-sess-act="del" data-id="${id}" aria-label="Delete" title="Delete">&#128465;</button>
      </div>`;
  }

  _renderThemes() {
    if (!this._themesContent) return;
    this._themesContent.innerHTML = `
      <h3>Panel theme</h3>
      <div class="none">Applies to the DigiSpark panel only; your Home Assistant theme is unchanged.</div>
      <div class="theme-grid">${THEMES.map((t) => this._themeCard(t)).join("")}</div>`;
  }

  _themeCard(t) {
    const active = t.id === this._theme ? " active" : "";
    const v = t.vars;
    const bg = v ? v["primary-background-color"] : "var(--primary-background-color, #fafafa)";
    const card = v ? v["card-background-color"] : "var(--card-background-color, #fff)";
    const accent = v ? v["primary-color"] : "var(--primary-color, #03a9f4)";
    const text = v ? v["primary-text-color"] : "var(--primary-text-color, #212121)";
    const sub = v ? v["secondary-text-color"] : "var(--secondary-text-color, #727272)";
    return `<button class="theme-card${active}" data-theme-id="${esc(t.id)}">
        <span class="swatch" style="background:${esc(bg)}">
          <span class="sw-card" style="background:${esc(card)};color:${esc(text)}">Aa</span>
          <span class="sw-accent" style="background:${esc(accent)}"></span>
          <span class="sw-line" style="background:${esc(sub)}"></span>
        </span>
        <span class="theme-name">${esc(t.name)}${active ? ' <span class="dot">•</span>' : ""}</span>
      </button>`;
  }

  _renderMessages() {
    if (!this._log) return;
    let html = this._messages
      .map((m) => {
        const body = m.role === "assistant" ? md(m.content) : esc(m.content);
        return `<div class="msg ${esc(m.role)}">${body}</div>`;
      })
      .join("");
    if (this._busy) html += `<div class="msg thinking">Thinking…</div>`;
    if (!html) html = `<div class="empty">Ask your home a question to get started.</div>`;
    this._log.innerHTML = html;
    this._log.scrollTop = this._log.scrollHeight;
    if (this._input) this._input.disabled = this._busy;
    if (this._sendBtn) this._sendBtn.disabled = this._busy;
  }

  _renderPending() {
    if (!this._pendingBox) return;
    this._pendingBox.innerHTML = this._pending
      .map(
        (a) => `<div class="pend">
          <span class="what">Confirm: <b>${esc(a.domain)}.${esc(a.service)}</b> on <b>${esc(a.entity_id)}</b>
            <span class="why">— ${esc(a.reason)}</span></span>
          <button data-act="confirm" data-id="${esc(a.id)}">Approve</button>
          <button class="deny" data-act="deny" data-id="${esc(a.id)}">Deny</button>
        </div>`,
      )
      .join("");
  }

  _renderAutomations() {
    if (!this._autoContent) return;
    if (this._expandedCard && !this._drafts.some((d) => d.id === this._expandedCard))
      this._expandedCard = null;
    const staleCount = this._advisories.length;
    const suggCount = this._suggestions.length;
    const tabBar = `<div class="auto-tabs">
        <button class="subtab${this._autoTab !== "suggestions" ? " active" : ""}" data-auto-tab="automations">Automations${staleCount ? ` <span class="badge warn">${esc(staleCount)} stale</span>` : ""}</button>
        <button class="subtab${this._autoTab === "suggestions" ? " active" : ""}" data-auto-tab="suggestions">Suggestions${suggCount ? ' <span class="dot">•</span>' : ""}</button>
      </div>`;
    let body;
    if (this._autoTab === "suggestions") {
      const suggScanned = this._suggScannedAt
        ? `<span class="badge">scanned ${esc(this._suggScannedAt)}</span>`
        : "";
      let list;
      if (this._suggLoading && !this._suggestions.length)
        list = `<div class="none">Loading suggestions…</div>`;
      else if (this._suggError && !this._suggestions.length)
        list = `<div class="none">${esc(this._suggError)}</div>`;
      else if (this._suggestions.length)
        list = this._suggestions.map((s) => this._suggestionRow(s)).join("");
      else list = `<div class="none">No pattern suggestions.</div>`;
      body = `<div class="auto-body">
          <h3>Suggestions ${suggScanned}
            <button class="ghost" data-sugg-act="rescan">${this._suggLoading ? "Scanning…" : "Rescan"}</button>
          </h3>
          ${list}</div>`;
    } else {
      let cards;
      if (this._draftsLoading && !this._drafts.length)
        cards = `<div class="none">Loading automations…</div>`;
      else if (this._draftsError && !this._drafts.length)
        cards = `<div class="none">${esc(this._draftsError)}</div>`;
      else if (this._drafts.length)
        cards = `<div class="cards">${this._drafts.map((d) => this._card(d)).join("")}</div>`;
      else cards = `<div class="none">No agent automations yet.</div>`;
      const orphans = this._advisories.filter(
        (a) => !this._drafts.some((d) => d.id === a.automation_id),
      );
      const orphanHtml = orphans.length
        ? `<h3>Other stale findings</h3>${orphans
            .map(
              (a) => `<div class="advice">
              <span class="badge warn">${esc(a.kind)}</span>
              <b>${esc(a.alias || a.automation_id)}</b>
              <span class="detail">${esc(a.detail)} — ${esc(a.suggested_action)}</span>
            </div>`,
            )
            .join("")}`
        : "";
      let staleStatus;
      if (this._staleLoading) staleStatus = "Scanning for stale automations…";
      else if (this._staleError) staleStatus = esc(this._staleError);
      else if (this._scannedAt) staleStatus = `Stale scan: ${esc(this._scannedAt)}`;
      else staleStatus = "";
      const scanBar = `<div class="auto-scan">
          <span class="none">${staleStatus}</span>
          <button class="ghost" data-auto-act="rescan-stale">${this._staleLoading ? "Scanning…" : "Rescan stale"}</button>
        </div>`;
      body = `<div class="auto-body">${scanBar}${cards}${orphanHtml}</div>`;
    }
    this._autoContent.innerHTML = tabBar + body;
    this._renderNav();
  }

  _card(d) {
    const stale = this._advisories.filter((a) => a.automation_id === d.id);
    const staleBadge = stale.length ? `<span class="badge warn">stale</span>` : "";
    const status = d.accepted
      ? `<span class="badge ok">accepted</span>`
      : `<span class="badge">draft</span>`;
    const expanded = this._expandedCard === d.id;
    const acceptBtn = d.accepted
      ? ""
      : `<button data-auto-act="accept" data-id="${esc(d.id)}">Accept</button>`;
    const staleDetail = stale.length
      ? stale
          .map(
            (a) => `<div class="advice"><span class="detail">${esc(a.detail)} — ${esc(a.suggested_action)}</span></div>`,
          )
          .join("")
      : "";
    let detail = "";
    if (expanded) {
      const t = this._cardDetailTab;
      const tabBtn = (k, l) =>
        `<button class="dtab${t === k ? " active" : ""}" data-detail-tab="${k}">${l}</button>`;
      let inner;
      if (t === "yaml") inner = this._yamlHtml(this._cardBody);
      else if (t === "history") inner = this._historyHtml(d.id);
      else inner = this._flowHtml(this._cardBody);
      detail = `<div class="detail">
          <div class="detail-tabs">${tabBtn("flow", "Flow")}${tabBtn("yaml", "YAML")}${tabBtn("history", "History")}</div>
          <div class="detail-body">${inner}</div>
        </div>`;
    }
    return `<div class="card${expanded ? " open" : ""}">
        <div class="card-head">
          <div class="card-title"><b>${esc(d.alias)}</b> ${status} ${staleBadge}</div>
          ${d.description ? `<div class="card-desc">${esc(d.description)}</div>` : ""}
        </div>
        ${staleDetail}
        <div class="card-actions">
          ${acceptBtn}
          <button class="discard" data-auto-act="discard" data-id="${esc(d.id)}">Discard</button>
          <button class="ghost" data-auto-act="expand" data-id="${esc(d.id)}">${expanded ? "Hide" : "Details"}</button>
        </div>
        ${detail}
      </div>`;
  }

  _flowHtml(body) {
    if (!body) return `<div class="none">No body available.</div>`;
    const section = (label, raw) => {
      const items = Array.isArray(raw) ? raw : raw != null ? [raw] : [];
      const rows = items.length
        ? items.map((it) => `<li>${esc(this._flowLabel(it))}</li>`).join("")
        : `<li class="muted">none</li>`;
      return `<div class="flow-sec"><div class="flow-h">${esc(label)} (${items.length})</div><ul>${rows}</ul></div>`;
    };
    return (
      section("Triggers", body.triggers != null ? body.triggers : body.trigger) +
      section(
        "Conditions",
        body.conditions != null ? body.conditions : body.condition,
      ) +
      section("Actions", body.actions != null ? body.actions : body.action)
    );
  }

  _flowLabel(it) {
    if (it == null || typeof it !== "object") return String(it);
    const ent = (e) => (Array.isArray(e) ? e.join(", ") : e);
    if (it.platform) return `platform: ${it.platform}${it.entity_id ? ` · ${ent(it.entity_id)}` : ""}`;
    if (it.trigger) return `trigger: ${it.trigger}${it.entity_id ? ` · ${ent(it.entity_id)}` : ""}`;
    if (it.condition) return `condition: ${it.condition}`;
    if (it.service)
      return `service: ${it.service}${it.target && it.target.entity_id ? ` · ${ent(it.target.entity_id)}` : it.entity_id ? ` · ${ent(it.entity_id)}` : ""}`;
    if (it.delay != null) return `delay: ${JSON.stringify(it.delay)}`;
    if (it.choose) return "choose (…)";
    if (it.repeat) return "repeat (…)";
    if (it.wait_template) return "wait_template (…)";
    const keys = Object.keys(it);
    return keys.length ? `${keys[0]}: ${JSON.stringify(it[keys[0]])}` : "(step)";
  }

  _yamlHtml(body) {
    if (!body) return `<div class="none">No body available.</div>`;
    return `<pre class="md-code">${esc(toYaml(body))}</pre>`;
  }

  _historyHtml(automationId) {
    if (!this._cardVersions.length)
      return `<div class="none">No recorded versions.</div>`;
    const rows = this._cardVersions
      .map(
        (v) => `<div class="vrow">
          <b>v${esc(v.version)}</b>
          <span class="badge">${esc(v.action)}</span>
          <span class="meta">${esc(v.timestamp)} · ${esc(v.author)}${v.note ? " · " + esc(v.note) : ""}</span>
        </div>`,
      )
      .join("");
    const options = (selectedLast) =>
      this._cardVersions
        .map((v, i) => {
          const sel = selectedLast
            ? i === this._cardVersions.length - 1
            : i === Math.max(0, this._cardVersions.length - 2);
          return `<option value="${esc(v.version)}"${sel ? " selected" : ""}>v${esc(v.version)}</option>`;
        })
        .join("");
    const diff = this._cardDiff
      ? `<pre class="diff">${this._cardDiff
          .map((l) => `<span class="${diffClass(String(l))}">${esc(l)}</span>`)
          .join("\n")}</pre>`
      : "";
    return `${rows}
      <div class="diffbar">
        <span>Diff</span>
        <select id="cdiff-from">${options(false)}</select>
        <span>→</span>
        <select id="cdiff-to">${options(true)}</select>
        <button class="ghost" data-auto-act="diff" data-id="${esc(automationId)}">Show</button>
      </div>
      ${diff}`;
  }

  _suggestionRow(s) {
    const c = s.candidate || {};
    const pct = Math.round((c.confidence || 0) * 100);
    return `<div class="row">
        <span class="what">
          <span class="badge">${esc(c.kind)}</span>
          ${esc(c.description)}
          <span class="badge ok">${esc(pct)}%</span>
        </span>
        <button data-sugg-act="accept" data-id="${esc(s.signature)}">Accept</button>
        <button class="discard" data-sugg-act="dismiss" data-id="${esc(s.signature)}">Dismiss</button>
      </div>`;
  }
}

customElements.define("digispark-agent-panel", DigiSparkAgentPanel);
