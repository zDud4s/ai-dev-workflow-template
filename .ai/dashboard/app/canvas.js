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

window.CanvasApp = {
  boot() {
    /* stub for now — Task 10 wires the bus + initial render here */
  },
};

if (typeof document !== "undefined") {
  document.addEventListener("DOMContentLoaded", function () {
    CanvasApp.boot();
  });
}
