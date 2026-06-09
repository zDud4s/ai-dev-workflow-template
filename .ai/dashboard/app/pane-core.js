// .ai/dashboard/app/pane-core.js
//
// SELF-SUFFICIENT, layout-agnostic pane render + stream engine, published
// as the single global ``window.PaneCore``. This is the ISOLATED CANVAS-SIDE
// renderer: it is loaded by ``app/canvas.html`` which does NOT load
// terminals.js. Every external symbol it references is satisfied by ONE of:
//
//   * core.js / skills.js  — escape, postJson, setMsg, $, DOMPurify, marked,
//     MODELS_BY_TOOL, COMPOSER_AUTOSIZE_MAX_PX, TERM_MSG_DURATION_MS, debounce
//     (DOMPurify / marked are CDN globals the page also loads).
//   * pane-helpers.js      — termSetPillState, termExportMarkdown,
//     termInitAutoFollow, termCloseAutocomplete, termClearThinkingPlaceholder,
//     termFormatCost, termFormatCostCompact, termRefreshCost, termRenderRaw,
//     renderBashCommand, termPtyMissingDeps, termPtyWsUrl.
//   * jobs.js              — loadJobs (guarded: a list refresh; no-op on canvas
//     when undefined).
//   * THIS FILE            — the entire chat/codex/session render + stream
//     engine, COPIED (and host-adapted) from terminals.js. This duplication
//     with terminals.js is the ACCEPTED COST of the isolated-renderer model:
//     terminals.js stays the dashboard's owner; pane-core.js owns the canvas.
//   * the HOST object      — registry / layout / open / persistence. The
//     caller (canvas.js, or the node sidecar's mock) supplies a host that
//     implements: register(key,handle) / unregister(key) / get(key) /
//     each(cb) / close(key) / persist() / setCollapsed(key,collapsed,opts) /
//     focusNewPane(key) / renderEmptyState() / openPane(kind,key,meta).
//     PaneCore consumes ONLY ``opts.collapsed`` (boolean), never a layout
//     string. ``host`` is the 3rd arg to ``PaneCore.mount(container,opts,host)``.
//
// NO ES modules: plain <script defer src> sharing the page's single global
// scope. NO top-level side effects: only function declarations + the single
// ``window.PaneCore`` export line at the end (node-loadable under a bare
// ``window`` stub).
//
// terminals.js and the dashboard's terminals tab are OFF-LIMITS / not used
// here. There is intentionally NO reference to TERMS, persistOpenPanes,
// termGetLayout, termClose, termClosePty, termOpen, termRenderEmptyState,
// termFocusNewPane, termSetCollapsed: every one of those concerns is routed
// through ``host.*`` so this file resolves with zero terminals.js globals.

// ───────────────────────────────────────────────────────────────────────────
// Host shim: PaneCore never reaches the registry/layout/open/persist layer
// directly. A null/partial host is tolerated (every method degrades to a safe
// no-op) so a bare mount with no host still builds the pane DOM + stream.
// ───────────────────────────────────────────────────────────────────────────
// Composer / toast constants. terminals.js defines these for the dashboard;
// the canvas page does NOT load terminals.js, so the isolated renderer owns
// its own copies (values kept in sync with terminals.js). pane-core.js loads
// ONLY on app/canvas.html and terminals.js loads ONLY on app/index.html — they
// never share a page, so these duplicated declarations never collide. (The two
// pages do both load canvas-bus.js, which is side-effect-free and idempotent.)
var COMPOSER_AUTOSIZE_MAX_PX = 220;
var TERM_MSG_DURATION_MS = 4000;

function paneCoreHost(host) {
  host = host || {};
  return {
    register(key, handle) { try { if (host.register) host.register(key, handle); } catch (_) {} },
    unregister(key) { try { if (host.unregister) host.unregister(key); } catch (_) {} },
    get(key) { try { return host.get ? host.get(key) : null; } catch (_) { return null; } },
    each(cb) { try { if (host.each) host.each(cb); } catch (_) {} },
    close(key) { try { if (host.close) host.close(key); } catch (_) {} },
    persist() { try { if (host.persist) host.persist(); } catch (_) {} },
    setCollapsed(key, collapsed, opts) { try { if (host.setCollapsed) host.setCollapsed(key, collapsed, opts); } catch (_) {} },
    focusNewPane(key) { try { if (host.focusNewPane) host.focusNewPane(key); } catch (_) {} },
    renderEmptyState() { try { if (host.renderEmptyState) host.renderEmptyState(); } catch (_) {} },
    openPane(kind, key, meta) { try { if (host.openPane) host.openPane(kind, key, meta); } catch (_) {} },
    // Ask the host to FORGET a key from its persisted layout WITHOUT tearing
    // down the visible pane. Used when a session pane is found to have no
    // transcript (never-connected, budget exhausted) so a dead key is not
    // re-opened on the next reload. Degrades to a no-op on a bare/partial host.
    forget(key) { try { if (host.forget) host.forget(key); } catch (_) {} },
  };
}

// loadJobs is a dashboard list-refresh; on canvas it may be absent. A thin
// guarded wrapper keeps the copied stream code's fire-and-forget calls intact
// without dragging the list refresher into the canvas surface.
function paneCoreLoadJobs() {
  try {
    if (typeof loadJobs === "function") return Promise.resolve(loadJobs());
  } catch (_) {}
  return Promise.resolve();
}

// ───────────────────────────────────────────────────────────────────────────
// COPIED pane-intrinsic render/stream/send engine (host-adapted).
// These are derived from terminals.js; calls into the registry/layout/open/
// persist layer there are rewritten to ``h.*`` (the host shim above), where
// ``h`` is captured per-pane at mount time and stashed on ``t._host`` so the
// stream/render functions (which only receive ``t``) can reach it.
// ───────────────────────────────────────────────────────────────────────────

// Resolve the host for a term object (falls back to a no-op host so a stray
// call on a host-less pane can't throw).
function paneCoreT_host(t) {
  return (t && t._host) || paneCoreHost(null);
}

// ----- status / activity -----
function paneCoreSetActivity(t, label, cls) {
  if (!t || !t.pane) return;
  const el = t.pane.querySelector(".term-head .activity");
  if (!el) return;
  const prevCls = t._activityCls || "";
  el.textContent = label || "";
  el.classList.remove("busy", "waiting", "ready", "ended");
  if (cls) el.classList.add(cls);
  t._activityCls = cls || "";
  const operatorWaiting = cls === "waiting";
  t.pane.classList.toggle("is-waiting", operatorWaiting);
  if (t.pane.classList.contains("collapsed") && cls === "busy") {
    t.pane.classList.add("has-update");
  }
  // Auto-expand on the transition INTO operator-waiting. The collapse
  // semantics are owned by the host (canvas = minimize/no-op); we just
  // signal the intent.
  if (operatorWaiting && prevCls !== "waiting" && t.pane.classList.contains("collapsed")) {
    paneCoreT_host(t).setCollapsed(t.jobId, false);
  }
}

// Render a calm one-line "no transcript" note into a session pane body when
// the stream never connected (the session has no history yet). Idempotent:
// it replaces any prior note rather than appending duplicates.
function paneCoreSessionEmptyNote(t) {
  if (!t || !t.body) return;
  let note = t.body.querySelector(".session-empty-note");
  if (!note) {
    note = document.createElement("div");
    note.className = "session-empty-note";
    t.body.appendChild(note);
  }
  note.textContent = "This session has no transcript yet — nothing to show.";
}

function paneCoreSetDead(t, label) {
  paneCoreClearThinking(t);
  if (t._composerTimer) { clearTimeout(t._composerTimer); t._composerTimer = null; }
  if (t._popOpen) { termCloseAutocomplete(t); }
  if (t.toolUseEls && typeof t.toolUseEls.clear === "function") t.toolUseEls.clear();
  if (t._waitingMsg) t._waitingMsg = null;
  t.pane.classList.add("dead");
  const status = t.pane.querySelector(".status-pill");
  if (status && label) {
    status.classList.remove("running", "done", "bad", "warn", "queued", "cancelling", "cancelled");
    status.className = "pill " + (label === "done" ? "done" : "bad") + " status-pill";
    status.textContent = label;
  }
  paneCoreSetActivity(t, label || "ended", label === "done" ? "ready" : "ended");
  if (t.input) t.input.disabled = true;
  if (t.sendBtn) t.sendBtn.disabled = true;
}

// termClearThinkingPlaceholder is a pure leaf in pane-helpers.js. Use a thin
// guarded local so the engine still works if the page omits it.
function paneCoreClearThinking(t) {
  if (typeof termClearThinkingPlaceholder === "function") { termClearThinkingPlaceholder(t); return; }
  if (t && t.body) t.body.querySelectorAll(".thinking-placeholder").forEach((el) => el.remove());
}

// ----- auto-scroll -----
function paneCoreAutoScroll(t) {
  if (!t.autoFollowBottom) return;
  if (t._markProgrammaticScroll) t._markProgrammaticScroll();
  if (t.firstScroll) {
    t.firstScroll = false;
    requestAnimationFrame(() => {
      try { t.body.scrollTo({ top: t.body.scrollHeight, behavior: "smooth" }); }
      catch (_) { t.body.scrollTop = t.body.scrollHeight; }
    });
    return;
  }
  t.body.scrollTop = t.body.scrollHeight;
}

// ----- in-pane search (Ctrl+F). Internal copies under DISTINCT names so the
// terminals.js-pinning tests are irrelevant to these. -----
var PANE_CORE_SEARCH_NODE_CAP = 20000;
function paneCoreToggleSearch(t, open) {
  const bar = t.pane.querySelector(".term-search");
  const wantOpen = open === undefined ? !bar.classList.contains("open") : open;
  bar.classList.toggle("open", wantOpen);
  if (wantOpen) { bar.querySelector("input").focus(); paneCoreRunSearch(t); }
  else { paneCoreClearSearchHighlights(t); }
}
function paneCoreClearSearchHighlights(t) {
  t.body.querySelectorAll("mark.term-hit").forEach((m) => {
    const txt = document.createTextNode(m.textContent);
    m.parentNode.replaceChild(txt, m);
  });
  if (t._searchActive) { t.body.normalize(); t._searchActive = false; }
  t._searchHits = [];
  t._searchIdx = 0;
  const m = t.pane.querySelector(".term-search .matches");
  if (m) m.textContent = "0 / 0";
}
function paneCoreRunSearch(t) {
  paneCoreClearSearchHighlights(t);
  const q = t.pane.querySelector(".term-search input").value;
  if (!q) return;
  const lower = q.toLowerCase();
  const walker = document.createTreeWalker(t.body, NodeFilter.SHOW_TEXT, null);
  const targets = [];
  let scanned = 0;
  while (walker.nextNode()) {
    if (++scanned > PANE_CORE_SEARCH_NODE_CAP) break;
    const n = walker.currentNode;
    if (!n.nodeValue) continue;
    if (n.parentElement.closest(".term-search, mark.term-hit")) continue;
    if (n.nodeValue.toLowerCase().includes(lower)) targets.push(n);
  }
  const hits = [];
  for (const n of targets) {
    const text = n.nodeValue;
    const parent = n.parentNode;
    let cursor = 0;
    const frag = document.createDocumentFragment();
    let i;
    while ((i = text.toLowerCase().indexOf(lower, cursor)) !== -1) {
      if (i > cursor) frag.appendChild(document.createTextNode(text.slice(cursor, i)));
      const mark = document.createElement("mark");
      mark.className = "term-hit";
      mark.textContent = text.slice(i, i + lower.length);
      frag.appendChild(mark);
      hits.push(mark);
      cursor = i + lower.length;
    }
    if (cursor < text.length) frag.appendChild(document.createTextNode(text.slice(cursor)));
    parent.replaceChild(frag, n);
  }
  t._searchHits = hits;
  t._searchIdx = 0;
  t._searchActive = hits.length > 0;
  const matches = t.pane.querySelector(".term-search .matches");
  if (matches) matches.textContent = hits.length ? "1 / " + hits.length : "0 / 0";
  if (hits.length) {
    hits[0].classList.add("current");
    try { hits[0].scrollIntoView({ block: "center", behavior: "smooth" }); } catch (_) {}
  }
}
function paneCoreSearchStep(t, delta) {
  const hits = t._searchHits || [];
  if (!hits.length) return;
  hits[t._searchIdx]?.classList.remove("current");
  t._searchIdx = (t._searchIdx + delta + hits.length) % hits.length;
  const next = hits[t._searchIdx];
  next.classList.add("current");
  try { next.scrollIntoView({ block: "center", behavior: "smooth" }); } catch (_) {}
  const m = t.pane.querySelector(".term-search .matches");
  if (m) m.textContent = (t._searchIdx + 1) + " / " + hits.length;
}

