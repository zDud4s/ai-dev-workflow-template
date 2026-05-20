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
    try { data = await r.json(); } catch (_) { /* ignore */ }
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
    $q("#imp-enabled").checked = !!cfg.enabled;
    $q("#imp-small-change").value = numOrEmpty(cfg.small_change_max_lines);
    $q("#imp-min-interval").value = numOrEmpty(cfg.min_interval_seconds);
    $q("#imp-timeout").value      = numOrEmpty(cfg.timeout_seconds);
    $q("#imp-revert").value       = numOrEmpty(cfg.revert_after_n_uses);

    var warn = $q("#improver-env-warning");
    var lock = !!cfg.disabled_by_env;
    if (lock) {
      warn.textContent = "AI_WORKFLOW_DISABLE_IMPROVER is set — the improver is forced off regardless of the YAML value below. Unset the env var to re-enable.";
      warn.classList.add("is-active");
    } else {
      warn.classList.remove("is-active");
      warn.textContent = "";
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
    var body = {
      enabled: $q("#imp-enabled").checked,
      small_change_max_lines: $q("#imp-small-change").value,
      min_interval_seconds:   $q("#imp-min-interval").value,
      timeout_seconds:        $q("#imp-timeout").value,
      revert_after_n_uses:    $q("#imp-revert").value,
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
    $q("#as-enabled").checked = !!cfg.enabled;
    $q("#as-budget").value = cfg.token_budget || "medium";
    var wrap = $q("#as-phases");
    wrap.innerHTML = "";
    var current = cfg.phases || [];
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
      var toolPill = '<span class="ph-tool-pill ph-tool-' + (tool || "unknown") + '">' + (p.tool || "?") + '</span>';
      var effectiveCell = isAuto
        ? '<span class="ph-eff ph-eff-auto" title="The planner picks this per task at runtime via auto-select">auto · per task</span>'
        : '<span class="ph-eff ph-eff-yaml" title="The orchestrator reads this row directly from models.yaml">from fallback ↑</span>';
      var rowClass = isAuto ? "ph-row is-auto" : "ph-row";
      return ''
        + '<tr class="' + rowClass + '" data-phase="' + ph + '">'
        + '  <td class="ph-name">' + ph + (isAuto ? ' <span class="ph-auto-pill" title="auto-select active">AUTO</span>' : '') + '</td>'
        + '  <td>' + toolPill + ' <span class="ph-meta">' + (p.model || "?") + '</span></td>'
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
    wrap.innerHTML = html;
    wrap.querySelectorAll("button.ph-save").forEach(function (btn) {
      btn.addEventListener("click", function () { savePhaseRow(btn.closest("tr"), btn); });
    });
  }

  async function savePhaseRow(tr, btn) {
    var ph = tr.dataset.phase;
    var body = { phase: ph, timeout_seconds: tr.querySelector(".ph-timeout").value };
    var rEl = tr.querySelector(".ph-reasoning");
    if (rEl) body.reasoning_effort = rEl.value;
    setMsg("phases-msg", "");
    try {
      await withBusy(btn, function () { return postJson("/api/models/phase", body); });
      setMsg("phases-msg", "Saved " + ph, "good");
    } catch (e) {
      setMsg("phases-msg", ph + ": " + (e.message || "save failed"), "bad");
    }
  }

  // ---------- Repository updates ----------
  function setGitStatus(text, tone) {
    var el = $q("#git-status");
    if (!el) return;
    el.textContent = text;
    el.style.color = tone === "good" ? "var(--good)"
      : tone === "bad" ? "var(--bad)"
      : tone === "warn" ? "var(--warn)"
      : "var(--text-dim)";
  }
  function setGitOutput(text) {
    var el = $q("#git-output");
    if (!el) return;
    if (!text) { el.style.display = "none"; el.textContent = ""; return; }
    el.style.display = "block";
    el.textContent = text;
  }
  function setGitBusy(busy) {
    var c = $q("#btn-git-check");
    var p = $q("#btn-git-pull");
    if (c) c.disabled = busy;
    if (p && busy) p.disabled = true;
  }
  function escHtml(s) {
    return String(s == null ? "" : s).replace(/[<>&]/g, function (ch) {
      return { "<": "&lt;", ">": "&gt;", "&": "&amp;" }[ch];
    });
  }
  function showGitLog(commits) {
    var wrap = $q("#git-log-wrap");
    var box = $q("#git-log");
    if (!wrap || !box) return;
    if (!commits || !commits.length) { wrap.style.display = "none"; box.innerHTML = ""; return; }
    box.innerHTML = commits.map(function (c) {
      return '<div><span class="sha">' + escHtml((c.sha || "").substring(0, 7))
        + '</span> · ' + escHtml(c.subject || "") + '</div>';
    }).join("");
    wrap.style.display = "block";
  }

  async function gitCheck() {
    setGitBusy(true);
    setGitStatus("Checking for updates…");
    setGitOutput("");
    showGitLog([]);
    try {
      var data = await getJson("/api/git/check");
      if (data.error) {
        setGitStatus(data.message || data.error, "bad");
        $q("#btn-git-pull").disabled = true;
        return;
      }
      var line = "Branch " + (data.branch || "?") + " · upstream " + (data.upstream || "?")
        + " · ahead " + (data.ahead || 0) + ", behind " + (data.behind || 0);
      setGitStatus(line + " — " + (data.message || ""), data.has_updates ? "warn" : "good");
      $q("#btn-git-pull").disabled = !data.has_updates;
      if (data.has_updates) {
        try {
          var log = await getJson("/api/git/log");
          showGitLog(log.commits || []);
        } catch (_) { /* swallow */ }
      }
    } catch (e) {
      setGitStatus("Network error: " + e.message, "bad");
    } finally {
      setGitBusy(false);
    }
  }

  async function gitPull() {
    setGitBusy(true);
    setGitStatus("Applying update…");
    setGitOutput("");
    try {
      var data = await postJson("/api/git/pull", {});
      setGitStatus(data.message || (data.success ? "Pull OK" : "Pull failed"),
                   data.success ? "good" : "bad");
      if (data.output) setGitOutput(data.output);
      if (data.success) {
        $q("#btn-git-pull").disabled = true;
        showGitLog([]);
      }
    } catch (e) {
      setGitStatus("Network error: " + e.message, "bad");
    } finally {
      var c = $q("#btn-git-check");
      if (c) c.disabled = false;
    }
  }

  // ---------- Loading & coordination ----------
  function showLoadingState() {
    setMsg("settings-meta", "Loading…");
    $q("#phases-table").innerHTML = '<div class="settings-skeleton">loading…</div>';
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
      $q("#phases-table").innerHTML = '<div class="settings-skeleton" style="color:var(--bad)">load failed</div>';
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
    bindOnce("btn-git-check",       "click", gitCheck);
    bindOnce("btn-git-pull",        "click", gitPull);
    bindOnce("btn-settings-reload", "click", loadAllSettings);
    if (!loadedOnce) loadAllSettings();
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
