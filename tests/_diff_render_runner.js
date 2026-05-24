// Helper invoked by pytest to drive renderUnifiedDiff() out of skills.js.
// Reads the JS source, lifts the three functions we need into an evaluable
// blob, executes it with stub `escape`, then prints JSON for the calling
// Python test to assert on. Keeping this in a sidecar file (instead of inline
// `node -e`) avoids PowerShell's backslash-stripping behaviour that mangles
// regex literals on Windows runners.
"use strict";

const fs = require("fs");
const path = require("path");

const SKILLS = path.resolve(__dirname, "..", ".ai", "dashboard", "app", "skills.js");
const src = fs.readFileSync(SKILLS, "utf-8");

function extract(name) {
  const re = new RegExp("(?:async\\s+)?function\\s+" + name + "\\s*\\(", "g");
  const m = re.exec(src);
  if (!m) throw new Error("function not found: " + name);
  const start = m.index;
  let i = src.indexOf("{", m.index + m[0].length);
  let depth = 0;
  let j = i;
  while (j < src.length) {
    const c = src[j];
    if (c === "{") depth++;
    else if (c === "}") {
      depth--;
      if (depth === 0) return src.slice(start, j + 1);
    }
    j++;
  }
  throw new Error("unbalanced: " + name);
}

// Stub `escape` matches the dashboard's helper closely enough for our
// assertions (we only care about correctness of structure, not byte-perfect
// HTML output).
function escape(s) {
  return String(s).replace(/[&<>"']/g, (ch) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch])
  );
}

const code = [
  "var LCS_LINE_CAP = 2000;",
  extract("_diffFallbackForLargeFiles"),
  extract("renderUnifiedDiff"),
  extract("lcsTable"),
  "module.exports = { renderUnifiedDiff };",
].join("\n");

const mod = { exports: {} };
new Function("escape", "module", code)(escape, mod);

// Read fixture spec from stdin: { oldText, newText }.
let stdin = "";
process.stdin.setEncoding("utf-8");
process.stdin.on("data", (d) => (stdin += d));
process.stdin.on("end", () => {
  const spec = JSON.parse(stdin);
  const html = mod.exports.renderUnifiedDiff(spec.oldText, spec.newText);
  process.stdout.write(JSON.stringify({ html }));
});