// ----- model-label helpers -----
function paneCoreFormatModel(model) {
  if (!model) return "";
  return String(model)
    .replace(/-\d{8}$/, "")
    .replace(/-(\d+)-(\d+)(?=$|-)/, " $1.$2")
    .replace(/-/g, " ")
    .toUpperCase();
}
function paneCoreAssistantRoleLabel(t) {
  if (t.model) return paneCoreFormatModel(t.model);
  if (t.kind === "chat") return "claude";
  if (t.kind === "chat-codex") return "codex";
  return "assistant";
}
function paneCoreSetPaneModel(t, model) {
  if (!model || t.model === model) return;
  t.model = model;
  const label = paneCoreFormatModel(model);
  const title = "model: " + model;
  t.body.querySelectorAll(".msg.assistant:not(.thinking-placeholder) .role").forEach((r) => {
    if (r.dataset.roleLocked === "1") return;
    r.textContent = label;
    r.title = title;
  });
}

// ----- chat message render leaves -----
function paneCoreShowThinking(t) {
  if (!t || !t.body) return;
  if (t.kind !== "chat") return;
  paneCoreClearThinking(t);
  const msg = document.createElement("div");
  msg.className = "msg assistant thinking-placeholder";
  msg.innerHTML = `<div class="role">thinking</div>`
    + `<div class="thinking-dots" aria-label="generating response">`
    + `<span class="dot"></span><span class="dot"></span><span class="dot"></span>`
    + `</div>`;
  t.body.appendChild(msg);
  paneCoreSetActivity(t, "thinking…", "busy");
  paneCoreAutoScroll(t);
}
function paneCoreRenderUserMessage(t, text) {
  const msg = document.createElement("div");
  msg.className = "msg user";
  msg.innerHTML = `<div class="role">user</div><div class="text"></div>`;
  msg.querySelector(".text").textContent = text;
  t.body.appendChild(msg);
  t.currentAssistant = null;
}
function paneCoreAssistantBlock(t) {
  if (t.currentAssistant && t.currentAssistant.isConnected) return t.currentAssistant;
  paneCoreClearThinking(t);
  const msg = document.createElement("div");
  msg.className = "msg assistant";
  const label = paneCoreAssistantRoleLabel(t);
  const titleAttr = t.model ? ` title="model: ${escape(t.model)}"` : "";
  msg.innerHTML = `<div class="role"${titleAttr}>${escape(label)}</div><div class="text"></div>`;
  t.body.appendChild(msg);
  t.currentAssistant = msg;
  return msg;
}
function paneCoreAppendAssistantText(t, text) {
  if (!text) return;
  const block = paneCoreAssistantBlock(t);
  const textEl = block.querySelector(".text");
  if (!Array.isArray(textEl._rawBuf)) {
    textEl._rawBuf = textEl.dataset.raw ? [textEl.dataset.raw] : [];
  }
  textEl._rawBuf.push(text);
  if (textEl._renderPending) { paneCoreSetActivity(t, "responding…", "busy"); return; }
  textEl._renderPending = true;
  requestAnimationFrame(() => {
    textEl._renderPending = false;
    if (!textEl.isConnected) { textEl._rawBuf = []; return; }
    const latest = (textEl._rawBuf || []).join("");
    textEl.dataset.raw = latest;
    try { textEl.innerHTML = DOMPurify.sanitize(marked.parse(latest)); }
    catch (_) { textEl.textContent = latest; }
  });
  paneCoreSetActivity(t, "responding…", "busy");
}

// ----- inline tool-detail renderers -----
function paneCoreRenderEditDiff(filePath, oldStr, newStr) {
  const wrap = document.createElement("div");
  wrap.className = "tool-detail diff-view";
  if (filePath) {
    const h = document.createElement("div");
    h.className = "diff-header";
    h.textContent = filePath;
    wrap.appendChild(h);
  }
  for (const part of paneCoreSimpleLineDiff(oldStr || "", newStr || "")) {
    const line = document.createElement("div");
    line.className = "diff-line " + part.kind;
    const prefix = part.kind === "removed" ? "- " : part.kind === "added" ? "+ " : "  ";
    line.textContent = prefix + part.text;
    wrap.appendChild(line);
  }
  return wrap;
}
function paneCoreRenderNewFile(filePath, content) {
  const wrap = document.createElement("div");
  wrap.className = "tool-detail diff-view";
  const h = document.createElement("div");
  h.className = "diff-header";
  h.textContent = (filePath || "(new file)") + "  · " + content.split("\n").length + " lines";
  wrap.appendChild(h);
  for (const ln of content.split("\n")) {
    const line = document.createElement("div");
    line.className = "diff-line added";
    line.textContent = "+ " + ln;
    wrap.appendChild(line);
  }
  return wrap;
}
function paneCoreRenderReadIntent(filePath, offset, limit) {
  const wrap = document.createElement("div");
  wrap.className = "tool-detail diff-view";
  const h = document.createElement("div");
  h.className = "diff-header";
  const range = (offset != null || limit != null)
    ? "  · lines " + (offset ?? 1) + "–" + ((offset ?? 1) + (limit ?? 2000) - 1)
    : "";
  h.textContent = filePath + range;
  wrap.appendChild(h);
  return wrap;
}
function paneCoreRenderGrep(input) {
  const wrap = document.createElement("div");
  wrap.className = "tool-detail bash-view";
  const h = document.createElement("div");
  h.className = "diff-header";
  const where = input.path ? " in " + input.path : "";
  const glob = input.glob ? " (glob: " + input.glob + ")" : "";
  const type = input.type ? " (type: " + input.type + ")" : "";
  h.textContent = "Grep: /" + input.pattern + "/" + where + glob + type;
  wrap.appendChild(h);
  return wrap;
}
function paneCoreRenderGlob(input) {
  const wrap = document.createElement("div");
  wrap.className = "tool-detail bash-view";
  const h = document.createElement("div");
  h.className = "diff-header";
  const where = input.path ? " in " + input.path : "";
  h.textContent = "Glob: " + input.pattern + where;
  wrap.appendChild(h);
  return wrap;
}
function paneCoreRenderWebTool(name, input) {
  const wrap = document.createElement("div");
  wrap.className = "tool-detail bash-view";
  const h = document.createElement("div");
  h.className = "diff-header";
  h.textContent = name + ": " + (input.url || input.query || "");
  wrap.appendChild(h);
  if (input.prompt) {
    const p = document.createElement("pre");
    p.className = "bash-cmd";
    p.textContent = input.prompt;
    wrap.appendChild(p);
  }
  return wrap;
}
function paneCoreFallbackDiffStub(oldLines, newLines) {
  const n = oldLines.length, m = newLines.length;
  return [
    { kind: "common", text: "(diff too large to display inline; " + n + " old / " + m + " new lines)" },
    ...oldLines.map((ln) => ({ kind: "removed", text: ln })),
    ...newLines.map((ln) => ({ kind: "added", text: ln })),
  ];
}
var PANE_CORE_DIFF_CELL_CAP = 100000;
function paneCoreSimpleLineDiff(oldStr, newStr) {
  const oldLines = oldStr.split("\n");
  const newLines = newStr.split("\n");
  const n = oldLines.length, m = newLines.length;
  if (oldLines.length * newLines.length > PANE_CORE_DIFF_CELL_CAP) {
    return paneCoreFallbackDiffStub(oldLines, newLines);
  }
  const a = oldLines, b = newLines;
  const dp = new Array(n + 1);
  for (let i = 0; i <= n; i++) dp[i] = new Int32Array(m + 1);
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
    }
  }
  const out = [];
  let i = 0, j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) { out.push({ kind: "common", text: a[i] }); i++; j++; }
    else if (dp[i + 1][j] >= dp[i][j + 1]) { out.push({ kind: "removed", text: a[i] }); i++; }
    else { out.push({ kind: "added", text: b[j] }); j++; }
  }
  while (i < n) out.push({ kind: "removed", text: a[i++] });
  while (j < m) out.push({ kind: "added", text: b[j++] });
  return out;
}
function paneCoreRenderTodoWrite(t, toolUseId, input) {
  const block = paneCoreAssistantBlock(t);
  const textEl = block.querySelector(".text");
  const todos = Array.isArray(input?.todos) ? input.todos : [];
  const done = todos.filter((x) => x?.status === "completed").length;
  const wrap = document.createElement("div");
  wrap.className = "todo-widget";
  const header = document.createElement("div");
  header.className = "todo-header";
  header.innerHTML = `<span>TodoWrite</span><span class="meta">${done}/${todos.length} done</span>`;
  wrap.appendChild(header);
  const ul = document.createElement("ul");
  ul.className = "todo-list";
  for (const todo of todos) {
    const li = document.createElement("li");
    const status = todo?.status || "pending";
    li.className = "todo-item " + status;
    const label = (status === "in_progress" && todo?.activeForm) ? todo.activeForm : (todo?.content ?? todo?.activeForm ?? "(unnamed)");
    const labelEl = document.createElement("span");
    labelEl.className = "todo-label";
    labelEl.textContent = label;
    li.appendChild(labelEl);
    ul.appendChild(li);
  }
  wrap.appendChild(ul);
  textEl.appendChild(wrap);
  t.toolUseEls.set(toolUseId, { pill: wrap, detail: null });
}
function paneCoreSummariseToolInput(input) {
  if (!input || typeof input !== "object") return "";
  const keys = Object.keys(input);
  if (!keys.length) return "";
  const candidate = ["command", "file_path", "path", "pattern", "url", "query"]
    .find((k) => typeof input[k] === "string" && input[k]);
  if (candidate) {
    const v = String(input[candidate]);
    return v.length > 60 ? v.slice(0, 57) + "…" : v;
  }
  return "(" + keys.slice(0, 3).join(", ") + (keys.length > 3 ? "…" : "") + ")";
}
function paneCoreAddToolPill(t, toolUseId, name, input) {
  paneCoreSetActivity(t, "tool: " + (name || "?"), "busy");
  if (name === "TodoWrite") return paneCoreRenderTodoWrite(t, toolUseId, input);
  const block = paneCoreAssistantBlock(t);
  const textEl = block.querySelector(".text");
  const wrap = document.createElement("div");
  const pill = document.createElement("span");
  pill.className = "tool-pill";
  pill.setAttribute("role", "button");
  pill.setAttribute("tabindex", "0");
  pill.setAttribute("aria-expanded", "false");
  const argSummary = paneCoreSummariseToolInput(input);
  pill.textContent = name + (argSummary ? "  " + argSummary : "");
  let detail;
  if (name === "Edit" && typeof input?.old_string === "string" && typeof input?.new_string === "string") {
    detail = paneCoreRenderEditDiff(input.file_path, input.old_string, input.new_string);
  } else if (name === "Write" && typeof input?.content === "string") {
    detail = paneCoreRenderNewFile(input.file_path, input.content);
  } else if (name === "Read" && input?.file_path) {
    detail = paneCoreRenderReadIntent(input.file_path, input.offset, input.limit);
  } else if (name === "Bash" && typeof input?.command === "string") {
    detail = renderBashCommand(input.command, input.description);
  } else if (name === "Grep" && typeof input?.pattern === "string") {
    detail = paneCoreRenderGrep(input);
  } else if (name === "Glob" && typeof input?.pattern === "string") {
    detail = paneCoreRenderGlob(input);
  } else if ((name === "WebFetch" || name === "WebSearch") && (input?.url || input?.query)) {
    detail = paneCoreRenderWebTool(name, input);
  } else {
    detail = document.createElement("pre");
    detail.className = "tool-detail";
    detail.textContent = JSON.stringify(input ?? {}, null, 2);
  }
  const togglePill = () => {
    const open = detail.classList.toggle("open");
    pill.setAttribute("aria-expanded", open ? "true" : "false");
  };
  pill.addEventListener("click", togglePill);
  pill.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); togglePill(); }
  });
  wrap.appendChild(pill);
  wrap.appendChild(detail);
  textEl.appendChild(wrap);
  if (toolUseId) t.toolUseEls.set(toolUseId, { pill, detail });
  // NOTE (isolated canvas renderer): terminals.js auto-opens a "dispatch
  // tracker" pane here when a Bash tool spawns an LLM (codex exec / claude
  // -p). That affordance is LIST-ONLY — it reaches #terms-grid, termGetLayout,
  // the AUTO_OPENED_ONCE suppression set, and termAutoOpenEnabled. The canvas
  // has no list and opens panes via the bus, not auto-spawn, so we render the
  // Bash pill plainly and do NOT auto-open a tracker. (A canvas operator opens
  // the dispatched session pane explicitly from the status list instead.)
}
function paneCoreMarkToolResult(t, toolUseId, isError, content) {
  const entry = t.toolUseEls.get(toolUseId);
  if (entry) {
    entry.pill.classList.add(isError ? "error" : "result");
    if (entry.detail) {
      const result = "\n--- result ---\n" + (typeof content === "string" ? content : JSON.stringify(content, null, 2));
      entry.detail.textContent += result;
    }
  }
  // (No dispatch-tracker forwarding here — see paneCoreAddToolPill note.)
}
function paneCoreRenderSystem(t, obj) {
  const sub = obj.subtype || obj.type;
  const div = document.createElement("div");
  div.className = "msg system";
  div.textContent = `[${obj.type}${sub && sub !== obj.type ? ":" + sub : ""}]`;
  if (sub === "init" || sub === "shutdown" || /error/i.test(String(sub))) {
    t.body.appendChild(div);
  }
}
function paneCoreRenderResult(t, obj) {
  paneCoreClearThinking(t);
  const div = document.createElement("div");
  const subtype = String(obj.subtype || obj.result || "").toLowerCase();
  const isError = obj.is_error === true
    || (subtype && subtype !== "success" && /error|fail|max_turns|interrupt/i.test(subtype));
  div.className = "msg result" + (isError ? " result-error" : "");
  const usd = (obj.cost_usd ?? obj.total_cost_usd);
  const dur = (obj.duration_ms != null) ? `${(obj.duration_ms / 1000).toFixed(1)}s` : "";
  const turns = (obj.num_turns != null) ? `${obj.num_turns}t` : "";
  const cost = (usd != null) ? `$${Number(usd).toFixed(4)}` : "";
  const meta = [dur, turns, cost].filter(Boolean).join(" · ");
  const label = isError ? (subtype || "error") : "done";
  div.textContent = `[${label}${meta ? "  " + meta : ""}]`;
  if (isError) div.style.color = "var(--bad)";
  t.body.appendChild(div);
  t.currentAssistant = null;
  termRefreshCost(t);
  try { window.scheduleTokenUsageRefresh?.(); } catch (_) {}
  paneCoreSetActivity(t, isError ? label : "waiting", isError ? "ended" : "waiting");
  paneCoreNotifyTurnComplete(t, meta);
}
var paneCoreNotifyPermAsked = false;
function paneCoreNotifyTurnComplete(t, metaStr) {
  if (typeof Notification === "undefined") return;
  if (typeof document !== "undefined" && document.visibilityState === "visible" && document.hasFocus()) return;
  const fire = () => {
    try {
      const title = (t.task || "Chat").slice(0, 80);
      const body = "Turn finished" + (metaStr ? "  ·  " + metaStr : "");
      const n = new Notification(title, { body, tag: "term-" + t.jobId, silent: false });
      n.onclick = () => { window.focus(); try { t.pane.scrollIntoView({ behavior: "smooth" }); } catch (_) {} n.close(); };
      setTimeout(() => { try { n.close(); } catch (_) {} }, 8000);
    } catch (_) {}
  };
  if (Notification.permission === "granted") return fire();
  if (Notification.permission === "denied") return;
  if (paneCoreNotifyPermAsked) return;
  paneCoreNotifyPermAsked = true;
  try { Notification.requestPermission().then((p) => { if (p === "granted") fire(); }); } catch (_) {}
}

