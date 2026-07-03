/*
 * DigiSpark HA Agent — sidebar chat panel with a review drawer.
 * Copyright (c) 2026 DigiSpark Advisory LLC. All rights reserved.
 * Clean-room implementation authored from SPEC.md §9, §11, §12, §13.
 *
 * Vanilla custom element (no build step). Talks to the authenticated Home
 * Assistant WebSocket API: restores the server-side conversation on load
 * (digispark_ha_agent/history), sends turns via digispark_ha_agent/chat with
 * a "thinking" indicator, and offers a review drawer: agent draft inbox with
 * Accept/Discard (digispark_ha_agent/list_drafts, accept_draft,
 * discard_draft), per-automation version history with a diff viewer between
 * any two versions (list_versions, get_version with diff_against, SPEC §12),
 * advisory-only stale findings (stale_advisories, SPEC §13), and a pattern
 * suggestions inbox (list_suggestions, accept_suggestion, dismiss_suggestion,
 * SPEC §11), plus a provider Settings drawer (provider_settings,
 * update_provider_settings, test_connection, list_models, SPEC §8) — the
 * stored API key and extra-header values are never sent to the browser.
 * All text is escaped before rendering.
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
    this._versions = [];
    this._historyFor = null;
    this._diffLines = null;
    this._reviewOpen = false;
    this._busy = false;
    this._restored = false;
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

  async _ws(msg) {
    return this._hass.connection.sendMessagePromise(msg);
  }

  async _restore() {
    try {
      const res = await this._ws({ type: "digispark_ha_agent/history" });
      this._messages = (res && res.messages) || [];
      this._renderMessages();
    } catch (_err) {
      /* no entry yet, or not permitted — start empty */
    }
    this._refreshPending();
    this._refreshReview();
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

  async _refreshReview(rescan) {
    if (!this._hass) return;
    try {
      const res = await this._ws({ type: "digispark_ha_agent/list_drafts" });
      this._drafts = (res && res.drafts) || [];
    } catch (_err) {
      this._drafts = [];
    }
    try {
      const msg = { type: "digispark_ha_agent/stale_advisories" };
      if (rescan) msg.rescan = true;
      const res = await this._ws(msg);
      this._advisories = (res && res.advisories) || [];
      this._scannedAt = (res && res.scanned_at) || "";
    } catch (_err) {
      this._advisories = [];
      this._scannedAt = "";
    }
    await this._refreshSuggestions(false);
    if (this._historyFor) await this._loadHistory(this._historyFor);
    this._renderReview();
  }

  async _refreshSuggestions(rescan) {
    if (!this._hass) return;
    try {
      const msg = { type: "digispark_ha_agent/list_suggestions" };
      if (rescan) msg.rescan = true;
      const res = await this._ws(msg);
      this._suggestions = (res && res.suggestions) || [];
      this._suggScannedAt = (res && res.scanned_at) || "";
    } catch (_err) {
      this._suggestions = [];
      this._suggScannedAt = "";
    }
  }

  async _rescanSuggestions() {
    await this._refreshSuggestions(true);
    this._renderReview();
  }

  async _actOnSuggestion(act, signature) {
    const type =
      act === "sugg-accept"
        ? "digispark_ha_agent/accept_suggestion"
        : "digispark_ha_agent/dismiss_suggestion";
    try {
      const res = await this._ws({ type, signature });
      if (act === "sugg-accept") {
        this._messages.push({
          role: "assistant",
          content: `Suggestion accepted as a disabled draft (${res.automation_id}). Review and enable it under Agent automations.`,
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

  async _loadHistory(automationId) {
    try {
      const res = await this._ws({
        type: "digispark_ha_agent/list_versions",
        automation_id: automationId,
      });
      this._versions = (res && res.versions) || [];
    } catch (_err) {
      this._versions = [];
    }
  }

  async _toggleHistory(automationId) {
    if (this._historyFor === automationId) {
      this._historyFor = null;
      this._versions = [];
      this._diffLines = null;
    } else {
      this._historyFor = automationId;
      this._diffLines = null;
      await this._loadHistory(automationId);
    }
    this._renderReview();
  }

  async _showDiff(automationId) {
    const from = parseInt(this.shadowRoot.getElementById("diff-from").value, 10);
    const to = parseInt(this.shadowRoot.getElementById("diff-to").value, 10);
    try {
      const res = await this._ws({
        type: "digispark_ha_agent/get_version",
        automation_id: automationId,
        version: to,
        diff_against: from,
      });
      this._diffLines = (res && res.diff) || [];
      if (!this._diffLines.length)
        this._diffLines = ["(the two versions are identical)"];
    } catch (err) {
      this._diffLines = [(err && err.message) || "diff failed"];
    }
    this._renderReview();
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
    if (this._historyFor === automationId && act === "discard") {
      this._historyFor = null;
      this._versions = [];
      this._diffLines = null;
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
    try {
      const res = await this._ws({
        type: "digispark_ha_agent/chat",
        message: text,
      });
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
  }

  _render() {
    this.shadowRoot.innerHTML = `
      <style>
        :host { display: block; height: 100%; background: var(--primary-background-color, #fafafa); }
        .wrap { display: flex; flex-direction: column; height: 100%; box-sizing: border-box; }
        .log { flex: 1; overflow-y: auto; padding: 16px; display: flex; flex-direction: column; gap: 8px; }
        .msg { max-width: 80%; padding: 8px 12px; border-radius: 12px; white-space: pre-wrap; word-wrap: break-word; line-height: 1.4; }
        .msg.user { align-self: flex-end; background: var(--primary-color, #03a9f4); color: var(--text-primary-color, #fff); }
        .msg.assistant { align-self: flex-start; background: var(--card-background-color, #fff); color: var(--primary-text-color, #212121); border: 1px solid var(--divider-color, #e0e0e0); }
        .msg.error { align-self: flex-start; background: var(--error-color, #db4437); color: #fff; }
        .msg.thinking { align-self: flex-start; color: var(--secondary-text-color, #727272); font-style: italic; }
        .bar { display: flex; gap: 8px; padding: 12px 16px; border-top: 1px solid var(--divider-color, #e0e0e0); background: var(--card-background-color, #fff); }
        input { flex: 1; padding: 10px 12px; border-radius: 8px; border: 1px solid var(--divider-color, #e0e0e0); background: var(--primary-background-color, #fafafa); color: var(--primary-text-color, #212121); font-size: 14px; }
        input:disabled { opacity: 0.6; }
        button { padding: 0 16px; border: none; border-radius: 8px; background: var(--primary-color, #03a9f4); color: var(--text-primary-color, #fff); cursor: pointer; font-size: 14px; }
        button:disabled { opacity: 0.6; cursor: default; }
        .empty { color: var(--secondary-text-color, #727272); text-align: center; margin-top: 24px; }
        .pending { border-top: 1px solid var(--divider-color, #e0e0e0); background: var(--card-background-color, #fff); }
        .pend { display: flex; align-items: center; gap: 8px; padding: 8px 16px; font-size: 13px; color: var(--primary-text-color, #212121); }
        .pend .what { flex: 1; }
        .pend .why { color: var(--secondary-text-color, #727272); }
        .pend button { padding: 4px 12px; font-size: 13px; }
        .pend button.deny { background: var(--error-color, #db4437); }
        .review { border-top: 1px solid var(--divider-color, #e0e0e0); background: var(--card-background-color, #fff); max-height: 45%; overflow-y: auto; font-size: 13px; color: var(--primary-text-color, #212121); }
        .review h3 { margin: 8px 16px 4px; font-size: 12px; text-transform: uppercase; letter-spacing: 0.05em; color: var(--secondary-text-color, #727272); display: flex; align-items: center; gap: 8px; }
        .review h3 button { padding: 2px 10px; font-size: 12px; }
        .row { display: flex; align-items: center; gap: 8px; padding: 6px 16px; }
        .row .what { flex: 1; }
        .row button { padding: 4px 12px; font-size: 13px; }
        .row button.discard { background: var(--error-color, #db4437); }
        .row button.ghost { background: transparent; color: var(--primary-color, #03a9f4); border: 1px solid var(--divider-color, #e0e0e0); }
        .badge { display: inline-block; padding: 1px 8px; border-radius: 10px; font-size: 11px; background: var(--divider-color, #e0e0e0); color: var(--primary-text-color, #212121); }
        .badge.ok { background: #c8e6c9; }
        .badge.warn { background: #ffe0b2; }
        .history { margin: 0 16px 8px; padding: 8px; border: 1px solid var(--divider-color, #e0e0e0); border-radius: 8px; }
        .vrow { display: flex; gap: 8px; padding: 2px 0; color: var(--primary-text-color, #212121); }
        .vrow .meta { color: var(--secondary-text-color, #727272); }
        .diffbar { display: flex; align-items: center; gap: 8px; margin-top: 6px; }
        .diffbar select { padding: 4px; border-radius: 6px; border: 1px solid var(--divider-color, #e0e0e0); background: var(--primary-background-color, #fafafa); color: var(--primary-text-color, #212121); }
        pre.diff { margin: 6px 0 0; padding: 8px; border-radius: 6px; background: var(--primary-background-color, #fafafa); overflow-x: auto; font-size: 12px; line-height: 1.35; }
        pre.diff .add { color: #2e7d32; }
        pre.diff .del { color: #c62828; }
        pre.diff .hunk { color: var(--secondary-text-color, #727272); }
        .advice { padding: 6px 16px; }
        .advice .detail { color: var(--secondary-text-color, #727272); }
        .none { padding: 4px 16px 8px; color: var(--secondary-text-color, #727272); }
        .field { display: flex; flex-direction: column; gap: 2px; padding: 6px 16px; }
        .field label { font-size: 12px; color: var(--secondary-text-color, #727272); }
        .field input, .field select, .field textarea { padding: 8px 10px; border-radius: 8px; border: 1px solid var(--divider-color, #e0e0e0); background: var(--primary-background-color, #fafafa); color: var(--primary-text-color, #212121); font-size: 13px; }
        .field input[type="checkbox"] { flex: none; padding: 0; }
        .result-ok { color: #2e7d32; }
        .result-bad { color: #c62828; }
      </style>
      <div class="wrap">
        <div class="log" id="log"></div>
        <div class="review" id="review" hidden></div>
        <div class="review" id="settings" hidden></div>
        <div class="pending" id="pending"></div>
        <form class="bar" id="form">
          <input id="input" type="text" placeholder="Ask your home…" autocomplete="off" />
          <button id="settings-toggle" type="button">Settings</button>
          <button id="review-toggle" type="button">Review</button>
          <button id="send" type="submit">Send</button>
        </form>
      </div>`;
    this._log = this.shadowRoot.getElementById("log");
    this._pendingBox = this.shadowRoot.getElementById("pending");
    this._reviewBox = this.shadowRoot.getElementById("review");
    this._input = this.shadowRoot.getElementById("input");
    this._sendBtn = this.shadowRoot.getElementById("send");
    this._reviewToggle = this.shadowRoot.getElementById("review-toggle");
    this._pendingBox.addEventListener("click", (ev) => {
      const btn = ev.target.closest("button[data-act]");
      if (btn) this._actOnPending(btn.dataset.act, btn.dataset.id);
    });
    this._reviewBox.addEventListener("click", (ev) => {
      const btn = ev.target.closest("button[data-review]");
      if (!btn) return;
      const act = btn.dataset.review;
      const id = btn.dataset.id;
      if (act === "accept" || act === "discard") this._actOnDraft(act, id);
      else if (act === "history") this._toggleHistory(id);
      else if (act === "diff") this._showDiff(id);
      else if (act === "rescan") this._refreshReview(true);
      else if (act === "sugg-accept" || act === "sugg-dismiss")
        this._actOnSuggestion(act, id);
      else if (act === "sugg-rescan") this._rescanSuggestions();
    });
    this._settingsBox = this.shadowRoot.getElementById("settings");
    this._settingsToggle = this.shadowRoot.getElementById("settings-toggle");
    this._settingsBox.addEventListener("click", (ev) => {
      const btn = ev.target.closest("button[data-settings]");
      if (!btn) return;
      const act = btn.dataset.settings;
      if (act === "save") this._saveSettings();
      else if (act === "test") this._testSettingsConnection();
      else if (act === "fetch-models") this._fetchSettingsModels();
    });
    this._settingsToggle.addEventListener("click", () => {
      this._settingsOpen = !this._settingsOpen;
      this._settingsBox.hidden = !this._settingsOpen;
      if (this._settingsOpen) {
        this._reviewOpen = false;
        this._reviewBox.hidden = true;
        this._loadSettings();
      }
    });
    this._reviewToggle.addEventListener("click", () => {
      this._reviewOpen = !this._reviewOpen;
      this._reviewBox.hidden = !this._reviewOpen;
      if (this._reviewOpen) {
        this._settingsOpen = false;
        this._settingsBox.hidden = true;
        this._refreshReview();
      }
    });
    this.shadowRoot.getElementById("form").addEventListener("submit", (ev) => {
      ev.preventDefault();
      const value = this._input.value.trim();
      this._input.value = "";
      this._send(value);
    });
    this._renderMessages();
  }

  _renderMessages() {
    if (!this._log) return;
    let html = this._messages
      .map((m) => `<div class="msg ${esc(m.role)}">${esc(m.content)}</div>`)
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

  _renderReview() {
    if (!this._reviewBox) return;
    const drafts = this._drafts.length
      ? this._drafts.map((d) => this._draftRow(d)).join("")
      : `<div class="none">No agent automations yet.</div>`;
    const advisories = this._advisories.length
      ? this._advisories
          .map(
            (a) => `<div class="advice">
              <span class="badge warn">${esc(a.kind)}</span>
              <b>${esc(a.alias || a.automation_id)}</b>
              <span class="detail">${esc(a.detail)} — ${esc(a.suggested_action)}</span>
            </div>`,
          )
          .join("")
      : `<div class="none">No stale automations found.</div>`;
    const suggestions = this._suggestions.length
      ? this._suggestions.map((s) => this._suggestionRow(s)).join("")
      : `<div class="none">No pattern suggestions.</div>`;
    const scanned = this._scannedAt
      ? `<span class="badge">scanned ${esc(this._scannedAt)}</span>`
      : "";
    const suggScanned = this._suggScannedAt
      ? `<span class="badge">scanned ${esc(this._suggScannedAt)}</span>`
      : "";
    this._reviewBox.innerHTML = `
      <h3>Suggestions ${suggScanned}
        <button class="ghost" data-review="sugg-rescan">Rescan</button>
      </h3>
      ${suggestions}
      <h3>Agent automations</h3>
      ${drafts}
      <h3>Stale findings ${scanned}
        <button class="ghost" data-review="rescan">Rescan</button>
      </h3>
      ${advisories}`;
    this._reviewToggle.textContent =
      this._drafts.some((d) => !d.accepted) || this._suggestions.length
        ? "Review •"
        : "Review";
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
        <button data-review="sugg-accept" data-id="${esc(s.signature)}">Accept</button>
        <button class="discard" data-review="sugg-dismiss" data-id="${esc(s.signature)}">Dismiss</button>
      </div>`;
  }

  _draftRow(d) {
    const status = d.accepted
      ? `<span class="badge ok">accepted</span>`
      : `<span class="badge">draft</span>`;
    const actions = d.accepted
      ? ""
      : `<button data-review="accept" data-id="${esc(d.id)}">Accept</button>`;
    const open = this._historyFor === d.id;
    let history = "";
    if (open) history = this._historyBlock(d.id);
    return `<div class="row">
        <span class="what"><b>${esc(d.alias)}</b> ${status}</span>
        ${actions}
        <button class="discard" data-review="discard" data-id="${esc(d.id)}">Discard</button>
        <button class="ghost" data-review="history" data-id="${esc(d.id)}">${open ? "Hide" : "History"}</button>
      </div>${history}`;
  }

  _historyBlock(automationId) {
    if (!this._versions.length)
      return `<div class="history none">No recorded versions.</div>`;
    const rows = this._versions
      .map(
        (v) => `<div class="vrow">
          <b>v${esc(v.version)}</b>
          <span class="badge">${esc(v.action)}</span>
          <span class="meta">${esc(v.timestamp)} · ${esc(v.author)}${v.note ? " · " + esc(v.note) : ""}</span>
        </div>`,
      )
      .join("");
    const options = (selectedLast) =>
      this._versions
        .map((v, i) => {
          const sel = selectedLast
            ? i === this._versions.length - 1
            : i === Math.max(0, this._versions.length - 2);
          return `<option value="${esc(v.version)}"${sel ? " selected" : ""}>v${esc(v.version)}</option>`;
        })
        .join("");
    const diff = this._diffLines
      ? `<pre class="diff">${this._diffLines
          .map((l) => `<span class="${diffClass(String(l))}">${esc(l)}</span>`)
          .join("\n")}</pre>`
      : "";
    return `<div class="history">
        ${rows}
        <div class="diffbar">
          <span>Diff</span>
          <select id="diff-from">${options(false)}</select>
          <span>→</span>
          <select id="diff-to">${options(true)}</select>
          <button class="ghost" data-review="diff" data-id="${esc(automationId)}">Show</button>
        </div>
        ${diff}
      </div>`;
  }
}

customElements.define("digispark-agent-panel", DigiSparkAgentPanel);
