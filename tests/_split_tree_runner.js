"use strict";
const fs = require("fs");
const path = require("path");
const SRC = path.resolve(__dirname, "..", ".ai", "dashboard", "app", "split-tree.js");
const src = fs.readFileSync(SRC, "utf-8");
// The file ends with `window.SplitTree = {...}`. Execute it with a fake window.
const fn = new Function("window", src + "\nreturn window.SplitTree;");
const SplitTree = fn({});
const cmd = JSON.parse(fs.readFileSync(0, "utf-8"));   // stdin
const out = SplitTree[cmd.op](...cmd.args);
process.stdout.write(JSON.stringify({ result: out }));
