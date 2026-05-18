// .ai/dashboard/app/settings.js -- Settings tab: workflow knobs (improver,
// auto_select, per-phase tuning) + git update controls. Loads current values
// from `/api/settings` on init and whenever the tab is opened.

(function () {
  var ALL_PHASES = ["plan", "execute", "review", "rescue", "maintenance", "bootstrap"];
  var AS_PHASES_AVAILABLE = ["execute", "review", "rescue"];
  var REASONING_LEVELS = ["", "xhigh", "high", "medium", "low"];

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
  function setMsg(id, text, tone) {
    var el = $q("#" + id);
    if (!el) return;
    el.textContent = text || "";
    el.classList.remove("ok", "err");
    if (tone === "good") el.classList.add("ok");
    else if (tone === "bad") el.classList.add("err");
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
    } catch (e) {
      setMsg("as-msg", e.message || "save failed", "bad");
    }
  }

  // ---------- Per-phase tuning ----------
  function renderPhasesTable(phases) {
    phases = phases || {};
    var rows = ALL_PHASES.map(function (ph) {
      var p = phases[ph] || {};
      var current = p.reasoning_effort || "";
      var options = REASONING_LEVELS.map(function (r) {
        var sel = current === r ? " selected" : "";
        var label = r || "(default)";
        return '<option value="' + r + '"' + sel + '>' + label + '</option>';
      }).join("");
      var to = p.timeout_seconds || "";
      return ''
        + '<tr data-phase="' + ph + '">'
        + '  <td class="ph-name">' + ph + '</td>'
        + '  <td class="ph-meta">' + (p.tool || "?") + ' / ' + (p.model || "?") + '</td>'
        + '  <td><select class="ph-reasoning">' + options + '</select></td>'
        + '  <td><input type="number" class="ph-timeout" min="30" max="7200" value="' + to + '" placeholder="(default)" /></td>'
        + '  <td><button class="btn secondary ph-save" type="button">Save</button></td>'
        + '</tr>';
    }).join("");
    var html = ''
      + '<table class="phases-table">'
      + '  <thead><tr>'
      + '    <th>Phase</th><th>Tool / model</th><th>Reasoning effort</th>'
      + '    <th>Timeout seconds</th><th></th>'
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
    var body = {
      phase: ph,
      reasoning_effort: tr.querySelector(".ph-reasoning").value,
      timeout_seconds:  tr.querySelector(".ph-timeout").value,
    };
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
      renderPhasesTable(data.phases || {});
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
