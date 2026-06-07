// .ai/dashboard/app/pane-helpers.js
//
// Pure, layout-AGNOSTIC pane render leaves shared by both dashboard pages:
//   * index.html  — the Terminals tab (terminals.js layout shim)
//   * app/canvas.html — the canvas window (split-tree / canvas-bus)
//
// These helpers were extracted verbatim from terminals.js (Phase 1 of the
// shared-pane-engine re-derivation). A "leaf" here is a function that only
// touches its own arguments, the DOM it is handed, other leaves in THIS
// file, and shared globals already loaded before it by core.js / skills.js
// (escape, postJson, setMsg, $ …). None of them reach the pane REGISTRY
// (TERMS), LAYOUT (termSetCollapsed / termGetLayout / termRenderEmptyState /
// termFocusNewPane), persistence (persistOpenPanes), the OPENERS
// (termOpen* / termOpenSession) or the composer SEND path (termSend /
// termSendSession). Those stay in terminals.js and become the Phase-2
// host-seam.
//
// NO ES modules: this is a plain <script defer src> sharing the page's
// single global scope. It declares plain ``function``s + a couple of
// helper-local ``var`` constants and has NO top-level side effects, so it
// is safe to load in any order relative to its peers AND node-checkable in
// isolation. terminals.js keeps calling these by their bare global names
// (resolved across the <script> boundary), so loading this file BEFORE
// terminals.js leaves every existing call site working unchanged. A
// ``window.PaneHelpers`` namespace is also exported at the bottom for code
// (e.g. the canvas) that prefers an explicit handle.
//
// DEPENDS ON (loaded earlier, NOT owned here): escape, postJson, setMsg
// (core.js / skills.js); the xterm CDN globals Terminal / FitAddon
// (presence-checked by termPtyMissingDeps); ``location`` (termPtyWsUrl).

// ----- status-pill state -----
// Normalises every pill-state transition: strips known state classes,
// applies the new one, mirrors the label into dataset for stable signals.
// Tool-identity classes ("claude", "codex") are preserved.
var _PILL_STATE_CLASSES = [
  "running", "queued", "done", "bad", "warn",
  "cancelling", "cancelled",
];
function termSetPillState(pill, state, text) {
  if (!pill) return;
  for (const c of _PILL_STATE_CLASSES) pill.classList.remove(c);
  if (state) pill.classList.add(state);
  if (text != null) pill.textContent = text;
  // Mirror the visible label into dataset.pillText so Close-finished
  // (and any future feature) can read a stable, i18n-resistant signal
  // without parsing the rendered text. dataset.state already covers
  // the class-name dimension; dataset.pillText covers the label.
  if (text != null) pill.dataset.pillText = String(text).toLowerCase();
  if (state) pill.dataset.state = state;
}

