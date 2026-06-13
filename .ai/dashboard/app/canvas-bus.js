// canvas-bus.js
// Cross-window messaging bus, persisted-state schema, key normalization, and
// a queue-until-ready buffer.
//
// Convention: plain <script> global scope, NO top-level side effects.
// All browser-global access (BroadcastChannel, localStorage) lives INSIDE
// functions and is guarded with typeof checks so that the node.js test sidecar
// can load this file safely via:
//   new Function("window", src + "\nreturn window.CanvasBus;")({}));
"use strict";

// ─── Storage key / channel name ──────────────────────────────────────────────
var CANVAS_KEY = "dash.canvas.v1";

// ─── Key normalisation ───────────────────────────────────────────────────────
// Single source of truth for the key space that mirrors the dashboard TERMS map.
// Prefixes kept: job:<id>, ide:<sid>
// Prefix stripped: pty:<id>  →  bare id
// Bare ids pass through unchanged.
function normalizeKey(raw) {
  if (typeof raw !== "string") return raw;
  if (raw.slice(0, 4) === "pty:") return raw.slice(4);
  return raw;
}

// ─── Stale detection ─────────────────────────────────────────────────────────
// Returns true when now - (state.lastSeen || 0) > 3 * intervalMs.
function isStale(state, now, intervalMs) {
  var lastSeen = (state && state.lastSeen != null) ? state.lastSeen : 0;
  return (now - lastSeen) > 3 * intervalMs;
}

// ─── Queue-until-ready buffer ─────────────────────────────────────────────────
// Returns { push(msg), flush(handler), ready() }.
//
// Before flush():  push() buffers messages.
// flush(handler):  marks the queue ready; calls handler(msg) for every buffered
//                  message IN ORDER; clears the buffer.
// After flush():   push(msg) calls handler(msg) immediately (no buffering).
// ready():         returns whether flush has been called.
function makeQueue() {
  var buffer = [];
  var _ready = false;
  var _handler = null;

  return {
    push: function (msg) {
      if (_ready) {
        _handler(msg);
      } else {
        buffer.push(msg);
      }
    },
    flush: function (handler) {
      _handler = handler;
      _ready = true;
      for (var i = 0; i < buffer.length; i++) {
        handler(buffer[i]);
      }
      buffer = [];
    },
    ready: function () {
      return _ready;
    },
  };
}

// ─── Persisted state ─────────────────────────────────────────────────────────
// Schema: { tree, open, tokens, lastSeen }

function loadState() {
  try {
    if (typeof localStorage === "undefined") return null;
    var raw = localStorage.getItem(CANVAS_KEY);
    if (!raw) return null;
    return JSON.parse(raw);
  } catch (_e) {
    return null;
  }
}

function saveState(state) {
  try {
    if (typeof localStorage === "undefined") return;
    localStorage.setItem(CANVAS_KEY, JSON.stringify(state));
  } catch (_e) {
    // silently ignore quota / security errors
  }
}

// ─── BroadcastChannel wrapper ─────────────────────────────────────────────────
// Returns { post(msg), close() }.
// When BroadcastChannel is not available (node, old browsers) a no-op stub is
// returned so callers never crash.
function create(opts) {
  var onMessage = (opts && opts.onMessage) ? opts.onMessage : function () {};

  if (typeof BroadcastChannel === "undefined") {
    // No-op stub for environments without BroadcastChannel (e.g. node.js).
    return {
      post: function (_msg) {},
      close: function () {},
    };
  }

  var channel = new BroadcastChannel(CANVAS_KEY);
  channel.onmessage = function (evt) {
    onMessage(evt.data);
  };

  return {
    post: function (msg) {
      channel.postMessage(msg);
    },
    close: function () {
      channel.close();
    },
  };
}

// ─── Public API ──────────────────────────────────────────────────────────────
window.CanvasBus = {
  normalizeKey: normalizeKey,
  isStale: isStale,
  makeQueue: makeQueue,
  loadState: loadState,
  saveState: saveState,
  create: create,
};
