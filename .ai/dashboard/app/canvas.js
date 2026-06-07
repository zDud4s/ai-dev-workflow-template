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

// The most-recently mounted / focused pane key. `open` splits the active
// region (SplitTree.splitLeaf at ACTIVE_KEY); `focus` repoints it. Null
// until the first pane lands.
var ACTIVE_KEY = null;

// The cross-window bus handle (CanvasBus.create -> {post, close}) and the
// queue-until-ready buffer. Both are created in boot(); before that the bus
// is a no-op-safe null and the queue swallows nothing (we only push through
// the queue, which buffers until boot flushes it). Kept at module scope so
// handleBusMessage (the onMessage callback wired into the bus at create-time)
// can post acks without threading the handle through every call.
var BUS = null;
var QUEUE = null;

// Infer a pane kind from the key shape when an `open` message omits `kind`.
// The status list (Task 13) sends `kind` explicitly; this is the fallback for
// keys that arrive bare. `job:` → chat, `ide:` → transcript, `session:` →
// session, everything else → terminal (a bare PTY id). fetchMeta refines
// chat-vs-codex from the server record when needed.
function canvasInferKind(key) {
  if (typeof key !== "string") return "terminal";
  if (key.slice(0, 4) === "job:") return "chat";
  if (key.slice(0, 4) === "ide:") return "transcript";
  if (key.slice(0, 8) === "session:") return "session";
  return "terminal";
}

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

// ─── Persistence + restore (fleshed out in Task 12) ───────────────────────────
// Declared here so the Task-10 bus handlers can reference them; Task 12 fills
// in the real localStorage save/restore + heartbeat. Kept side-effect-free at
// load (just function declarations).
function saveCanvasState() { /* Task 12 */ }
function restoreCanvasState() { return Promise.resolve(); }
function startCanvasHeartbeat() { /* Task 12 */ }

// ─── Gutter geometry ──────────────────────────────────────────────────────────
// Walk the tree the SAME way SplitTree.computeRects does (row → divide width,
// col → divide height, offsets accumulate, no gutter subtraction) and emit one
// gutter descriptor per INTERNAL split boundary. Each descriptor is:
//   { path, axis, x, y, w, h, parentX, parentY, parentW, parentH }
// where `path` is the array of child indices from the root to the split node
// (the address SplitTree.resize expects), `axis` is "row" (a vertical gutter
// dragged left/right) or "col" (a horizontal gutter dragged up/down), and the
// rect is a thin band centred on the boundary between child[0] and child[1].
// parent* is the split node's full pixel extent, used to convert a pixel drag
// delta into the ratio delta SplitTree.resize wants.
//
// SplitTree.splitLeaf only ever creates 2-child splits and SplitTree.resize
// only shifts the child[0]|child[1] boundary, so we emit exactly ONE gutter per
// split node (its first boundary). The walk normalises ratios the same way
// computeRects does so the gutter lands exactly on the rendered seam.
var CANVAS_GUTTER_PX = 8; // hit-area thickness, centred on the boundary
function canvasNormalizeRatios(ratios) {
  var sum = 0;
  for (var i = 0; i < ratios.length; i++) sum += ratios[i];
  if (!sum) sum = 1;
  return ratios.map(function (r) { return r / sum; });
}
function canvasComputeGutters(tree, w, h) {
  var out = [];
  var walk = function (node, x, y, ww, hh, path) {
    if (!node || node.leaf !== undefined) return;
    var ratios = canvasNormalizeRatios(node.ratios);
    var isRow = node.split === "row";
    // Boundary between child[0] and child[1] sits after child[0]'s extent.
    var firstFrac = ratios[0];
    var half = CANVAS_GUTTER_PX / 2;
    if (isRow) {
      var bx = x + ww * firstFrac;
      out.push({
        path: path, axis: "row",
        x: bx - half, y: y, w: CANVAS_GUTTER_PX, h: hh,
        parentX: x, parentY: y, parentW: ww, parentH: hh,
      });
    } else {
      var byy = y + hh * firstFrac;
      out.push({
        path: path, axis: "col",
        x: x, y: byy - half, w: ww, h: CANVAS_GUTTER_PX,
        parentX: x, parentY: y, parentW: ww, parentH: hh,
      });
    }
    // Recurse into children to emit their nested gutters.
    var offset = 0;
    for (var i = 0; i < node.children.length; i++) {
      var frac = ratios[i];
      if (isRow) {
        var cw = ww * frac;
        walk(node.children[i], x + offset, y, cw, hh, path.concat([i]));
        offset += cw;
      } else {
        var ch = hh * frac;
        walk(node.children[i], x, y + offset, ww, ch, path.concat([i]));
        offset += ch;
      }
    }
  };
  walk(tree, 0, 0, w, h, []);
  return out;
}