// ----- export pane as markdown -----
function termExportMarkdown(t) {
  const lines = [];
  lines.push("# " + (t.task || "Chat") + "\n");
  lines.push("> session " + (t.jobId || "") + "  ·  " + new Date().toISOString());
  lines.push("");
  const messages = t.body.querySelectorAll(".msg");
  for (const m of messages) {
    const role = m.classList.contains("assistant") ? "assistant"
               : m.classList.contains("user") ? "user"
               : m.classList.contains("system") ? "system"
               : m.classList.contains("result") ? "result" : "note";
    const text = m.querySelector(".text")?.innerText || m.innerText;
    if (!text || !text.trim()) continue;
    lines.push(`## ${role}\n`);
    lines.push(text.trim());
    lines.push("");
  }
  const blob = new Blob([lines.join("\n")], { type: "text/markdown" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `chat-${(t.jobId || "session").slice(0, 8)}-${Date.now()}.md`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

// ----- auto-follow scroll behaviour -----
function termInitAutoFollow(t) {
  t.autoFollowBottom = true;
  t.firstScroll = true;
  // Use rAF to detect user-initiated scroll (vs our programmatic
  // scrollTo which also fires the event).
  let programmatic = false;
  t._markProgrammaticScroll = () => {
    programmatic = true;
    requestAnimationFrame(() => requestAnimationFrame(() => { programmatic = false; }));
  };
  const onScroll = () => {
    if (programmatic) return;
    const fromBottom = t.body.scrollHeight - t.body.scrollTop - t.body.clientHeight;
    t.autoFollowBottom = fromBottom < 40;
  };
  // Track the listener so termClose / termClosePty can remove it.
  // Without this, closed panes leak a scroll handler that keeps the
  // pane and the closure references alive forever.
  t._autoFollowScrollHandler = onScroll;
  t.body.addEventListener("scroll", onScroll);
}

// NOTE: termPasteImage + termRenderAttachments (+ _IMAGE_PASTE_MAX_BYTES)
// are pure render leaves too, but they were left in terminals.js: a
// static-lint sanitization test (tests/test_dashboard_sanitization.py)
// pins the image-mime allowlist regex and the
// termRenderAttachments/_IMAGE_PASTE_MAX_BYTES ordering to that file.
// Relocate them once that guard learns the new location.

// ----- composer autocomplete popup teardown -----
function termCloseAutocomplete(t) {
  const pop = t.pane.querySelector(".composer-pop");
  if (pop) { pop.remove(); t._popOpen = false; }
}

// ----- thinking-placeholder teardown -----
function termClearThinkingPlaceholder(t) {
  if (!t || !t.body) return;
  t.body.querySelectorAll(".thinking-placeholder").forEach((el) => el.remove());
}

// ----- cost chip formatting + debounced refresh -----
// Compact form for the header chip: just the dollar amount, rounded
// to 2 decimal places once the cost crosses $0.01 (anything smaller
// would round to "$0.00", which reads as broken — keep the 4-decimal
// long form there so the user sees something).
function termFormatCostCompact(c) {
  if (!c || c.cost_usd == null) return "";
  const v = Number(c.cost_usd);
  return "$" + v.toFixed(v >= 0.01 ? 2 : 4);
}
// Verbose form for tooltips and the legacy header layout: dollars + turns + duration.
function termFormatCost(c) {
  if (!c) return "";
  const parts = [];
  if (c.cost_usd != null) parts.push("$" + Number(c.cost_usd).toFixed(4));
  if (c.turns != null) parts.push(c.turns + " turn" + (c.turns === 1 ? "" : "s"));
  if (c.duration_ms != null && c.duration_ms > 0) parts.push((c.duration_ms / 1000).toFixed(1) + "s");
  return parts.join(" · ");
}

// Trailing-edge debounce per pane so a burst of turn-end events doesn't
// hammer /api/jobs/<id> with a refresh per call. 800ms keeps the chip
// visibly fresh without amplifying load when several models stream
// in parallel.
var _COST_REFRESH_DEBOUNCE_MS = 800;
async function _termRefreshCostNow(t) {
  if (!t || !t.pane.isConnected) return;
  if (t.kind !== "chat" && t.kind !== "chat-codex") return;
  try {
    const r = await fetch(`/api/jobs/${t.jobId}?tail=1`, { cache: "no-store" });
    if (!r.ok) return;
    const data = await r.json();
    const pill = t.pane.querySelector(".cost-pill");
    if (!pill) return;
    pill.textContent = termFormatCostCompact(data.cost);
    const verbose = termFormatCost(data.cost);
    pill.title = verbose
      ? verbose + "  ·  job " + (t.jobId || "").slice(0, 8)
      : "aggregated cost / turns / time for this session";
  } catch (_) { /* ignore */ }
}
function termRefreshCost(t) {
  if (!t) return;
  if (t._costRefreshTimer) clearTimeout(t._costRefreshTimer);
  t._costRefreshTimer = setTimeout(() => {
    t._costRefreshTimer = null;
    _termRefreshCostNow(t);
  }, _COST_REFRESH_DEBOUNCE_MS);
}

// ----- raw (non-JSON) stream line render -----
// Patterns that are pure noise from the operator's POV — Node deprecation
// warnings printed to stderr, the `[unhandled rate_limit_event]` line that
// claude prints when it hits a rate-limit telemetry frame, blank lines.
// Adding patterns here is preferred over surfacing them as "msg system"
// blocks that drown the actual conversation.
var RAW_NOISE_PATTERNS = [
  /^\s*$/,                                              // blank
  /^\(node:\d+\)\s/,                                    // node warnings
  /^\[unhandled (rate_limit_event|.*)\]\s*$/,           // unhandled telemetry
  /^DeprecationWarning:/,                               // node deprecation
  /^\(Use `node --trace-deprecation/,                   // node trace hint
  /^# job [0-9a-f-]+ kind=/,                            // pump-injected header
  /^# task:/,                                           // pump-injected task line
];
function termRenderRaw(t, line) {
  // Non-JSON line (rare: e.g. CLI noise). Silence known-noise patterns
  // entirely; everything else surfaces as a dim system block so we
  // notice genuinely-unexpected output rather than hiding it.
  for (const pat of RAW_NOISE_PATTERNS) {
    if (pat.test(line)) return;
  }
  const div = document.createElement("div");
  div.className = "msg system";
  div.textContent = line;
  t.body.appendChild(div);
}

// ----- tool-detail render leaf: Bash -----
function renderBashCommand(command, description) {
  const wrap = document.createElement("div");
  wrap.className = "tool-detail bash-view";
  if (description) {
    const d = document.createElement("div");
    d.className = "diff-header";
    d.textContent = description;
    wrap.appendChild(d);
  }
  const c = document.createElement("pre");
  c.className = "bash-cmd";
  c.textContent = "$ " + command;
  wrap.appendChild(c);
  return wrap;
}

// ----- PTY (terminal) leaf helpers -----
function termPtyMissingDeps() {
  return typeof Terminal === "undefined" || typeof FitAddon === "undefined";
}

function termPtyWsUrl(ptyId, token) {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const base = `${proto}//${location.host}/api/ptys/${encodeURIComponent(ptyId)}/io`;
  return token
    ? `${base}?token=${encodeURIComponent(token)}`
    : base;
}

// Optional explicit handle. The functions above stay as bare globals so
// terminals.js's existing call sites keep resolving unchanged; this
// namespace is for code (e.g. the canvas) that prefers an explicit ref.
window.PaneHelpers = {
  termSetPillState,
  termExportMarkdown,
  termInitAutoFollow,
  termCloseAutocomplete,
  termClearThinkingPlaceholder,
  termFormatCost,
  termFormatCostCompact,
  termRefreshCost,
  termRenderRaw,
  renderBashCommand,
  termPtyMissingDeps,
  termPtyWsUrl,
};
