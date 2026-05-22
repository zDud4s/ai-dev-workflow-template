// .ai/dashboard/app/settings.js -- Settings tab: workflow knobs (improver,
// auto_select, per-phase tuning) + git update controls. Loads current values
// from `/api/settings` on init and whenever the tab is opened.

(function () {
  var ALL_PHASES = ["plan", "execute", "review", "rescue", "maintenance", "bootstrap"];
  var AS_PHASES_AVAILABLE = ["execute", "review", "rescue"];
  // Union of claude (low/medium/high/xhigh/max) and codex (low/medium/high/xhigh).
  // `max` applies only to claude; the codex dispatcher rejects it.
  var REASONING_LEVELS = ["", "low", "medium", "high", "xhigh", "max"];

  var loadedOnce = false;
  // Busy guard for the reload button — prevents two concurrent loadAllSettings
  // calls from racing each other and rendering out of order. Mirrors the
  // _jobsLoadInFlight pattern in jobs.js.
  var _isLoading = false;

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
  async function withBusy(btn, fn) {
    if (!btn) return fn();
    var label = btn.textContent;
    btn.disabled = true;
    btn.dataset.prevLabel = label;
    btn.textContent = "Saving…";
    try {
      return await fn();
    } finally {
      btn.disabled = false;
      btn.textContent = btn.dataset.prevLabel || label;
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
      await withBusy(btn, function () { return postJson("/api/settings/auto_select", body); });
      setMsg("as-msg", "Saved to .ai/models.yaml", "good");
      // The per-phase fallback section depends on auto-select state, refresh it.
      loadAllSettings();
    } catch (e) {
      setMsg("as-msg", e.message || "save failed", "bad");
    }
  }

  // ---------- Per-phase tuning ----------
  function renderPhasesTable(phases, autoSelect) {
    phases = phases || {};
    autoSelect = autoSelect || {};
    var autoOn = !!autoSelect.enabled;
    var autoPhases = autoOn ? (autoSelect.phases || []) : [];

    // Banner above the table when auto-select is on.
    var warn = $q("#phases-auto-warning");
    if (warn) {
      if (autoOn) {
        warn.textContent = "Auto-select is ON for phases: " + autoPhases.join(", ")
          + ". The planner picks tool / model / reasoning_effort per task for these phases. The values below are the fallback used only when auto-select has no match.";
        warn.classList.add("is-active");
      } else {
        warn.classList.remove("is-active");
        warn.textContent = "";
      }
    }

    var rows = ALL_PHASES.map(function (ph) {
      var p = phases[ph] || {};
      var tool = (p.tool || "").toLowerCase();
      var isCodex = tool === "codex";
      var isAuto = autoOn && autoPhases.indexOf(ph) >= 0;
      var current = p.reasoning_effort || "";
      var options = REASONING_LEVELS.map(function (r) {
        // `max` is claude-only; hide it from the dropdown for codex phases.
        if (r === "max" && isCodex) return "";
        var sel = current === r ? " selected" : "";
        var label = r || "(default)";
        return '<option value="' + r + '"' + sel + '>' + label + '</option>';
      }).join("");
      var to = p.timeout_seconds || "";
      var reasoningTitle = isCodex
        ? "codex --config model_reasoning_effort (low/medium/high/xhigh)"
        : "claude --effort (low/medium/high/xhigh/max)";
      var reasoningCell = '<select class="ph-reasoning" title="' + reasoningTitle + '">' + options + '</select>';
      // Restrict tool to safe class-name chars [a-z0-9_-] (defense in depth:
      // today's values come from a known set, but YAML could grow to include
      // characters that would break the attribute and enable injection).
      var toolClass = String(tool || "unknown").replace(/[^a-z0-9_-]/gi, "");
      var toolPill = '<span class="ph-tool-pill ph-tool-' + toolClass + '">' + escHtml(p.tool || "?") + '</span>';
      var effectiveCell = isAuto
        ? '<span class="ph-eff ph-eff-auto" title="The planner picks this per task at runtime via auto-select">auto · per task</span>'
        : '<span class="ph-eff ph-eff-yaml" title="The orchestrator reads this row directly from models.yaml">from fallback ↑</span>';
      var rowClass = isAuto ? "ph-row is-auto" : "ph-row";
      return ''
        + '<tr class="' + rowClass + '" data-phase="' + ph + '">'
        + '  <td class="ph-name">' + ph + (isAuto ? ' <span class="ph-auto-pill" title="auto-select active">AUTO</span>' : '') + '</td>'
        + '  <td>' + toolPill + ' <span class="ph-meta">' + escHtml(p.model || "?") + '</span></td>'
        + '  <td>' + reasoningCell + '</td>'
        + '  <td><input type="number" class="ph-timeout" min="30" max="7200" value="' + to + '" placeholder="(default)" /></td>'
        + '  <td>' + effectiveCell + '</td>'
        + '  <td><button class="btn secondary ph-save" type="button">Save</button></td>'
        + '</tr>';
    }).join("");
    var html = ''
      + '<table class="phases-table">'
      + '  <thead><tr>'
      + '    <th>Phase</th><th>Tool / model</th><th>Reasoning effort</th>'
      + '    <th>Timeout</th><th>Effective at runtime</th><th></th>'
      + '  </tr></thead>'
      + '  <tbody>' + rows + '</tbody>'
      + '</table>';
    var wrap = $q("#phases-table");
    delete wrap.dataset.skeletoned;
    wrap.innerHTML = html;
    wrap.querySelectorAll("button.ph-save").forEach(function (btn) {
      btn.addEventListener("click", function () { savePhaseRow(btn.closest("tr"), btn); });
    });
  }

  async function savePhaseRow(tr, btn) {
    var ph = tr.dataset.phase;
    // Validate phase against the known set. tr.dataset.phase is user-mutable
    // via devtools; missing/tampered values would otherwise POST {phase:undefined}.
    if (!ph || ALL_PHASES.indexOf(ph) < 0) {
      setMsg("phases-msg", "invalid phase", "bad");
      return;
    }
    // HTML5 constraint validation (min=30 max=7200) only fires on form-submit,
    // not on type=button clicks. Check validity.valid before sending so the
    // server doesn't reject (or worse, accept) out-of-range integers.
    var tInput = tr.querySelector(".ph-timeout");
    if (tInput && !tInput.validity.valid) {
      setMsg("phases-msg", "timeout: " + tInput.validationMessage, "bad");
      return;
    }
    var body = { phase: ph, timeout_seconds: tInput ? tInput.value : "" };
    var rEl = tr.querySelector(".ph-reasoning");
    // Gemini ignores reasoning_effort (dispatch silently discards it); omit the
    // field from the POST body so the YAML doesn't drift from the UI promise
    // and a stale value can't reload next time.
    var toolPill = tr.querySelector(".ph-tool-pill");
    var tool = "";
    if (toolPill) {
      var m = (toolPill.className || "").match(/ph-tool-([a-z]+)/);
      if (m) tool = m[1];
    }
    if (rEl) body.reasoning_effort = rEl.value;
    setMsg("phases-msg", "");
    try {
      await withBusy(btn, function () { return postJson("/api/models/phase", body); });
      setMsg("phases-msg", "Saved " + ph, "good");
    } catch (e) {
      setMsg("phases-msg", ph + ": " + (e.message || "save failed"), "bad");
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
  function setWorkflowBusy(busy) {
    var c = $q("#btn-workflow-check");
    var p = $q("#btn-workflow-update");
    if (c) c.disabled = busy;
    if (p && busy) p.disabled = true;
  }
  function setRestartWarning(visible) {
    var el = $q("#workflow-restart-warning");
    if (el) el.style.display = visible ? "block" : "none";
  }
  function escHtml(s) {
    return String(s == null ? "" : s).replace(/[<>&]/g, function (ch) {
      return { "<": "&lt;", ">": "&gt;", "&": "&amp;" }[ch];
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
        $q("#btn-workflow-update").disabled = true;
        return;
      }
      var shortUp = (data.upstream_sha || "").substring(0, 7) || "?";
      var shortCur = data.current_sha ? data.current_sha.substring(0, 7) : "none";
      var line = "Upstream " + shortUp + " · installed " + shortCur;
      setWorkflowStatus(line + " — " + (data.message || ""),
                        data.has_updates ? "warn" : "good");
      // Enabled iff there ARE updates to apply. Don't gate on current_sha:
      // on a fresh install with no updates queued the old combined expression
      // would erroneously enable the button.
      $q("#btn-workflow-update").disabled = !data.has_updates;
      showWorkflowLog(data.commits || []);
    } catch (e) {
      setWorkflowStatus("Network error: " + e.message, "bad");
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
      }
    } catch (e) {
      setWorkflowStatus("Network error: " + e.message, "bad");
    } finally {
      var c = $q("#btn-workflow-check");
      var p = $q("#btn-workflow-update");
      if (c) c.disabled = false;
      if (p) p.disabled = false;
    }
  }

  // ---------- Loading & coordination ----------
  // Builds the structured skeleton variant used elsewhere in the dashboard
  // (.skeleton + .skeleton-table-row), so the Settings tab matches the
  // visual language of Agents / Skills / Events while data loads.
  function phasesTableSkeletonHtml() {
    var row = '<div class="skeleton-table-row">'
      + '<span class="skeleton skeleton-cell narrow"></span>'
      + '<span class="skeleton skeleton-cell"></span>'
      + '<span class="skeleton skeleton-cell narrow"></span>'
      + '<span class="skeleton skeleton-cell narrow"></span>'
      + '<span class="skeleton skeleton-cell"></span>'
      + '<span class="skeleton skeleton-cell narrow"></span>'
      + '</div>';
    return new Array(6).fill(row).join("");
  }
  function showLoadingState() {
    setMsg("settings-meta", "Loading…");
    var phases = $q("#phases-table");
    if (phases) {
      phases.innerHTML = phasesTableSkeletonHtml();
      phases.dataset.skeletoned = "1";
    }
  }

  async function loadAllSettings() {
    showLoadingState();
    try {
      var data = await getJson("/api/settings");
      fillImprover(data.improver || {});
      fillAutoSelect(data.auto_select || {});
      renderPhasesTable(data.phases || {}, data.auto_select || {});
      setMsg("settings-meta", "Loaded " + new Date().toLocaleTimeString(), "good");
      loadedOnce = true;
    } catch (e) {
      setMsg("settings-meta", "Failed to load: " + e.message, "bad");
      var wrap = $q("#phases-table");
      if (wrap) {
        delete wrap.dataset.skeletoned;
        wrap.innerHTML = '<div class="settings-skeleton" style="color:var(--bad)">load failed</div>';
      }
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

  // Re-fetch every time the user navigates back to the Settings tab so the
  // form reflects whatever is currently on disk (e.g. after an external edit).
  function bindNavRefresh() {
    var btn = document.querySelector('nav button[data-view="settings"]');
    if (!btn || btn.dataset.refreshWired === "1") return;
    btn.dataset.refreshWired = "1";
    btn.addEventListener("click", function () { loadAllSettings(); });
  }

  window.initSettings = initSettings;
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () { initSettings(); bindNavRefresh(); });
  } else {
    initSettings();
    bindNavRefresh();
  }
})();
