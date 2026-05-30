// .ai/dashboard/app/agent-orchestrations.js -- agent orchestration run view.

(function () {
  var LIST_URL = "/api/agent-orchestrations";
  var DETAIL_URL = function (slug) {
    return "/api/agent-orchestrations/" + encodeURIComponent(slug);
  };

  var _runsState = { all: [], filter: "all" };
  var _runDetailEpoch = 0;

  function qs(sel) {
    return typeof window.$ === "function" ? window.$(sel) : document.querySelector(sel);
  }

  function escapeHtml(s) {
    if (typeof window.escHtml === "function") return window.escHtml(s);
    return String(s ?? "").replace(/[&<>"']/g, function (c) {
      return {
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;",
      }[c];
    });
  }

  function shortDisplayPath(p) {
    if (typeof window.shortPath === "function") {
      try { return window.shortPath(p); } catch (_) {}
    }
    return String(p || "");
  }

  function truncate(s, maxLen) {
    s = String(s || "");
    if (s.length <= maxLen) return s;
    return s.slice(0, Math.max(0, maxLen - 3)) + "...";
  }

  function renderRunsSkeletons() {
    var grid = qs("#agent-runs-grid");
    var empty = qs(".agent-runs-empty");
    if (empty) empty.hidden = true;
    if (!grid || grid.dataset.skeletoned) return;
    grid.hidden = false;
    grid.innerHTML = Array.from({ length: 6 }).map(function () {
      return '<div class="card agent-run-card skeleton-agent-run-card">'
        + '<span class="skeleton skeleton-h"></span>'
        + '<span class="skeleton skeleton-desc-1"></span>'
        + '<span class="skeleton skeleton-tools"></span>'
        + '</div>';
    }).join("");
    grid.dataset.skeletoned = "1";
  }

  function defaultEmptyHtml() {
    return 'No agent runs yet. Invoke <code>Use the orchestrate-agents skill. Task: ...</code> to populate this view.';
  }

  function showRunsEmpty(message) {
    var grid = qs("#agent-runs-grid");
    var empty = qs(".agent-runs-empty");
    if (grid) {
      grid.hidden = true;
      grid.innerHTML = "";
      delete grid.dataset.skeletoned;
    }
    if (empty) {
      empty.hidden = false;
      if (message) empty.textContent = message;
      else empty.innerHTML = defaultEmptyHtml();
    }
    renderRunsSummary([]);
  }

  function renderRunsSummary(runs) {
    var summary = qs(".agent-runs-summary");
    if (!summary) return;
    var total = runs.length;
    var success = runs.filter(function (r) { return r.success === true; }).length;
    var failed = runs.filter(function (r) { return r.success === false; }).length;
    var pending = total - success - failed;
    summary.innerHTML = [
      '<span class="metric-pill">runs ' + escapeHtml(String(total)) + '</span>',
      '<span class="metric-pill">success ' + escapeHtml(String(success)) + '</span>',
      '<span class="metric-pill">failed ' + escapeHtml(String(failed)) + '</span>',
      '<span class="metric-pill">pending ' + escapeHtml(String(pending)) + '</span>',
    ].join(" ");
  }

  function runStatus(run) {
    if (run && run.success === true) return "success";
    if (run && run.success === false) return "failed";
    return "pending";
  }

  function filteredRuns() {
    if (_runsState.filter === "all") return _runsState.all;
    return _runsState.all.filter(function (run) {
      return runStatus(run) === _runsState.filter;
    });
  }

  function renderRunCards() {
    var grid = qs("#agent-runs-grid");
    if (!grid) return;
    var empty = qs(".agent-runs-empty");
    if (empty) empty.hidden = true;
    grid.hidden = false;
    delete grid.dataset.skeletoned;

    var runs = filteredRuns();
    if (!runs.length) {
      showRunsEmpty(_runsState.all.length ? "No agent runs match the current filter." : null);
      return;
    }

    renderRunsSummary(_runsState.all);
    grid.innerHTML = runs.map(function (run) {
      var slug = run.task_slug || "";
      var status = runStatus(run);
      var count = Number(run.dispatch_count || 0);
      var output = run.output_hint || "no output hint";
      var date = run.date || run.synthesis_ts || run.plan_ts || run.path || "";
      var pathTitle = run.path || date;
      var pipelinePill = "";
      if (run.pipeline) {
        pipelinePill = '<span class="pill-pipeline" title="' + escapeHtml('This run executed pipeline ' + run.pipeline) + '">'
          + escapeHtml('pipeline: ' + run.pipeline)
          + '</span>';
      }
      return '<div class="card agent-run-card" tabindex="0" role="button"'
        + ' data-slug="' + escapeHtml(slug) + '"'
        + ' data-status="' + escapeHtml(status) + '"'
        + ' title="Click for DAG">'
        + '<h3>' + escapeHtml(slug || "(untitled run)") + '</h3>'
        + '<div class="path" title="' + escapeHtml(pathTitle) + '">' + escapeHtml(shortDisplayPath(date)) + '</div>'
        + '<div class="meta-row">'
        + '<span class="badge-dispatch">' + escapeHtml(String(count)) + ' dispatch' + (count === 1 ? "" : "es") + '</span>'
        + '<span class="pill-output" title="' + escapeHtml(output) + '">' + escapeHtml(truncate(output, 40)) + '</span>'
        + pipelinePill
        + '</div>'
        + '</div>';
    }).join("");
  }

  async function loadAgentOrchestrations() {
    wireAgentOrchestrationsOnce();
    renderRunsSkeletons();
    try {
      var r = await fetch(LIST_URL, { cache: "no-store" });
      if (!r.ok) throw new Error("HTTP " + r.status);
      var data = await r.json();
      _runsState.all = Array.isArray(data) ? data : (data.runs || []);
      if (!_runsState.all.length) {
        showRunsEmpty();
        return;
      }
      renderRunCards();
    } catch (e) {
      console.warn("[dashboard] agent orchestrations load failed:", e && e.message ? e.message : e);
      showRunsEmpty("Agent runs load failed: " + (e && e.message ? e.message : e));
    }
  }

  function renderMarkdownSafe(el, markdownText, fallbackText) {
    if (!el) return;
    try {
      if (typeof DOMPurify === "undefined" || typeof marked === "undefined") {
        throw new Error("markdown libs unavailable");
      }
      el.innerHTML = DOMPurify.sanitize(marked.parse(markdownText || ""));
    } catch (_) {
      el.textContent = fallbackText || markdownText || "";
    }
  }

  async function openRunDetail(slug) {
    if (!slug) return;
    var modal = qs("#agent-run-modal");
    if (!modal) return;
    var myEpoch = ++_runDetailEpoch;
    var cached = _runsState.all.find(function (run) { return run.task_slug === slug; }) || {};
    modal.hidden = false;
    if (typeof window.trapFocusInModal === "function") {
      window.trapFocusInModal(modal, closeRunDetail);
    }

    var titleEl = qs("#agent-run-modal-title");
    var metaEl = qs("#agent-run-modal-meta");
    var bodyEl = qs("#agent-run-modal-body");
    if (titleEl) titleEl.textContent = slug;
    if (metaEl) {
      var initial = [];
      if (cached.objective) initial.push("Objective: " + cached.objective);
      if (cached.output_hint) initial.push("Output: " + cached.output_hint);
      metaEl.textContent = initial.join("\n");
    }
    if (bodyEl) bodyEl.textContent = "loading...";

    try {
      var r = await fetch(DETAIL_URL(slug), { cache: "no-store" });
      if (myEpoch !== _runDetailEpoch) return;
      var data = await r.json().catch(function () { return {}; });
      if (!r.ok) throw new Error(data.error || ("HTTP " + r.status));
      if (myEpoch !== _runDetailEpoch) return;

      if (titleEl) titleEl.textContent = data.task_slug || slug;
      var objective = data.objective || "";
      var outputHint = data.output_hint || "";
      var headerMd = [
        objective ? "**Objective:** " + objective : "",
        outputHint ? "**Output:** " + outputHint : "",
      ].filter(Boolean).join("\n\n");
      var fallback = [
        objective ? "Objective: " + objective : "",
        outputHint ? "Output: " + outputHint : "",
      ].filter(Boolean).join("\n");
      renderMarkdownSafe(metaEl, headerMd, fallback);

      if (bodyEl) {
        bodyEl.replaceChildren();
        var svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
        svg.setAttribute("class", "dag-svg");
        svg.setAttribute("xmlns", "http://www.w3.org/2000/svg");
        bodyEl.appendChild(svg);
        renderDag(svg, data.dag || []);
      }
    } catch (e) {
      if (myEpoch !== _runDetailEpoch) return;
      if (bodyEl) {
        bodyEl.innerHTML = '<div class="err">Failed to load agent run: '
          + escapeHtml(e && e.message ? e.message : e)
          + '</div>';
      }
    }
  }

  function closeRunDetail() {
    var modal = qs("#agent-run-modal");
    if (modal) modal.hidden = true;
    if (typeof window.releaseFocusTrap === "function") {
      window.releaseFocusTrap();
    }
  }

  function normaliseNodeStatus(status) {
    var raw = String(status || "pending").toLowerCase();
    if (["success", "succeeded", "done", "complete", "completed"].indexOf(raw) >= 0) return "completed";
    if (["fail", "failed", "error", "errored"].indexOf(raw) >= 0) return "failed";
    if (["queued", "waiting", "blocked"].indexOf(raw) >= 0) return "pending";
    return raw || "pending";
  }

  function svgEl(name, attrs) {
    var el = document.createElementNS("http://www.w3.org/2000/svg", name);
    Object.keys(attrs || {}).forEach(function (key) {
      el.setAttribute(key, attrs[key]);
    });
    return el;
  }

  function textNode(x, y, value, attrs) {
    var el = svgEl("text", Object.assign({ x: x, y: y }, attrs || {}));
    el.textContent = String(value || "");
    return el;
  }

  function prepareDagNodes(dag) {
    var byId = Object.create(null);
    var nodes = [];
    (Array.isArray(dag) ? dag : []).forEach(function (node, idx) {
      var id = node && node.id != null ? String(node.id) : "";
      if (!id) {
        console.warn("[dashboard] agent-run DAG node missing id at index " + idx);
        return;
      }
      if (byId[id]) {
        console.warn("[dashboard] agent-run DAG duplicate node id ignored: " + id);
        return;
      }
      var copy = Object.assign({}, node, {
        id: id,
        _idx: nodes.length,
        depends_on: Array.isArray(node.depends_on) ? node.depends_on.map(String) : [],
      });
      byId[id] = copy;
      nodes.push(copy);
    });
    nodes.forEach(function (node) {
      node.depends_on = node.depends_on.filter(function (parentId) {
        if (byId[parentId]) return true;
        console.warn("[dashboard] agent-run DAG unknown parent dropped: " + parentId + " -> " + node.id);
        return false;
      });
    });
    return { byId: byId, nodes: nodes };
  }

  function computeTopoLayout(dag) {
    var prepared = prepareDagNodes(dag);
    var byId = prepared.byId;
    var nodes = prepared.nodes;
    var indegree = Object.create(null);
    var children = Object.create(null);
    var layer = Object.create(null);

    nodes.forEach(function (node) {
      indegree[node.id] = node.depends_on.length;
      children[node.id] = [];
      layer[node.id] = 0;
    });
    nodes.forEach(function (node) {
      node.depends_on.forEach(function (parentId) {
        children[parentId].push(node.id);
      });
    });

    var queue = nodes.filter(function (node) { return indegree[node.id] === 0; });
    var ordered = [];
    for (var q = 0; q < queue.length; q += 1) {
      var node = queue[q];
      ordered.push(node);
      children[node.id].forEach(function (childId) {
        layer[childId] = Math.max(layer[childId], layer[node.id] + 1);
        indegree[childId] -= 1;
        if (indegree[childId] === 0) queue.push(byId[childId]);
      });
    }
    if (ordered.length !== nodes.length) {
      return { cycle: true, nodes: nodes };
    }
    return { cycle: false, nodes: ordered, byId: byId, layer: layer };
  }

  function renderDagCycle(svgElRef, nodes) {
    var width = 360;
    var height = Math.max(80, 40 + nodes.length * 18);
    svgElRef.replaceChildren();
    svgElRef.setAttribute("viewBox", "0 0 " + width + " " + height);
    svgElRef.setAttribute("width", "100%");
    svgElRef.appendChild(textNode(20, 30, "cycle detected", { "data-error": "cycle" }));
    nodes.slice(0, 12).forEach(function (node, idx) {
      svgElRef.appendChild(textNode(20, 56 + idx * 18, node.id, { class: "dag-cycle-node" }));
    });
  }

  function renderDag(svgElRef, dag) {
    var topo = computeTopoLayout(dag);
    if (topo.cycle) {
      renderDagCycle(svgElRef, topo.nodes);
      return;
    }

    var NODE_W = 140;
    var NODE_H = 42;
    var LAYER_GAP = 60;
    var PAD = 20;
    var V_GAP = PAD;
    var nodes = topo.nodes;

    svgElRef.replaceChildren();
    if (!nodes.length) {
      svgElRef.setAttribute("viewBox", "0 0 360 80");
      svgElRef.setAttribute("width", "100%");
      svgElRef.appendChild(textNode(20, 42, "No DAG nodes", { class: "dag-empty" }));
      return;
    }

    var layers = [];
    nodes.forEach(function (node) {
      var idx = topo.layer[node.id] || 0;
      if (!layers[idx]) layers[idx] = [];
      layers[idx].push(node);
    });

    var maxNodesPerLayer = layers.reduce(function (max, layerNodes) {
      return Math.max(max, (layerNodes || []).length);
    }, 1);
    var width = PAD * 2 + layers.length * NODE_W + Math.max(0, layers.length - 1) * LAYER_GAP;
    var height = PAD * 2 + maxNodesPerLayer * NODE_H + Math.max(0, maxNodesPerLayer - 1) * V_GAP;
    var pos = Object.create(null);

    layers.forEach(function (layerNodes, layerIdx) {
      (layerNodes || []).forEach(function (node, nodeIdx) {
        var x = PAD + layerIdx * (NODE_W + LAYER_GAP);
        var y = PAD + nodeIdx * (NODE_H + V_GAP);
        pos[node.id] = { x: x, y: y, cx: x + NODE_W / 2, cy: y + NODE_H / 2, node: node };
      });
    });

    svgElRef.setAttribute("viewBox", "0 0 " + width + " " + height);
    svgElRef.setAttribute("width", "100%");

    nodes.forEach(function (node) {
      var end = pos[node.id];
      node.depends_on.forEach(function (parentId) {
        var start = pos[parentId];
        if (!start || !end) return;
        svgElRef.appendChild(svgEl("line", {
          class: "dag-edge",
          x1: start.cx + NODE_W / 2,
          y1: start.cy,
          x2: end.cx - NODE_W / 2,
          y2: end.cy,
        }));
      });
    });

    var tooltip = buildDagTooltip();
    nodes.forEach(function (node) {
      var p = pos[node.id];
      var group = svgEl("g", {
        class: "dag-node",
        "data-status": normaliseNodeStatus(node.status),
        "data-node-idx": String(node._idx),
        tabindex: "0",
        role: "button",
        focusable: "true",
        transform: "translate(" + p.x + " " + p.y + ")",
        "aria-label": "DAG node " + node.id,
      });
      group.appendChild(svgEl("rect", { width: NODE_W, height: NODE_H, rx: 6 }));
      group.appendChild(textNode(10, 26, truncate(node.id, 18)));
      group.addEventListener("pointerenter", function () { showDagTooltip(tooltip, node, p, width, height); });
      group.addEventListener("focus", function () { showDagTooltip(tooltip, node, p, width, height); });
      group.addEventListener("click", function () { showDagTooltip(tooltip, node, p, width, height); });
      group.addEventListener("pointerleave", function () { hideDagTooltip(tooltip); });
      group.addEventListener("blur", function () { hideDagTooltip(tooltip); });
      svgElRef.appendChild(group);
    });
    svgElRef.appendChild(tooltip);
  }

  function buildDagTooltip() {
    var tooltip = svgEl("g", { class: "dag-tooltip", hidden: "" });
    tooltip.appendChild(svgEl("rect", {
      class: "dag-tooltip-bg",
      x: 0,
      y: 0,
      width: 10,
      height: 10,
      rx: 6,
      fill: "var(--bg)",
      stroke: "var(--border-strong)",
    }));
    return tooltip;
  }

  function tooltipLines(node) {
    return [
      "subtask: " + (node.subtask || node.task || node.id || "-"),
      "expected_output: " + (node.expected_output || "-"),
      "agent: " + (node.agent || "-"),
    ];
  }

  function showDagTooltip(tooltip, node, p, svgWidth, svgHeight) {
    if (!tooltip) return;
    var lines = tooltipLines(node);
    var maxChars = lines.reduce(function (max, line) { return Math.max(max, line.length); }, 0);
    var boxW = Math.min(320, Math.max(170, maxChars * 7 + 18));
    var boxH = lines.length * 16 + 14;
    var x = p.x + 150;
    var y = p.y;
    if (x + boxW > svgWidth - 8) x = Math.max(8, p.x - boxW - 10);
    if (y + boxH > svgHeight - 8) y = Math.max(8, svgHeight - boxH - 8);

    tooltip.setAttribute("transform", "translate(" + x + " " + y + ")");
    tooltip.removeAttribute("hidden");
    var bg = tooltip.querySelector("rect");
    if (bg) {
      bg.setAttribute("width", boxW);
      bg.setAttribute("height", boxH);
    }
    Array.from(tooltip.querySelectorAll("text")).forEach(function (el) {
      el.remove();
    });
    lines.forEach(function (line, idx) {
      tooltip.appendChild(textNode(9, 20 + idx * 16, truncate(line, 44), { class: "dag-tooltip-line" }));
    });
  }

  function hideDagTooltip(tooltip) {
    if (tooltip) tooltip.setAttribute("hidden", "");
  }

  function activateRunCard(card) {
    if (!card) return;
    openRunDetail(card.dataset.slug);
  }

  function wireAgentOrchestrationsOnce() {
    var view = qs("#view-agent-orchestrations");
    var grid = qs("#agent-runs-grid");
    var modal = qs("#agent-run-modal");
    if (view && view.dataset.wired !== "1") {
      view.dataset.wired = "1";
    }
    if (grid && grid.dataset.wired !== "1") {
      grid.addEventListener("click", function (e) {
        var card = e.target.closest(".agent-run-card[data-slug]");
        if (!card || !grid.contains(card)) return;
        activateRunCard(card);
      });
      grid.addEventListener("keydown", function (e) {
        if (e.key !== "Enter" && e.key !== " ") return;
        var card = e.target.closest(".agent-run-card[data-slug]");
        if (!card || !grid.contains(card)) return;
        e.preventDefault();
        activateRunCard(card);
      });
      grid.dataset.wired = "1";
    }
    if (modal && modal.dataset.wired !== "1") {
      Array.from(modal.querySelectorAll("#agent-run-modal-close, .modal-close")).forEach(function (btn) {
        btn.addEventListener("click", closeRunDetail);
      });
      modal.addEventListener("click", function (e) {
        if (e.target === modal) closeRunDetail();
      });
      document.addEventListener("keydown", function (e) {
        if (e.key !== "Escape") return;
        var current = qs("#agent-run-modal");
        if (current && !current.hidden) closeRunDetail();
      });
      modal.dataset.wired = "1";
    }
  }

  function initAgentOrchestrations() {
    wireAgentOrchestrationsOnce();
    var view = qs("#view-agent-orchestrations");
    if (view && view.classList.contains("active")) {
      loadAgentOrchestrations();
    }
  }

  window.loadAgentOrchestrations = loadAgentOrchestrations;

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initAgentOrchestrations);
  } else {
    initAgentOrchestrations();
  }
})();
