"use strict";
const fs = require("fs");
const path = require("path");

const SRC = path.resolve(__dirname, "..", ".ai", "dashboard", "app", "canvas-bus.js");
const src = fs.readFileSync(SRC, "utf-8");
// The file ends with `window.CanvasBus = {...}`.  Execute it with a fake
// window object (no BroadcastChannel, no localStorage) — all browser globals
// are guarded inside the implementation with typeof checks.
const fn = new Function("window", src + "\nreturn window.CanvasBus;");
const CanvasBus = fn({});

const cmd = JSON.parse(fs.readFileSync(0, "utf-8")); // stdin

let result;

if (cmd.op === "normalizeKey") {
  result = CanvasBus.normalizeKey(...cmd.args);
} else if (cmd.op === "isStale") {
  // args: [state, now, intervalMs]
  result = CanvasBus.isStale(...cmd.args);
} else if (cmd.op === "queueFlush") {
  // arg: { pushed: [...], readyAfter: n }
  const { pushed, readyAfter } = cmd.args[0];
  const q = CanvasBus.makeQueue();
  const out = [];
  // push first `readyAfter` messages — queue is not yet ready, so they buffer
  for (let i = 0; i < readyAfter; i++) {
    q.push(pushed[i]);
  }
  // flush: marks ready, drains buffer into out via handler
  q.flush(function (msg) { out.push(msg); });
  // push remaining messages — queue is now ready, handler called immediately
  for (let i = readyAfter; i < pushed.length; i++) {
    q.push(pushed[i]);
  }
  result = out;
} else if (cmd.op === "queueReady") {
  // Creates a queue, records ready() before flush, flushes with no-op, records after.
  // Returns [before, after].
  const q = CanvasBus.makeQueue();
  const before = q.ready();
  q.flush(function () {});
  const after = q.ready();
  result = [before, after];
} else if (cmd.op === "storageRoundtrip") {
  // Inject a fake global.localStorage backed by a plain map so loadState/saveState
  // exercise the real read/write code path (they reference bare `localStorage`).
  const store = {};
  global.localStorage = {
    getItem: function (k) { return Object.prototype.hasOwnProperty.call(store, k) ? store[k] : null; },
    setItem: function (k, v) { store[k] = String(v); },
  };
  // Reload CanvasBus so the new global.localStorage is visible to the closures.
  const CanvasBus2 = fn({});
  const state = cmd.args[0]; // {open: [...], lastSeen: N}
  CanvasBus2.saveState(state);
  result = CanvasBus2.loadState();
  delete global.localStorage;
} else if (cmd.op === "storageEmpty") {
  // Inject a fresh fake localStorage with no entries; loadState should return null.
  const store = {};
  global.localStorage = {
    getItem: function (k) { return Object.prototype.hasOwnProperty.call(store, k) ? store[k] : null; },
    setItem: function (k, v) { store[k] = String(v); },
  };
  const CanvasBus2 = fn({});
  result = CanvasBus2.loadState();
  delete global.localStorage;
} else if (cmd.op === "storageMissing") {
  // Ensure global.localStorage is absent; saveState should return without throwing.
  delete global.localStorage;
  const CanvasBus2 = fn({});
  CanvasBus2.saveState({ open: ["job:a"], lastSeen: 1 });
  result = "ok";
} else if (cmd.op === "createStub") {
  // No global BroadcastChannel in node → exercises the no-op stub path.
  // Ensures post() and close() do not throw.
  delete global.BroadcastChannel;
  const CanvasBus2 = fn({});
  const bus = CanvasBus2.create({ onMessage: function () {} });
  bus.post({ type: "x" });
  bus.close();
  result = "ok";
} else if (cmd.op === "createReal") {
  // Inject a fake global.BroadcastChannel to exercise the real channel path.
  let lastPosted = null;
  global.BroadcastChannel = function (name) {
    this._name = name;
    this.onmessage = null;
    this.postMessage = function (msg) { lastPosted = msg; };
    this.close = function () {};
  };
  const CanvasBus2 = fn({});
  const bus = CanvasBus2.create({ onMessage: function () {} });
  bus.post({ type: "ping" });
  bus.close();
  delete global.BroadcastChannel;
  result = lastPosted;
} else {
  throw new Error("Unknown op: " + cmd.op);
}

process.stdout.write(JSON.stringify({ result: result }));
