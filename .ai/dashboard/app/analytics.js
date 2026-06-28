// Analytics tab: fetches GET /api/analytics?range=… and renders a KPI strip plus
// four chart sections with the vendored Chart.js (v4.4.6, app/vendor/chart.umd.js).
// IIFE + `var` so identifiers behave like the other app/*.js modules.
(function () {
  "use strict";

  function $a(sel) { return document.querySelector(sel); }

  // Chart.js instances, keyed by canvas id. Destroyed + recreated on each load
  // so a re-fetch never leaks a detached chart or double-binds a canvas.
  var _charts = {};
  var _refreshTimer = null;
  var AUTO_REFRESH_MS = 60000;

  // Palette read from the design-system tokens so chart colors match the rest
  // of the platform (and track the theme). Hex fallbacks keep charts rendering
  // if a token can't be resolved. Modern browsers (which this dashboard already
  // requires for oklch/clip-path CSS) accept oklch() values in canvas.
  function cssVar(name, fallback) {
    try {
      var v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
      return v || fallback;
    } catch (_) { return fallback; }
  }
  var THEME = {
    accent:   cssVar("--accent",    "#46c7d6"),  // cyan — primary
    accentHi: cssVar("--accent-hi", "#74e2ee"),  // light cyan
    magenta:  cssVar("--magenta",   "#d24da0"),  // secondary
    violet:   cssVar("--violet",    "#8e6be8"),  // tertiary
    good:     cssVar("--good",      "#45c07e"),  // success / done   (status only)
    warn:     cssVar("--warn",      "#e6b040"),  // pending / open   (status only)
    bad:      cssVar("--bad",       "#eb5e4d"),  // failure          (status only)
    surf:     cssVar("--surf-2",    "#11161f"),  // panel surface — for arc separators
  };
  // Translucent variant for area fills — srgb mix is broadly supported in canvas.
  function alpha(color, pct) {
    return "color-mix(in srgb, " + color + " " + pct + "%, transparent)";
  }
  function mix(a, b) { return "color-mix(in oklch, " + a + ", " + b + ")"; }
  // Categorical sequence for ranked / multi-category charts: BRAND FAMILY ONLY
  // (cyan → violet → magenta and blends). Green/amber/red are deliberately
  // excluded here — they carry status meaning and would clash used as plain
  // category colors.
  var PALETTE = [THEME.accent, THEME.violet, THEME.magenta, THEME.accentHi,
                 mix(THEME.accent, THEME.violet), mix(THEME.violet, THEME.magenta)];
  // Gridlines use the platform's tinted border token (not pure white) so the
  // chart frame reads as the same line-work as the rest of the HUD.
  var GRID = cssVar("--border-soft", "rgba(255,255,255,0.08)");
  var TICK = cssVar("--text-dim", "rgba(220,230,240,0.62)");
  function cycle(n) {
    var out = [];
    for (var i = 0; i < n; i++) out.push(PALETTE[i % PALETTE.length]);
    return out;
  }
  // Semantic mappings so the same concept reads the same color everywhere.
  function outcomeColor(key) {
    return ({ done: THEME.good, failed: THEME.bad, cancelled: THEME.warn })[key] || THEME.accent;
  }
  function verdictColor(key) {
    return ({ approve: THEME.good, "request-changes": THEME.warn,
              escalate: THEME.bad, none: THEME.violet })[key] || THEME.accent;
  }
  function proposalColor(key) {
    return ({ pending: THEME.warn, applied: THEME.good, rejected: THEME.bad,
              no_change: THEME.violet, failed: THEME.bad })[key] || THEME.accent;
  }

  // Mix a color toward the panel surface so saturated status fills sit IN the
  // panel rather than floating on top.
  function soft(color, keepPct) {
    return "color-mix(in oklch, " + color + " " + (keepPct == null ? 86 : keepPct) +
           "%, " + THEME.surf + ")";
  }
  // Scriptable bar fill: a gradient anchored solid at the bar's base and fading
  // toward the surface at its tip, echoing the card chrome instead of a flat block.
  // `colors` may be one color or a per-bar array; `axis` "x" => horizontal bars.
  function barBg(colors, axis) {
    return function (c) {
      var area = c.chart.chartArea;
      var base = Array.isArray(colors) ? colors[c.dataIndex % colors.length] : colors;
      if (!area) return base;            // pre-layout / legend swatch -> solid
      var g = axis === "x"
        ? c.chart.ctx.createLinearGradient(area.left, 0, area.right, 0)
        : c.chart.ctx.createLinearGradient(0, area.bottom, 0, area.top);
      g.addColorStop(0, base);
      g.addColorStop(1, soft(base, 42));
      return g;
    };
  }

  // Make Chart.js typography coherent with the dashboard HUD (mono ticks/legend,
  // dimmed label color). Runs once at module load — chart.umd.js is a deferred
  // script ordered before this one, so window.Chart is defined here.
  (function applyChartDefaults() {
    if (typeof window.Chart === "undefined") return;
    var mono = "";
    try {
      mono = getComputedStyle(document.documentElement)
        .getPropertyValue("--ff-mono").trim();
    } catch (_) { /* ignore */ }
    window.Chart.defaults.font.family = mono || "ui-monospace, monospace";
    window.Chart.defaults.font.size = 11;
    window.Chart.defaults.color = TICK;
  })();

  function fmtUsd(v) { return "$" + (Number(v) || 0).toFixed(2); }
  // Costs can be fractions of a cent (e.g. a high-volume skill's per-run avg).
  // "$0.00" reads as "free" and hides the real figure, so show "<$0.01" for
  // tiny non-zero values; full precision goes in a title tooltip at the call site.
  function fmtCost(v) {
    v = Number(v) || 0;
    if (v > 0 && v < 0.01) return "<$0.01";
    return "$" + v.toFixed(2);
  }
  function fmtInt(v) { return String(Number(v) || 0); }
  function fmtPct(v) { return v == null ? "—" : Math.round(v * 100) + "%"; }
  function fmtDuration(ms) {
    if (ms == null) return "—";
    var s = ms / 1000;
    if (s < 90) return s.toFixed(1) + "s";
    var m = s / 60;
    if (m < 90) return m.toFixed(1) + "m";
    return (m / 60).toFixed(1) + "h";
  }
  // Turn raw ledger keys into human labels for chart axes/legends. Known keys
  // get explicit names; anything else has separators stripped and is capitalized.
  // Model/skill IDs are NOT passed through here — they read fine as-is to devs.
  function humanize(s) {
    var map = {
      no_change: "No change", "request-changes": "Request changes",
      none: "None", unknown: "Unknown", done: "Done", failed: "Failed",
      pending: "Pending", applied: "Applied", rejected: "Rejected",
      approve: "Approve", escalate: "Escalate", cancelled: "Cancelled",
    };
    var key = String(s == null ? "" : s);
    if (map[key]) return map[key];
    var t = key.replace(/[_-]+/g, " ");
    return t.charAt(0).toUpperCase() + t.slice(1);
  }

  function setError(msg) {
    var el = $a("#analytics-error");
    if (!el) return;
    if (!msg) { el.hidden = true; el.textContent = ""; return; }
    el.hidden = false;
    el.textContent = msg;
  }

  // Toggle a panel between its canvas and its "No data" placeholder.
  function setPanelEmpty(canvasId, isEmpty) {
    var canvas = document.getElementById(canvasId);
    if (!canvas) return;
    var panel = canvas.closest(".analytics-panel");
    if (!panel) return;
    var wrap = panel.querySelector(".analytics-canvas-wrap");
    var empty = panel.querySelector(".analytics-empty");
    if (wrap) wrap.hidden = !!isEmpty;
    if (empty) empty.hidden = !isEmpty;
  }

  function destroyChart(id) {
    if (_charts[id]) { _charts[id].destroy(); delete _charts[id]; }
  }

  // Build or UPDATE a chart. On a refresh, an existing chart of the same type is
  // updated in place (labels + datasets swapped, then update("none")) so it never
  // disappears/re-animates — the canvas stays on screen the whole time. Only a
  // first render, a type change, or a transition to/from the empty state creates
  // or destroys the instance.
  function makeChart(id, hasData, config) {
    setPanelEmpty(id, !hasData);
    if (!hasData) { destroyChart(id); return; }
    var canvas = document.getElementById(id);
    if (!canvas || typeof window.Chart === "undefined") return;
    // A11y: a <canvas> is opaque to screen readers. Expose the panel heading as
    // an image label so the chart's purpose is announced.
    if (!canvas.getAttribute("aria-label")) {
      var panel = canvas.closest(".analytics-panel");
      var heading = panel && panel.querySelector("h3, h4");
      canvas.setAttribute("role", "img");
      canvas.setAttribute("aria-label",
        (heading ? heading.textContent.trim() : "Analytics") + " chart");
    }
    var existing = _charts[id];
    if (existing && existing.config && existing.config.type === config.type) {
      // Refresh DATA ONLY. Never reassign options here: config-level settings
      // like indexAxis (horizontal bars) and doughnut layout are resolved at
      // construction and don't reliably re-apply on update(), which corrupted
      // the Top Skills and Outcomes charts on range changes.
      existing.data.labels = config.data.labels;
      existing.data.datasets = config.data.datasets;
      existing.update("none");   // no animation -> seamless, no flicker
      return;
    }
    destroyChart(id);
    _charts[id] = new window.Chart(canvas.getContext("2d"), config);
  }

  // Factory, NOT a shared constant: Chart.js v4 takes ownership of the options
  // object (and its nested scales) and mutates it during construction. Sharing
  // one object across charts cross-contaminates them — which broke the
  // horizontal-bar Top Skills chart. Each chart must get its own fresh copy.
  // Fresh each call (Chart.js may mutate options). Small SQUARE legend chips
  // (boxWidth == boxHeight, no rounding) to match the platform's sharp geometry.
  function legendCfg() {
    return { labels: { color: TICK, boxWidth: 10, boxHeight: 10, useBorderRadius: false } };
  }

  function baseOpts() {
    return {
      responsive: true,
      maintainAspectRatio: false,
      plugins: { legend: legendCfg() },
      scales: {
        x: { grid: { color: GRID }, ticks: { color: TICK } },
        y: { grid: { color: GRID }, ticks: { color: TICK }, beginAtZero: true },
      },
    };
  }

  function noScaleOpts() {
    return { responsive: true, maintainAspectRatio: false,
             plugins: { legend: legendCfg() } };
  }

  // ----- KPI strip ---------------------------------------------------------
  function savingsHasModeled(savings) {
    var tasks = (savings && savings.tasks) || {};
    return Object.keys(tasks).some(function (slug) {
      return tasks[slug] && tasks[slug].modeled === true;
    });
  }

  function fmtSignedCost(v) {
    var n = Number(v) || 0;
    return (n < 0 ? "-" : "") + fmtCost(Math.abs(n));
  }

  function savingsDetailHtml(savings) {
    if (!savings || !savings.totals) {
      return '<span class="kpi-delta" aria-hidden="true">&mdash;</span>';
    }
    var b = savings.totals.breakdown || {};
    var text = "routing " + fmtSignedCost(b.routing) +
               " / cache " + fmtSignedCost(b.cache) +
               " / gating " + fmtSignedCost(b.gating);
    var title = "routing $" + (Number(b.routing) || 0).toFixed(6) +
                " / cache $" + (Number(b.cache) || 0).toFixed(6) +
                " / gating $" + (Number(b.gating) || 0).toFixed(6);
    var modeled = savingsHasModeled(savings)
      ? '<span class="kpi-modeled" title="Some gating savings are modeled, not measured">modeled</span>'
      : "";
    return '<span class="kpi-delta kpi-breakdown" title="' + escapeHtml(title) + '">' +
           escapeHtml(text) + '</span>' + modeled;
  }

  function renderKpis(data) {
    var host = $a("#analytics-kpis");
    if (!host) return;
    delete host.dataset.skeletoned;
    var kpis = data && data.kpis ? data.kpis : (data || {});
    var savings = data && Object.prototype.hasOwnProperty.call(data, "savings") ? data.savings : null;
    var defs = [
      { key: "total_spend", label: "Total spend", fmt: fmtUsd, better: "down" },
      { key: "__savings", label: "Cost saved vs opus", fmt: function () {
          return savings && savings.totals ? fmtPct(savings.totals.savings_pct) : "&mdash;";
        }, detail: function () { return savingsDetailHtml(savings); } },
      { key: "success_rate", label: "Success rate", fmt: fmtPct, better: "up" },
      { key: "phase_runs", label: "Phase runs", fmt: fmtInt, better: "up" },
      { key: "avg_duration", label: "Avg duration", fmt: fmtDuration, better: "down" },
      { key: "open_todos", label: "Open todos", fmt: fmtInt, better: null },
      { key: "pending_proposals", label: "Pending proposals", fmt: fmtInt, better: null },
    ];
    host.innerHTML = defs.map(function (d) {
      var k = kpis[d.key] || {};
      var val = d.fmt(k.value);
      // Always render the delta line so every card is the same height (no ragged
      // strip). Show a real trend only when there's a non-zero prior period;
      // otherwise a dim neutral placeholder — never a misleading "▲ — vs prev".
      var deltaHtml = '<span class="kpi-delta" aria-hidden="true">—</span>';
      if (d.detail) {
        deltaHtml = d.detail();
      } else if (d.better && k.prev != null && k.value != null && k.prev !== 0) {
        var diff = k.value - k.prev;
        var up = diff > 0;
        var good = (d.better === "up" && up) || (d.better === "down" && !up);
        var arrow = diff === 0 ? "→" : (up ? "▲" : "▼");
        var cls = diff === 0 ? "" : (good ? "up" : "down");
        var pct = Math.round(Math.abs(diff) / Math.abs(k.prev) * 100) + "%";
        deltaHtml = '<span class="kpi-delta ' + cls + '">' + arrow + " " + pct +
                    " vs prev</span>";
      }
      // Reuse the standard .card structure (h3 + .val) so KPI cards inherit the
      // exact card chrome — gradient hairline, cut corner, beacon, hover.
      return '<div class="card"><h3>' + d.label + '</h3><div class="val big">' + val +
             "</div>" + deltaHtml + "</div>";
    }).join("");
  }

  // ----- Cost & efficiency -------------------------------------------------
  function renderCost(cost) {
    var sot = cost.spend_over_time || [];
    makeChart("chart-spend-over-time", sot.length, {
      type: "line",
      data: { labels: sot.map(function (r) { return r.date; }),
              datasets: [{ label: "$/day", data: sot.map(function (r) { return r.usd; }),
                           borderColor: THEME.accent, backgroundColor: alpha(THEME.accent, 16),
                           fill: true, tension: 0.25, pointRadius: 2,
                           pointBackgroundColor: THEME.accent, pointBorderColor: THEME.surf,
                           pointBorderWidth: 1 }] },
      options: baseOpts(),
    });

    var bm = cost.by_model || [];
    makeChart("chart-cost-by-model", bm.length, {
      type: "bar",
      data: { labels: bm.map(function (r) { return r.model === "unknown" ? "Unknown" : r.model; }),
              datasets: [{ label: "$", data: bm.map(function (r) { return r.usd; }),
                           backgroundColor: barBg(cycle(bm.length)) }] },
      options: baseOpts(),
    });

    var dp = cost.duration_by_phase || [];
    makeChart("chart-duration-by-phase", dp.length, {
      type: "bar",
      data: { labels: dp.map(function (r) { return r.phase; }),
              datasets: [{ label: "total seconds",
                           data: dp.map(function (r) { return Math.round(r.duration_ms / 1000); }),
                           backgroundColor: barBg(cycle(dp.length)) }] },
      options: baseOpts(),
    });
  }

  // ----- Workflow health ---------------------------------------------------
  function renderHealth(health) {
    var rot = health.runs_over_time || [];
    makeChart("chart-runs-over-time", rot.length, {
      type: "bar",
      data: { labels: rot.map(function (r) { return r.date; }),
              datasets: [
                { label: "Done", data: rot.map(function (r) { return r.done; }),
                  backgroundColor: barBg(THEME.good), stack: "s" },
                { label: "Failed", data: rot.map(function (r) { return r.failed; }),
                  backgroundColor: barBg(THEME.bad), stack: "s" },
              ] },
      options: Object.assign(baseOpts(), {
        scales: { x: { stacked: true, grid: { color: GRID }, ticks: { color: TICK } },
                  y: { stacked: true, beginAtZero: true, grid: { color: GRID }, ticks: { color: TICK } } },
      }),
    });

    var outcomes = health.outcomes || {};
    var oKeys = Object.keys(outcomes);
    makeChart("chart-outcomes", oKeys.length, {
      type: "doughnut",
      data: { labels: oKeys.map(humanize),
              datasets: [{ data: oKeys.map(function (k) { return outcomes[k]; }),
                           backgroundColor: oKeys.map(function (k) { return soft(outcomeColor(k)); }),
                           borderColor: THEME.surf, borderWidth: 2 }] },
      options: noScaleOpts(),
    });

    var verdicts = health.review_verdicts || {};
    var vKeys = Object.keys(verdicts);
    makeChart("chart-verdicts", vKeys.length, {
      type: "bar",
      data: { labels: vKeys.map(humanize),
              datasets: [{ label: "count", data: vKeys.map(function (k) { return verdicts[k]; }),
                           backgroundColor: barBg(vKeys.map(verdictColor)) }] },
      options: baseOpts(),
    });
  }

  // ----- Skills & agents ---------------------------------------------------
  function renderSkills(skills) {
    var top = (skills.top_by_invocations || []).slice(0, 12);
    makeChart("chart-top-skills", top.length, {
      type: "bar",
      data: { labels: top.map(function (r) { return r.skill; }),
              datasets: [{ label: "invocations", data: top.map(function (r) { return r.invocations; }),
                           backgroundColor: barBg(THEME.accent, "x") }] },
      options: Object.assign(baseOpts(), { indexAxis: "y" }),
    });

    var cbs = (skills.cost_by_skill || []).filter(function (r) { return r.usd > 0; }).slice(0, 12);
    makeChart("chart-cost-by-skill", cbs.length, {
      type: "bar",
      data: { labels: cbs.map(function (r) { return r.skill; }),
              datasets: [{ label: "$", data: cbs.map(function (r) { return r.usd; }),
                           backgroundColor: barBg(cycle(cbs.length)) }] },
      options: baseOpts(),
    });

    renderSkillTable(skills.table || []);
  }

  function sparkSvg(values) {
    if (!values || !values.length) return "";
    var w = 80, h = 18, max = Math.max.apply(null, values) || 1;
    var step = values.length > 1 ? w / (values.length - 1) : w;
    var pts = values.map(function (v, i) {
      var x = (i * step).toFixed(1);
      var y = (h - (v / max) * (h - 2) - 1).toFixed(1);
      return x + "," + y;
    }).join(" ");
    return '<svg class="analytics-spark" width="' + w + '" height="' + h +
           '" viewBox="0 0 ' + w + " " + h + '" preserveAspectRatio="none">' +
           '<polyline fill="none" stroke="' + THEME.accent + '" stroke-width="1.5" points="' +
           pts + '"/></svg>';
  }

  function renderSkillTable(rows) {
    var host = $a("#analytics-skill-table");
    if (!host) return;
    var panel = host.closest(".analytics-panel");
    var empty = panel ? panel.querySelector(".analytics-empty") : null;
    if (!rows.length) {
      host.innerHTML = "";
      if (empty) empty.hidden = false;
      return;
    }
    if (empty) empty.hidden = true;
    var head = "<thead><tr><th>Skill</th><th class='num'>Runs</th>" +
               "<th class='num'>Success</th><th class='num'>Avg cost</th><th>Trend</th></tr></thead>";
    var body = rows.map(function (r) {
      return "<tr><td>" + escapeHtml(r.skill) + "</td>" +
             "<td class='num'>" + fmtInt(r.runs) + "</td>" +
             "<td class='num'>" + fmtPct(r.success_rate) + "</td>" +
             "<td class='num' title='$" + (Number(r.avg_cost_usd) || 0).toFixed(6) + " avg/run'>" +
                 fmtCost(r.avg_cost_usd) + "</td>" +
             "<td>" + sparkSvg(r.spark) + "</td></tr>";
    }).join("");
    host.innerHTML = "<table>" + head + "<tbody>" + body + "</tbody></table>";
  }

  // ----- Improvements & backlog --------------------------------------------
  function renderBacklog(backlog) {
    var ps = backlog.proposal_status || {};
    var psKeys = Object.keys(ps);
    var psHasData = psKeys.some(function (k) { return ps[k] > 0; });
    makeChart("chart-proposal-status", psHasData, {
      type: "bar",
      data: { labels: psKeys.map(humanize),
              datasets: [{ label: "proposals", data: psKeys.map(function (k) { return ps[k]; }),
                           backgroundColor: barBg(psKeys.map(proposalColor)) }] },
      options: baseOpts(),
    });

    var tb = backlog.todo_burndown || [];
    makeChart("chart-todo-burndown", tb.length, {
      type: "line",
      data: { labels: tb.map(function (r) { return r.date; }),
              datasets: [
                { label: "Open", data: tb.map(function (r) { return r.open; }),
                  borderColor: THEME.warn, tension: 0.25, pointRadius: 2,
                  pointBackgroundColor: THEME.warn, pointBorderColor: THEME.surf, pointBorderWidth: 1 },
                { label: "Resolved", data: tb.map(function (r) { return r.resolved; }),
                  borderColor: THEME.good, tension: 0.25, pointRadius: 2,
                  pointBackgroundColor: THEME.good, pointBorderColor: THEME.surf, pointBorderWidth: 1 },
              ] },
      options: baseOpts(),
    });

    renderActivity(backlog.recent_activity || []);
  }

  function renderActivity(rows) {
    var host = $a("#analytics-activity");
    if (!host) return;
    var panel = host.closest(".analytics-panel");
    var empty = panel ? panel.querySelector(".analytics-empty") : null;
    if (!rows.length) {
      host.innerHTML = "";
      if (empty) empty.hidden = false;
      return;
    }
    if (empty) empty.hidden = true;
    host.innerHTML = rows.map(function (r) {
      var when = r.ts ? new Date(r.ts).toLocaleString() : "";
      return '<div class="act-row"><span class="act-summary">' +
             escapeHtml(r.summary || r.kind || "event") +
             '</span><span class="act-ts">' + escapeHtml(when) + "</span></div>";
    }).join("");
  }

  function escapeHtml(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  // ----- Load + wiring -----------------------------------------------------
  function currentRange() {
    var sel = $a("#analytics-range");
    return (sel && sel.value) || "30d";
  }

  // Reuse the global topbar "updated HH:MM" indicator (#meta) — same structured
  // markup loadAll() builds — rather than a duplicate meta in the analytics view.
  function updateGlobalMeta() {
    var metaEl = document.getElementById("meta");
    if (!metaEl) return;
    var time = new Date().toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
    metaEl.replaceChildren();
    var stack = document.createElement("span");
    stack.className = "meta-stack";
    var label = document.createElement("span");
    label.className = "meta-label";
    label.textContent = "updated";
    var value = document.createElement("span");
    value.className = "meta-value";
    value.textContent = time;
    stack.append(label, value);
    metaEl.appendChild(stack);
  }

  // Re-trigger the "live" beacon pulse (remove + reflow + add restarts the CSS
  // animation) so every refresh produces a visible in-view acknowledgment.
  function pulseLive() {
    var live = $a("#analytics-live");
    if (!live) return;
    live.classList.remove("pulse");
    void live.offsetWidth;
    live.classList.add("pulse");
  }

  function loadAnalytics() {
    fetch("/api/analytics?range=" + encodeURIComponent(currentRange()))
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(function (data) {
        setError("");
        renderKpis(data || {});
        renderCost(data.cost || {});
        renderHealth(data.health || {});
        renderSkills(data.skills || {});
        renderBacklog(data.backlog || {});
        updateGlobalMeta();
        pulseLive();
      })
      .catch(function (e) {
        setError("Failed to load analytics: " + (e && e.message ? e.message : e));
      });
  }

  function analyticsVisible() {
    var view = $a("#view-analytics");
    return !!(view && view.classList.contains("active"));
  }

  function startAutoRefresh() {
    if (_refreshTimer) return;
    _refreshTimer = setInterval(function () {
      if (document.hidden) return;
      if (analyticsVisible()) loadAnalytics();
    }, AUTO_REFRESH_MS);
  }

  document.addEventListener("DOMContentLoaded", function () {
    var sel = $a("#analytics-range");
    if (sel) sel.addEventListener("change", loadAnalytics);
    startAutoRefresh();
  });

  // Exposed so core.js's nav handler can lazy-load on tab activation.
  window.loadAnalytics = loadAnalytics;
})();
