// canvas.js — boot + DOM renderer for the canvas window.
//
// Convention: plain <script defer src> sharing the page's single global
// scope. NO ES modules. No top-level side effects EXCEPT the guarded
// DOMContentLoaded listener at the bottom (guarded by `typeof document`
// so the node sidecar can load this file harmlessly).
//
// DEPENDS ON shared globals loaded before it on canvas.html:
//   window.PaneCore   (pane-core.js)  — PaneCore.mount(container, opts)
//   window.SplitTree  (split-tree.js) — pure layout engine + computeRects
//   window.CanvasBus  (canvas-bus.js) — cross-window bus (used in Task 10)
"use strict";

// ─── Module state ────────────────────────────────────────────────────────────
// The live split tree. Starts empty; Task 10 drives it from bus `open` /
// `close` messages and persisted state, calling renderTree() after each change.
var TREE = (typeof window !== "undefined" && window.SplitTree)
  ? window.SplitTree.empty()
  : null;

// Mounted PaneCore handles, keyed by paneKey (the leaf key in TREE). Lets
// renderTree() tell membership changes (mount) from pure resizes (reuse) and
// tear down panes that have left the tree (handle.close()).
var PANES = new Map();

// The pane container <div>s, keyed by paneKey, so a pure resize repositions
// the existing element instead of re-mounting. Parallel to PANES.
var CONTAINERS = new Map();

// Per-key render inputs that PaneCore.mount needs but the geometry tree does
// NOT carry. Task 10 populates these from bus `open` messages BEFORE calling
// renderTree (e.g. KIND_BY_KEY[key]="terminal"; META_BY_KEY[key]={...}). They
// are plain objects (not Maps) so the bus handler can assign by key directly.
// renderTree reads them through resolveKind/resolveMeta below, which also
// accept a per-call `lookup` override ({kind, meta} functions or maps) so a
// caller can drive a render without mutating module state — keeps Task 10
// free to choose either path.
var KIND_BY_KEY = {};
var META_BY_KEY = {};

function canvasResolveKind(key, lookup) {
  if (lookup) {
    if (typeof lookup.kind === "function") return lookup.kind(key);
    if (lookup.kind && typeof lookup.kind === "object") return lookup.kind[key];
  }
  return KIND_BY_KEY[key];
}

function canvasResolveMeta(key, lookup) {
  if (lookup) {
    if (typeof lookup.meta === "function") return lookup.meta(key);
    if (lookup.meta && typeof lookup.meta === "object") return lookup.meta[key];
  }
  return META_BY_KEY[key] || {};
}

window.CanvasApp = {
  boot() {
    /* stub for now — Task 10 wires the bus + initial render here */
  },

  // Test/Task-10 seam: expose the module maps so the bus handler (and the
  // node sidecar later) can read/replace TREE and the kind/meta lookups
  // without reaching through closures.
  _state() {
    return { TREE: TREE, PANES: PANES, CONTAINERS: CONTAINERS, KIND_BY_KEY: KIND_BY_KEY, META_BY_KEY: META_BY_KEY };
  },
  setTree(tree) { TREE = tree; },

  // Re-lay the canvas from the current TREE. Reads #canvas-root's pixel size,
  // asks the pure SplitTree.computeRects for each leaf's absolute rect, then:
  //   * mounts a pane (PaneCore.mount) ONLY on first appearance of a key
  //     (membership change) — reuses the existing container on pure resize;
  //   * absolutely positions every container at its rect;
  //   * closes + removes panes whose key has left the tree.
  // `lookup` (optional) = { kind, meta } overriding the module maps for this
  // call only; see canvasResolveKind/Meta.
  renderTree(lookup) {
    var root = document.getElementById("canvas-root");
    if (!root) return;
    var w = root.clientWidth;
    var h = root.clientHeight;
    var rects = window.SplitTree.computeRects(TREE, w, h);

    var present = new Set();
    for (var i = 0; i < rects.length; i++) {
      var rect = rects[i];
      var key = rect.key;
      present.add(key);

      var container = CONTAINERS.get(key);
      if (!container) {
        // First appearance → create + mount via PaneCore.
        container = document.createElement("div");
        container.className = "canvas-pane";
        container.dataset.paneKey = key;
        root.appendChild(container);
        CONTAINERS.set(key, container);
        var kind = canvasResolveKind(key, lookup);
        var meta = canvasResolveMeta(key, lookup);
        try {
          var handle = window.PaneCore.mount(container, { kind: kind, key: key, meta: meta });
          PANES.set(key, handle);
        } catch (err) {
          // A bad/unknown kind shouldn't wedge the whole render. Surface it
          // in the container so the operator sees which pane failed.
          container.textContent = "[pane mount failed: " + (err && err.message ? err.message : err) + "]";
        }
      }

      // Position (mount or pure resize both land here).
      container.style.left = rect.x + "px";
      container.style.top = rect.y + "px";
      container.style.width = rect.w + "px";
      container.style.height = rect.h + "px";
    }

    // Tear down panes whose key has left the tree.
    CONTAINERS.forEach(function (container, key) {
      if (present.has(key)) return;
      var handle = PANES.get(key);
      if (handle && typeof handle.close === "function") {
        try { handle.close(); } catch (_) {}
      }
      if (container && container.parentNode) container.parentNode.removeChild(container);
      PANES.delete(key);
      CONTAINERS.delete(key);
    });
  },
};

if (typeof document !== "undefined") {
  document.addEventListener("DOMContentLoaded", function () {
    CanvasApp.boot();
  });
}