// ----- user-prompt cleaner (strip IDE/system wrapper envelopes) -----
function paneCoreCleanUserPrompt(text) {
  if (!text) return null;
  let s = String(text);
  s = s.replace(/<system-reminder>[\s\S]*?<\/system-reminder>/g, "");
  s = s.replace(/<EXTREMELY[_-]IMPORTANT>[\s\S]*?<\/EXTREMELY[_-]IMPORTANT>/g, "");
  s = s.replace(/<ide_opened_file>[\s\S]*?<\/ide_opened_file>/g, "");
  s = s.replace(/<ide_selection>[\s\S]*?<\/ide_selection>/g, "");
  s = s.replace(/<task-notification>[\s\S]*?<\/task-notification>/g, "");
  s = s.replace(/<local-command-stdout>[\s\S]*?<\/local-command-stdout>/g, "");
  s = s.replace(/<local-command-stderr>[\s\S]*?<\/local-command-stderr>/g, "");
  const nameMatch = s.match(/<command-name>([^<]*)<\/command-name>/);
  const argsMatch = s.match(/<command-args>([\s\S]*?)<\/command-args>/);
  if (nameMatch) {
    const name = (nameMatch[1] || "").trim();
    const args = (argsMatch ? (argsMatch[1] || "") : "").trim();
    s = s.replace(/<command-[\w-]+>[\s\S]*?<\/command-[\w-]+>/g, "");
    const compact = (name + (args ? " " + args : "")).trim();
    const rest = s.trim();
    return rest ? `${compact}\n\n${rest}` : (compact || null);
  }
  s = s.trim();
  return s || null;
}

// ----- stream-json (Claude) object render -----
var PANE_CORE_TRANSCRIPT_META_NOISE = new Set([
  "attachment", "queue-operation", "file-history-snapshot",
  "summary", "compaction", "last-prompt",
]);
function paneCoreRenderJsonObject(t, obj) {
  if (!obj || typeof obj !== "object") return;
  const type = obj.type;
  if (PANE_CORE_TRANSCRIPT_META_NOISE.has(type)) return;
  const declaredModel = obj.model || (obj.message && obj.message.model);
  if (declaredModel) paneCoreSetPaneModel(t, declaredModel);
  if (type === "ai-title" && typeof obj.aiTitle === "string") {
    const head = t.pane.querySelector(".term-head .task");
    if (head) head.textContent = obj.aiTitle;
    return;
  }
  if (type === "system") return paneCoreRenderSystem(t, obj);
  if (type === "result") return paneCoreRenderResult(t, obj);
  if (type === "assistant" && obj.message) {
    const content = obj.message.content;
    if (Array.isArray(content)) {
      for (const blk of content) {
        if (blk.type === "text" && typeof blk.text === "string") {
          const cur = t.currentAssistant;
          const accSoFar = cur ? (cur.querySelector(".text").dataset.raw || "") : "";
          if (!accSoFar) paneCoreAppendAssistantText(t, blk.text);
        } else if (blk.type === "tool_use") {
          if (!t.toolUseEls.has(blk.id)) paneCoreAddToolPill(t, blk.id, blk.name, blk.input);
        } else if (blk.type === "thinking" && typeof blk.thinking === "string") {
          const block = paneCoreAssistantBlock(t);
          const t2 = block.querySelector(".text");
          const det = document.createElement("details");
          det.className = "thinking-block";
          const sum = document.createElement("summary");
          sum.textContent = `thinking · ${blk.thinking.length} chars`;
          const pre = document.createElement("pre");
          pre.textContent = blk.thinking;
          det.appendChild(sum);
          det.appendChild(pre);
          t2.appendChild(det);
        }
      }
    } else if (typeof content === "string") {
      t.currentAssistant = null;
      paneCoreAppendAssistantText(t, content);
    }
    paneCoreSetActivity(t, "waiting", "waiting");
    return;
  }
  if (type === "user" && obj.message) {
    const content = obj.message.content;
    if (typeof content === "string") {
      const cleaned = paneCoreCleanUserPrompt(content);
      if (cleaned) paneCoreRenderUserMessage(t, cleaned);
    } else if (Array.isArray(content)) {
      for (const blk of content) {
        if (blk.type === "tool_result") {
          paneCoreMarkToolResult(t, blk.tool_use_id, !!blk.is_error, blk.content);
        } else if (blk.type === "text" && typeof blk.text === "string") {
          const cleaned = paneCoreCleanUserPrompt(blk.text);
          if (cleaned) paneCoreRenderUserMessage(t, cleaned);
        }
      }
    }
    return;
  }
  if (type === "stream_event") {
    const ev = obj.event || {};
    if (ev.type === "content_block_delta" && ev.delta && ev.delta.type === "text_delta") {
      paneCoreAppendAssistantText(t, ev.delta.text || "");
    } else if (ev.type === "content_block_start" && ev.content_block) {
      const cb = ev.content_block;
      if (cb.type === "tool_use") paneCoreAddToolPill(t, cb.id, cb.name, cb.input || {});
    }
    return;
  }
  const pre = document.createElement("pre");
  pre.style.color = "var(--text-dim)";
  pre.style.fontSize = "11px";
  pre.style.margin = "4px 0";
  pre.textContent = "[unhandled " + (type || "?") + "]";
  t.body.appendChild(pre);
}
function paneCoreHandleChatChunk(t, chunk) {
  if (!Array.isArray(t.jsonBuf)) t.jsonBuf = t.jsonBuf ? [t.jsonBuf] : [];
  t.jsonBuf.push(chunk);
  const joined = t.jsonBuf.join("");
  const lastNl = joined.lastIndexOf("\n");
  if (lastNl === -1) { paneCoreAutoScroll(t); return; }
  const complete = joined.slice(0, lastNl);
  const remnant = joined.slice(lastNl + 1);
  t.jsonBuf = remnant ? [remnant] : [];
  const carry = [];
  const lines = complete.split("\n");
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const trimmed = line.trim();
    if (!trimmed) continue;
    let obj;
    try { obj = JSON.parse(trimmed); }
    catch (_) {
      if (trimmed.startsWith("{") && !trimmed.endsWith("}") && i === lines.length - 1) {
        carry.push(line);
        continue;
      }
      termRenderRaw(t, line);
      continue;
    }
    paneCoreRenderJsonObject(t, obj);
  }
  if (carry.length) t.jsonBuf = (t.jsonBuf || []).concat(carry);
  paneCoreAutoScroll(t);
}

// ----- codex event render -----
function paneCoreRenderCodexEvent(t, obj) {
  if (!obj || typeof obj !== "object") return;
  const type = obj.type;
  const payload = obj.payload || {};
  if (type === "session_meta") {
    if (payload.id && !t.sessionId) t.sessionId = payload.id;
    return;
  }
  if (type === "turn_context") {
    const m = payload.model;
    if (m && typeof m === "string") paneCoreSetPaneModel(t, m);
    return;
  }
  if (type === "response_item") {
    const role = payload.role;
    const kind = payload.type;
    if (kind === "message" && role === "assistant" && Array.isArray(payload.content)) {
      t.currentAssistant = null;
      const text = payload.content.map((c) => c.text || c.output_text || "").join("");
      if (text) paneCoreAppendAssistantText(t, text);
      return;
    }
    if (kind === "reasoning" && Array.isArray(payload.content)) {
      const block = paneCoreAssistantBlock(t);
      const txtEl = block.querySelector(".text");
      const det = document.createElement("details");
      det.className = "thinking-block";
      const sum = document.createElement("summary");
      const txt = payload.content.map((c) => c.text || "").join("\n");
      sum.textContent = `reasoning · ${txt.length} chars`;
      const pre = document.createElement("pre");
      pre.textContent = txt;
      det.appendChild(sum);
      det.appendChild(pre);
      txtEl.appendChild(det);
      return;
    }
    if (kind === "function_call") {
      const name = payload.name || "(tool)";
      let args = {};
      try { args = JSON.parse(payload.arguments || "{}"); } catch (e) { console.warn("[pane-core] codex function_call args parse failed: " + (e && e.message ? e.message : e)); }
      const callId = payload.call_id || payload.id || null;
      paneCoreAddToolPill(t, callId, name, args);
      return;
    }
    if (kind === "function_call_output") {
      const callId = payload.call_id || payload.id;
      if (callId) paneCoreMarkToolResult(t, callId, false, payload.output || "");
      return;
    }
    return;
  }
  if (type === "event_msg") {
    const sub = payload.type;
    if (sub === "agent_message_delta") { paneCoreAppendAssistantText(t, payload.delta || ""); return; }
    if (sub === "task_started") { paneCoreSetActivity(t, "thinking…", "busy"); return; }
    if (sub === "task_complete") { paneCoreSetActivity(t, "responding…", "busy"); return; }
    return;
  }
}
function paneCoreHandleCodexChunk(t, chunk) {
  if (!chunk) return;
  if (!Array.isArray(t.jsonBuf)) t.jsonBuf = t.jsonBuf ? [t.jsonBuf] : [];
  t.jsonBuf.push(chunk);
  const joined = t.jsonBuf.join("");
  const lastNl = joined.lastIndexOf("\n");
  if (lastNl === -1) { paneCoreAutoScroll(t); return; }
  const complete = joined.slice(0, lastNl);
  const remnant = joined.slice(lastNl + 1);
  t.jsonBuf = remnant ? [remnant] : [];
  for (const line of complete.split("\n")) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    let obj;
    try { obj = JSON.parse(trimmed); } catch (_) { continue; }
    paneCoreRenderCodexEvent(t, obj);
  }
  paneCoreAutoScroll(t);
}

