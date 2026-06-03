// .ai/dashboard/app/settings.js -- Settings tab (improver + workflow
// updates) plus the auto-select form + warning banner that live under
// Models & dispatch. Loads current values from `/api/settings` on init
// and whenever either tab is opened.

(function () {
  var AS_PHASES_AVAILABLE = ["execute", "review", "rescue"];

  var loadedOnce = false;
  // Busy guard for the reload button — prevents two concurrent loadAllSettings
  // calls from racing each other and rendering out of order. Mirrors the
  // _jobsLoadInFlight pattern in jobs.js.
  var _isLoading = false;
  // Last-known version token from /api/settings. When the server returns a
  // `version` field (mtime, hash, or monotonic counter — server may add it
  // opportunistically), saves echo it back as `_if_match` so the server can
  // refuse a stale write. Until the server opts in, this is a no-op safeguard
  // on the client side — when the field is null we just don't send it and
  // behave as last-write-wins, the previous semantics.
  var _settingsVersion = null;
  // Wall-clock at last successful loadAllSettings — savePhaseRow refreshes
  // when it goes stale so a long-open settings tab doesn't try to save
  // with a now-rejected _if_match.
  var _settingsLoadedAt = 0;

  // ---------- HTTP helpers ----------
  async function getJson(url) {
    var r = await fetch(url, { cache: "no-store" });
    if (!r.ok) throw new Error("HTTP " + r.status);
    return r.json();
  }
  async function postJson(url, body) {
    var r = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
    var data = null;
    try {
      data = await r.json();
    } catch (e) {
      // Server returned non-JSON (truncated body, proxy HTML, etc.). Don't change
      // control flow — caller still gets {} and treats it as success on r.ok —
      // but surface the parse failure for debugging.
      console.warn("[settings] postJson response body parse failed:", e.message);
    }
    if (!r.ok) {
      throw new Error((data && data.error) ? data.error : ("HTTP " + r.status));
    }
    return data || {};
  }

  // ---------- DOM helpers ----------
  function $q(sel) { return document.querySelector(sel); }
  // Save / error feedback routes through the global toast stack defined in
  // core.js so settings actions surface the same way as memory, decisions,
  // jobs and terminals. The `settings-meta` channel is the only exception:
  // it's a persistent toolbar timestamp ("Loaded HH:MM:SS"), not a transient
  // notification, so it stays inline.
  function setMsg(id, text, tone) {
    if (id === "settings-meta") {
      var el = $q("#" + id);
      if (!el) return;
      el.textContent = text || "";
      el.classList.remove("ok", "err");
      if (tone === "good") el.classList.add("ok");
      else if (tone === "bad") el.classList.add("err");
      return;
    }
    var kind = tone === "good" ? "ok" : tone === "bad" ? "err" : "";
    if (typeof window.setMsg === "function") {
      // window.setMsg bypasses the local shadowing introduced by this IIFE.
      window.setMsg("#" + id, kind, text || "");
    }
  }
  async function withBusy(btn, fn, busyLabel) {
    if (!btn) return fn();
    var label = btn.textContent;
    btn.disabled = true;
    btn.dataset.prevLabel = label;
    // Optional override so non-save callers (Accept / Reject / Suggest)
    // get an accurate progress label instead of the misleading "Saving…".
    btn.textContent = busyLabel || "Saving…";
    try {
      return await fn();
    } finally {
      // The button may have been detached from the DOM while we were
      // awaiting (e.g. a parallel rerender swapped its container). Skip
      // the restore in that case — writing to a stale node leaks references
      // and confuses the GC; the new node already has the correct state.
      if (btn.isConnected) {
        btn.disabled = false;
        btn.textContent = btn.dataset.prevLabel || label;
      }
    }
  }

  // ---------- Auto-improver ----------
  function fillImprover(cfg) {
    cfg = cfg || {};
    var root = $q("#imp-enabled");
    if (!root) return;
    root.checked = !!cfg.enabled;
    $q("#imp-small-change").value = numOrEmpty(cfg.small_change_max_lines);
    $q("#imp-min-interval").value = numOrEmpty(cfg.min_interval_seconds);
    $q("#imp-timeout").value      = numOrEmpty(cfg.timeout_seconds);
    $q("#imp-revert").value       = numOrEmpty(cfg.revert_after_n_uses);

    var warn = $q("#improver-env-warning");
    var lock = !!cfg.disabled_by_env;
    if (warn) {
      if (lock) {
        warn.textContent = "AI_WORKFLOW_DISABLE_IMPROVER is set — the improver is forced off regardless of the YAML value below. Unset the env var to re-enable.";
        warn.classList.add("is-active");
      } else {
        warn.classList.remove("is-active");
        warn.textContent = "";
      }
    }
    ["imp-enabled", "imp-small-change", "imp-min-interval", "imp-timeout", "imp-revert"].forEach(function (id) {
      var el = $q("#" + id);
      if (el) el.disabled = lock;
    });
    var saveBtn = $q("#btn-imp-save");
    if (saveBtn) saveBtn.disabled = lock;
  }

  function numOrEmpty(v) {
    if (v === null || v === undefined || v === "") return "";
    return v;
  }

  async function saveImprover() {
    var btn = $q("#btn-imp-save");
    setMsg("imp-msg", "");
    var raw = {
      small_change_max_lines: $q("#imp-small-change").value,
      min_interval_seconds:   $q("#imp-min-interval").value,
      timeout_seconds:        $q("#imp-timeout").value,
      revert_after_n_uses:    $q("#imp-revert").value,
    };
    // Client-side numeric validation: each field must be a positive integer
    // within a reasonable upper bound. Catches "abc", "-5", "1e9", "" before
    // they reach the server. First bad field wins the error toast.
    var bounds = {
      small_change_max_lines: 100,
      min_interval_seconds:   86400,
      timeout_seconds:        3600,
      revert_after_n_uses:    100,
    };
    var parsed = {};
    var fields = ["small_change_max_lines", "min_interval_seconds", "timeout_seconds", "revert_after_n_uses"];
    for (var i = 0; i < fields.length; i++) {
      var k = fields[i];
      var s = String(raw[k] == null ? "" : raw[k]).trim();
      if (s === "") {
        setMsg("imp-msg", k + ": invalid value", "bad");
        return;
      }
      // Reject non-integer strings (e.g. "1e9", "1.5", "abc", "5x").
      if (!/^-?\d+$/.test(s)) {
        setMsg("imp-msg", k + ": invalid value", "bad");
        return;
      }
      var n = parseInt(s, 10);
      if (isNaN(n) || n <= 0 || n > bounds[k]) {
        setMsg("imp-msg", k + ": invalid value", "bad");
        return;
      }
      parsed[k] = n;
    }
    var body = {
      enabled: $q("#imp-enabled").checked,
      small_change_max_lines: parsed.small_change_max_lines,
      min_interval_seconds:   parsed.min_interval_seconds,
      timeout_seconds:        parsed.timeout_seconds,
      revert_after_n_uses:    parsed.revert_after_n_uses,
    };
    // Optimistic concurrency: include the last-seen version if we have one.
    // Server ignores it today; the moment it grows version-aware this
    // becomes the lost-edit guard with no further client change.
    if (_settingsVersion != null) body._if_match = _settingsVersion;
    try {
      await withBusy(btn, function () { return postJson("/api/settings/improver", body); });
      setMsg("imp-msg", "Saved to .ai/models.yaml", "good");
    } catch (e) {
      setMsg("imp-msg", e.message || "save failed", "bad");
    }
  }

  // ---------- Auto-select ----------
  function fillAutoSelect(cfg) {
    cfg = cfg || {};
    var root = $q("#as-enabled");
    if (!root) return;
    root.checked = !!cfg.enabled;
    var budget = $q("#as-budget");
    if (budget) budget.value = cfg.token_budget || "medium";
    var wrap = $q("#as-phases");
    if (!wrap) return;
    wrap.innerHTML = "";
    // Coerce phases to an array. If the server returns a CSV string ("execute,review"),
    // a plain string `.indexOf(ph)` would behave as a SUBSTRING match, so e.g.
    // "reviewer".indexOf("review") === 0 would falsely flag "review" as selected.
    var current = Array.isArray(cfg.phases)
      ? cfg.phases
      : (typeof cfg.phases === "string"
          ? cfg.phases.split(",").map(function (s) { return s.trim(); })
          : []);
    AS_PHASES_AVAILABLE.forEach(function (ph) {
      var id = "as-phase-" + ph;
      var lbl = document.createElement("label");
      var cb = document.createElement("input");
      cb.type = "checkbox";
      cb.id = id;
      cb.dataset.phase = ph;
      cb.checked = current.indexOf(ph) >= 0;
      lbl.appendChild(cb);
      lbl.appendChild(document.createTextNode(" " + ph));
      wrap.appendChild(lbl);
    });
  }

  async function saveAutoSelect() {
    var btn = $q("#btn-as-save");
    setMsg("as-msg", "");
    var phases = AS_PHASES_AVAILABLE.filter(function (ph) {
      var cb = $q("#as-phase-" + ph);
      return cb && cb.checked;
    });
    var body = {
      enabled: $q("#as-enabled").checked,
      token_budget: $q("#as-budget").value,
      phases: phases,
    };
    try {
      // Snapshot the version we read on the last load to detect concurrent
      // edits (a second tab, an external YAML edit). If the server's current
      // version doesn't match, refuse to clobber and prompt a reload.
      var ifMatch = _settingsVersion;
      if (ifMatch != null) body._if_match = ifMatch;
      await withBusy(btn, function () { return postJson("/api/settings/auto_select", body); });
      setMsg("as-msg", "Saved to .ai/models.yaml", "good");
      // Surgical refresh: just re-render the dependent banner + per-phase
      // table without re-triggering the skeleton-flash path. The improver
      // section is independent of auto-select so it doesn't need a redraw,
      // and the auto-select checkboxes already reflect the just-saved state.
      await refreshPhasesSection();
    } catch (e) {
      setMsg("as-msg", e.message || "save failed", "bad");
    }
  }

  // Re-fetch /api/settings without triggering showLoadingState() so the user
  // doesn't see a skeleton flicker on every save. Only repaints the
  // auto-select-warning banner — that's the one region that depends on the
  // just-changed auto-select state (the table itself is rendered by core.js
  // renderModels and reloads via loadAll on phase-row save).
  // Canonical phases the orchestrator dispatches plus the `session`
  // pseudo-phase. data-phase is user-mutable via devtools, so savePhaseRow
  // validates against this allow-list before any POST reaches the server.
  var ALL_PHASES = ["session", "plan", "execute", "review", "rescue", "maintenance", "bootstrap"];

  // Surgical re-fetch of /api/settings that repaints the phase routing table
  // IN PLACE — no showLoadingState(), so the just-saved row never flashes the
  // skeleton. Used by save handlers as the optimistic-UI follow-up.
  async function refreshPhasesSection() {
    try {
      var data = await getJson("/api/settings");
      _settingsVersion = data.version != null ? data.version : _settingsVersion;
      renderAutoSelectBanner(data.auto_select || {});
      renderPhasesTable(data.phases || {});
    } catch (e) {
      // Non-fatal: the previous table/banner stays as-is — strictly better
      // than a flash-clear followed by a load failure.
      console.warn("[settings] refreshPhasesSection failed:", e.message);
    }
  }

  // Repaint #models-table from a phases map without disturbing the skeleton
  // state machine. Row shape matches core.js renderModels() so the delegated
  // Edit/Save click handler wired there keeps working after an in-place
  // repaint. Every interpolated value is escaped; the tool name additionally
  // feeds a CSS class, so it is restricted to safe class-identifier chars.
  function renderPhasesTable(phases) {
    var table = $q("#models-table");
    if (!table) return;
    phases = phases || {};
    var rows = ALL_PHASES.map(function (ph) {
      var p = phases[ph] || {};
      var showMode = ph !== "session";
      // Case-insensitive: a YAML that drifted ("Low", "HIGH") still maps to a
      // canonical lowercase value for display + dropdown matching.
      var current = String(p.reasoning_effort || "").toLowerCase();
      var effort = showMode ? (current || "default") : "—";
      var timeout = showMode
        ? (p.timeout_seconds ? escHtml(String(p.timeout_seconds)) + "s" : "default")
        : "—";
      // Restrict to CSS class-identifier chars before building the modifier
      // class — a tampered tool string must not break out of the attribute.
      var toolClass = String(p.tool || "none").replace(/[^a-z0-9_-]/gi, "");
      return '<tr data-phase="' + escHtml(ph) + '">'
        + '<td class="mono"><strong>' + escHtml(ph) + '</strong></td>'
        + '<td data-field="tool"><span class="pill ph-tool-' + toolClass + '">'
        + escHtml(p.tool || "—") + '</span></td>'
        + '<td class="mono" data-field="model">' + escHtml(p.model || "—") + '</td>'
        + '<td data-field="mode">' + (showMode ? escHtml(p.mode || "auto") : "—") + '</td>'
        + '<td class="mono" data-field="effort">' + escHtml(effort) + '</td>'
        + '<td class="mono" data-field="timeout">' + escHtml(timeout) + '</td>'
        + '<td style="text-align:right"><button class="btn secondary" data-edit-phase="'
        + escHtml(ph) + '">Edit</button></td>'
        + '</tr>';
    }).join("");
    delete table.dataset.skeletoned;
    table.innerHTML = '<table>'
      + '<thead><tr><th>Phase</th><th>Tool</th><th>Model</th><th>Override</th>'
      + '<th>Effort</th><th>Timeout</th><th></th></tr></thead>'
      + '<tbody>' + rows + '</tbody></table>';
  }

  // Per-row save for the phase routing table. `tr` is the edited row element;
  // its dataset.phase is the (devtools-mutable) phase key. We validate the
  // phase against ALL_PHASES and the timeout input's HTML5 validity before
  // any POST, and echo the version token as _if_match for concurrency control.
  async function savePhaseRow(tr) {
    if (!tr || !tr.dataset) return;
    var phase = tr.dataset.phase;
    if (!phase || ALL_PHASES.indexOf(phase) < 0) {
      setMsg("models-phase-msg", "invalid phase: " + String(phase), "bad");
      return;
    }
    var toolEl = tr.querySelector("#pe-tool");
    var modelEl = tr.querySelector("#pe-model");
    var payload = {
      phase: phase,
      tool: toolEl ? toolEl.value : "",
      model: modelEl ? modelEl.value.trim() : "",
    };
    if (phase !== "session") {
      var modeEl = tr.querySelector("#pe-mode");
      var reffEl = tr.querySelector("#pe-reff");
      if (modeEl) payload.mode = modeEl.value || "";
      if (reffEl) payload.reasoning_effort = reffEl.value || "";
      var tInput = tr.querySelector("#pe-timeout");
      if (tInput) {
        // HTML5 min/max only validate on form submit, not type=button. Reject
        // out-of-range timeouts here so the server never sees a bad value.
        if (!tInput.validity.valid) {
          setMsg("models-phase-msg", "Timeout: " + tInput.validationMessage, "bad");
          return;
        }
        payload.timeout_seconds = tInput.value;
      }
    }
    if (!payload.model) {
      setMsg("models-phase-msg", "Model is required", "bad");
      return;
    }
    if (_settingsVersion != null) payload._if_match = _settingsVersion;
    try {
      await postJson("/api/models/phase", payload);
      setMsg("models-phase-msg", "Saved " + phase, "good");
      // Optimistic UI: surgical repaint, no skeleton flash.
      await refreshPhasesSection();
    } catch (e) {
      setMsg("models-phase-msg", "Save failed: " + e.message, "bad");
    }
  }

  // ---------- Auto-select banner above Phase routing ----------
  // The phases table itself is rendered by core.js renderModels() — this
  // function only paints the auto-select-on banner that sits above it.
  // Per-row Save is handled by core.js savePhaseRow() (one editable table,
  // one save path).
  function renderAutoSelectBanner(autoSelect) {
    autoSelect = autoSelect || {};
    var autoOn = !!autoSelect.enabled;
    var autoPhases = autoOn ? (autoSelect.phases || []) : [];
    var warn = $q("#phases-auto-warning");
    if (!warn) return;
    if (autoOn && autoPhases.length) {
      warn.textContent = "Auto-select is ON for phases: " + autoPhases.join(", ")
        + ". The planner picks tool / model / reasoning_effort per task for these phases; the values in the table below are the fallback used when auto-select has no match.";
      warn.classList.add("is-active");
    } else {
      warn.classList.remove("is-active");
      warn.textContent = "";
    }
  }

  // ---------- Workflow updates ----------
  // Wired against POST /api/workflow/{check,update}. Both endpoints clone the
  // template upstream into a temp dir server-side; the JS just orchestrates
  // status text, the output panel, and the restart-required banner.
  function setWorkflowStatus(text, tone) {
    var el = $q("#workflow-status");
    if (!el) return;
    el.textContent = text;
    el.style.color = tone === "good" ? "var(--good)"
      : tone === "bad" ? "var(--bad)"
      : tone === "warn" ? "var(--warn)"
      : "var(--text-dim)";
  }
  function setWorkflowOutput(text) {
    var el = $q("#workflow-output");
    if (!el) return;
    if (!text) { el.style.display = "none"; el.textContent = ""; return; }
    el.style.display = "block";
    el.textContent = text;
  }
  // Single source of truth for the workflow-update widget. Every UI change
  // flows through `renderWorkflowButtons(_workflowState)`; never poke
  // `.disabled` directly from event handlers. The previous code split
  // ownership between `setWorkflowBusy` (transient busy flag) and ad-hoc
  // writes in `workflowCheck` / `workflowUpdate` (`has_updates` derivation),
  // making it impossible to reason about the button's actual state at any
  // given moment.
  var _workflowState = {
    busy: false,        // a check or update is currently running
    hasUpdates: false,  // last successful check said there are pending updates
    checked: false,     // we've seen a successful response at least once
  };
  function renderWorkflowButtons() {
    var c = $q("#btn-workflow-check");
    var p = $q("#btn-workflow-update");
    if (c) c.disabled = _workflowState.busy;
    if (p) {
      // Update button is enabled iff: we've checked, there are updates,
      // and nothing else is in flight. No other site is allowed to flip
      // this flag — workflowCheck/workflowUpdate must mutate the state
      // object and call renderWorkflowButtons().
      p.disabled = _workflowState.busy
        || !_workflowState.checked
        || !_workflowState.hasUpdates;
    }
  }
  function setWorkflowBusy(busy) {
    _workflowState.busy = !!busy;
    renderWorkflowButtons();
  }
  function setRestartWarning(visible) {
    var el = $q("#workflow-restart-warning");
    if (el) el.style.display = visible ? "block" : "none";
  }
  // Defense in depth: cover the full OWASP-recommended set so this helper is
  // safe in attribute position too (today's callers all use it between tags,
  // but future callers shouldn't have to think about context). Single and
  // double quotes plus `<`, `>`, `&` cover both text and unquoted-attr sinks.
  function escHtml(s) {
    return String(s == null ? "" : s).replace(/[<>&'"]/g, function (ch) {
      return {
        "<": "&lt;",
        ">": "&gt;",
        "&": "&amp;",
        "'": "&#39;",
        '"': "&quot;",
      }[ch];
    });
  }
  function showWorkflowLog(commits) {
    var wrap = $q("#workflow-log-wrap");
    var box = $q("#workflow-log");
    if (!wrap || !box) return;
    if (!commits || !commits.length) { wrap.style.display = "none"; box.innerHTML = ""; return; }
    box.innerHTML = commits.map(function (c) {
      return '<div><span class="sha">' + escHtml((c.sha || "").substring(0, 7))
        + '</span> · ' + escHtml(c.subject || "") + '</div>';
    }).join("");
    wrap.style.display = "block";
  }

  async function workflowCheck() {
    setWorkflowBusy(true);
    setWorkflowStatus("Cloning template upstream…");
    setWorkflowOutput("");
    setRestartWarning(false);
    showWorkflowLog([]);
    try {
      var data = await postJson("/api/workflow/check", {});
      if (data.success === false) {
        setWorkflowStatus(data.message || data.error || "Check failed", "bad");
        if (data.output) setWorkflowOutput(data.output);
        // Reset to "no updates known" on failure; renderWorkflowButtons in
        // the finally block will keep the update button disabled.
        _workflowState.hasUpdates = false;
        _workflowState.checked = false;
        return;
      }
      var shortUp = (data.upstream_sha || "").substring(0, 7) || "?";
      var shortCur = data.current_sha ? data.current_sha.substring(0, 7) : "none";
      var line = "Upstream " + shortUp + " · installed " + shortCur;
      setWorkflowStatus(line + " — " + (data.message || ""),
                        data.has_updates ? "warn" : "good");
      // Single source of truth: mutate _workflowState and let
      // renderWorkflowButtons() derive .disabled. The visual update is
      // still immediate (renderWorkflowButtons runs synchronously) without
      // duplicating the has_updates -> .disabled mapping at the call site.
      _workflowState.checked = true;
      _workflowState.hasUpdates = !!data.has_updates;
      renderWorkflowButtons();
      showWorkflowLog(data.commits || []);
    } catch (e) {
      setWorkflowStatus("Network error: " + e.message, "bad");
      _workflowState.checked = false;
      _workflowState.hasUpdates = false;
    } finally {
      setWorkflowBusy(false);
    }
  }

  async function workflowUpdate() {
    setWorkflowBusy(true);
    setWorkflowStatus("Applying update — cloning template and running update-workflow.sh…");
    setWorkflowOutput("");
    setRestartWarning(false);
    try {
      var data = await postJson("/api/workflow/update", {});
      setWorkflowStatus(data.message || (data.success ? "Workflow updated." : "Update failed."),
                        data.success ? "good" : "bad");
      if (data.output) setWorkflowOutput(data.output);
      if (data.success) {
        showWorkflowLog([]);
        if (data.restart_dashboard) setRestartWarning(true);
        // Successful apply: there are no longer pending updates. Force the
        // state to reflect that until the next workflowCheck() runs.
        _workflowState.hasUpdates = false;
        // Sync the main.js startup-check cache too — without this the
        // 6h cache would still report has_updates=true and the banner
        // would re-appear on the next page load even though we just
        // applied the update.
        try {
          var raw = localStorage.getItem("dash.updateCheck.v2");
          if (raw) {
            var cached = JSON.parse(raw);
            if (cached && cached.data) {
              cached.data.has_updates = false;
              if (data.upstream_sha) cached.data.current_sha = data.upstream_sha;
              localStorage.setItem("dash.updateCheck.v2", JSON.stringify(cached));
            }
          }
        } catch (_) { /* ignore cache write failure */ }
      }
    } catch (e) {
      setWorkflowStatus("Network error: " + e.message, "bad");
    } finally {
      // Single source of truth: clear busy and let renderWorkflowButtons
      // derive both buttons' enabled state from _workflowState.
      setWorkflowBusy(false);
    }
  }

  // ---------- Loading & coordination ----------
  function showLoadingState() {
    setMsg("settings-meta", "Loading…");
  }

  async function loadAllSettings() {
    showLoadingState();
    try {
      var data = await getJson("/api/settings");
      // Capture the version token (if any) so subsequent saves can include
      // it as `_if_match` to detect concurrent edits.
      _settingsVersion = data.version != null ? data.version : null;
      _settingsLoadedAt = Date.now();
      fillImprover(data.improver || {});
      fillAutoSelect(data.auto_select || {});
      renderAutoSelectBanner(data.auto_select || {});
      setMsg("settings-meta", "Loaded " + new Date().toLocaleTimeString(), "good");
      loadedOnce = true;
    } catch (e) {
      setMsg("settings-meta", "Failed to load: " + e.message, "bad");
      // Reset version token so a retry-then-save can't ship a stale
      // _if_match. Without this, a subsequent save would be rejected
      // with "stale version" when the real cause is "never reloaded".
      _settingsVersion = null;
    }
  }

  function bindOnce(id, evt, fn) {
    var el = $q("#" + id);
    if (!el || el.dataset.wired === "1") return;
    el.dataset.wired = "1";
    el.addEventListener(evt, fn);
  }

  function initSettings() {
    bindOnce("btn-imp-save",        "click", saveImprover);
    bindOnce("btn-as-save",         "click", saveAutoSelect);
    bindOnce("btn-workflow-check",  "click", workflowCheck);
    bindOnce("btn-workflow-update", "click", workflowUpdate);
    bindOnce("btn-settings-reload", "click", reloadSettingsGuarded);
    if (!loadedOnce) loadAllSettings();
  }

  // Click handler for the reload button. Coalesces concurrent clicks: if a
  // load is already in flight, the second click is dropped. Disables the
  // button for the duration so the user sees the busy state.
  async function reloadSettingsGuarded() {
    if (_isLoading) return;
    _isLoading = true;
    var btn = $q("#btn-settings-reload");
    if (btn) btn.disabled = true;
    try {
      await loadAllSettings();
    } finally {
      _isLoading = false;
      if (btn) btn.disabled = false;
    }
  }

  // Re-fetch every time the user navigates back to Settings OR Models &
  // dispatch — auto-select + phase-tuning live under Models now, but the
  // same /api/settings endpoint feeds both views, so a single loader keeps
  // them in sync. Missing target elements degrade gracefully (every fill
  // function is null-guarded).
  function bindNavRefresh() {
    ["settings", "models"].forEach(function (view) {
      var btn = document.querySelector('nav button[data-view="' + view + '"]');
      if (!btn || btn.dataset.refreshWired === "1") return;
      btn.dataset.refreshWired = "1";
      btn.addEventListener("click", function () { loadAllSettings(); });
    });
  }

  window.initSettings = initSettings;
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () { initSettings(); bindNavRefresh(); });
  } else {
    initSettings();
    bindNavRefresh();
  }
})();
