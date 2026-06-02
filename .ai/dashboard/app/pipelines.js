// .ai/dashboard/app/pipelines.js -- pipeline list view + canvas pipeline editor.
// Surface: window.loadPipelines, window.openPipelineEditor.
// Self-inits on DOMContentLoaded (dataset.wired='1' idempotency).

(function () {
  var LIST_URL = "/api/pipelines";
  var DETAIL_URL = function (slug) { return "/api/pipelines/" + encodeURIComponent(slug); };
  var CATALOG_URL = "/api/agents/all";
  var SLUG_RE = /^[a-z0-9-]+$/;

  var _state = { all: [], catalog: [] };
  var _editorState = { slug: null, description: "", nodes: [] };
  var SINK_KINDS = ["synthesize", "collect", "passthrough"];
  var _loadEpoch = 0;
  var _catalogLoaded = false;

  // ----- DOM helpers --------------------------------------------------------
  function qs(sel, root) { return (root || document).querySelector(sel); }
  function qsa(sel, root) { return Array.from((root || document).querySelectorAll(sel)); }
  function setText(el, v) { if (el) el.textContent = v == null ? "" : String(v); }
  function clear(el) { if (!el) return; while (el.firstChild) el.removeChild(el.firstChild); }
  function el(tag, opts) {
    var n = document.createElement(tag);
    if (!opts) return n;
    if (opts.className) n.className = opts.className;
    if (opts.text != null) n.textContent = String(opts.text);
    if (opts.attrs) Object.keys(opts.attrs).forEach(function (k) {
      var v = opts.attrs[k]; if (v != null) n.setAttribute(k, String(v));
    });
    return n;
  }

  // ----- List view ----------------------------------------------------------
  function renderPipelineSkeletons() {
    var grid = qs("#pipelines-grid"), empty = qs(".pipelines-empty");
    if (empty) empty.hidden = true;
    if (!grid || grid.dataset.skeletoned) return;
    grid.hidden = false;
    var html = "";
    for (var i = 0; i < 4; i += 1) {
      html += '<div class="card pipeline-card skeleton-pipeline-card">'
        + '<span class="skeleton skeleton-h"></span>'
        + '<span class="skeleton skeleton-desc-1"></span>'
        + '<span class="skeleton skeleton-tools"></span></div>';
    }
    grid.innerHTML = html; // trusted constant
    grid.dataset.skeletoned = "1";
  }

  function showEmpty() {
    var grid = qs("#pipelines-grid"), empty = qs(".pipelines-empty");
    if (grid) { grid.hidden = true; clear(grid); delete grid.dataset.skeletoned; }
    if (empty) empty.hidden = false;
  }

  function renderCards() {
    var grid = qs("#pipelines-grid");
    if (!grid) return;
    var empty = qs(".pipelines-empty");
    if (empty) empty.hidden = true;
    grid.hidden = false;
    delete grid.dataset.skeletoned;
    clear(grid);
    _state.all.forEach(function (row) {
      var slug = String(row.slug || "");
      var card = el("div", {
        className: "pipeline-card",
        attrs: { "data-slug": slug, tabindex: "0", role: "button", title: "Click to edit" },
      });
      var title = el("div", { className: "pipeline-card-title" });
      setText(title, slug || "(untitled)");
      card.appendChild(title);
      var desc = el("div", { className: "pipeline-card-desc" });
      setText(desc, row.description || "—");
      card.appendChild(desc);
      var meta = el("div", { className: "pipeline-card-meta" });
      meta.appendChild(el("span", {
        className: "badge-shape", text: String(row.shape || "linear"),
        attrs: { title: "pipeline shape" },
      }));
      meta.appendChild(el("span", {
        className: "badge-shape",
        text: (row.node_count || 0) + " node" + (row.node_count === 1 ? "" : "s"),
        attrs: { title: "node count", "data-kind": "count" },
      }));
      if (row.output_mode) meta.appendChild(el("span", {
        className: "badge-shape", text: row.output_mode,
        attrs: { title: "output mode", "data-mode": String(row.output_mode) },
      }));
      card.appendChild(meta);
      var actions = el("div", { className: "pipeline-card-actions" });
      [["Edit", "pipeline-action-edit"], ["Run", "pipeline-action-run"],
       ["Delete", "pipeline-action-delete"]].forEach(function (pair) {
        actions.appendChild(el("button", {
          className: "refresh " + pair[1], text: pair[0],
          attrs: { type: "button", "data-slug": slug },
        }));
      });
      card.appendChild(actions);
      grid.appendChild(card);
    });
  }

  async function loadPipelines() {
    wirePipelinesOnce();
    renderPipelineSkeletons();
    try {
      var r = await fetch(LIST_URL, { cache: "no-store" });
      if (!r.ok) throw new Error("HTTP " + r.status);
      var data = await r.json();
      var rows = Array.isArray(data) ? data : (data.pipelines || []);
      _state.all = rows;
      var c = qs("#count-pipelines"); if (c) setText(c, String(rows.length));
      if (!rows.length) { showEmpty(); return; }
      renderCards();
    } catch (e) {
      console.warn("[dashboard] pipelines load failed:",
        e && e.message ? e.message : e);
      showEmpty();
      var grid = qs("#pipelines-grid");
      if (grid) {
        grid.hidden = false; clear(grid);
        var err = el("div", { className: "err" });
        setText(err, "Pipelines load failed: " + (e && e.message ? e.message : e));
        grid.appendChild(err);
      }
    }
  }

  // ----- Catalog ------------------------------------------------------------
  async function loadCatalog(force) {
    if (_catalogLoaded && !force) return;
    try {
      var r = await fetch(CATALOG_URL, { cache: "no-store" });
      if (!r.ok) throw new Error("HTTP " + r.status);
      var data = await r.json();
      var raw = Array.isArray(data) ? data : (data.agents || []);
      _state.catalog = raw.map(function (a) {
        return {
          name: a.name || "",
          subagent_type: a.subagent_type || a.name || "",
          source: a.source || "",
          source_label: a.source_label || a.source || "",
          model: a.model || "",
          tools: a.tools || "",
          description: a.description || "",
          path: a.path || "",
        };
      });
      _catalogLoaded = true;
      renderCatalogList();
    } catch (e) {
      console.warn("[dashboard] pipeline catalog load failed:",
        e && e.message ? e.message : e);
      var list = qs(".pipeline-catalog-list");
      if (list) {
        clear(list);
        var err = el("div", { className: "err" });
        setText(err, "Catalog load failed: " + (e && e.message ? e.message : e));
        list.appendChild(err);
      }
    }
  }

  function renderCatalogList() {
    var list = qs(".pipeline-catalog-list");
    if (!list) return;
    var search = qs(".pipeline-catalog-search");
    var q = (search && search.value || "").trim().toLowerCase();
    clear(list);
    _state.catalog
      .filter(function (a) {
        if (!q) return true;
        return (a.name || "").toLowerCase().indexOf(q) >= 0
          || (a.description || "").toLowerCase().indexOf(q) >= 0;
      })
      .slice(0, 200)
      .forEach(function (a) {
        var item = el("div", {
          className: "catalog-item",
          attrs: {
            role: "button",
            tabindex: "0",
            "data-skill-id": a.subagent_type || a.name,
            "data-subagent-type": a.subagent_type || a.name,
            "data-name": a.name,
          },
        });
        item._agent = a;
        // Pointer-based drag (replaces native HTML5 DnD). Native DnD draws an
        // OS cursor (no-drop / copy) that ignores CSS `cursor`, so the drag
        // never matched the "Targeting HUD" cursor set. Pointer events let us
        // hold the grabbing cursor for the whole drag (forced globally via
        // body.pipeline-drag-active) and render a custom ghost chasing the
        // pointer. Enter/Space drops the node onto the canvas — keyboard
        // access that native DnD never offered.
        item.addEventListener("pointerdown", function (e) {
          startCatalogDrag(e, item, a);
        });
        item.addEventListener("keydown", function (e) {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            addNodeAtCanvasCenter(item.getAttribute("data-skill-id") || "");
          }
        });
        item.addEventListener("mouseenter", function () { showAgentTooltip(item, a); });
        item.addEventListener("mouseleave", function () { scheduleHideAgentTooltip(); });
        item.addEventListener("focus", function () { showAgentTooltip(item, a); });
        item.addEventListener("blur", function () { scheduleHideAgentTooltip(); });
        var n = el("span", { className: "catalog-item-name" });
        setText(n, a.name || "(unnamed)");
        item.appendChild(n);
        if (a.source_label || a.source) {
          var p = el("span", { className: "pill" });
          setText(p, a.source_label || a.source);
          item.appendChild(p);
        }
        list.appendChild(item);
      });
  }

  // ----- Catalog hover tooltip ---------------------------------------------
  var _tooltipEl = null;
  var _tooltipAnchor = null;  // current item the tooltip is attached to
  var _tooltipHideTimer = null;
  var TOOLTIP_HIDE_DELAY = 220;
  var TOOLTIP_MAX_DESC = 280;

  function ensureAgentTooltip() {
    if (_tooltipEl && document.body.contains(_tooltipEl)) return _tooltipEl;
    _tooltipEl = el("div", {
      className: "catalog-tooltip",
      attrs: { role: "tooltip", "aria-hidden": "true" },
    });
    _tooltipEl.hidden = true;
    _tooltipEl.addEventListener("mouseenter", cancelHideAgentTooltip);
    _tooltipEl.addEventListener("mouseleave", scheduleHideAgentTooltip);
    document.body.appendChild(_tooltipEl);
    return _tooltipEl;
  }

  function showAgentTooltip(item, a) {
    if (!a || !item) return;
    // Suppress tooltips while an agent is being dragged — hovering/focusing
    // other catalog items mid-drag should not pop their info boxes.
    if (document.body.classList.contains("pipeline-drag-active")) return;
    cancelHideAgentTooltip();
    var tip = ensureAgentTooltip();
    _tooltipAnchor = item;
    clear(tip);
    var head = el("div", { className: "catalog-tooltip-head" });
    head.appendChild(el("span", {
      className: "catalog-tooltip-name", text: a.name || "(unnamed)",
    }));
    if (a.source_label || a.source) {
      head.appendChild(el("span", {
        className: "catalog-tooltip-scope", text: a.source_label || a.source,
      }));
    }
    tip.appendChild(head);
    function addMeta(label, value) {
      if (!value) return;
      var row = el("div", { className: "catalog-tooltip-row" });
      row.appendChild(el("span", { className: "catalog-tooltip-key", text: label }));
      row.appendChild(el("span", { className: "catalog-tooltip-val", text: String(value) }));
      tip.appendChild(row);
    }
    addMeta("model", a.model);
    var toolsText = "";
    if (typeof a.tools === "string") toolsText = a.tools;
    else if (Array.isArray(a.tools)) toolsText = a.tools.join(", ");
    addMeta("tools", toolsText);
    addMeta("subagent_type", a.subagent_type && a.subagent_type !== a.name ? a.subagent_type : "");
    if (a.description) {
      appendAgentTooltipDesc(tip, a);
    } else if (a.path && typeof window.openAgentDetail === "function") {
      appendAgentTooltipMore(tip, a);
    }
    tip.hidden = false;
    tip.setAttribute("aria-hidden", "false");
    positionAgentTooltip(item, tip);
  }

  function appendAgentTooltipDesc(tip, a) {
    var full = String(a.description).replace(/\s+/g, " ").trim();
    var truncated = full.length > TOOLTIP_MAX_DESC
      ? full.slice(0, TOOLTIP_MAX_DESC - 1).trimEnd() + "…"
      : full;
    var box = el("div", { className: "catalog-tooltip-desc" });
    box.textContent = truncated;
    tip.appendChild(box);
    appendAgentTooltipMore(tip, a);
  }

  function appendAgentTooltipMore(tip, a) {
    // "see details" opens the existing #agent-detail-modal (defined in
    // agents.js). Only render the link if the modal helper is wired and the
    // agent has a path the backend can stream content from.
    if (!a.path || typeof window.openAgentDetail !== "function") return;
    var btn = el("button", {
      className: "catalog-tooltip-more",
      text: "see details",
      attrs: { type: "button" },
    });
    btn.addEventListener("click", function (e) {
      e.preventDefault();
      e.stopPropagation();
      hideAgentTooltip();
      try { window.openAgentDetail(a.path, a.name, a.source); }
      catch (err) {
        console.warn("[dashboard] openAgentDetail failed:",
          err && err.message ? err.message : err);
      }
    });
    tip.appendChild(btn);
  }

  function positionAgentTooltip(item, tip) {
    var rect = item.getBoundingClientRect();
    // Render at top-left first (off-screen-ish) to measure
    tip.style.left = "-9999px"; tip.style.top = "-9999px";
    var w = tip.offsetWidth;
    var h = tip.offsetHeight;
    var margin = 10;
    var x = rect.right + margin;
    var y = rect.top - 4;
    if (x + w > window.innerWidth - margin) {
      // Place to the left of the item if overflow
      x = rect.left - w - margin;
    }
    if (x < margin) x = margin;
    if (y + h > window.innerHeight - margin) {
      y = window.innerHeight - h - margin;
    }
    if (y < margin) y = margin;
    tip.style.left = x + "px";
    tip.style.top = y + "px";
  }

  function scheduleHideAgentTooltip() {
    cancelHideAgentTooltip();
    _tooltipHideTimer = setTimeout(hideAgentTooltip, TOOLTIP_HIDE_DELAY);
  }

  function cancelHideAgentTooltip() {
    if (_tooltipHideTimer) { clearTimeout(_tooltipHideTimer); _tooltipHideTimer = null; }
  }

  function hideAgentTooltip() {
    cancelHideAgentTooltip();
    if (!_tooltipEl) return;
    _tooltipEl.hidden = true;
    _tooltipEl.setAttribute("aria-hidden", "true");
    _tooltipAnchor = null;
  }

  // On catalog scroll: reposition tooltip to follow the anchor if still in
  // viewport; hide if the anchor scrolled out (so a stale tooltip doesn't
  // hover next to nothing).
  function repositionTooltipOnScroll() {
    if (!_tooltipEl || _tooltipEl.hidden || !_tooltipAnchor) return;
    if (!document.body.contains(_tooltipAnchor)) { hideAgentTooltip(); return; }
    var r = _tooltipAnchor.getBoundingClientRect();
    if (r.bottom < 0 || r.top > window.innerHeight) { hideAgentTooltip(); return; }
    positionAgentTooltip(_tooltipAnchor, _tooltipEl);
  }

  // ----- Editor: open / close ----------------------------------------------
  function resetEditorState() {
    _editorState.slug = null;
    _editorState.description = "";
    _editorState.nodes = [];
  }

  function seedFlowNodes() {
    _editorState.nodes = [
      { id: "input", kind: "input", x: PAD, y: PAD },
      { id: "output", kind: "synthesize",
        x: PAD + 2 * (NODE_W + LAYER_GAP), y: PAD, depends_on: [] },
    ];
  }

  function closePipelineEditor() {
    var modal = qs("#pipeline-editor-modal");
    if (modal) modal.hidden = true;
    hideAgentTooltip();
    if (typeof window.releaseFocusTrap === "function") window.releaseFocusTrap();
  }

  async function openPipelineEditor(slug) {
    var modal = qs("#pipeline-editor-modal");
    if (!modal) return;
    wirePipelinesOnce();
    var myEpoch = ++_loadEpoch;
    resetEditorState();
    _zoom = 1.0; _pan = { x: 0, y: 0 };
    loadCatalog().catch(function () {});
    modal.hidden = false;
    if (typeof window.trapFocusInModal === "function") {
      window.trapFocusInModal(modal, closePipelineEditor);
    }
    setText(qs("#pipeline-editor-title"),
      slug ? ("Edit pipeline · " + slug) : "New pipeline");
    setText(qs(".pipeline-editor-msg"), "");
    var errBox = qs(".pipeline-errors");
    if (errBox) { errBox.hidden = true; clear(errBox); }

    if (slug) {
      _editorState.slug = String(slug);
      try {
        var r = await fetch(DETAIL_URL(slug), { cache: "no-store" });
        if (myEpoch !== _loadEpoch) return;
        var data = await r.json().catch(function () { return {}; });
        if (!r.ok) throw new Error(data.error || ("HTTP " + r.status));
        if (myEpoch !== _loadEpoch) return;
        _editorState.description = typeof data.description === "string" ? data.description : "";
        var nodes = Array.isArray(data.nodes) ? data.nodes : [];
        _editorState.nodes = nodes.map(function (n) {
          var c = { id: String(n && n.id != null ? n.id : "") };
          if (n && typeof n.kind === "string") c.kind = n.kind;
          else c.agent = String(n && n.agent != null ? n.agent : "");
          if (n && Array.isArray(n.depends_on)) c.depends_on = n.depends_on.map(String);
          return c;
        });
        populateEditorFromState();
      } catch (e) {
        if (myEpoch !== _loadEpoch) return;
        renderEditorErrors(["Failed to load pipeline: "
          + (e && e.message ? e.message : e)]);
      }
    } else {
      seedFlowNodes();
      populateEditorFromState();
    }
  }

  function populateEditorFromState() {
    var d = qs(".pipeline-description"); if (d) d.value = _editorState.description || "";
    renderNodes();
    validateEditor();
  }

  // ----- Editor: canvas rendering ------------------------------------------
  function renderNodes() {
    var canvasEl = qs(".pipeline-canvas");
    if (canvasEl) canvasEl.hidden = false;
    renderCanvas();
  }

  // ----- SVG DAG editor -----------------------------------------------------
  var SVG_NS = "http://www.w3.org/2000/svg";
  var NODE_W = 210, NODE_H = 64, LAYER_GAP = 70, PAD = 24, V_GAP = 24;
  // Snap radius (screen px) for the wire-drag drop target. The cursor doesn't
  // have to land exactly on the IN port circle — anywhere within this radius
  // of a port counts. Cursor inside any node group also snaps to that node's
  // IN port (bypasses the radius — see findSnappedInPort).
  var WIRE_SNAP_RADIUS = 60;
  // Hover-OUT delay (ms): when the cursor leaves a hover target, wait this
  // long before clearing the highlight. Prevents flicker when the cursor
  // crosses tiny gaps between snap-eligible elements (e.g. node body to port).
  var WIRE_HOVER_UNSET_DELAY = 80;
  var CANVAS_MIN_W = 820, CANVAS_MIN_H = 360;
  var PORT_R = 6;
  // Zoom bounds + step. Wheel uses ZOOM_WHEEL_STEP; buttons use ZOOM_BTN_STEP.
  var MIN_ZOOM = 0.4, MAX_ZOOM = 2.5;
  var ZOOM_WHEEL_STEP = 1.1, ZOOM_BTN_STEP = 1.25;
  // Per-render canvas state; reset every renderCanvas() invocation.
  var _canvasDrag = null;
  var _canvasEdgeDraft = null;
  var _canvasSvgRef = null;
  var _canvasPos = null;
  var _canvasViewBox = { width: CANVAS_MIN_W, height: CANVAS_MIN_H };
  // Zoom + pan applied to the natural viewBox. Reset on each openPipelineEditor.
  var _zoom = 1.0;
  var _pan = { x: 0, y: 0 };

  function svgEl(name, attrs) {
    var node = document.createElementNS(SVG_NS, name);
    if (attrs) Object.keys(attrs).forEach(function (k) {
      var v = attrs[k]; if (v != null) node.setAttribute(k, String(v));
    });
    return node;
  }

  function numberOrNull(v) {
    return (typeof v === "number" && isFinite(v)) ? v : null;
  }

  function edgePath(x1, y1, x2, y2) {
    var dx = Math.max(50, Math.abs(x2 - x1) * 0.45);
    return "M " + x1 + " " + y1
      + " C " + (x1 + dx) + " " + y1
      + ", " + (x2 - dx) + " " + y2
      + ", " + x2 + " " + y2;
  }

  function updateNodeReferences(oldId, newId) {
    if (!oldId || oldId === newId) return;
    _editorState.nodes.forEach(function (n) {
      if (!n || !Array.isArray(n.depends_on)) return;
      n.depends_on = n.depends_on.map(function (d) { return d === oldId ? newId : d; });
    });
  }

  // Topo-sort via Kahn's algorithm; returns {cycle:true} or layered layout.
  function computeCanvasLayout(rawNodes) {
    var byId = Object.create(null), nodes = [], refsById = Object.create(null);
    (rawNodes || []).forEach(function (ref) {
      if (!ref || typeof ref.id !== "string" || !ref.id || byId[ref.id]) return;
      var copy = { id: ref.id, agent: ref.agent || "",
        depends_on: Array.isArray(ref.depends_on) ? ref.depends_on.map(String) : [],
        ref: ref };
      byId[ref.id] = copy; refsById[ref.id] = ref; nodes.push(copy);
    });
    // Validate depends_on refs; drop unknowns with warn.
    nodes.forEach(function (n) {
      n.depends_on = n.depends_on.filter(function (p) {
        if (byId[p]) return true;
        console.warn("[dashboard] pipeline canvas: unknown depends_on '" + p + "' on '" + n.id + "'");
        return false;
      });
    });
    // Kahn: in_deg seeded from depends_on length.
    var inDeg = Object.create(null), children = Object.create(null);
    nodes.forEach(function (n) { inDeg[n.id] = n.depends_on.length; children[n.id] = []; });
    nodes.forEach(function (n) {
      n.depends_on.forEach(function (p) { children[p].push(n.id); });
    });
    var ready = nodes.filter(function (n) { return inDeg[n.id] === 0; }), ordered = [];
    for (var q = 0; q < ready.length; q += 1) {
      var n = ready[q]; ordered.push(n);
      children[n.id].forEach(function (cid) {
        inDeg[cid] -= 1;
        if (inDeg[cid] === 0) ready.push(byId[cid]);
      });
    }
    if (ordered.length !== nodes.length) return { cycle: true, nodes: nodes };
    // Layer assignment: 0 for roots, else max(parent.layer)+1.
    var layer = Object.create(null);
    ordered.forEach(function (n) {
      if (!n.depends_on.length) { layer[n.id] = 0; return; }
      var m = 0;
      n.depends_on.forEach(function (p) { if (layer[p] + 1 > m) m = layer[p] + 1; });
      layer[n.id] = m;
    });
    return { cycle: false, nodes: ordered, byId: byId, refsById: refsById, layer: layer };
  }

  // Apply natural viewBox + zoom-scaled pixel size so overflow:auto on the
  // .pipeline-canvas div handles panning at any zoom. getScreenCTM().inverse()
  // (used by svgPointFromEvent) reflects the CSS pixel size so drag/drop math
  // works at every zoom level.
  function applyZoom(svg, naturalW, naturalH) {
    _canvasViewBox = { width: naturalW, height: naturalH };
    svg.setAttribute("viewBox", "0 0 " + naturalW + " " + naturalH);
    svg.style.width = (naturalW * _zoom) + "px";
    svg.style.height = (naturalH * _zoom) + "px";
    refreshZoomIndicator();
  }

  function refreshZoomIndicator() {
    var ind = qs(".pipeline-canvas-zoom-level");
    if (ind) setText(ind, Math.round(_zoom * 100) + "%");
  }

  function clampZoom(z) {
    return Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, z));
  }

  function resetZoom() {
    _zoom = 1.0; _pan = { x: 0, y: 0 };
    if (_canvasSvgRef) applyZoom(_canvasSvgRef, _canvasViewBox.width, _canvasViewBox.height);
    var canvasEl = qs(".pipeline-canvas");
    if (canvasEl) { canvasEl.scrollLeft = 0; canvasEl.scrollTop = 0; }
    refreshZoomIndicator();
  }

  // Zoom centered on canvas viewport center (used by + / − buttons).
  function zoomByFactor(factor) {
    var canvasEl = qs(".pipeline-canvas");
    var svg = _canvasSvgRef;
    if (!canvasEl || !svg) return;
    var prevZoom = _zoom;
    var nextZoom = clampZoom(prevZoom * factor);
    if (nextZoom === prevZoom) return;
    // Pivot at viewport center
    var cx = canvasEl.clientWidth / 2 + canvasEl.scrollLeft;
    var cy = canvasEl.clientHeight / 2 + canvasEl.scrollTop;
    var ratio = nextZoom / prevZoom;
    _zoom = nextZoom;
    applyZoom(svg, _canvasViewBox.width, _canvasViewBox.height);
    canvasEl.scrollLeft = cx * ratio - canvasEl.clientWidth / 2;
    canvasEl.scrollTop = cy * ratio - canvasEl.clientHeight / 2;
  }

  // Zoom anchored to cursor (used by wheel).
  function zoomAtCursor(factor, ev) {
    var canvasEl = qs(".pipeline-canvas");
    var svg = _canvasSvgRef;
    if (!canvasEl || !svg) return;
    var prevZoom = _zoom;
    var nextZoom = clampZoom(prevZoom * factor);
    if (nextZoom === prevZoom) return;
    var rect = canvasEl.getBoundingClientRect();
    // Cursor position within the scrollable canvas content (pre-zoom)
    var cx = ev.clientX - rect.left + canvasEl.scrollLeft;
    var cy = ev.clientY - rect.top + canvasEl.scrollTop;
    var ratio = nextZoom / prevZoom;
    _zoom = nextZoom;
    applyZoom(svg, _canvasViewBox.width, _canvasViewBox.height);
    // Keep cursor anchored over the same logical point
    canvasEl.scrollLeft = cx * ratio - (ev.clientX - rect.left);
    canvasEl.scrollTop  = cy * ratio - (ev.clientY - rect.top);
  }

  function renderCanvas() {
    var canvasEl = qs(".pipeline-canvas");
    if (!canvasEl) return;
    var svg = qs("svg.dag-svg", canvasEl);
    if (!svg) {
      svg = svgEl("svg", { class: "dag-svg", "aria-label": "Pipeline DAG" });
      clear(canvasEl); canvasEl.appendChild(svg);
    }
    while (svg.firstChild) svg.removeChild(svg.firstChild);
    _canvasDrag = null; _canvasEdgeDraft = null;
    wireCanvasZoom(canvasEl);
    if (svg.dataset.wired !== "1") { // wire SVG-level pointer events once
      svg.addEventListener("pointermove", onCanvasPointerMove);
      svg.addEventListener("pointerup", onCanvasPointerUp);
      svg.addEventListener("pointercancel", onCanvasPointerUp);
      // Click on an edge's invisible hit overlay removes the connection.
      svg.addEventListener("click", onCanvasClick);
      svg.dataset.wired = "1";
    }
    _canvasSvgRef = svg; _canvasPos = null;
    var topo = computeCanvasLayout(_editorState.nodes);
    if (topo.cycle) {
      applyZoom(svg, CANVAS_MIN_W, CANVAS_MIN_H);
      var err = svgEl("text", { x: 20, y: 30, "data-error": "cycle" });
      err.textContent = "cycle detected";
      svg.appendChild(err); return;
    }
    var nodes = topo.nodes;
    if (!nodes.length) {
      applyZoom(svg, CANVAS_MIN_W, CANVAS_MIN_H);
      var empty = svgEl("text", { x: 24, y: 44, class: "dag-empty" });
      empty.textContent = "Drag an agent here to begin";
      svg.appendChild(empty); return;
    }

    // Group by layer; layout L→R per layer, T→B within layer.
    var layers = [];
    nodes.forEach(function (n) {
      var L = topo.layer[n.id] || 0;
      if (!layers[L]) layers[L] = [];
      layers[L].push(n);
    });
    var maxPerLayer = layers.reduce(function (m, lyr) { return Math.max(m, (lyr || []).length); }, 1);
    var width = PAD * 2 + layers.length * NODE_W + Math.max(0, layers.length - 1) * LAYER_GAP;
    var height = PAD * 2 + maxPerLayer * NODE_H + Math.max(0, maxPerLayer - 1) * V_GAP;
    var pos = Object.create(null);
    layers.forEach(function (layerNodes, layerIdx) {
      (layerNodes || []).forEach(function (n, idx) {
        var ref = n.ref || n;
        var x = numberOrNull(ref.x);
        var y = numberOrNull(ref.y);
        if (x == null) x = layerIdx * (NODE_W + LAYER_GAP) + PAD;
        if (y == null) y = idx * (NODE_H + V_GAP) + PAD;
        ref.x = x; ref.y = y;
        pos[n.id] = { x: x, y: y, cx: x + NODE_W / 2, cy: y + NODE_H / 2, node: ref };
      });
    });
    _canvasPos = pos;
    Object.keys(pos).forEach(function (id) {
      width = Math.max(width, pos[id].x + NODE_W + PAD);
      height = Math.max(height, pos[id].y + NODE_H + PAD);
    });
    width = Math.max(width, CANVAS_MIN_W);
    height = Math.max(height, CANVAS_MIN_H);
    applyZoom(svg, width, height);
    // Edges first so nodes paint on top. Each edge is two paths: a wide
    // transparent hit overlay (captures clicks) and a thin visible line. The
    // hit path's stroke-width is ~16 screen px so the edge is comfortable to
    // click without changing its visual weight.
    nodes.forEach(function (n) {
      var end = pos[n.id];
      n.depends_on.forEach(function (p) {
        var start = pos[p]; if (!start || !end) return;
        var d = edgePath(start.x + NODE_W, start.y + NODE_H / 2,
          end.x, end.y + NODE_H / 2);
        svg.appendChild(svgEl("path", {
          class: "dag-edge-hit", "data-from": p, "data-to": n.id, d: d,
        }));
        svg.appendChild(svgEl("path", {
          class: "dag-edge", "data-from": p, "data-to": n.id, d: d,
        }));
      });
    });
    // Nodes with drag + port circles (left=in, right=out).
    nodes.forEach(function (n) {
      var p = pos[n.id];
      var node = p.node;
      var g = svgEl("g", {
        class: "dag-node" + (node.kind ? " dag-node-flow" : ""), "data-id": node.id,
        tabindex: "0",
        transform: "translate(" + p.x + " " + p.y + ")",
        "aria-label": "Pipeline node " + node.id,
      });
      g.appendChild(svgEl("rect", { class: "dag-node-rect", width: NODE_W, height: NODE_H, rx: 6 }));
      var fo = svgEl("foreignObject", { x: 10, y: 9, width: NODE_W - 20, height: NODE_H - 18 });
      var body = el("div", { className: "dag-node-body" });
      if (node.kind === "input") {
        var lbl = el("span", { className: "dag-node-fixed-label", text: "Input" });
        body.appendChild(lbl);
      } else if (node.kind && SINK_KINDS.indexOf(node.kind) >= 0) {
        var sel = el("select", { className: "dag-sink-kind dag-node-control" });
        SINK_KINDS.forEach(function (k) {
          sel.appendChild(el("option", { attrs: { value: k }, text: k }));
        });
        sel.value = node.kind;
        sel.addEventListener("change", function () {
          node.kind = sel.value || "synthesize";
          validateEditor();
        });
        body.appendChild(sel);
      } else {
        var input = el("input", {
          className: "dag-node-label dag-node-control",
          attrs: { type: "text", "aria-label": "Node id", value: node.id || "" },
        });
        input.value = node.id || "";
        input.addEventListener("input", function () {
          var oldId = node.id || "";
          node.id = input.value || "";
          updateNodeReferences(oldId, node.id);
          g.setAttribute("data-id", node.id);
          qsa(".dag-port", g).forEach(function (port) { port.setAttribute("data-id", node.id); });
          if (_canvasPos && oldId && _canvasPos[oldId]) {
            _canvasPos[node.id] = _canvasPos[oldId];
            delete _canvasPos[oldId];
          }
          validateEditor();
        });
        var del = el("button", {
          className: "dag-node-delete dag-node-control",
          text: "x",
          attrs: { type: "button", "aria-label": "Delete node" },
        });
        del.addEventListener("click", function () { deleteNodeByRef(node); });
        body.appendChild(input);
        body.appendChild(del);
      }
      fo.appendChild(body);
      g.appendChild(fo);
      if (node.kind !== "input") {
        g.appendChild(svgEl("circle", { class: "dag-port dag-port-in", "data-kind": "in",
          "data-id": node.id, cx: 0, cy: NODE_H / 2, r: PORT_R }));
      }
      if (!(node.kind && SINK_KINDS.indexOf(node.kind) >= 0)) {
        g.appendChild(svgEl("circle", { class: "dag-port dag-port-out", "data-kind": "out",
          "data-id": node.id, cx: NODE_W, cy: NODE_H / 2, r: PORT_R }));
      }
      g.addEventListener("pointerdown", function (e) { onCanvasPointerDown(e, g, node); });
      svg.appendChild(g);
    });
  }

  function svgPointFromEvent(svg, e) {
    var pt = svg.createSVGPoint ? svg.createSVGPoint() : null;
    if (!pt) {
      var rect = svg.getBoundingClientRect();
      return { x: e.clientX - rect.left, y: e.clientY - rect.top };
    }
    pt.x = e.clientX; pt.y = e.clientY;
    var ctm = svg.getScreenCTM();
    if (!ctm) return { x: e.clientX, y: e.clientY };
    var loc = pt.matrixTransform(ctm.inverse());
    return { x: loc.x, y: loc.y };
  }

  // ----- Catalog -> canvas drag (pointer-based, HUD-consistent) ------------
  // Native HTML5 DnD draws an OS cursor (no-drop / copy) that CSS can't
  // override, so the drag ignored the "Targeting HUD" cursor set. Pointer
  // events keep the grabbing cursor (forced globally via the body class
  // .pipeline-drag-active — see styles.css) for the whole drag and let us show
  // a custom ghost chasing the pointer. Drop is resolved by hit-testing the
  // canvas rect on pointerup.
  var _catalogDrag = null;
  var CATALOG_DRAG_THRESHOLD = 4; // px of travel before a press becomes a drag

  function pointInRect(x, y, r) {
    return !!r && x >= r.left && x <= r.right && y >= r.top && y <= r.bottom;
  }

  function makeCatalogGhost(label) {
    var g = el("div", { className: "catalog-drag-ghost" });
    setText(g, label || "");
    return g;
  }

  // Keyboard / no-drag fallback: drop the node at the canvas center.
  function addNodeAtCanvasCenter(skillId) {
    skillId = String(skillId || "").trim();
    if (!skillId) return;
    var canvasEl = qs(".pipeline-canvas");
    var r = canvasEl && canvasEl.getBoundingClientRect();
    var cx = r ? r.left + r.width / 2 : 0;
    var cy = r ? r.top + r.height / 2 : 0;
    addNodeFromCatalog(skillId, { clientX: cx, clientY: cy });
  }

  function startCatalogDrag(e, item, agent) {
    if (e.pointerType === "mouse" && e.button !== 0) return; // primary button only
    var skillId = item.getAttribute("data-skill-id") || "";
    if (!skillId) return;
    hideAgentTooltip();
    _catalogDrag = {
      skillId: skillId,
      label: (agent && agent.name) || skillId,
      startX: e.clientX, startY: e.clientY,
      started: false, ghost: null,
    };
    // Listen on document (capture) so we keep tracking outside the item.
    document.addEventListener("pointermove", onCatalogDragMove, true);
    document.addEventListener("pointerup", onCatalogDragUp, true);
    document.addEventListener("pointercancel", onCatalogDragUp, true);
  }

  function onCatalogDragMove(e) {
    if (!_catalogDrag) return;
    if (!_catalogDrag.started) {
      var dx = e.clientX - _catalogDrag.startX, dy = e.clientY - _catalogDrag.startY;
      if (dx * dx + dy * dy < CATALOG_DRAG_THRESHOLD * CATALOG_DRAG_THRESHOLD) return;
      _catalogDrag.started = true;
      document.body.classList.add("pipeline-drag-active");
      _catalogDrag.ghost = makeCatalogGhost(_catalogDrag.label);
      document.body.appendChild(_catalogDrag.ghost);
    }
    e.preventDefault();
    var ghost = _catalogDrag.ghost;
    if (ghost) { ghost.style.left = e.clientX + "px"; ghost.style.top = e.clientY + "px"; }
    var canvasEl = qs(".pipeline-canvas");
    if (canvasEl) {
      canvasEl.classList.toggle("drag-over",
        pointInRect(e.clientX, e.clientY, canvasEl.getBoundingClientRect()));
    }
  }

  function onCatalogDragUp(e) {
    if (!_catalogDrag) return;
    var drag = _catalogDrag; _catalogDrag = null;
    document.removeEventListener("pointermove", onCatalogDragMove, true);
    document.removeEventListener("pointerup", onCatalogDragUp, true);
    document.removeEventListener("pointercancel", onCatalogDragUp, true);
    document.body.classList.remove("pipeline-drag-active");
    if (drag.ghost && drag.ghost.parentNode) drag.ghost.parentNode.removeChild(drag.ghost);
    var canvasEl = qs(".pipeline-canvas");
    if (canvasEl) canvasEl.classList.remove("drag-over");
    if (!drag.started || e.type === "pointercancel") return; // click / cancel = no drop
    if (canvasEl && pointInRect(e.clientX, e.clientY, canvasEl.getBoundingClientRect())) {
      addNodeFromCatalog(drag.skillId, e);
    }
  }

  function wireCanvasZoom(canvasEl) {
    if (!canvasEl || canvasEl.dataset.zoomWired === "1") return;
    // Mouse wheel anchored to cursor. Plain wheel = zoom (intuitive on a canvas);
    // Shift+wheel = horizontal scroll; default vertical scroll otherwise.
    canvasEl.addEventListener("wheel", function (e) {
      if (e.shiftKey) return; // let browser handle horizontal scroll
      // Always treat wheel-over-canvas as zoom for the DAG surface.
      e.preventDefault();
      var factor = e.deltaY < 0 ? ZOOM_WHEEL_STEP : (1 / ZOOM_WHEEL_STEP);
      zoomAtCursor(factor, e);
    }, { passive: false });

    // Button controls live in .pipeline-canvas-zoom (sibling overlay).
    var zoomEl = canvasEl.parentElement
      && qs(".pipeline-canvas-zoom", canvasEl.parentElement);
    if (zoomEl && zoomEl.dataset.wired !== "1") {
      var inBtn = qs(".pipeline-canvas-zoom-in", zoomEl);
      var outBtn = qs(".pipeline-canvas-zoom-out", zoomEl);
      var resetBtn = qs(".pipeline-canvas-zoom-reset", zoomEl);
      if (inBtn)    inBtn.addEventListener("click",    function () { zoomByFactor(ZOOM_BTN_STEP); });
      if (outBtn)   outBtn.addEventListener("click",   function () { zoomByFactor(1 / ZOOM_BTN_STEP); });
      if (resetBtn) resetBtn.addEventListener("click", function () { resetZoom(); });
      zoomEl.dataset.wired = "1";
    }
    canvasEl.dataset.zoomWired = "1";
  }

  function addNodeFromCatalog(skillId, e) {
    skillId = String(skillId || "").trim();
    if (!skillId) return;
    readEditorStateFromDom();
    var svg = _canvasSvgRef || qs(".pipeline-canvas svg.dag-svg");
    var p = svg ? svgPointFromEvent(svg, e) : { x: PAD, y: PAD };
    var existing = Object.create(null);
    _editorState.nodes.forEach(function (n) {
      if (n && typeof n.id === "string") existing[n.id] = true;
    });
    _editorState.nodes.push({
      id: suggestNodeId(skillId, existing),
      agent: skillId,
      x: Math.max(PAD, Math.min(p.x - NODE_W / 2, _canvasViewBox.width - NODE_W - PAD)),
      y: Math.max(PAD, Math.min(p.y - NODE_H / 2, _canvasViewBox.height - NODE_H - PAD)),
    });
    renderNodes();
    validateEditor();
  }

  function onCanvasPointerDown(e, group, node) {
    var svg = _canvasSvgRef, pos = _canvasPos || {};
    if (!svg) return;
    var target = e.target;
    if (target && target.classList && target.classList.contains("dag-port")) {
      if (target.getAttribute("data-kind") !== "out") return;
      var nodeId = node && node.id;
      var src = pos[nodeId]; if (!src) return;
      var draft = svgEl("path", {
        class: "dag-edge dag-edge-draft",
        d: edgePath(src.x + NODE_W, src.y + NODE_H / 2,
          src.x + NODE_W, src.y + NODE_H / 2),
      });
      svg.appendChild(draft);
      _canvasEdgeDraft = { fromId: nodeId, line: draft, svg: svg };
      try { svg.setPointerCapture(e.pointerId); } catch (_) {}
      // Pointer capture routes the cursor to the SVG, so the per-node `:active`
      // cursor stops applying mid-drag. Force it globally via a body class.
      document.body.classList.add("pipeline-wire-active");
      e.preventDefault(); return;
    }
    if (target && target.closest && target.closest(".dag-node-control")) return;
    var nodeId = node && node.id;
    var p = svgPointFromEvent(svg, e), cur = pos[nodeId];
    if (!cur) return;
    _canvasDrag = { id: nodeId, node: node, group: group, svg: svg,
      offsetX: p.x - cur.x, offsetY: p.y - cur.y, x: cur.x, y: cur.y };
    try { svg.setPointerCapture(e.pointerId); } catch (_) {}
    document.body.classList.add("pipeline-drag-active");
    e.preventDefault();
  }

  function onCanvasPointerMove(e) {
    if (_canvasEdgeDraft) {
      var svg = _canvasEdgeDraft.svg;
      var p = svgPointFromEvent(svg, e);
      var src = (_canvasPos || {})[_canvasEdgeDraft.fromId];
      if (src) {
        _canvasEdgeDraft.line.setAttribute("d",
          edgePath(src.x + NODE_W, src.y + NODE_H / 2, p.x, p.y));
      }
      // Hit-test for the IN port under (or near) the cursor and apply a
      // "will-connect" (or "would-be-invalid") highlight. Pointer capture
      // suppresses CSS :hover so we mirror it manually. Direct hit on an IN
      // port wins; otherwise findSnappedInPort handles whole-node-body hits
      // and proximity to nearby IN ports within WIRE_SNAP_RADIUS.
      var hit = (typeof document.elementFromPoint === "function")
        ? document.elementFromPoint(e.clientX, e.clientY) : null;
      var hitEl = null;
      if (hit && hit.classList && hit.classList.contains("dag-port")
          && hit.getAttribute("data-kind") === "in") {
        hitEl = hit;
      } else {
        hitEl = findSnappedInPort(e.clientX, e.clientY, _canvasEdgeDraft.fromId, hit);
      }
      var verdict = hitEl
        ? wireTargetVerdict(_canvasEdgeDraft.fromId, hitEl.getAttribute("data-id"))
        : null;
      updateWireHighlight(_canvasEdgeDraft, hitEl, verdict);
      return;
    }
    if (_canvasDrag) {
      var p2 = svgPointFromEvent(_canvasDrag.svg, e);
      _canvasDrag.x = p2.x - _canvasDrag.offsetX;
      _canvasDrag.y = p2.y - _canvasDrag.offsetY;
      _canvasDrag.node.x = _canvasDrag.x;
      _canvasDrag.node.y = _canvasDrag.y;
      _canvasDrag.group.setAttribute("transform",
        "translate(" + _canvasDrag.x + " " + _canvasDrag.y + ")");
    }
  }

  function onCanvasPointerUp(e) {
    document.body.classList.remove("pipeline-drag-active", "pipeline-wire-active");
    if (_canvasEdgeDraft) {
      var draft = _canvasEdgeDraft; _canvasEdgeDraft = null;
      clearWireHighlight(draft);
      if (draft.line && draft.line.parentNode) draft.line.parentNode.removeChild(draft.line);
      // Same hit-test logic as pointermove: direct hit wins, else findSnappedInPort
      // handles node-body hits and proximity.
      var t = (typeof document.elementFromPoint === "function")
        ? document.elementFromPoint(e.clientX, e.clientY) : null;
      var targetPort = null;
      if (t && t.classList && t.classList.contains("dag-port")
          && t.getAttribute("data-kind") === "in") {
        targetPort = t;
      } else {
        targetPort = findSnappedInPort(e.clientX, e.clientY, draft.fromId, t);
      }
      if (targetPort) {
        var targetId = targetPort.getAttribute("data-id");
        if (targetId && targetId !== draft.fromId
            && wireTargetVerdict(draft.fromId, targetId) === "valid") {
          commitCanvasEdge(draft.fromId, targetId);
        }
      }
      return;
    }
    if (_canvasDrag) {
      _canvasDrag = null;
      renderNodes();
    }
  }

  function commitCanvasEdge(fromId, toId) {
    var target = _editorState.nodes.filter(function (n) { return n && n.id === toId; })[0];
    if (!target) return;
    if (!Array.isArray(target.depends_on)) target.depends_on = [];
    if (target.depends_on.indexOf(fromId) >= 0) return;
    target.depends_on.push(fromId);
    renderNodes(); validateEditor();
  }

  function disconnectCanvasEdge(fromId, toId) {
    var target = _editorState.nodes.filter(function (n) { return n && n.id === toId; })[0];
    if (!target || !Array.isArray(target.depends_on)) return;
    var idx = target.depends_on.indexOf(fromId);
    if (idx < 0) return;
    target.depends_on.splice(idx, 1);
    renderNodes(); validateEditor();
  }

  function onCanvasClick(e) {
    var t = e.target;
    if (!t || !t.classList) return;
    // Hit-area overlay catches the click; visible .dag-edge has pointer-events:none.
    var hit = t.classList.contains("dag-edge-hit")
      ? t
      : (t.closest && t.closest(".dag-edge-hit"));
    if (!hit) return;
    var fromId = hit.getAttribute("data-from");
    var toId = hit.getAttribute("data-to");
    if (!fromId || !toId) return;
    e.preventDefault();
    e.stopPropagation();
    disconnectCanvasEdge(fromId, toId);
  }

  // Resolve the IN port that the user is "aiming at" right now. Two strategies:
  //   1. If `elementFromPoint` already hit any descendant of a node group
  //      (body, label, port, delete button — anything in the .dag-node tree),
  //      use that node's IN port. This makes the entire node a drop target,
  //      not just the tiny 12px port circle.
  //   2. Otherwise, find the closest IN port whose center is within
  //      WIRE_SNAP_RADIUS in screen px.
  // Excludes the source's own IN port. Returns null if nothing in range.
  function findSnappedInPort(clientX, clientY, fromId, directHit) {
    var svg = _canvasSvgRef || document.querySelector(".pipeline-canvas svg.dag-svg");
    if (!svg) return null;
    // Strategy 1: cursor is over a node group (body, label, port, etc.).
    var owningNode = directHit && directHit.closest
      ? directHit.closest(".pipeline-canvas svg .dag-node")
      : null;
    if (owningNode) {
      var owningId = owningNode.getAttribute("data-id");
      if (owningId && owningId !== fromId) {
        var ownPort = owningNode.querySelector('.dag-port[data-kind="in"]');
        if (ownPort) return ownPort;
      }
    }
    // Strategy 2: proximity to any IN port within WIRE_SNAP_RADIUS.
    var ports = svg.querySelectorAll('.dag-port[data-kind="in"]');
    var best = null, bestDist = WIRE_SNAP_RADIUS;
    for (var i = 0; i < ports.length; i++) {
      var port = ports[i];
      if (port.getAttribute("data-id") === fromId) continue;
      var r = port.getBoundingClientRect();
      var cx = r.left + r.width / 2;
      var cy = r.top + r.height / 2;
      var dx = clientX - cx, dy = clientY - cy;
      var dist = Math.sqrt(dx * dx + dy * dy);
      if (dist <= bestDist) { best = port; bestDist = dist; }
    }
    return best;
  }

  // Returns "valid" | "self" | "duplicate" | "cycle" — used during the wire
  // drag so the IN port hovered under the cursor gets a strong "will-connect"
  // (green) or "would-be-invalid" (red) hint instead of just sitting silent.
  function wireTargetVerdict(fromId, toId) {
    if (!fromId || !toId) return "self";
    if (fromId === toId) return "self";
    var target = _editorState.nodes.filter(function (n) { return n && n.id === toId; })[0];
    if (!target) return "self";
    if (Array.isArray(target.depends_on) && target.depends_on.indexOf(fromId) >= 0) {
      return "duplicate";
    }
    if (target.kind === "passthrough"
        && Array.isArray(target.depends_on) && target.depends_on.length >= 1) {
      return "duplicate"; // passthrough already has its one input -> reject as invalid
    }
    // Cycle iff fromId already depends (transitively) on toId.
    var nodes = _editorState.nodes || [];
    var depMap = Object.create(null);
    nodes.forEach(function (n) {
      if (n && typeof n.id === "string") {
        depMap[n.id] = Array.isArray(n.depends_on) ? n.depends_on.slice() : [];
      }
    });
    var stack = [fromId], seen = Object.create(null);
    while (stack.length) {
      var cur = stack.pop();
      if (cur === toId) return "cycle";
      if (seen[cur]) continue;
      seen[cur] = true;
      var deps = depMap[cur] || [];
      for (var i = 0; i < deps.length; i++) stack.push(deps[i]);
    }
    return "valid";
  }

  function applyWireHighlightClasses(el, verdict) {
    if (!el) return;
    if (verdict === "valid") {
      el.classList.add("dag-port-wire-target");
      el.classList.remove("dag-port-wire-invalid");
    } else {
      el.classList.add("dag-port-wire-invalid");
      el.classList.remove("dag-port-wire-target");
    }
  }

  function removeWireHighlightClasses(el) {
    if (!el) return;
    el.classList.remove("dag-port-wire-target", "dag-port-wire-invalid");
  }

  function updateWireHighlight(draft, hitEl, verdict) {
    // Switched to a new (or same) hit element: cancel any pending clear and
    // apply immediately. Lets the user move directly between adjacent targets
    // without a perceived gap.
    if (hitEl) {
      if (draft._unhoverTimer) {
        clearTimeout(draft._unhoverTimer);
        draft._unhoverTimer = null;
      }
      if (draft.hoverEl && draft.hoverEl !== hitEl) {
        removeWireHighlightClasses(draft.hoverEl);
      }
      applyWireHighlightClasses(hitEl, verdict);
      draft.hoverEl = hitEl;
      draft.lastVerdict = verdict;
      if (draft.line) {
        draft.line.classList.toggle("dag-edge-draft-valid", verdict === "valid");
        draft.line.classList.toggle("dag-edge-draft-invalid",
          !!verdict && verdict !== "valid");
      }
      return;
    }
    // Lost the target. Don't clear immediately — the cursor often briefly
    // exits the snap area while still aiming at a node (gap between port and
    // body, the dashed draft line itself overlapping a port, etc.). Wait
    // WIRE_HOVER_UNSET_DELAY before un-highlighting; a new hit within that
    // window will cancel the timer above.
    if (!draft.hoverEl) return; // nothing to clear
    if (draft._unhoverTimer) return; // already pending
    var staleEl = draft.hoverEl;
    var staleLine = draft.line;
    draft._unhoverTimer = setTimeout(function () {
      draft._unhoverTimer = null;
      if (draft.hoverEl !== staleEl) return; // user moved to a new target meanwhile
      removeWireHighlightClasses(staleEl);
      draft.hoverEl = null;
      draft.lastVerdict = null;
      if (staleLine) {
        staleLine.classList.remove("dag-edge-draft-valid", "dag-edge-draft-invalid");
      }
    }, WIRE_HOVER_UNSET_DELAY);
  }

  function clearWireHighlight(draft) {
    if (!draft) return;
    if (draft._unhoverTimer) {
      clearTimeout(draft._unhoverTimer);
      draft._unhoverTimer = null;
    }
    if (draft.hoverEl) {
      removeWireHighlightClasses(draft.hoverEl);
      draft.hoverEl = null;
    }
    if (draft.line) {
      draft.line.classList.remove("dag-edge-draft-valid", "dag-edge-draft-invalid");
    }
  }

  // ----- Editor: validation (mirrors pipeline_schema.py) -------------------
  function validateEditor() {
    var errors = [], nodes = _editorState.nodes || [];
    if (!nodes.length) errors.push("pipeline invalid: nodes must be a non-empty list");
    var seen = Object.create(null);
    nodes.forEach(function (n, i) {
      var nid = n && n.id;
      if (typeof nid !== "string" || !nid) {
        errors.push("pipeline invalid: node #" + i + " missing id"); return;
      }
      var hasAgent = typeof (n && n.agent) === "string" && !!n.agent;
      var hasKind = !!(n && "kind" in n);
      if (hasAgent === hasKind) {
        errors.push("pipeline invalid: node '" + nid + "' must have either 'agent' or 'kind', not both");
      }
      if (hasKind && ["input"].concat(SINK_KINDS).indexOf(n.kind) < 0) {
        errors.push("pipeline invalid: node '" + nid + "' has unknown kind '" + n.kind + "'");
      }
      if (seen[nid]) errors.push("pipeline invalid: duplicate id '" + nid + "'");
      seen[nid] = true;
    });
    var idSet = Object.create(null);
    nodes.forEach(function (n) { if (n && typeof n.id === "string") idSet[n.id] = true; });
    nodes.forEach(function (n) {
      if (!n || !Array.isArray(n.depends_on)) return;
      n.depends_on.forEach(function (d) {
        if (!idSet[d]) errors.push(
          "pipeline invalid: '" + (n.id || "") + "' depends on unknown '" + d + "'");
      });
    });
    var hasDeps = nodes.some(function (n) { return n && Array.isArray(n.depends_on); });
    if (!errors.length && hasDeps) {
      var inDeg = Object.create(null), children = Object.create(null);
      nodes.forEach(function (n) {
        if (!n || typeof n.id !== "string") return;
        inDeg[n.id] = (n.depends_on || []).length;
        children[n.id] = [];
      });
      nodes.forEach(function (n) {
        if (!n || typeof n.id !== "string") return;
        (n.depends_on || []).forEach(function (d) {
          if (children[d]) children[d].push(n.id);
        });
      });
      var ready = Object.keys(inDeg).filter(function (k) { return inDeg[k] === 0; });
      var visited = 0;
      while (ready.length) {
        var nid = ready.pop(); visited += 1;
        (children[nid] || []).forEach(function (c) {
          inDeg[c] -= 1; if (inDeg[c] === 0) ready.push(c);
        });
      }
      if (visited !== Object.keys(inDeg).length) {
        errors.push("pipeline invalid: cycle detected");
      }
    }
    var inputNodes = nodes.filter(function (n) { return n && n.kind === "input"; });
    var sinkNodes = nodes.filter(function (n) { return n && SINK_KINDS.indexOf(n.kind) >= 0; });
    if (inputNodes.length !== 1) {
      errors.push("pipeline invalid: pipeline must have exactly one input node (found " + inputNodes.length + ")");
    }
    if (sinkNodes.length !== 1) {
      errors.push("pipeline invalid: pipeline must have exactly one sink node (found " + sinkNodes.length + ")");
    }
    var dependents = Object.create(null);
    nodes.forEach(function (n) { if (n && n.id) dependents[n.id] = 0; });
    nodes.forEach(function (n) {
      if (n && Array.isArray(n.depends_on)) {
        n.depends_on.forEach(function (d) { if (d in dependents) dependents[d] += 1; });
      }
    });
    inputNodes.forEach(function (n) {
      if (Array.isArray(n.depends_on) && n.depends_on.length) {
        errors.push("pipeline invalid: input node '" + n.id + "' must not depend on anything");
      }
      if (!dependents[n.id]) {
        errors.push("pipeline invalid: input node '" + n.id + "' has no downstream nodes");
      }
    });
    sinkNodes.forEach(function (n) {
      var deps = Array.isArray(n.depends_on) ? n.depends_on : [];
      if (!deps.length) errors.push("pipeline invalid: sink node '" + n.id + "' has no inputs");
      if (dependents[n.id]) errors.push("pipeline invalid: sink node '" + n.id + "' must be terminal");
      if (n.kind === "passthrough" && deps.length !== 1) {
        errors.push("pipeline invalid: passthrough sink '" + n.id + "' must have exactly one input");
      }
    });
    renderEditorErrors(errors);
    var saveBtn = qs(".pipeline-editor-save");
    if (saveBtn) saveBtn.disabled = errors.length > 0;
    return errors.length === 0;
  }

  function renderEditorErrors(errors) {
    var box = qs(".pipeline-errors");
    if (!box) return;
    if (!errors || !errors.length) { box.hidden = true; clear(box); return; }
    clear(box);
    var list = el("ul", { className: "pipeline-errors-list" });
    errors.forEach(function (msg) {
      var li = el("li"); setText(li, msg); list.appendChild(li);
    });
    box.appendChild(list);
    box.hidden = false;
  }

  // ----- YAML serializer ----------------------------------------------------
  // Server re-canonicalises via yaml.safe_dump; we only need parseable output.
  var _YAML_BARE_OK = /^[A-Za-z0-9_][A-Za-z0-9_\-.\/]*$/;
  var _YAML_AMBIGUOUS = /^(?:true|false|null|yes|no|on|off|~)$/i;

  function yamlQuote(value) {
    if (value == null) return '""';
    var s = String(value);
    if (s === "") return '""';
    if (/^-?\d+(?:\.\d+)?$/.test(s)) return '"' + s + '"';
    if (_YAML_AMBIGUOUS.test(s)) return '"' + s + '"';
    if (_YAML_BARE_OK.test(s) && s.indexOf("#") < 0) return s;
    return '"' + s.replace(/\\/g, "\\\\").replace(/"/g, '\\"') + '"';
  }

  function serializeYaml(state) {
    var s = state || _editorState, lines = [];
    lines.push("description: " + yamlQuote(s.description == null ? "" : String(s.description)));
    lines.push("nodes:");
    (s.nodes || []).forEach(function (n) {
      lines.push("  - id: " + yamlQuote(n.id || ""));
      if (n.kind) lines.push("    kind: " + yamlQuote(n.kind));
      else lines.push("    agent: " + yamlQuote(n.agent || ""));
      if (Array.isArray(n.depends_on) && n.depends_on.length) {
        lines.push("    depends_on:");
        n.depends_on.forEach(function (d) {
          lines.push("      - " + yamlQuote(d));
        });
      }
    });
    return lines.join("\n") + "\n";
  }

  // ----- DOM <-> state sync -------------------------------------------------
  function readEditorStateFromDom() {
    var d = qs(".pipeline-description"); if (d) _editorState.description = d.value || "";
    qsa(".dag-node").forEach(function (group) {
      var currentId = group.getAttribute("data-id") || "";
      var node = _editorState.nodes.filter(function (n) { return n && n.id === currentId; })[0];
      if (!node) return;
      if (node.kind && SINK_KINDS.indexOf(node.kind) >= 0) {
        var sel = qs(".dag-sink-kind", group);
        if (sel && sel.value) node.kind = sel.value;
        return;
      }
      if (node.kind) return; // input node: fixed id, nothing to sync
      var idIn = qs(".dag-node-label", group);
      if (idIn && node.id !== (idIn.value || "")) {
        var oldId = node.id || "";
        node.id = idIn.value || "";
        updateNodeReferences(oldId, node.id);
      }
    });
  }

  // ----- Save / Delete / Run -----------------------------------------------
  async function saveEditor() {
    readEditorStateFromDom();
    if (!validateEditor()) return;
    var slug = _editorState.slug;
    if (!slug) {
      var input = window.prompt("Pipeline slug (a-z, 0-9, -):", "");
      if (input == null) return;
      slug = String(input).trim();
      if (!SLUG_RE.test(slug)) {
        renderEditorErrors(["pipeline invalid: slug must match /^[a-z0-9-]+$/"]);
        return;
      }
      _editorState.slug = slug;
    }
    var saveBtn = qs(".pipeline-editor-save");
    if (saveBtn) saveBtn.disabled = true;
    var msg = qs(".pipeline-editor-msg");
    if (msg) setText(msg, "Saving…");
    var yamlText = serializeYaml(_editorState);
    try {
      var r = await fetch(DETAIL_URL(slug), {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ yaml: yamlText }),
      });
      var body = await r.json().catch(function () { return {}; });
      if (r.ok) {
        if (msg) setText(msg, "Saved.");
        await loadPipelines();
        closePipelineEditor();
        return;
      }
      var errs = [];
      if (Array.isArray(body.errors)) {
        body.errors.forEach(function (e) {
          if (e && typeof e === "object" && e.message) errs.push(String(e.message));
          else if (typeof e === "string") errs.push(e);
        });
      } else if (typeof body.error === "string") {
        errs.push(body.error);
      } else {
        errs.push("Save failed: HTTP " + r.status);
      }
      renderEditorErrors(errs);
      if (msg) setText(msg, "");
    } catch (e) {
      renderEditorErrors(["Save failed: " + (e && e.message ? e.message : e)]);
      if (msg) setText(msg, "");
    } finally {
      validateEditor();
    }
  }

  async function deletePipeline(slug) {
    if (!slug) return;
    if (!window.confirm("Delete pipeline " + slug + "?")) return;
    try {
      var r = await fetch(DETAIL_URL(slug), { method: "DELETE" });
      if (!r.ok) {
        var body = await r.json().catch(function () { return {}; });
        var detail = body.error || ("HTTP " + r.status);
        console.warn("[dashboard] pipeline delete failed:", detail);
        window.alert("Delete failed: " + detail);
        return;
      }
      await loadPipelines();
    } catch (e) {
      console.warn("[dashboard] pipeline delete error:", e && e.message ? e.message : e);
      window.alert("Delete error: " + (e && e.message ? e.message : e));
    }
  }

  function runPipeline(slug) {
    if (!slug) return;
    var snippet = "Use the run-pipeline skill. Pipeline: " + slug + ". Task: <…>";
    var tell = function (text) {
      try { if (typeof window.toast === "function") { window.toast(text); return; } } catch (_) {}
      var summary = qs(".pipelines-summary");
      if (summary) {
        var note = el("span", { className: "metric-pill" });
        setText(note, text);
        summary.appendChild(note);
        setTimeout(function () { if (note.parentNode) note.parentNode.removeChild(note); }, 4000);
      }
    };
    var done = function () { tell("Copied to clipboard. Paste in the chat and replace <...>."); };
    var fail = function () { window.prompt("Copy this snippet to invoke the pipeline:", snippet); };
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(snippet).then(done, fail);
        return;
      }
    } catch (_) {}
    fail();
  }

  // ----- UI-driven node mutations ------------------------------------------
  function suggestNodeId(agentName, existing) {
    var base = String(agentName || "node").toLowerCase()
      .replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
    if (!base) base = "node";
    if (!existing[base]) return base;
    var n = 2;
    while (existing[base + "-" + n]) n += 1;
    return base + "-" + n;
  }

  function deleteNodeByRef(nodeRef) {
    readEditorStateFromDom();
    var idx = _editorState.nodes.indexOf(nodeRef);
    if (idx < 0 || idx >= _editorState.nodes.length) return;
    var removedId = _editorState.nodes[idx] && _editorState.nodes[idx].id;
    _editorState.nodes.splice(idx, 1);
    _editorState.nodes.forEach(function (n) {
      if (Array.isArray(n.depends_on)) {
        n.depends_on = n.depends_on.filter(function (d) { return d !== removedId; });
      }
    });
    renderNodes();
    validateEditor();
  }

  // ----- Wiring (idempotent via dataset.wired) -----------------------------
  function wirePipelinesOnce() {
    var view = qs("#view-pipelines");
    if (view && view.dataset.wired !== "1") view.dataset.wired = "1";

    var grid = qs("#pipelines-grid");
    if (grid && grid.dataset.wired !== "1") {
      grid.addEventListener("click", function (e) {
        var t = e.target; if (!t || !t.closest) return;
        var ed = t.closest(".pipeline-action-edit");
        if (ed && grid.contains(ed)) { e.stopPropagation(); openPipelineEditor(ed.getAttribute("data-slug")); return; }
        var rn = t.closest(".pipeline-action-run");
        if (rn && grid.contains(rn)) { e.stopPropagation(); runPipeline(rn.getAttribute("data-slug")); return; }
        var dl = t.closest(".pipeline-action-delete");
        if (dl && grid.contains(dl)) { e.stopPropagation(); deletePipeline(dl.getAttribute("data-slug")); return; }
        var card = t.closest(".pipeline-card[data-slug]");
        if (card && grid.contains(card)) openPipelineEditor(card.getAttribute("data-slug"));
      });
      grid.addEventListener("keydown", function (e) {
        if (e.key !== "Enter" && e.key !== " ") return;
        var card = e.target.closest && e.target.closest(".pipeline-card[data-slug]");
        if (!card || !grid.contains(card)) return;
        e.preventDefault();
        openPipelineEditor(card.getAttribute("data-slug"));
      });
      grid.dataset.wired = "1";
    }

    var newBtn = qs("#btn-new-pipeline");
    if (newBtn && newBtn.dataset.wired !== "1") {
      newBtn.addEventListener("click", function () { openPipelineEditor(null); });
      newBtn.dataset.wired = "1";
    }

    var modal = qs("#pipeline-editor-modal");
    if (modal && modal.dataset.wired !== "1") {
      qsa(".modal-close", modal).forEach(function (b) {
        b.addEventListener("click", closePipelineEditor);
      });
      var cancel = qs(".pipeline-editor-cancel", modal);
      if (cancel) cancel.addEventListener("click", closePipelineEditor);
      var backdrop = qs(".modal-backdrop", modal);
      if (backdrop) backdrop.addEventListener("click", closePipelineEditor);
      modal.addEventListener("click", function (e) {
        if (e.target === modal) closePipelineEditor();
      });
      var saveBtn = qs(".pipeline-editor-save", modal);
      if (saveBtn) saveBtn.addEventListener("click", saveEditor);
      var descInput = qs(".pipeline-description", modal);
      if (descInput) descInput.addEventListener("input", function () {
        _editorState.description = descInput.value || "";
        validateEditor();
      });
      var catSearch = qs(".pipeline-catalog-search", modal);
      if (catSearch) catSearch.addEventListener("input", function () { renderCatalogList(); });
      var catList = qs(".pipeline-catalog-list", modal);
      if (catList) catList.addEventListener("scroll", repositionTooltipOnScroll, { passive: true });
      document.addEventListener("keydown", function (e) {
        if (e.key !== "Escape") return;
        var cur = qs("#pipeline-editor-modal");
        if (cur && !cur.hidden) closePipelineEditor();
      });
      modal.dataset.wired = "1";
    }
  }

  function initPipelines() {
    wirePipelinesOnce();
    var view = qs("#view-pipelines");
    if (view && view.classList && view.classList.contains("active")) loadPipelines();
  }

  window.loadPipelines = loadPipelines;
  window.openPipelineEditor = openPipelineEditor;

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initPipelines);
  } else {
    initPipelines();
  }
})();
