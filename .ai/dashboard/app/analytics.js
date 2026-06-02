// .ai/dashboard/app/analytics.js
// Analytics tab: fetches GET /api/analytics?range=… and renders a KPI strip plus
// four chart sections with the vendored Chart.js (v4.4.6, app/vendor/chart.umd.js).
// See .ai/specs/2026-06-02-analytics-page-design.md.
// IIFE + `var` so identifiers behave like the other app/*.js modules.
(function () {
  "use strict";

  function $a(sel) { return document.querySelector(sel); }

  // Chart.js instances, keyed by canvas id. Destroyed + recreated on each load
  // so a re-fetch never leaks a detached chart or double-binds a canvas.
  var _charts = {};
  var _refreshTimer = null;
  var AUTO_REFRESH_MS = 60000;

  // Palette pulled from the dashboard accent family; kept literal so charts
  // render even if CSS custom properties aren't resolvable from canvas context.
  var COLORS = ["#5ec8d8", "#c45ec8", "#7aa2ff", "#f2b14c", "#7ad97a", "#f2715e",
                "#b07af2", "#9aa7b2"];
  var GRID = "rgba(255,255,255,0.08)";
  var TICK = "rgba(220,230,240,0.62)";

  function fmtUsd(v) { return "$" + (Number(v) || 0).toFixed(2); }
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

  // Build (or rebuild) a chart. `series` empty => show the empty state, skip draw.
  function makeChart(id, hasData, config) {
    destroyChart(id);
    setPanelEmpty(id, !hasData);
    if (!hasData) return;
    var canvas = document.getElementById(id);
    if (!canvas || typeof window.Chart === "undefined") return;
    _charts[id] = new window.Chart(canvas.getContext("2d"), config);
  }

  var BASE_OPTS = {
    responsive: true,
    maintainAspectRatio: false,
    plugins: { legend: { labels: { color: TICK, boxWidth: 12 } } },
    scales: {
      x: { grid: { color: GRID }, ticks: { color: TICK } },
      y: { grid: { color: GRID }, ticks: { color: TICK }, beginAtZero: true },
    },
  };

  function noScaleOpts() {
    return { responsive: true, maintainAspectRatio: false,
             plugins: { legend: { labels: { color: TICK, boxWidth: 12 } } } };
  }

  // ----- KPI strip ---------------------------------------------------------
  function renderKpis(kpis) {
    var host = $a("#analytics-kpis");
    if (!host) return;
    delete host.dataset.skeletoned;
    var defs = [
      { key: "total_spend", label: "Total spend", fmt: fmtUsd, better: "down" },
      { key: "success_rate", label: "Success rate", fmt: fmtPct, better: "up" },
      { key: "phase_runs", label: "Phase runs", fmt: fmtInt, better: "up" },
      { key: "avg_duration", label: "Avg duration", fmt: fmtDuration, better: "down" },
      { key: "open_todos", label: "Open todos", fmt: fmtInt, better: null },
      { key: "pending_proposals", label: "Pending proposals", fmt: fmtInt, better: null },
    ];
    host.innerHTML = defs.map(function (d) {
      var k = kpis[d.key] || {};
      var val = d.fmt(k.value);
      var deltaHtml = "";
      // Only show a delta when a meaningful previous value exists.
      if (k.prev != null && k.value != null && d.better) {
        var diff = k.value - k.prev;
        if (k.prev !== 0 || diff !== 0) {
          var up = diff > 0;
          var good = (d.better === "up" && up) || (d.better === "down" && !up);
          var arrow = diff === 0 ? "→" : (up ? "▲" : "▼");
          var cls = diff === 0 ? "" : (good ? "up" : "down");
          var pct = k.prev ? Math.round(Math.abs(diff) / Math.abs(k.prev) * 100) + "%"
                           : "—";
          deltaHtml = '<span class="kpi-delta ' + cls + '">' + arrow + " " + pct +
                      " vs prev</span>";
        }
      }
      return '<div class="analytics-kpi"><span class="kpi-label">' + d.label +
             '</span><span class="kpi-value">' + val + "</span>" + deltaHtml + "</div>";
    }).join("");
  }

  // ----- Cost & efficiency -------------------------------------------------
  function renderCost(cost) {
    var sot = cost.spend_over_time || [];
    makeChart("chart-spend-over-time", sot.length, {
      type: "line",
      data: { labels: sot.map(function (r) { return r.date; }),
              datasets: [{ label: "$/day", data: sot.map(function (r) { return r.usd; }),
                           borderColor: COLORS[0], backgroundColor: "rgba(94,200,216,0.18)",
                           fill: true, tension: 0.25, pointRadius: 2 }] },
      options: BASE_OPTS,
    });

    var bm = cost.by_model || [];
    makeChart("chart-cost-by-model", bm.length, {
      type: "bar",
      data: { labels: bm.map(function (r) { return r.model; }),
              datasets: [{ label: "$", data: bm.map(function (r) { return r.usd; }),
                           backgroundColor: COLORS[2] }] },
      options: BASE_OPTS,
    });

    var dp = cost.duration_by_phase || [];
    makeChart("chart-duration-by-phase", dp.length, {
      type: "bar",
      data: { labels: dp.map(function (r) { return r.phase; }),
              datasets: [{ label: "total seconds",
                           data: dp.map(function (r) { return Math.round(r.duration_ms / 1000); }),
                           backgroundColor: COLORS[3] }] },
      options: BASE_OPTS,
    });
  }

  // ----- Workflow health ---------------------------------------------------
  function renderHealth(health) {
    var rot = health.runs_over_time || [];
    makeChart("chart-runs-over-time", rot.length, {
      type: "bar",
      data: { labels: rot.map(function (r) { return r.date; }),
              datasets: [
                { label: "done", data: rot.map(function (r) { return r.done; }),
                  backgroundColor: COLORS[4], stack: "s" },
                { label: "failed", data: rot.map(function (r) { return r.failed; }),
                  backgroundColor: COLORS[5], stack: "s" },
              ] },
      options: Object.assign({}, BASE_OPTS, {
        scales: { x: { stacked: true, grid: { color: GRID }, ticks: { color: TICK } },
                  y: { stacked: true, beginAtZero: true, grid: { color: GRID }, ticks: { color: TICK } } },
      }),
    });

    var outcomes = health.outcomes || {};
    var oKeys = Object.keys(outcomes);
    makeChart("chart-outcomes", oKeys.length, {
      type: "doughnut",
      data: { labels: oKeys,
              datasets: [{ data: oKeys.map(function (k) { return outcomes[k]; }),
                           backgroundColor: [COLORS[4], COLORS[5], COLORS[3], COLORS[7]] }] },
      options: noScaleOpts(),
    });

    var verdicts = health.review_verdicts || {};
    var vKeys = Object.keys(verdicts);
    makeChart("chart-verdicts", vKeys.length, {
      type: "bar",
      data: { labels: vKeys,
              datasets: [{ label: "count", data: vKeys.map(function (k) { return verdicts[k]; }),
                           backgroundColor: COLORS[1] }] },
      options: BASE_OPTS,
    });
  }

  // ----- Skills & agents ---------------------------------------------------
  function renderSkills(skills) {
    var top = (skills.top_by_invocations || []).slice(0, 12);
    makeChart("chart-top-skills", top.length, {
      type: "bar",
      data: { labels: top.map(function (r) { return r.skill; }),
              datasets: [{ label: "invocations", data: top.map(function (r) { return r.invocations; }),
                           backgroundColor: COLORS[0] }] },
      options: Object.assign({}, BASE_OPTS, { indexAxis: "y" }),
    });

    var cbs = (skills.cost_by_skill || []).filter(function (r) { return r.usd > 0; }).slice(0, 12);
    makeChart("chart-cost-by-skill", cbs.length, {
      type: "bar",
      data: { labels: cbs.map(function (r) { return r.skill; }),
              datasets: [{ label: "$", data: cbs.map(function (r) { return r.usd; }),
                           backgroundColor: COLORS[6] }] },
      options: BASE_OPTS,
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
           '<polyline fill="none" stroke="' + COLORS[0] + '" stroke-width="1.5" points="' +
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
             "<td class='num'>" + fmtUsd(r.avg_cost_usd) + "</td>" +
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
      data: { labels: psKeys,
              datasets: [{ label: "proposals", data: psKeys.map(function (k) { return ps[k]; }),
                           backgroundColor: COLORS[1] }] },
      options: BASE_OPTS,
    });

    var tb = backlog.todo_burndown || [];
    makeChart("chart-todo-burndown", tb.length, {
      type: "line",
      data: { labels: tb.map(function (r) { return r.date; }),
              datasets: [
                { label: "open", data: tb.map(function (r) { return r.open; }),
                  borderColor: COLORS[3], tension: 0.25, pointRadius: 2 },
                { label: "resolved", data: tb.map(function (r) { return r.resolved; }),
                  borderColor: COLORS[4], tension: 0.25, pointRadius: 2 },
              ] },
      options: BASE_OPTS,
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

  function loadAnalytics() {
    var meta = $a("#analytics-meta");
    if (meta) meta.textContent = "loading…";
    fetch("/api/analytics?range=" + encodeURIComponent(currentRange()))
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(function (data) {
        setError("");
        renderKpis(data.kpis || {});
        renderCost(data.cost || {});
        renderHealth(data.health || {});
        renderSkills(data.skills || {});
        renderBacklog(data.backlog || {});
        if (meta) {
          var t = new Date().toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
          meta.textContent = "updated " + t;
        }
      })
      .catch(function (e) {
        if (meta) meta.textContent = "";
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
      if (analyticsVisible()) loadAnalytics();
    }, AUTO_REFRESH_MS);
  }

  document.addEventListener("DOMContentLoaded", function () {
    var sel = $a("#analytics-range");
    if (sel) sel.addEventListener("change", loadAnalytics);
    var btn = $a("#analytics-refresh");
    if (btn) btn.addEventListener("click", loadAnalytics);
    startAutoRefresh();
  });

  // Exposed so core.js's nav handler can lazy-load on tab activation.
  window.loadAnalytics = loadAnalytics;
})();