// ─── Drag-to-split state ──────────────────────────────────────────────────────
// The pane key currently being dragged by its head bar (set on dragstart,
// cleared on dragend/drop). A module var backs up the dataTransfer payload
// because some browsers withhold dataTransfer.getData during dragover.
var CANVAS_DRAG_KEY = null;
// The single reused drop-zone highlight element (lazily created, parented to
// #canvas-root). pointer-events:none so it never eats the drop.
var CANVAS_DROPZONE = null;

// Which third of `rect` the cursor (cx,cy, relative to rect origin) is in.
// Compares horizontal vs vertical edge proximity so the larger pull wins;
// centre defaults to "right". Returns "left" | "right" | "top" | "bottom".
function canvasDropDir(cx, cy, w, h) {
  var fx = cx / w;       // 0..1 across width
  var fy = cy / h;       // 0..1 down height
  // Distance into the nearest horizontal / vertical edge band.
  var left = fx, right = 1 - fx, top = fy, bottom = 1 - fy;
  var min = Math.min(left, right, top, bottom);
  // Only treat it as an edge drop when the cursor is within a third of an edge;
  // otherwise (centre) default to "right".
  if (min > 1 / 3) return "right";
  if (min === left) return "left";
  if (min === right) return "right";
  if (min === top) return "top";
  return "bottom";
}

function canvasEnsureDropzone(root) {
  if (CANVAS_DROPZONE && CANVAS_DROPZONE.parentNode === root) return CANVAS_DROPZONE;
  CANVAS_DROPZONE = document.createElement("div");
  CANVAS_DROPZONE.className = "canvas-dropzone";
  CANVAS_DROPZONE.style.display = "none";
  root.appendChild(CANVAS_DROPZONE);
  return CANVAS_DROPZONE;
}

// Position the drop-zone highlight over the HALF of `container` that the new
// pane will occupy for direction `dir`.
function canvasShowDropzone(root, container, dir) {
  var zone = canvasEnsureDropzone(root);
  var left = container.offsetLeft, top = container.offsetTop;
  var w = container.offsetWidth, h = container.offsetHeight;
  var zx = left, zy = top, zw = w, zh = h;
  if (dir === "left") { zw = w / 2; }
  else if (dir === "right") { zx = left + w / 2; zw = w / 2; }
  else if (dir === "top") { zh = h / 2; }
  else if (dir === "bottom") { zy = top + h / 2; zh = h / 2; }
  zone.style.display = "block";
  zone.style.left = zx + "px";
  zone.style.top = zy + "px";
  zone.style.width = zw + "px";
  zone.style.height = zh + "px";
}
function canvasHideDropzone() {
  if (CANVAS_DROPZONE) CANVAS_DROPZONE.style.display = "none";
}