// ----- generic (orchestrate/plan) raw chunk render -----
function paneCoreAppendChunk(t, chunk) {
  if (!t || !t.body) return;
  const MAX = 200000;
  const span = document.createElement("span");
  span.textContent = chunk;
  t.body.appendChild(span);
  if (t.body.textContent.length > MAX) {
    t.body.textContent = t.body.textContent.slice(-MAX);
  }
  paneCoreSetActivity(t, "streaming…", "busy");
  paneCoreAutoScroll(t);
}

// ----- codex multi-turn (one job per turn, SSE rewired in-place) -----
function paneCoreCodexBeginTurn(t) {
  if (t.input) t.input.disabled = true;
  if (t.sendBtn) { t.sendBtn.disabled = true; t.sendBtn.textContent = "running…"; }
  paneCoreSetActivity(t, "running…", "busy");
}
async function paneCoreCodexAwaitNextTurn(t) {
  if (t._codexAwaitInFlight) return;
  t._codexAwaitInFlight = true;
  try {
    paneCoreClearThinking(t);
    paneCoreSetActivity(t, "waiting", "waiting");
    termSetPillState(t.pane.querySelector(".status-pill"), "done", "ready");
    if (t.input) {
      t.input.disabled = false;
      t.input.placeholder = "type, /skill, @file — Enter sends next turn (Codex resumes session)";
    }
    if (t.sendBtn) { t.sendBtn.disabled = false; t.sendBtn.textContent = "send"; }
    try {
      const r = await fetch(`/api/jobs/${t.jobId}`, { cache: "no-store" });
      if (r.ok) {
        const j = await r.json();
        if (j.session_id) t.sessionId = j.session_id;
        if (j.model) t.model = j.model;
      }
    } catch (_) {}
    termRefreshCost(t);
  } finally {
    t._codexAwaitInFlight = false;
  }
}
async function paneCoreSendCodexNextTurn(t, text, attached) {
  if (!t.sessionId) {
    try {
      const r = await fetch(`/api/jobs/${t.jobId}`, { cache: "no-store" });
      if (r.ok) { const j = await r.json(); if (j.session_id) t.sessionId = j.session_id; }
    } catch (_) {}
  }
  if (!t.sessionId) {
    const err = document.createElement("div");
    err.className = "msg system";
    err.style.color = "var(--bad)";
    err.textContent = "[codex session id unavailable — wait for the current turn to finish before sending again]";
    t.body.appendChild(err);
    setMsg("#term-msg", "err", "Codex session not yet captured; try again in a moment.", TERM_MSG_DURATION_MS);
    return;
  }
  paneCoreRenderUserMessage(t, text);
  paneCoreShowThinking(t);
  t.input.value = "";
  if (t.input.tagName === "TEXTAREA") t.input.style.height = "";
  t.attached = { images: [], files: [] };
  paneCoreRenderAttachments(t);
  paneCoreCodexBeginTurn(t);
  const h = paneCoreT_host(t);
  try {
    const payload = { kind: "chat-codex", task: text, resume_session_id: t.sessionId };
    if (t.model) payload.model = t.model;
    const res = await postJson("/api/jobs", payload);
    // Re-key the host registry so the same pane is reachable by its NEW job id.
    var oldJobId = t.jobId;
    h.unregister(t.jobId);
    t.jobId = res.id;
    t.pane.dataset.jobId = res.id;
    h.register(res.id, t._paneHandle || t);
    if (oldJobId && oldJobId !== res.id) {
      window._JOB_ID_ALIASES = window._JOB_ID_ALIASES || {};
      window._JOB_ID_ALIASES[oldJobId] = res.id;
    }
    h.persist();
    const idEl = t.pane.querySelector(".id");
    if (idEl) idEl.textContent = res.id.slice(0, 8);
    termSetPillState(t.pane.querySelector(".status-pill"), "running", "connecting");
    try { t.source && t.source.close(); } catch (_) {}
    t.jsonBuf = [];
    t.currentAssistant = null;
    const es = new EventSource(`/api/jobs/${res.id}/stream`);
    t.source = es;
    es.onopen = () => {
      termSetPillState(t.pane.querySelector(".status-pill"), "running", "live");
      paneCoreSetActivity(t, "live", "busy");
    };
    es.onmessage = (ev) => paneCoreHandleCodexChunk(t, ev.data);
    es.addEventListener("end", () => {
      try { es.close(); } catch (_) {}
      paneCoreCodexAwaitNextTurn(t);
      paneCoreLoadJobs().catch((e) => console.warn("[pane-core] loadJobs after codex end failed: " + (e && e.message ? e.message : e)));
    });
    es.onerror = () => {
      if (t.pane.classList.contains("dead")) return;
      if (es.readyState !== EventSource.CLOSED) return;
      paneCoreCodexAwaitNextTurn(t);
    };
    paneCoreLoadJobs().catch((e) => console.warn("[pane-core] loadJobs after codex rekey failed: " + (e && e.message ? e.message : e)));
  } catch (e) {
    paneCoreClearThinking(t);
    const err = document.createElement("div");
    err.className = "msg system";
    err.style.color = "var(--bad)";
    err.textContent = `[next turn failed: ${e.message}]`;
    t.body.appendChild(err);
    setMsg("#term-msg", "err", "next turn failed: " + e.message, TERM_MSG_DURATION_MS);
    if (t.input) t.input.disabled = false;
    if (t.sendBtn) { t.sendBtn.disabled = false; t.sendBtn.textContent = "send"; }
    paneCoreSetActivity(t, "error", "ended");
  }
}

// ----- generic job send (orchestrate / plan); chat-codex routes to next-turn -----
async function paneCoreSend(t) {
  if (!t) return;
  const text = t.input.value;
  const attached = t.attached || { images: [], files: [] };
  if (!text.trim() && !attached.images.length && !attached.files.length) return;
  if (t.kind === "chat-codex") { await paneCoreSendCodexNextTurn(t, text, attached); return; }
  t.sendBtn.disabled = true;
  try {
    const payload = { text };
    if (attached.images.length) payload.images = attached.images;
    if (attached.files.length) payload.files = attached.files;
    await postJson(`/api/jobs/${t.jobId}/input`, payload);
    const echo = document.createElement("span");
    echo.className = "stdin-echo";
    echo.textContent = `\n> ${text}\n`;
    t.body.appendChild(echo);
    t.body.scrollTop = t.body.scrollHeight;
    t.input.value = "";
    if (t.input.tagName === "TEXTAREA") t.input.style.height = "";
    t.attached = { images: [], files: [] };
    paneCoreRenderAttachments(t);
  } catch (e) {
    const err = document.createElement("span");
    err.style.color = "var(--bad)";
    err.textContent = `\n[input failed: ${e.message}]\n`;
    t.body.appendChild(err);
    setMsg("#term-msg", "err", "Send failed: " + e.message, TERM_MSG_DURATION_MS);
    if (/not running|409/i.test(e.message)) paneCoreSetDead(t, "ended");
  } finally {
    t.sendBtn.disabled = false;
    t.input.focus();
  }
}

// ----- composer: image attachments + @/ autocomplete (copied) -----
var PANE_CORE_IMAGE_PASTE_MAX_BYTES = 5 * 1024 * 1024;
function paneCoreRenderAttachments(t) {
  const tray = t.pane.querySelector(".attach-tray");
  if (!tray) return;
  const a = t.attached || { images: [], files: [] };
  if (!a.images.length && !a.files.length) {
    tray.style.display = "none";
    tray.innerHTML = "";
    return;
  }
  tray.style.display = "flex";
  tray.innerHTML = "";
  const wireChipKeyboard = (chip, onRemove) => {
    chip.setAttribute("role", "button");
    chip.setAttribute("tabindex", "0");
    chip.setAttribute("aria-label", "Remove attachment");
    chip.addEventListener("click", onRemove);
    chip.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " " || e.key === "Backspace" || e.key === "Delete") {
        e.preventDefault();
        onRemove();
      }
    });
  };
  a.files.forEach((f, i) => {
    const chip = document.createElement("span");
    chip.className = "attach-chip";
    chip.textContent = "@ " + f + "  ×";
    wireChipKeyboard(chip, () => { a.files.splice(i, 1); paneCoreRenderAttachments(t); });
    tray.appendChild(chip);
  });
  a.images.forEach((img, i) => {
    const chip = document.createElement("span");
    chip.className = "attach-chip";
    const src = "data:" + (img.media_type || "image/png") + ";base64," + img.data;
    const thumb = document.createElement("img");
    thumb.src = src;
    thumb.style.height = "18px";
    thumb.style.verticalAlign = "middle";
    thumb.style.borderRadius = "2px";
    thumb.style.marginRight = "6px";
    chip.appendChild(thumb);
    chip.appendChild(document.createTextNode("image  ×"));
    wireChipKeyboard(chip, () => { a.images.splice(i, 1); paneCoreRenderAttachments(t); });
    tray.appendChild(chip);
  });
}
function paneCorePasteImage(t, file) {
  if (file && typeof file.size === "number" && file.size > PANE_CORE_IMAGE_PASTE_MAX_BYTES) {
    const mb = (file.size / (1024 * 1024)).toFixed(1);
    setMsg("#term-msg", "warn", `Image too large (${mb} MB); 5 MB max.`, 5000);
    return;
  }
  const reader = new FileReader();
  reader.onload = () => {
    const r = reader.result || "";
    const comma = r.indexOf(",");
    if (comma < 0) return;
    const data = r.slice(comma + 1);
    const rawMt = (r.slice(5, comma).split(";")[0]) || "image/png";
    const mt = /^image\/(png|jpeg|gif|webp)$/.test(rawMt) ? rawMt : "image/png";
    t.attached = t.attached || { images: [], files: [] };
    t.attached.images.push({ data, media_type: mt });
    paneCoreRenderAttachments(t);
  };
  reader.readAsDataURL(file);
}
function paneCoreOpenAutocomplete(t, items, onPick) {
  termCloseAutocomplete(t);
  if (!items.length) return;
  const pop = document.createElement("div");
  pop.className = "composer-pop";
  items.slice(0, 20).forEach((it, idx) => {
    const row = document.createElement("div");
    row.className = "composer-pop-row" + (idx === 0 ? " active" : "");
    row.innerHTML = `<span class="pop-name">${escape(it.label)}</span>` +
      (it.detail ? `<span class="pop-detail">${escape(it.detail)}</span>` : "");
    row.addEventListener("mousedown", (e) => { e.preventDefault(); onPick(it); termCloseAutocomplete(t); });
    pop.appendChild(row);
  });
  t.pane.querySelector(".term-foot").appendChild(pop);
  t._popOpen = true;
}
var PANE_CORE_SKILLS_CACHE = { at: 0, data: null };
var PANE_CORE_SKILLS_TTL_MS = 5000;
function paneCoreScheduleComposerInput(t) {
  if (t._composerTimer) clearTimeout(t._composerTimer);
  t._composerTimer = setTimeout(() => {
    t._composerTimer = null;
    paneCoreHandleComposerInput(t);
  }, 120);
}
async function paneCoreHandleComposerInput(t) {
  const input = t.input;
  const val = input.value;
  const caret = input.selectionStart || val.length;
  const before = val.slice(0, caret);
  const m = before.match(/([@/])([^\s]*)$/);
  if (!m) { termCloseAutocomplete(t); return; }
  const trigger = m[1];
  const prefix = m[2];
  t._composerSeq = (t._composerSeq || 0) + 1;
  const seq = t._composerSeq;
  const isLatest = () => t._composerSeq === seq && document.activeElement === input;
  if (trigger === "/") {
    try {
      let skills;
      if (PANE_CORE_SKILLS_CACHE.data && (Date.now() - PANE_CORE_SKILLS_CACHE.at) < PANE_CORE_SKILLS_TTL_MS) {
        skills = PANE_CORE_SKILLS_CACHE.data;
      } else {
        let r;
        try { r = await fetch("/api/skills", { cache: "no-store" }); }
        catch (netErr) { PANE_CORE_SKILLS_CACHE = { at: 0, data: null }; return; }
        if (!r.ok) { PANE_CORE_SKILLS_CACHE = { at: 0, data: null }; return; }
        const data = await r.json();
        skills = data.skills || [];
        PANE_CORE_SKILLS_CACHE = { at: Date.now(), data: skills };
      }
      if (!isLatest()) return;
      const items = skills
        .filter((s) => s.name.toLowerCase().includes(prefix.toLowerCase()))
        .map((s) => ({ label: "/" + s.name, detail: s.description || "", pick: "/" + s.name }));
      paneCoreOpenAutocomplete(t, items, (it) => {
        const curVal = input.value;
        const curCaret = input.selectionStart || curVal.length;
        if (curVal !== val || curCaret !== caret) { termCloseAutocomplete(t); return; }
        const newVal = val.slice(0, caret - prefix.length - 1) + it.pick + val.slice(caret);
        input.value = newVal;
        input.focus();
        const pos = caret - prefix.length - 1 + it.pick.length;
        input.setSelectionRange(pos, pos);
      });
    } catch (e) { console.warn("[pane-core] /api/skills autocomplete failed:", e); }
  } else {
    try {
      const r = await fetch("/api/files/list?prefix=" + encodeURIComponent(prefix), { cache: "no-store" });
      if (!r.ok) return;
      const data = await r.json();
      if (!isLatest()) return;
      const items = (data.files || []).map((f) => ({ label: "@" + f, detail: "", pick: f }));
      paneCoreOpenAutocomplete(t, items, (it) => {
        const curVal = input.value;
        const curCaret = input.selectionStart || curVal.length;
        if (curVal !== val || curCaret !== caret) { termCloseAutocomplete(t); return; }
        t.attached = t.attached || { images: [], files: [] };
        t.attached.files.push(it.pick);
        const newVal = val.slice(0, caret - prefix.length - 1) + val.slice(caret);
        input.value = newVal;
        input.focus();
        const pos = caret - prefix.length - 1;
        input.setSelectionRange(pos, pos);
        paneCoreRenderAttachments(t);
      });
    } catch (e) { console.warn("[pane-core] /api/files/list autocomplete failed:", e); }
  }
}

