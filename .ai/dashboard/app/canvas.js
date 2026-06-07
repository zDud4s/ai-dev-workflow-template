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
        appendCanvasPaneHead(container, key);
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