// Wire drag-to-split on a pane container's head + body. dragstart records the
// key; dragover computes + shows the drop zone; drop moves the pane via
// remove-then-split; dragend/leave clear the highlight.
function canvasWireDragToSplit(container, head, key) {
  head.setAttribute("draggable", "true");
  head.addEventListener("dragstart", function (e) {
    CANVAS_DRAG_KEY = key;
    try { e.dataTransfer.setData("text/plain", key); } catch (_e) {}
    if (e.dataTransfer) e.dataTransfer.effectAllowed = "move";
    container.classList.add("dragging");
  });
  head.addEventListener("dragend", function () {
    CANVAS_DRAG_KEY = null;
    container.classList.remove("dragging");
    canvasHideDropzone();
  });
  container.addEventListener("dragover", function (e) {
    if (!CANVAS_DRAG_KEY || CANVAS_DRAG_KEY === key) return;
    e.preventDefault(); // allow drop
    if (e.dataTransfer) e.dataTransfer.dropEffect = "move";
    var r = container.getBoundingClientRect();
    var dir = canvasDropDir(e.clientX - r.left, e.clientY - r.top, r.width, r.height);
    var root = document.getElementById("canvas-root");
    if (root) canvasShowDropzone(root, container, dir);
  });
  container.addEventListener("dragleave", function (e) {
    // Only hide when the cursor actually left the container (dragleave fires
    // for child elements too).
    if (e.relatedTarget && container.contains(e.relatedTarget)) return;
    canvasHideDropzone();
  });
  container.addEventListener("drop", function (e) {
    e.preventDefault();
    canvasHideDropzone();
    var dragged = CANVAS_DRAG_KEY;
    CANVAS_DRAG_KEY = null;
    if (!dragged || dragged === key) return;
    var r = container.getBoundingClientRect();
    var dir = canvasDropDir(e.clientX - r.left, e.clientY - r.top, r.width, r.height);
    // Remove-then-split MOVES the dragged pane next to the target (it does not
    // duplicate it). The PaneCore handle survives because renderTree only
    // tears down keys absent from the NEW tree — `dragged` is still present.
    var ST = window.SplitTree;
    CanvasApp.setTree(ST.splitLeaf(ST.remove(TREE, dragged), key, dragged, dir));
    ACTIVE_KEY = dragged;
    CanvasApp.renderTree();
    saveCanvasState();
  });
}

// Render gutter overlays for the current TREE into #canvas-root. Removes any
// previous gutters first (they're cheap, position-only divs) and wires each to
// a live pointer-drag → SplitTree.resize at its split node's path.
function canvasRenderGutters(root, w, h) {
  // Clear prior gutters.
  var old = root.querySelectorAll(".canvas-gutter");
  for (var i = 0; i < old.length; i++) old[i].parentNode.removeChild(old[i]);
  var gutters = canvasComputeGutters(TREE, w, h);
  gutters.forEach(function (g) {
    var el = document.createElement("div");
    el.className = "canvas-gutter canvas-gutter-" + g.axis;
    el.style.left = g.x + "px";
    el.style.top = g.y + "px";
    el.style.width = g.w + "px";
    el.style.height = g.h + "px";
    canvasWireGutter(el, g);
    root.appendChild(el);
  });
}

// Live-drag a single gutter. pointerdown captures the pointer; pointermove
// converts the pixel delta along the gutter's axis into a fraction of the
// parent split's extent and calls SplitTree.resize(path, delta); pointerup
// releases + persists. We resize from a SNAPSHOT of the tree taken at
// pointerdown so accumulated rounding doesn't drift the boundary.
function canvasWireGutter(el, g) {
  el.addEventListener("pointerdown", function (e) {
    e.preventDefault();
    var startX = e.clientX, startY = e.clientY;
    var isRow = g.axis === "row";
    var extent = isRow ? g.parentW : g.parentH;
    if (!extent) return;
    var baseTree = window.SplitTree.serialize(TREE); // snapshot for drift-free resize
    try { el.setPointerCapture(e.pointerId); } catch (_e) {}
    var onMove = function (ev) {
      var deltaPx = isRow ? (ev.clientX - startX) : (ev.clientY - startY);
      var deltaRatio = deltaPx / extent;
      CanvasApp.setTree(window.SplitTree.resize(window.SplitTree.deserialize(baseTree), g.path, deltaRatio));
      CanvasApp.renderTree();
    };
    var onUp = function (evUp) {
      el.removeEventListener("pointermove", onMove);
      el.removeEventListener("pointerup", onUp);
      try { el.releasePointerCapture(evUp.pointerId); } catch (_e) {}
      saveCanvasState();
    };
    el.addEventListener("pointermove", onMove);
    el.addEventListener("pointerup", onUp);
  });
}