// ----- unified session: chip + event render + send (copied, host-free) -----
function paneCoreSessionChipUpdate(t) {
  const pill = t.pane && t.pane.querySelector(".status-pill");
  if (!pill) return;
  const state = t.state || "mirror";
  let label, pillCls;
  if (state === "mirror") { label = "mirror"; pillCls = "done"; paneCoreSetActivity(t, "idle", "ready"); }
  else if (state === "acquiring") { label = "acquiring…"; pillCls = "running"; paneCoreSetActivity(t, "acquiring…", "busy"); }
  else if (state === "engine") { label = "live"; pillCls = "running"; paneCoreSetActivity(t, "live", "busy"); }
  else if (state === "foreign") { label = "external"; pillCls = "warn"; paneCoreSetActivity(t, "external", "busy"); }
  else { label = state; pillCls = "warn"; paneCoreSetActivity(t, state, "ready"); }
  if (t.pending) label = label + " · queued";
  termSetPillState(pill, pillCls, label);
}
function paneCoreHandleSessionEvent(t, ev) {
  if (!ev || typeof ev !== "object") return;
  const kind = ev.kind;
  if (kind === "state_change") {
    t.state = ev.state || t.state;
    t.pending = !!ev.pending;
    paneCoreSessionChipUpdate(t);
    return;
  }
  if (kind === "warning") {
    const warn = document.createElement("div");
    warn.className = "msg system";
    warn.style.color = "var(--warn, #e6a817)";
    warn.textContent = "[warning] " + (ev.text || "");
    t.body.appendChild(warn);
    paneCoreAutoScroll(t);
    return;
  }
  if (kind === "message") {
    const role = ev.role || "system";
    const text = ev.text || "";
    if (role === "user") {
      paneCoreRenderUserMessage(t, text);
    } else if (role === "assistant") {
      paneCoreAppendAssistantText(t, text);
      if (!ev.partial) t.currentAssistant = null;
    } else {
      const note = document.createElement("div");
      note.className = "msg system";
      note.textContent = text;
      t.body.appendChild(note);
    }
    paneCoreAutoScroll(t);
    return;
  }
  if (kind === "tool_use") {
    paneCoreAddToolPill(t, ev.id || "", ev.name || "tool", ev.input || {});
    paneCoreAutoScroll(t);
    return;
  }
  if (kind === "tool_result") {
    paneCoreMarkToolResult(t, ev.tool_use_id || ev.id || "", !!ev.is_error, ev.content || ev.output || "");
    paneCoreAutoScroll(t);
    return;
  }
  if (kind === "system") {
    const note = document.createElement("div");
    note.className = "msg system";
    note.textContent = ev.text || "";
    t.body.appendChild(note);
    paneCoreAutoScroll(t);
    return;
  }
}
function paneCoreClientId() {
  if (paneCoreClientId._id) return paneCoreClientId._id;
  let id = null;
  try { id = sessionStorage.getItem("dash.sessionOwnerId"); } catch (_) {}
  if (!id) {
    id = (window.crypto && crypto.randomUUID)
      ? crypto.randomUUID()
      : ("xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
          const r = Math.random() * 16 | 0;
          const v = c === "x" ? r : (r & 0x3 | 0x8);
          return v.toString(16);
        }));
    try { sessionStorage.setItem("dash.sessionOwnerId", id); } catch (_) {}
  }
  paneCoreClientId._id = id;
  return id;
}
async function paneCoreSendSession(t, text) {
  if (!t || !t.sid) return;
  const trimmed = (text || "").trim();
  if (!trimmed) return;
  if (t._sessionSendInFlight) return;
  t._sessionSendInFlight = true;
  t.sendBtn.disabled = true;
  try {
    const payload = { text: trimmed, owner: paneCoreClientId() };
    if (t.model) payload.model = t.model;
    const r = await fetch(
      "/api/sessions/" + encodeURIComponent(t.sid) + "/input",
      { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }
    );
    if (r.status === 202) {
      t.input.value = "";
      if (t.input.tagName === "TEXTAREA") t.input.style.height = "";
      t.pending = true;
      paneCoreSessionChipUpdate(t);
      const note = document.createElement("div");
      note.className = "msg system";
      note.textContent = "[queued — turn will be processed shortly]";
      t.body.appendChild(note);
      paneCoreAutoScroll(t);
    } else if (r.status === 409) {
      const note = document.createElement("div");
      note.className = "msg system";
      note.style.color = "var(--warn, #e6a817)";
      note.textContent = "[already queued — please wait before sending again]";
      t.body.appendChild(note);
      paneCoreAutoScroll(t);
      setMsg("#term-msg", "warn", "Already queued — text preserved.", TERM_MSG_DURATION_MS);
    } else if (r.ok) {
      t.input.value = "";
      if (t.input.tagName === "TEXTAREA") t.input.style.height = "";
    } else {
      const body = await r.text().catch(() => "");
      throw new Error("HTTP " + r.status + (body ? ": " + body.slice(0, 120) : ""));
    }
  } catch (e) {
    const err = document.createElement("div");
    err.className = "msg system";
    err.style.color = "var(--bad)";
    err.textContent = "[send failed: " + e.message + "]";
    t.body.appendChild(err);
    paneCoreAutoScroll(t);
    setMsg("#term-msg", "err", "Send failed: " + e.message, TERM_MSG_DURATION_MS);
  } finally {
    t._sessionSendInFlight = false;
    t.sendBtn.disabled = false;
    try { t.input.focus(); } catch (_) {}
  }
}

// Shared activity-observer wiring used by every kind's paneHandle. Observes
// the pane's ``.activity`` chip and fans changes ({label, cls}) out to
// subscribers registered via the returned ``onActivity``. ``disconnect`` must
// be called from the handle's ``close()``.
function paneCoreActivityWiring(pane) {
  const activitySubs = [];
  const activityEl = pane.querySelector(".term-head .activity");
  let _activityObserver = null;
  const emitActivity = () => {
    if (!activityEl) return;
    const label = activityEl.textContent || "";
    let cls = "";
    for (const c of ["busy", "waiting", "ready", "ended"]) {
      if (activityEl.classList.contains(c)) { cls = c; break; }
    }
    for (const cb of activitySubs) {
      try { cb({ label, cls }); } catch (_) {}
    }
  };
  const ensureObserver = () => {
    if (_activityObserver || !activityEl || typeof MutationObserver === "undefined") return;
    _activityObserver = new MutationObserver(emitActivity);
    _activityObserver.observe(activityEl, {
      childList: true, characterData: true, subtree: true, attributes: true, attributeFilter: ["class"],
    });
  };
  return {
    onActivity(cb) {
      if (typeof cb !== "function") return () => {};
      activitySubs.push(cb);
      ensureObserver();
      return () => {
        const i = activitySubs.indexOf(cb);
        if (i >= 0) activitySubs.splice(i, 1);
      };
    },
    disconnect() {
      if (_activityObserver) { try { _activityObserver.disconnect(); } catch (_) {} _activityObserver = null; }
    },
  };
}

// Shared close path for chat / session / transcript panes (NOT pty). Tears
// down the stream + listeners locally, then asks the host to drop the pane
// from its registry / persistence. This is the host-adapted replacement for
// terminals.js's termClose registry/layout/persist body.
function paneCoreCloseStreamPane(t) {
  if (!t) return;
  try { t.source && t.source.close(); } catch (e) { console.warn("[pane-core] close: SSE close failed: " + (e && e.message ? e.message : e)); }
  if (t._sseHeartbeat) { clearInterval(t._sseHeartbeat); t._sseHeartbeat = null; }
  if (t._costRefreshTimer) { clearTimeout(t._costRefreshTimer); t._costRefreshTimer = null; }
  if (t._sessReconnectTimer) { clearTimeout(t._sessReconnectTimer); t._sessReconnectTimer = null; }
  if (t.kind === "session") t._sessReconnectStopped = true;
  if (typeof t._sessReconnectN === "number") {
    t._sessReconnectN = 0;
  }
  if (t._autoFollowScrollHandler && t.body) {
    try { t.body.removeEventListener("scroll", t._autoFollowScrollHandler); } catch (_) {}
    t._autoFollowScrollHandler = null;
  }
  if (t._activityDisconnect) { try { t._activityDisconnect(); } catch (_) {} }
}

// ───────────────────────────────────────────────────────────────────────────
// MOUNT BODIES — build the pane DOM + wire stream/composer/search/teardown.
// Each receives the host shim ``h`` (captured + stashed on t._host so the
// copied stream/render functions reach it). Returns a paneHandle.
// ───────────────────────────────────────────────────────────────────────────

