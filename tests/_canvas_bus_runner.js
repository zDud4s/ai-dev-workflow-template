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
} else {
  throw new Error("Unknown op: " + cmd.op);
}

process.stdout.write(JSON.stringify({ result: result }));