// Build + append the canvas-level head bar for a pane container. Carries a
// short label and a close button that routes to CanvasApp.closePane (the
// canvas owns tree membership — see the call site in renderTree). Task 11
// makes this bar the drag handle.
function appendCanvasPaneHead(container, key) {
  var head = document.createElement("div");
  head.className = "canvas-pane-head";
  var label = document.createElement("span");
  label.className = "canvas-pane-title";
  label.textContent = key;
  label.title = key;
  var close = document.createElement("button");
  close.className = "canvas-pane-close";
  close.type = "button";
  close.title = "Close this pane";
  close.textContent = "×"; // ×
  close.addEventListener("click", function (e) {
    e.stopPropagation();
    CanvasApp.closePane(key);
  });
  head.appendChild(label);
  head.appendChild(close);
  container.appendChild(head);
  return head;
}

// Cross-window message handler. Wired into the bus at create-time, but every
// `open` / `focus` is routed through QUEUE.push so messages that arrive before
// boot() has flushed are buffered and replayed in order. `hello` / `ready`
// don't need buffering (they're idempotent re-announcements) but go through
// the same queue for ordering simplicity.
function handleBusMessage(msg) {
  if (!msg || typeof msg !== "object") return;
  if (QUEUE) { QUEUE.push(msg); return; }
  dispatchBusMessage(msg);
}

// The actual per-type dispatch, called by the queue handler once ready (and
// directly if the queue somehow isn't set up). Async because an `open` with
// no provided meta may need to await PaneCore.fetchMeta before mounting.
async function dispatchBusMessage(msg) {
  var ST = window.SplitTree;
  if (msg.type === "open") {
    var key = window.CanvasBus.normalizeKey(msg.key);
    if (!key) return;
    // Already mounted → treat as a focus, don't duplicate the pane.
    if (ST.keys(TREE).indexOf(key) !== -1) {
      CanvasApp.focusPane(key);
      return;
    }
    // Stash the render inputs PaneCore.mount needs but the tree doesn't carry.
    KIND_BY_KEY[key] = msg.kind || canvasInferKind(key);
    META_BY_KEY[key] = msg.meta || null;
    // If no meta was supplied, try to fetch it so the pane mounts with a real
    // server record. fetchMeta returning null is fine — mount falls back to {}.
    if (!META_BY_KEY[key] && window.PaneCore && typeof window.PaneCore.fetchMeta === "function") {
      try {
        var fetched = await window.PaneCore.fetchMeta(key);
        if (fetched) META_BY_KEY[key] = fetched;
      } catch (_e) { /* mount proceeds with empty meta */ }
    }
    // Insert: first pane fills the canvas; subsequent panes split the active
    // region to the right.
    if (ST.keys(TREE).length === 0) {
      CanvasApp.setTree(ST.insertFirst(ST.empty(), key));
    } else {
      var target = (ACTIVE_KEY && ST.keys(TREE).indexOf(ACTIVE_KEY) !== -1)
        ? ACTIVE_KEY
        : ST.keys(TREE)[ST.keys(TREE).length - 1];
      CanvasApp.setTree(ST.splitLeaf(TREE, target, key, "right"));
    }
    ACTIVE_KEY = key;
    CanvasApp.renderTree();
    saveCanvasState();
    if (BUS) BUS.post({ type: "opened", key: key });
    return;
  }
  if (msg.type === "focus") {
    CanvasApp.focusPane(window.CanvasBus.normalizeKey(msg.key));
    return;
  }
  if (msg.type === "hello") {
    // A newly-opened list (other window) is asking who's around — re-announce.
    if (BUS) BUS.post({ type: "ready", open: window.SplitTree.keys(TREE) });
    return;
  }
}