function paneCoreMountChat(container, opts, h) {
  const kind = (opts && opts.kind) || "chat";
  const jobId = opts && opts.key;
  const meta = (opts && opts.meta) || {};

  const taskPreview = (meta.task || "").replace(/\s+/g, " ").slice(0, 120);
  const pane = document.createElement("div");
  pane.className = "term-pane";
  pane.dataset.jobId = jobId;
  pane.innerHTML = `
    <div class="term-head">
      <span class="pill running status-pill" title="job ${escape(jobId)}">connecting</span>
      <span class="task" title="${escape(meta.task || "")}">${escape(taskPreview || jobId)}</span>
      <span class="activity" title="current activity in this pane">connecting…</span>
      <span class="cost-pill" title="aggregated cost / turns / time for this session"></span>
      <span class="id">${escape(jobId.slice(0, 8))}</span>
      <span class="actions">
        <button class="expand-btn" title="Show or hide this terminal's output">expand</button>
        <button class="stop-btn" title="Interrupt the current generation (keep session alive)">stop</button>
        <button class="search-btn" title="Search in this pane (Ctrl+F)">find</button>
        <button class="pin-btn" title="Maximise / restore this pane">pin</button>
        <button class="export-btn" title="Export as markdown">export</button>
        <button class="cancel-btn danger" title="Cancel the running subprocess">cancel</button>
        <button class="close-btn" title="Close this pane">close</button>
      </span>
    </div>
    <div class="term-search">
      <input type="text" placeholder="search in this pane (Esc to close)" />
      <span class="matches">0 / 0</span>
      <button class="search-prev">↑</button>
      <button class="search-next">↓</button>
      <button class="search-close">×</button>
    </div>
    <div class="term-body" tabindex="0"></div>
    <div class="attach-tray" style="display:none"></div>
    <div class="term-foot">
      <textarea class="stdin-input" rows="1" autocomplete="off" placeholder="type, /skill, @file, paste/drop images, Enter sends · Shift+Enter newline"></textarea>
      <button class="send-btn">send</button>
    </div>
  `;
  container.appendChild(pane);

  const body = pane.querySelector(".term-body");
  const input = pane.querySelector(".stdin-input");
  const sendBtn = pane.querySelector(".send-btn");
  const autosize = () => {
    input.style.height = "auto";
    const next = Math.min(input.scrollHeight, COMPOSER_AUTOSIZE_MAX_PX);
    input.style.height = next + "px";
  };
  input.addEventListener("input", autosize);
  if (kind === "chat" || kind === "chat-codex") body.classList.add("chat");
  const t = {
    jobId, pane, body, input, sendBtn,
    _host: h,
    source: null,
    task: meta.task || "",
    kind,
    jsonBuf: [],
    currentAssistant: null,
    toolUseEls: new Map(),
    attached: { images: [], files: [] },
    sessionId: meta.session_id || "",
    model: meta.model || "",
  };

  input.addEventListener("input", () => paneCoreScheduleComposerInput(t));
  input.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { termCloseAutocomplete(t); return; }
    if (t._popOpen && e.key === "Enter") {
      const first = t.pane.querySelector(".composer-pop-row.active");
      if (first) {
        first.dispatchEvent(new MouseEvent("mousedown"));
        e.preventDefault();
        e.stopImmediatePropagation();
        return;
      }
    }
  });
  input.addEventListener("paste", (e) => {
    const items = e.clipboardData?.items || [];
    for (const it of items) {
      if (it.kind === "file" && it.type.startsWith("image/")) {
        const f = it.getAsFile();
        if (f) { paneCorePasteImage(t, f); e.preventDefault(); }
      }
    }
  });
  pane.addEventListener("dragover", (e) => { e.preventDefault(); pane.classList.add("dragover"); });
  pane.addEventListener("dragleave", () => pane.classList.remove("dragover"));
  pane.addEventListener("drop", (e) => {
    e.preventDefault();
    pane.classList.remove("dragover");
    for (const f of e.dataTransfer.files || []) {
      if (f.type.startsWith("image/")) paneCorePasteImage(t, f);
    }
  });
  termInitAutoFollow(t);

  pane.addEventListener("click", () => {
    document.querySelectorAll(".term-pane.focus").forEach((p) => p.classList.remove("focus"));
    pane.classList.add("focus");
  });
  pane.querySelector(".close-btn").addEventListener("click", (e) => {
    e.stopPropagation();
    h.close(t.jobId);
  });
  pane.querySelector(".cancel-btn").addEventListener("click", async (e) => {
    e.stopPropagation();
    try { await postJson(`/api/jobs/${t.jobId}/cancel`, {}); }
    catch (err) { setMsg("#term-msg", "err", "Cancel failed: " + err.message, TERM_MSG_DURATION_MS); }
  });
  pane.querySelector(".stop-btn")?.addEventListener("click", async (e) => {
    e.stopPropagation();
    try { await postJson(`/api/jobs/${t.jobId}/interrupt`, {}); }
    catch (err) {
      const note = document.createElement("div");
      note.className = "msg system";
      note.style.color = "var(--bad)";
      note.textContent = "[stop failed: " + err.message + "]";
      t.body.appendChild(note);
      setMsg("#term-msg", "err", "Stop failed: " + err.message, TERM_MSG_DURATION_MS);
    }
  });
  pane.querySelector(".export-btn")?.addEventListener("click", (e) => {
    e.stopPropagation();
    termExportMarkdown(t);
  });
  pane.querySelector(".search-btn")?.addEventListener("click", (e) => {
    e.stopPropagation();
    paneCoreToggleSearch(t);
  });
  const searchBar = pane.querySelector(".term-search");
  const searchInput = searchBar.querySelector("input");
  let _searchDebounce = null;
  searchInput.addEventListener("input", () => {
    if (_searchDebounce) clearTimeout(_searchDebounce);
    _searchDebounce = setTimeout(() => paneCoreRunSearch(t), 150);
  });
  searchInput.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { paneCoreToggleSearch(t, false); return; }
    if (e.key === "Enter") { e.preventDefault(); paneCoreSearchStep(t, e.shiftKey ? -1 : +1); }
  });
  searchBar.querySelector(".search-next").addEventListener("click", () => paneCoreSearchStep(t, +1));
  searchBar.querySelector(".search-prev").addEventListener("click", () => paneCoreSearchStep(t, -1));
  searchBar.querySelector(".search-close").addEventListener("click", () => paneCoreToggleSearch(t, false));
  pane.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "f") {
      e.preventDefault();
      paneCoreToggleSearch(t, true);
    }
  });
  sendBtn.addEventListener("click", () => paneCoreSend(t));
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); paneCoreSend(t); }
  });

  const es = new EventSource(`/api/jobs/${jobId}/stream`);
  t.source = es;
  const statusPill = pane.querySelector(".status-pill");
  t._lastSSEEvent = Date.now();
  const SSE_STALE_MS = 60000;
  const heartbeatTick = () => {
    if (!t.pane || !t.pane.isConnected) return;
    if (t.pane.classList.contains("dead")) return;
    if (typeof document !== "undefined" && document.hidden) return;
    if (Date.now() - (t._lastSSEEvent || 0) < SSE_STALE_MS) return;
    try { es.close(); } catch (_) {}
    termSetPillState(statusPill, "warn", "disconnected");
    paneCoreSetActivity(t, "disconnected", "ended");
    if (t.kind === "chat-codex") paneCoreCodexAwaitNextTurn(t);
    else paneCoreSetDead(t, "ended");
    clearInterval(t._sseHeartbeat);
    t._sseHeartbeat = null;
  };
  t._restartSseHeartbeat = () => {
    if (t._sseHeartbeat) return;
    t._sseHeartbeat = setInterval(heartbeatTick, 15000);
  };
  t._restartSseHeartbeat();
  es.onopen = () => {
    t._lastSSEEvent = Date.now();
    termSetPillState(statusPill, "running", "live");
    paneCoreSetActivity(t, "live", "busy");
  };
  es.onmessage = (ev) => {
    t._lastSSEEvent = Date.now();
    if (t.kind === "chat") paneCoreHandleChatChunk(t, ev.data);
    else if (t.kind === "chat-codex") paneCoreHandleCodexChunk(t, ev.data);
    else paneCoreAppendChunk(t, ev.data);
  };
  es.addEventListener("end", () => {
    try { es.close(); } catch (_) {}
    if (t._sseHeartbeat) { clearInterval(t._sseHeartbeat); t._sseHeartbeat = null; }
    if (t.kind === "chat-codex") paneCoreCodexAwaitNextTurn(t);
    else paneCoreSetDead(t, "done");
    paneCoreLoadJobs().catch((e) => console.warn("[pane-core] loadJobs after SSE end failed: " + (e && e.message ? e.message : e)));
  });
  es.onerror = () => {
    if (t.pane.classList.contains("dead")) return;
    if (es.readyState !== EventSource.CLOSED) return;
    if (t.kind === "chat-codex") paneCoreCodexAwaitNextTurn(t);
    else paneCoreSetDead(t, "ended");
  };
  termRefreshCost(t);

  if (kind === "chat-codex") paneCoreCodexBeginTurn(t);

  const activity = paneCoreActivityWiring(pane);
  t._activityDisconnect = function () { try { activity.disconnect(); } catch (_) {} };

  const handle = {
    t, pane, key: jobId, kind,
    close() { paneCoreCloseStreamPane(t); t.pane.remove(); h.unregister(t.jobId); h.persist(); h.renderEmptyState(); paneCoreLoadJobs().catch(() => {}); },
    onActivity: activity.onActivity,
  };
  t._paneHandle = handle;
  h.register(jobId, handle);
  return handle;
}

function paneCoreMountPty(container, opts, h) {
  const ptyId = opts && opts.key;
  const meta = (opts && opts.meta) || {};
  const initialCommand = opts && opts.initialCommand;

  const shellLabel = (meta.argv && meta.argv[0]) || meta.shell || "shell";
  const shortShell = String(shellLabel).split(/[\\/]/).pop() || shellLabel;
  const pane = document.createElement("div");
  pane.className = "term-pane term-pty focus";
  pane.dataset.jobId = ptyId;
  pane.innerHTML = `
    <div class="term-head">
      <span class="pill running status-pill" title="PTY ${escape(ptyId)}">connecting</span>
      <span class="task" title="${escape(meta.cwd || "")}">${escape(shortShell)} · ${escape(meta.cwd || "")}</span>
      <span class="activity" title="current activity in this pane">connecting…</span>
      <span class="id">${escape(ptyId.slice(0, 8))}</span>
      <span class="actions">
        <button class="expand-btn" title="Show or hide this terminal">expand</button>
        <button class="pin-btn" title="Maximise / restore this pane">pin</button>
        <button class="kill-btn danger" title="Terminate the shell (SIGTERM)">kill</button>
        <button class="close-btn" title="Close this pane (and kill the shell)">close</button>
      </span>
    </div>
    <div class="term-body term-pty-body" tabindex="0"></div>
  `;
  container.appendChild(pane);

  const body = pane.querySelector(".term-body");
  const t = {
    jobId: ptyId,
    _host: h,
    pane, body,
    input: null, sendBtn: null,
    source: null,
    task: meta.cwd || "",
    kind: "terminal",
    shell: meta.shell || "auto",
    attached: { images: [], files: [] },
  };

  pane.addEventListener("click", () => {
    document.querySelectorAll(".term-pane.focus").forEach((p) => p.classList.remove("focus"));
    pane.classList.add("focus");
  });
  pane.querySelector(".close-btn").addEventListener("click", (e) => {
    e.stopPropagation();
    h.close(ptyId);
  });
  pane.querySelector(".kill-btn").addEventListener("click", async (e) => {
    e.stopPropagation();
    try { await postJson(`/api/ptys/${ptyId}/kill`, {}); }
    catch (err) { setMsg("#term-msg", "err", "Kill failed: " + err.message, TERM_MSG_DURATION_MS); }
  });

  const activity = paneCoreActivityWiring(pane);
  t._activityDisconnect = function () { try { activity.disconnect(); } catch (_) {} };

  // Local PTY teardown: cancel initial-command timers, drop the resize
  // observer / fallback listener, close the WS, kill the server shell, then
  // ask the host to drop the registry entry + persist.
  const closePty = () => {
    t._closed = true;
    if (Array.isArray(t._runStepTimers)) { for (const tm of t._runStepTimers) { try { clearTimeout(tm); } catch (_) {} } t._runStepTimers = []; }
    if (t._resizeObserver) { try { t._resizeObserver.disconnect(); } catch (_) {} t._resizeObserver = null; }
    if (t._resizeFallback) { try { window.removeEventListener("resize", t._resizeFallback); } catch (_) {} t._resizeFallback = null; }
    try { t.source && t.source.close(); } catch (_) {}
    try { t._term && t._term.dispose(); } catch (_) {}
    if (t._activityDisconnect) { try { t._activityDisconnect(); } catch (_) {} }
    // Best-effort server-side kill (fire-and-forget).
    try { postJson(`/api/ptys/${ptyId}/kill`, {}).catch(() => {}); } catch (_) {}
    t.pane.remove();
    h.unregister(ptyId);
    h.persist();
    h.renderEmptyState();
  };

  const fit = () => {
    if (pane.classList.contains("collapsed")) return;
    try { t._fitAddon && t._fitAddon.fit(); } catch (_) {}
  };
  const handle = {
    t, pane, key: ptyId, kind: "terminal",
    close() { closePty(); },
    onActivity: activity.onActivity,
    fit,
  };
  t._paneHandle = handle;
  h.register(ptyId, handle);

  if (termPtyMissingDeps()) {
    body.innerHTML = `<div class="msg system" style="color:var(--bad);padding:12px">
      xterm.js failed to load (CDN blocked?). Reload the page or check your network.
    </div>`;
    return handle;
  }

  const term = new Terminal({
    cursorBlink: true,
    fontFamily: "var(--ff-mono), JetBrains Mono, Menlo, Consolas, monospace",
    fontSize: 13,
    scrollback: 5000,
    convertEol: false,
    theme: {
      background: "#0b0f14",
      foreground: "#d8dee9",
      cursor: "#4fcdcd",
      selectionBackground: "#3b4252",
      black: "#3b4252",
      red: "#bf616a",
      green: "#a3be8c",
      yellow: "#ebcb8b",
      blue: "#81a1c1",
      magenta: "#b48ead",
      cyan: "#88c0d0",
      white: "#e5e9f0",
      brightBlack: "#4c566a",
      brightRed: "#bf616a",
      brightGreen: "#a3be8c",
      brightYellow: "#ebcb8b",
      brightBlue: "#81a1c1",
      brightMagenta: "#b48ead",
      brightCyan: "#8fbcbb",
      brightWhite: "#eceff4",
    },
  });
  const fitAddon = new FitAddon.FitAddon();
  term.loadAddon(fitAddon);
  if (typeof WebLinksAddon !== "undefined") {
    try { term.loadAddon(new WebLinksAddon.WebLinksAddon()); } catch (_) {}
  }
  term.open(body);
  t._term = term;
  t._fitAddon = fitAddon;
  requestAnimationFrame(() => { try { fitAddon.fit(); } catch (_) {} });

  const token = (meta && meta.token) || (window._PTY_TOKENS && window._PTY_TOKENS[ptyId]);
  const ws = new WebSocket(termPtyWsUrl(ptyId, token));
  ws.binaryType = "arraybuffer";
  t.source = ws;
  const decoder = new TextDecoder("utf-8", { fatal: false });
  const statusPill = pane.querySelector(".status-pill");

  ws.onopen = () => {
    termSetPillState(statusPill, "running", "live");
    paneCoreSetActivity(t, "live", "busy");
    sendResize();
    const steps = Array.isArray(initialCommand)
      ? initialCommand
      : (initialCommand
          ? (typeof initialCommand === "string" ? [{ text: initialCommand }] : [initialCommand])
          : []);
    const enc = new TextEncoder();
    t._runStepTimers = t._runStepTimers || [];
    const runStep = (i) => {
      if (i >= steps.length) return;
      if (t._closed) return;
      const s = steps[i] || {};
      const text = s.text != null ? String(s.text) : "";
      const appendCR = s.appendCR !== false;
      const payload = appendCR ? text + "\r" : text;
      if (ws.readyState === WebSocket.OPEN && payload) {
        try { ws.send(enc.encode(payload)); } catch (_) {}
      }
      if (i + 1 < steps.length) {
        const nextDelay = Math.max(0, Number(steps[i + 1].delay) || 0);
        const tm = setTimeout(() => runStep(i + 1), nextDelay);
        t._runStepTimers.push(tm);
      }
    };
    if (steps.length) {
      const firstDelay = Math.max(0, Number(steps[0].delay) || 0);
      const tm = setTimeout(() => runStep(0), firstDelay);
      t._runStepTimers.push(tm);
    }
    term.focus();
  };

  ws.onmessage = (ev) => {
    if (typeof ev.data === "string") {
      let msg;
      try { msg = JSON.parse(ev.data); } catch (e) { console.warn("[pane-core] PTY control frame JSON parse failed: " + (e && e.message ? e.message : e)); return; }
      if (msg.type === "exit") {
        termSetPillState(statusPill, "done", "ended");
        paneCoreSetActivity(t, "ended", "ended");
        pane.classList.add("dead");
      }
      return;
    }
    const buf = ev.data instanceof ArrayBuffer ? ev.data : new Uint8Array(ev.data);
    const text = decoder.decode(buf, { stream: true });
    if (text) term.write(text);
  };

  ws.onerror = () => {
    termSetPillState(statusPill, "warn", "disconnected");
    paneCoreSetActivity(t, "disconnected", "ended");
  };

  ws.onclose = () => {
    if (!pane.classList.contains("dead")) {
      termSetPillState(statusPill, "cancelled", "closed");
      paneCoreSetActivity(t, "closed", "ended");
      pane.classList.add("dead");
    }
  };

  term.onData((data) => {
    if (ws.readyState === WebSocket.OPEN) ws.send(new TextEncoder().encode(data));
  });

  let lastCols = 0, lastRows = 0;
  const sendResize = () => {
    try { fitAddon.fit(); } catch (_) {}
    const cols = term.cols, rows = term.rows;
    if (!cols || !rows) return;
    if (cols === lastCols && rows === lastRows) return;
    lastCols = cols; lastRows = rows;
    if (ws.readyState === WebSocket.OPEN) {
      try { ws.send(JSON.stringify({ type: "resize", cols, rows })); }
      catch (e) { console.warn("[pane-core] PTY resize send failed: " + (e && e.message ? e.message : e)); }
    }
  };
  term.onResize(({ cols, rows }) => {
    if (cols === lastCols && rows === lastRows) return;
    lastCols = cols; lastRows = rows;
    if (ws.readyState === WebSocket.OPEN) {
      try { ws.send(JSON.stringify({ type: "resize", cols, rows })); }
      catch (e) { console.warn("[pane-core] PTY resize send failed: " + (e && e.message ? e.message : e)); }
    }
  });
  const debouncedResize = (typeof window.debounce === "function")
    ? window.debounce(sendResize, 80)
    : sendResize;
  if (typeof ResizeObserver !== "undefined") {
    const ro = new ResizeObserver(() => debouncedResize());
    ro.observe(body);
    t._resizeObserver = ro;
  } else {
    t._resizeFallback = debouncedResize;
    window.addEventListener("resize", debouncedResize);
  }

  return handle;
}

function paneCoreMountTranscript(container, opts, h) {
  const sessionId = (opts && opts.meta && opts.meta.sessionId) || (opts && opts.key && String(opts.key).replace(/^ide:/, ""));
  const paneKey = "ide:" + sessionId;

  const pane = document.createElement("div");
  pane.className = "term-pane focus";
  pane.dataset.jobId = paneKey;
  pane.innerHTML = `
    <div class="term-head">
      <span class="pill claude status-pill">IDE mirror</span>
      <span class="task" title="mirror of Claude Code session ${escape(sessionId)}">IDE chat ${escape(sessionId.slice(0, 8))}…</span>
      <span class="activity" title="current activity in this pane">mirroring…</span>
      <span class="id">${escape(sessionId.slice(0, 8))}</span>
      <span class="actions">
        <button class="expand-btn" title="Show or hide this terminal's output">expand</button>
        <button class="close-btn" title="Close this pane">close</button>
      </span>
    </div>
    <div class="term-body chat" tabindex="0"></div>
    <div class="term-foot">
      <textarea class="stdin-input" rows="1" placeholder="type to fork this IDE session — Enter forks &amp; sends · Shift+Enter newline"></textarea>
      <button class="send-btn">fork &amp; send</button>
    </div>
  `;
  container.appendChild(pane);
  const body = pane.querySelector(".term-body");
  const t = {
    jobId: paneKey,
    _host: h,
    pane, body,
    input: pane.querySelector(".stdin-input"),
    sendBtn: pane.querySelector(".send-btn"),
    source: null,
    task: "IDE session " + sessionId,
    kind: "transcript",
    jsonBuf: [],
    currentAssistant: null,
    toolUseEls: new Map(),
  };
  termInitAutoFollow(t);
  pane.querySelector(".close-btn").addEventListener("click", (e) => {
    e.stopPropagation();
    h.close(paneKey);
  });
  pane.addEventListener("click", () => {
    document.querySelectorAll(".term-pane.focus").forEach((p) => p.classList.remove("focus"));
    pane.classList.add("focus");
  });

  let forking = false;
  const forkAndSend = async () => {
    if (forking) return;
    const text = t.input.value.trim();
    if (!text) return;
    forking = true;
    t.input.value = "";
    t.input.disabled = true;
    t.sendBtn.disabled = true;
    t.sendBtn.textContent = "forking…";
    try {
      const res = await postJson("/api/jobs", { kind: "chat", task: text, resume_session_id: sessionId });
      const banner = document.createElement("div");
      banner.className = "msg system";
      banner.style.color = "var(--warn)";
      banner.textContent = `[forked into dashboard chat ${res.id.slice(0, 8)} — new pane opened to the right]`;
      t.body.appendChild(banner);
      t.input.placeholder = "mirror pane is read-only — continue in the fork pane";
      t.sendBtn.textContent = "forked";
      termSetPillState(t.pane.querySelector(".status-pill"), "warn", "forked");
      // Open the writable chat pane via the host (canvas = mount-as-split).
      h.openPane("chat", res.id, res);
      h.focusNewPane(res.id);
      paneCoreLoadJobs().catch(() => {});
    } catch (e) {
      const err = document.createElement("div");
      err.className = "msg system";
      err.style.color = "var(--bad)";
      err.textContent = `[fork failed: ${e.message}]`;
      t.body.appendChild(err);
      setMsg("#term-msg", "err", "Fork failed: " + e.message, TERM_MSG_DURATION_MS);
      t.input.value = text;
      t.input.disabled = false;
      t.sendBtn.disabled = false;
      t.sendBtn.textContent = "fork & send";
      forking = false;
    }
  };
  t.sendBtn.addEventListener("click", forkAndSend);
  t.input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); forkAndSend(); }
  });
  const transcriptAutosize = () => {
    t.input.style.height = "auto";
    t.input.style.height = Math.min(t.input.scrollHeight, COMPOSER_AUTOSIZE_MAX_PX) + "px";
  };
  t.input.addEventListener("input", transcriptAutosize);

  const statusPill = pane.querySelector(".status-pill");
  // Lazy stream attach/detach. The IDE-mirror SSE renders via the copied
  // Claude stream-json renderer (transcripts are stream-json shaped).
  t.openStream = () => {
    if (t.source) return;
    const es = new EventSource(`/api/transcripts/${sessionId}/stream`);
    t.source = es;
    es.onopen = () => {
      statusPill.textContent = "IDE live";
      paneCoreSetActivity(t, "mirroring…", "busy");
    };
    es.onmessage = (ev) => paneCoreHandleChatChunk(t, ev.data + "\n");
    es.addEventListener("end", () => {
      termSetPillState(statusPill, "done", "IDE ended");
      paneCoreSetActivity(t, "ended", "ready");
      try { es.close(); } catch (_) {}
      t.source = null;
    });
    es.onerror = () => {
      if (t.pane.classList.contains("dead")) return;
      if (es.readyState !== EventSource.CLOSED) return;
      termSetPillState(statusPill, "warn", "disconnected");
      paneCoreSetActivity(t, "disconnected", "ended");
    };
  };
  t.closeStream = () => {
    if (!t.source) return;
    try { t.source.close(); } catch (_) {}
    t.source = null;
    statusPill.textContent = "IDE paused";
    paneCoreSetActivity(t, t._pausedActivity || "paused (expand to resume)", "ready");
  };
  statusPill.textContent = "IDE paused";
  paneCoreSetActivity(t, "paused (expand to resume)", "ready");

  // Hydrate the header title without opening SSE. /api/transcripts may list
  // the session with an ai-title; fall back silently to the SID placeholder.
  try {
    fetch("/api/transcripts", { cache: "no-store" })
      .then((r) => (r.ok ? r.json() : null))
      .then((data) => {
        if (!data) return;
        const entry = (data.transcripts || []).find((e) => e.session_id === sessionId);
        if (entry && entry.ai_title) {
          const head = t.pane.querySelector(".term-head .task");
          if (head) head.textContent = entry.ai_title;
        }
      })
      .catch(() => {});
  } catch (_) {}

  if (opts && opts.collapsed) pane.classList.add("collapsed");
  if (!pane.classList.contains("collapsed")) t.openStream();

  const activity = paneCoreActivityWiring(pane);
  t._activityDisconnect = function () { try { activity.disconnect(); } catch (_) {} };

  const handle = {
    t, pane, key: paneKey, kind: "transcript",
    close() { paneCoreCloseStreamPane(t); t.pane.remove(); h.unregister(paneKey); h.persist(); h.renderEmptyState(); },
    onActivity: activity.onActivity,
    openStream: () => t.openStream(),
    closeStream: () => t.closeStream(),
  };
  t._paneHandle = handle;
  h.register(paneKey, handle);
  return handle;
}