window.CanvasApp = {
  boot() {
    // Wire the bus EARLY (before any await) so messages arriving during boot
    // are captured by handleBusMessage and buffered in the queue; the flush
    // at the end of boot replays them in order.
    QUEUE = window.CanvasBus.makeQueue();
    BUS = window.CanvasBus.create({ onMessage: handleBusMessage });

    // Restore persisted layout (Task 12) before announcing readiness so the
    // `ready` we post reflects the rehydrated open set. restoreCanvasState is
    // async (it fetches per-pane meta); we chain the ready-post + flush after
    // it resolves so a restore-then-open race can't drop the queued opens.
    var finish = function () {
      // Start the lastSeen heartbeat + beforeunload teardown (Task 12).
      startCanvasHeartbeat();
      // Announce our current open set to any listening status list, then
      // open the floodgates: replay any messages buffered during boot and
      // handle all subsequent ones immediately.
      if (BUS) BUS.post({ type: "ready", open: window.SplitTree.keys(TREE) });
      QUEUE.flush(function (msg) { dispatchBusMessage(msg); });
    };

    Promise.resolve(restoreCanvasState()).then(finish, finish);
  },

  // Highlight + activate the pane for `key`: add `.focused` (removing it from
  // every other canvas pane), scroll it into view, and repoint ACTIVE_KEY so
  // the next `open` splits next to it. No bus post (focus acks are optional).
  focusPane(key) {
    if (!key) return;
    var found = false;
    CONTAINERS.forEach(function (container, k) {
      if (k === key) {
        container.classList.add("focused");
        found = true;
        try { container.scrollIntoView({ block: "nearest", behavior: "smooth" }); } catch (_e) {}
      } else {
        container.classList.remove("focused");
      }
    });
    if (found) ACTIVE_KEY = key;
  },

  // Remove a pane from the tree (the canvas is authoritative over tree
  // membership). renderTree's teardown pass then calls the pane handle's
  // close() — which disconnects the activity observer and tears down the
  // stream via termClose/termClosePty. Also re-announces the new open set so
  // the status list clears the badge.
  closePane(key) {
    var k = window.CanvasBus.normalizeKey(key);
    CanvasApp.setTree(window.SplitTree.remove(TREE, k));
    delete KIND_BY_KEY[k];
    delete META_BY_KEY[k];
    if (ACTIVE_KEY === k) ACTIVE_KEY = window.SplitTree.keys(TREE)[0] || null;
    CanvasApp.renderTree();
    saveCanvasState();
    if (BUS) BUS.post({ type: "closed", key: k });
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
        // Canvas-level chrome: a thin head bar that (a) carries the
        // drag-to-split handle (Task 11) and (b) owns the close affordance.
        // Closing through THIS button routes to CanvasApp.closePane so the
        // canvas tree stays authoritative — renderTree's teardown then calls
        // the PaneCore handle.close() (stream + activity-observer teardown).
        // We deliberately do NOT observe the PaneCore inner close button:
        // that path tears the pane down but leaves the tree pointing at a
        // ghost leaf. The canvas owns membership; the head close is the one
        // robust entry point. The head is the FIRST child so the mounted
        // .term-pane stacks below it.
        var paneHead = appendCanvasPaneHead(container, key);
        // Drag the head to move this pane next to another (drop-zone split).
        canvasWireDragToSplit(container, paneHead, key);
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

    // Draw the resizable gutter overlays for the current tree. These are
    // overlays (pointer-events on themselves only) and do NOT consume layout
    // space — they sit on top of the seams computeRects produced.
    canvasRenderGutters(root, w, h);
  },
};

if (typeof document !== "undefined") {
  document.addEventListener("DOMContentLoaded", function () {
    CanvasApp.boot();
  });
}