function paneCoreMountSession(container, opts, h) {
  const sid = (opts && opts.meta && opts.meta.sid) || (opts && opts.key && String(opts.key).replace(/^session:/, ""));
  const paneKey = "session:" + sid;

  const pane = document.createElement("div");
  pane.className = "term-pane focus";
  pane.dataset.jobId = paneKey;
  pane.innerHTML = `
    <div class="term-head">
      <span class="pill running status-pill" title="session ${escape(sid)}">connecting</span>
      <span class="task" title="session ${escape(sid)}">session ${escape(sid.slice(0, 8))}…</span>
      <span class="activity" title="current activity in this pane">connecting…</span>
      <span class="id">${escape(sid.slice(0, 8))}</span>
      <span class="actions">
        <button class="release-btn" title="Release session control back to the engine">release</button>
        <button class="interrupt-btn" title="Interrupt the current session turn">interrupt</button>
        <button class="expand-btn" title="Show or hide this pane">expand</button>
        <button class="close-btn" title="Close this pane">close</button>
      </span>
    </div>
    <div class="term-body chat" tabindex="0"></div>
    <div class="term-foot">
      <textarea class="stdin-input" rows="1" autocomplete="off" placeholder="type a message · Enter sends · Shift+Enter newline"></textarea>
      <button class="send-btn">send</button>
    </div>
  `;
  container.appendChild(pane);

  const body = pane.querySelector(".term-body");
  const input = pane.querySelector(".stdin-input");
  const sendBtn = pane.querySelector(".send-btn");
  const statusPill = pane.querySelector(".status-pill");

  const autosize = () => {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, COMPOSER_AUTOSIZE_MAX_PX) + "px";
  };
  input.addEventListener("input", autosize);

  const t = {
    jobId: paneKey,
    sid,
    _host: h,
    pane, body, input, sendBtn,
    source: null,
    task: "session " + sid,
    kind: "session",
    state: "mirror",
    currentAssistant: null,
    toolUseEls: new Map(),
  };

  termInitAutoFollow(t);

  pane.querySelector(".close-btn").addEventListener("click", (e) => {
    e.stopPropagation();
    h.close(paneKey);
  });
  pane.querySelector(".release-btn")?.addEventListener("click", (e) => {
    e.stopPropagation();
    fetch("/api/sessions/" + encodeURIComponent(sid) + "/release", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",
    }).catch((err) => { setMsg("#term-msg", "err", "Release failed: " + err.message, TERM_MSG_DURATION_MS); });
  });
  pane.querySelector(".interrupt-btn")?.addEventListener("click", (e) => {
    e.stopPropagation();
    fetch("/api/sessions/" + encodeURIComponent(sid) + "/interrupt", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",
    }).catch((err) => { setMsg("#term-msg", "err", "Interrupt failed: " + err.message, TERM_MSG_DURATION_MS); });
  });
  pane.addEventListener("click", () => {
    document.querySelectorAll(".term-pane.focus").forEach((p) => p.classList.remove("focus"));
    pane.classList.add("focus");
  });

  const doSend = () => paneCoreSendSession(t, t.input.value);
  sendBtn.addEventListener("click", doSend);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); doSend(); }
  });

  const SESSION_STREAM_RECONNECT_DELAY_MS = 600;
  const SESSION_STREAM_RECONNECT_MAX = 12;

  t.openStream = () => {
    if (t.source) return;
    t._sessReconnectStopped = false;
    if (!t._sessReconnectN) {
      t.body.innerHTML = "";
      t.currentAssistant = null;
      t.toolUseEls = new Map();
      t.autoFollowBottom = true;
      t.firstScroll = true;
      // Fresh open (not a mid-reconnect retry): reset the "ever connected"
      // flag so the never-connected vs. real-drop decision below is scoped
      // to THIS attempt cycle.
      t._sessEverConnected = false;
    }
    const es = new EventSource("/api/sessions/" + encodeURIComponent(sid) + "/stream");
    t.source = es;
    es.onopen = () => {
      t._sessReconnectN = 0;
      t._sessEverConnected = true;
      termSetPillState(statusPill, "running", "connecting");
      paneCoreSetActivity(t, "connecting…", "busy");
    };
    es.onmessage = (ev) => {
      t._sessReconnectN = 0;
      t._sessEverConnected = true;
      let obj;
      try { obj = JSON.parse(ev.data); } catch (_) { return; }
      paneCoreHandleSessionEvent(t, obj);
    };
    es.addEventListener("end", () => {
      t._sessReconnectStopped = true;
      t._sessEverConnected = true;
      try { es.close(); } catch (_) {}
      t.source = null;
      termSetPillState(statusPill, "done", "ended");
      paneCoreSetActivity(t, "ended", "ready");
    });
    es.onerror = () => {
      if (es.readyState !== EventSource.CLOSED) return;
      if (t._sessReconnectStopped) return;
      t.source = null;
      if ((t._sessReconnectN || 0) < SESSION_STREAM_RECONNECT_MAX) {
        t._sessReconnectN = (t._sessReconnectN || 0) + 1;
        termSetPillState(statusPill, "running", "connecting");
        paneCoreSetActivity(t, "connecting…", "busy");
        if (!t._sessReconnectTimer) {
          t._sessReconnectTimer = setTimeout(() => {
            t._sessReconnectTimer = null;
            if (t._sessReconnectStopped) return;
            t.openStream();
          }, SESSION_STREAM_RECONNECT_DELAY_MS);
        }
        return;
      }
      // Reconnect budget exhausted. Distinguish a session that NEVER produced
      // a transcript (every retry 404'd — no onopen / message / end ever
      // fired) from a stream that was live and then dropped.
      t._sessReconnectN = 0;
      t._sessReconnectStopped = true;
      if (!t._sessEverConnected) {
        // Never connected → this session has no transcript. Show a CALM "no
        // history" state (neutral "done"-style pill, not the warn/error
        // style) and stop retrying. Ask the host to forget this key so a
        // dead/aborted session id is not re-opened on the next reload.
        termSetPillState(statusPill, "done", "empty");
        paneCoreSetActivity(t, "no transcript", "ready");
        paneCoreSessionEmptyNote(t);
        paneCoreT_host(t).forget(t.jobId);
        return;
      }
      // Was live, then dropped → a real disconnect.
      termSetPillState(statusPill, "warn", "disconnected");
      paneCoreSetActivity(t, "disconnected", "ended");
    };
  };
  t.closeStream = () => {
    if (t._sessReconnectTimer) { clearTimeout(t._sessReconnectTimer); t._sessReconnectTimer = null; }
    t._sessReconnectN = 0;
    t._sessReconnectStopped = true;
    if (!t.source) {
      termSetPillState(statusPill, "done", "paused");
      paneCoreSetActivity(t, "paused (expand to resume)", "ready");
      return;
    }
    try { t.source.close(); } catch (_) {}
    t.source = null;
    termSetPillState(statusPill, "done", "paused");
    paneCoreSetActivity(t, "paused (expand to resume)", "ready");
  };

  if (opts && opts.collapsed) pane.classList.add("collapsed");
  if (!pane.classList.contains("collapsed")) {
    t.openStream();
  } else {
    termSetPillState(statusPill, "done", "paused");
    paneCoreSetActivity(t, "paused (expand to resume)", "ready");
  }

  const activity = paneCoreActivityWiring(pane);
  t._activityDisconnect = function () { try { activity.disconnect(); } catch (_) {} };

  const handle = {
    t, pane, key: paneKey, kind: "session",
    close() { paneCoreCloseStreamPane(t); t.pane.remove(); h.unregister(paneKey); h.persist(); h.renderEmptyState(); },
    onActivity: activity.onActivity,
    openStream: () => t.openStream(),
    closeStream: () => t.closeStream(),
  };
  t._paneHandle = handle;
  h.register(paneKey, handle);
  return handle;
}

// Per-kind metadata fetch used by both restore and the list. (Unchanged from
// the extraction — pure fetch, no terminals.js globals.)
function paneCoreFetchMeta(key) {
  const raw = typeof key === "string" ? key : String(key == null ? "" : key);

  if (raw.slice(0, 4) === "ide:") {
    const sid = raw.slice(4);
    if (!sid) return Promise.resolve(null);
    return fetch("/api/transcripts", { cache: "no-store" })
      .then((r) => (r.ok ? r.json() : null))
      .then((data) => {
        if (!data) return null;
        const entry = (data.transcripts || []).find((e) => e.session_id === sid);
        return entry ? { kind: "transcript", sessionId: sid } : null;
      })
      .catch(() => null);
  }

  if (raw.slice(0, 8) === "session:") {
    const sid = raw.slice(8);
    if (!sid) return Promise.resolve(null);
    return fetch("/api/sessions", { cache: "no-store" })
      .then((r) => (r.ok ? r.json() : null))
      .then((data) => {
        if (!data) return null;
        const items = data.sessions || data || [];
        const entry = (Array.isArray(items) ? items : []).find(
          (e) => e && (e.sid === sid || e.session_id === sid)
        );
        return entry ? { kind: "session", sid } : null;
      })
      .catch(() => null);
  }

  if (raw.slice(0, 4) !== "job:") {
    return fetch("/api/ptys/" + encodeURIComponent(raw), { cache: "no-store" })
      .then((r) => (r.ok ? r.json() : null))
      .then((meta) => {
        if (!meta) return null;
        if (meta.status && meta.status !== "running") return null;
        return Object.assign({ kind: "terminal" }, meta);
      })
      .catch(() => null);
  }

  const jobId = raw.slice(0, 4) === "job:" ? raw.slice(4) : raw;
  return fetch("/api/jobs/" + encodeURIComponent(jobId), { cache: "no-store" })
    .then((r) => (r.ok ? r.json() : null))
    .catch(() => null);
}

// Coerce a non-canvas / unknown job kind to a canvas-renderable one. Generic
// orchestrate / plan jobs are not first-class canvas kinds — render them
// through the chat path (the generic-job branch of paneCoreSend / SSE handles
// plain-text output) so a stray job key never throws "unsupported kind". (I1.)
function paneCoreCoerceKind(kind) {
  if (kind === "chat" || kind === "chat-codex" || kind === "terminal"
      || kind === "transcript" || kind === "session") {
    return kind;
  }
  // orchestrate / plan / dispatch / unknown → chat (generic plain-text path).
  return "chat";
}

// Public entry: builds the pane for the given kind into ``container``, routing
// every registry/layout/open/persist concern through ``host``.
//   PaneCore.mount(container, opts, host)
//     opts = { kind, key, meta, collapsed?, initialCommand? }
function paneCoreMount(container, opts, host) {
  const h = paneCoreHost(host);
  const kind = paneCoreCoerceKind((opts && opts.kind) || "");
  const useOpts = (kind === ((opts && opts.kind))) ? opts : Object.assign({}, opts, { kind });
  if (kind === "chat" || kind === "chat-codex") return paneCoreMountChat(container, useOpts, h);
  if (kind === "terminal") return paneCoreMountPty(container, useOpts, h);
  if (kind === "transcript") return paneCoreMountTranscript(container, useOpts, h);
  if (kind === "session") return paneCoreMountSession(container, useOpts, h);
  // Unreachable (coerce guarantees a known kind) — defensive.
  throw new Error("PaneCore.mount: unsupported kind " + JSON.stringify(kind));
}

window.PaneCore = {
  mount: paneCoreMount,
  fetchMeta: paneCoreFetchMeta,
};
