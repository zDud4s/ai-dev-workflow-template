// .ai/dashboard/app/pane-core.js
//
// Layout-agnostic pane render + stream engine, published as the single
// global ``window.PaneCore``. Extracted from terminals.js (plan Task 6)
// so both the dashboard Terminals tab and the upcoming canvas window can
// share ONE battle-tested chat renderer.
//
// NO ES modules: this is a plain <script defer src> sharing the page's
// single global scope. It DEPENDS ON (but does not own / redefine) shared
// globals already loaded before it by core.js / skills.js / terminals.js:
//   $, escape, postJson, setMsg, loadJobs, MODELS_BY_TOOL,
//   COMPOSER_AUTOSIZE_MAX_PX, TERM_MSG_DURATION_MS,
//   termScheduleComposerInput, termCloseAutocomplete, termPasteImage,
//   termInitAutoFollow, termSend, termExportMarkdown, termToggleSearch,
//   termRunSearch, termSearchStep, termClose, termSetDead, termSetActivity,
//   termSetPillState, termHandleChatChunk, termHandleCodexChunk,
//   termAppendChunk, termRefreshCost, termCodexBeginTurn,
//   termCodexAwaitNextTurn.
// Task 7 adds these shared-global deps (PTY / transcript / session panes):
//   Terminal, FitAddon, WebLinksAddon (xterm CDN globals),
//   termPtyMissingDeps, termPtyWsUrl, termClosePty, termFocusNewPane,
//   termOpen (transcript fork target), termHandleTranscriptChunk,
//   applyTranscriptStatus, fetchTranscriptsListCached,
//   ensureTranscriptStatusPoll, termRefreshTranscriptPicker,
//   termSendSession, termHandleSessionEvent, termSessionChipUpdate.
//
// termSetDead NOTE: ``termSetDead`` and the chat dead-pane resume
// affordance it configures (the ``t.deadResume`` flag consumed by
// ``termSend`` / ``termSendResumeChat``) stay as shared globals in
// terminals.js. They are pane-intrinsic in spirit, but the resume flow is
// tightly coupled to ``termSend``/``termSendResumeChat`` (which Task 6 left
// in terminals.js), so relocating just ``termSetDead`` would split a
// single coupled unit and drag in terminals.js-local layout helpers
// (termSetCollapsed, termClearThinkingPlaceholder). PaneCore therefore
// continues to DEPEND ON ``termSetDead`` as a shared global (exactly as the
// chat path already did after Task 6); no behavior changes.
//
// LAYOUT concerns (collapse/expand/pin/list, #terms-grid placement,
// termGetLayout, persistOpenPanes, termRenderEmptyState, the expand/pin
// buttons) STAY in terminals.js — PaneCore only ever operates on the
// ``container`` element it is handed and carries pane-INTRINSIC chrome
// (title bar, close, search, export, composer).
//
// No top-level side effects at load: just function declarations + the
// single window.PaneCore export line at the end (keeps it node-extractable
// and safe to load in any order relative to its peers).

// Shared activity-observer wiring used by every kind's paneHandle.
// Observes the pane's ``.activity`` chip and fans changes ({label, cls})
// out to subscribers registered via the returned ``onActivity``. The
// canvas uses this to mirror per-pane activity into its status list
// without each render path knowing about subscribers. Returns:
//   { onActivity(cb) -> unsubscribe, disconnect() }
// ``disconnect`` must be called from the handle's ``close()`` so the
// MutationObserver is released when the pane is torn down.
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

// Build the chat / chat-codex pane DOM into ``container`` and wire its
// SSE stream, composer, search, export, and teardown. Returns a
// ``paneHandle`` (see the export contract at the bottom of this file).
//
// ``opts`` = { kind, key, meta }:
//   * kind — "chat" or "chat-codex"
//   * key  — the stable pane key (chat job id)
//   * meta — server metadata ({ task, kind, session_id, model, ... })
function paneCoreMountChat(container, opts) {
  const kind = (opts && opts.kind) || "orchestrate";
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
  // Auto-grow the textarea up to a sensible max so long prompts don't
  // get clipped to one line but also don't eat the entire pane.
  const autosize = () => {
    input.style.height = "auto";
    const next = Math.min(input.scrollHeight, COMPOSER_AUTOSIZE_MAX_PX);
    input.style.height = next + "px";
  };
  input.addEventListener("input", autosize);
  // Both Claude and Codex chat panes use the same chat-bubble styling.
  // Even though codex is one-shot per subprocess, the pane renders
  // multi-turn conversations via SSE rewiring (see termSendCodexNextTurn).
  if (kind === "chat" || kind === "chat-codex") body.classList.add("chat");
  const t = {
    jobId, pane, body, input, sendBtn,
    source: null,
    task: meta.task || "",
    kind,
    jsonBuf: [],
    currentAssistant: null,   // element for the in-progress assistant message
    toolUseEls: new Map(),    // tool_use_id -> {pill, detail}
    attached: { images: [], files: [] },
    sessionId: meta.session_id || "",  // enables resume on dead-pane
    model: meta.model || "",  // seed from /api/jobs; replaced on first init/assistant frame
  };

  // Composer wiring (only meaningful for chat panes; harmless otherwise).
  // termScheduleComposerInput debounces + race-protects the
  // /api/skills + /api/files/list fetches; see its definition.
  input.addEventListener("input", () => termScheduleComposerInput(t));
  input.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { termCloseAutocomplete(t); return; }
    if (t._popOpen && e.key === "Enter") {
      // Pick the highlighted autocomplete row. The second keydown
      // listener below also matches Enter and would otherwise send
      // the message simultaneously — stopImmediatePropagation halts
      // it so the operator's Enter ONLY picks the suggestion.
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
        if (f) { termPasteImage(t, f); e.preventDefault(); }
      }
    }
  });
  pane.addEventListener("dragover", (e) => { e.preventDefault(); pane.classList.add("dragover"); });
  pane.addEventListener("dragleave", () => pane.classList.remove("dragover"));
  pane.addEventListener("drop", (e) => {
    e.preventDefault();
    pane.classList.remove("dragover");
    for (const f of e.dataTransfer.files || []) {
      if (f.type.startsWith("image/")) termPasteImage(t, f);
    }
  });
  termInitAutoFollow(t);

  pane.addEventListener("click", () => {
    document.querySelectorAll(".term-pane.focus").forEach((p) => p.classList.remove("focus"));
    pane.classList.add("focus");
  });
  // IMPORTANT: read ``t.jobId`` lazily (not the closure-captured
  // ``jobId``). chat-codex panes re-key ``t.jobId`` on every
  // follow-up turn (see termSendCodexNextTurn) — using the captured
  // value would post to the FIRST turn's id, which has already
  // exited, and the buttons would silently 404 from turn 2 onwards.
  pane.querySelector(".close-btn").addEventListener("click", (e) => {
    e.stopPropagation();
    termClose(t.jobId);
  });
  pane.querySelector(".cancel-btn").addEventListener("click", async (e) => {
    e.stopPropagation();
    try {
      await postJson(`/api/jobs/${t.jobId}/cancel`, {});
    } catch (err) {
      // Previously this branch silently swallowed errors — the operator
      // clicked "cancel", nothing happened, no toast, no log. Surface
      // the failure so they know the cancel did not actually land.
      setMsg("#term-msg", "err", "Cancel failed: " + err.message, TERM_MSG_DURATION_MS);
    }
  });
  pane.querySelector(".stop-btn")?.addEventListener("click", async (e) => {
    e.stopPropagation();
    try {
      await postJson(`/api/jobs/${t.jobId}/interrupt`, {});
    } catch (err) {
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
    termToggleSearch(t);
  });
  // In-pane search wiring. The input handler debounces by 150ms — a
  // fresh TreeWalker walks the entire body text on every keystroke,
  // which on large panes (100s of KB of DOM text) is O(n) per stroke
  // and was visibly janking the input cursor.
  const searchBar = pane.querySelector(".term-search");
  const searchInput = searchBar.querySelector("input");
  let _termSearchDebounce = null;
  searchInput.addEventListener("input", () => {
    if (_termSearchDebounce) clearTimeout(_termSearchDebounce);
    _termSearchDebounce = setTimeout(() => termRunSearch(t), 150);
  });
  searchInput.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { termToggleSearch(t, false); return; }
    if (e.key === "Enter") { e.preventDefault(); termSearchStep(t, e.shiftKey ? -1 : +1); }
  });
  searchBar.querySelector(".search-next").addEventListener("click", () => termSearchStep(t, +1));
  searchBar.querySelector(".search-prev").addEventListener("click", () => termSearchStep(t, -1));
  searchBar.querySelector(".search-close").addEventListener("click", () => termToggleSearch(t, false));
  // Ctrl+F / Cmd+F inside the body opens the search bar.
  pane.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "f") {
      e.preventDefault();
      termToggleSearch(t, true);
    }
  });
  sendBtn.addEventListener("click", () => termSend(t));
  input.addEventListener("keydown", (e) => {
    // !e.isComposing prevents Enter from sending mid-IME-composition
    // text (Japanese/Chinese/Korean input) — matches the transcript
    // composer's existing guard.
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); termSend(t); }
  });

  // Wire SSE
  const es = new EventSource(`/api/jobs/${jobId}/stream`);
  t.source = es;
  const statusPill = pane.querySelector(".status-pill");
  // Heartbeat tracking: some browsers (Firefox notably) keep
  // EventSource.readyState at 0 (CONNECTING) forever when the server
  // half-closes the socket, so the ``readyState !== CLOSED`` guard in
  // onerror never trips and the pane stays "live" indefinitely. We
  // stamp ``_lastSSEEvent`` on every onmessage and run a 15s watchdog;
  // if no event has arrived in 60s we force the close path ourselves.
  t._lastSSEEvent = Date.now();
  const SSE_STALE_MS = 60_000;
  // Expose a restarter the visibilitychange handler in jobs.js can
  // call after a pause-on-hidden. Closes over `t`, `es`, `statusPill`
  // so the resumed timer reuses the same SSE + DOM refs.
  // Don't kill the pane while the tab is in the background — the
  // browser throttles SSE in hidden tabs (Chrome especially) which
  // makes `_lastSSEEvent` look stale even when the server is fine.
  // Resume the staleness check on visibility restore. Without this,
  // returning to the dashboard after >60s showed every pane as
  // "ended/disconnected" until manually reopened.
  const heartbeatTick = () => {
    if (!t.pane || !t.pane.isConnected) return;
    if (t.pane.classList.contains("dead")) return;
    if (typeof document !== "undefined" && document.hidden) return;
    if (Date.now() - (t._lastSSEEvent || 0) < SSE_STALE_MS) return;
    // Stale connection — surface the disconnect and walk the standard
    // close path so the pane is consistent with a normal "ended" state.
    try { es.close(); } catch (_) {}
    termSetPillState(statusPill, "warn", "disconnected");
    termSetActivity(t, "disconnected", "ended");
    if (t.kind === "chat-codex") {
      termCodexAwaitNextTurn(t);
    } else {
      termSetDead(t, "ended");
    }
    clearInterval(t._sseHeartbeat);
    t._sseHeartbeat = null;
  };
  t._restartSseHeartbeat = () => {
    if (t._sseHeartbeat) return;
    t._sseHeartbeat = setInterval(heartbeatTick, 15_000);
  };
  t._restartSseHeartbeat();
  es.onopen = () => {
    t._lastSSEEvent = Date.now();
    termSetPillState(statusPill, "running", "live");
    termSetActivity(t, "live", "busy");
  };
  // Each chat tool has its own structured event stream:
  //   * Claude — Anthropic stream-json (system/assistant/user/result/stream_event)
  //   * Codex  — Rust CLI rollout events (session_meta/response_item/event_msg)
  // Non-chat kinds (orchestrate / plan) dump plain text.
  //
  // IMPORTANT: do NOT append "\n" here. The server's pump reads stdout
  // in 1024-byte chunks, so a single long JSON record (e.g. the 8KB
  // SessionStart hook context) gets split across multiple SSE events.
  // ``ev.data`` already preserves the original chunk's newline boundaries
  // (an internal trailing newline becomes a final empty data: line);
  // forcing an extra "\n" would prematurely terminate a partial line and
  // hand a corrupt half-record to JSON.parse, which then falls through to
  // termRenderRaw and dumps it as a raw "msg system" block.
  es.onmessage = (ev) => {
    // Refresh the heartbeat timestamp on every event — used by the
    // stale-connection watchdog above to detect Firefox-style
    // half-open EventSources that never trip onerror.
    t._lastSSEEvent = Date.now();
    if (t.kind === "chat") termHandleChatChunk(t, ev.data);
    else if (t.kind === "chat-codex") termHandleCodexChunk(t, ev.data);
    else termAppendChunk(t, ev.data);
  };
  es.addEventListener("end", () => {
    try { es.close(); } catch (_) {}
    if (t._sseHeartbeat) { clearInterval(t._sseHeartbeat); t._sseHeartbeat = null; }
    if (t.kind === "chat-codex") {
      // Codex job exited after one turn. Don't mark the pane dead —
      // it's our multi-turn vehicle. The next send will spawn a
      // resume job and rewire SSE in-place.
      termCodexAwaitNextTurn(t);
    } else {
      termSetDead(t, "done");
    }
    // Fire-and-forget: a rejection here (e.g. server restarted while the
    // stream was ending) would otherwise become an unhandled rejection.
    Promise.resolve(loadJobs()).catch((e) => console.warn("[pane-core] loadJobs after SSE end failed: " + (e && e.message ? e.message : e)));
  });
  es.onerror = () => {
    // EventSource fires onerror on any disconnect — including transient
    // network blips, server restarts, and proxies idling the connection.
    // The browser will automatically reconnect when readyState ===
    // CONNECTING. Closing the stream here (or marking the pane dead)
    // converts a recoverable hiccup into a permanent failure, so we
    // only react when readyState === CLOSED (the browser has given up)
    // or when the `end` event has already declared the run finished.
    if (t.pane.classList.contains("dead")) return;
    if (es.readyState !== EventSource.CLOSED) return;
    if (t.kind === "chat-codex") {
      termCodexAwaitNextTurn(t);
    } else {
      termSetDead(t, "ended");
    }
  };
  // Initial cost fetch (also handles resumed sessions that already
  // have prior turns accumulated on disk).
  termRefreshCost(t);

  // Codex panes start a turn the moment the subprocess spawns (via
  // initial_stdin on the server). Lock the composer until SSE 'end'
  // fires and termCodexAwaitNextTurn captures session_id, so the
  // operator can't try to send a follow-up before the resume target
  // is known.
  if (kind === "chat-codex") termCodexBeginTurn(t);

  // ----- paneHandle (the layout-agnostic contract) -----
  // Activity subscribers fire whenever the pane's activity chip changes
  // ({label, cls}); see paneCoreActivityWiring. The canvas uses this to
  // broadcast `activity` to the status list.
  const activity = paneCoreActivityWiring(pane);

  // Store the disconnect on `t` so termClose (which only has the `t`
  // object, not the pane handle) can also tear down the observer.
  // Idempotent: calling _activityDisconnect() more than once is safe.
  t._activityDisconnect = function () {
    try { activity.disconnect(); } catch (_) {}
  };

  const handle = {
    // The shared term-object (TERMS entry). Layout code (terminals.js) and
    // the canvas register/own this; PaneCore just builds + wires it.
    t,
    pane,
    key: jobId,
    kind,
    // Tear down stream + listeners. Delegates to the shared termClose,
    // which already owns the leak-safe SSE/heartbeat/scroll-listener
    // cleanup; PaneCore additionally disconnects its activity observer.
    close() {
      t._activityDisconnect();
      termClose(t.jobId);
    },
    // Subscribe to activity changes ({label, cls}). Returns an
    // unsubscribe function.
    onActivity: activity.onActivity,
  };
  return handle;
}

// Build a PTY (terminal) pane into ``container`` and wire its xterm.js
// instance + WebSocket. Layout-AGNOSTIC: collapse-on-list, expand/pin
// affordances, empty-state + persistence stay in the terminals.js shim.
//
// ``opts`` = { key, meta, initialCommand }:
//   * key            — pane key (the PTY id)
//   * meta           — { argv, shell, cwd, token, ... } from /api/ptys
//   * initialCommand — optional string | array | {text,delay,appendCR}
//
// paneHandle additions: ``.fit()`` re-fits the xterm grid to the pane's
// current geometry (the shim calls it after expand / pin / layout change,
// since fit() can't measure a display:none body while collapsed).
function paneCoreMountPty(container, opts) {
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
    pane, body,
    input: null, sendBtn: null,
    source: null,            // WebSocket goes in here
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
    termClosePty(ptyId);
  });
  pane.querySelector(".kill-btn").addEventListener("click", async (e) => {
    e.stopPropagation();
    try {
      await postJson(`/api/ptys/${ptyId}/kill`, {});
    } catch (err) {
      setMsg("#term-msg", "err", "Kill failed: " + err.message, TERM_MSG_DURATION_MS);
    }
  });

  const activity = paneCoreActivityWiring(pane);

  // Store the disconnect on `t` so termClosePty (which only has `t`,
  // not the pane handle) can also tear down the observer. Idempotent.
  t._activityDisconnect = function () {
    try { activity.disconnect(); } catch (_) {}
  };

  // ``.fit()`` re-fits the xterm grid. No-op while collapsed (the body is
  // display:none so its computed size is 0 → fit() can't run).
  const fit = () => {
    if (pane.classList.contains("collapsed")) return;
    try { t._fitAddon && t._fitAddon.fit(); } catch (_) {}
  };
  const handle = {
    t, pane, key: ptyId, kind: "terminal",
    close() { t._activityDisconnect(); termClosePty(ptyId); },
    onActivity: activity.onActivity,
    fit,
  };

  if (termPtyMissingDeps()) {
    body.innerHTML = `<div class="msg system" style="color:var(--bad);padding:12px">
      xterm.js failed to load (CDN blocked?). Reload the page or check your network.
    </div>`;
    return handle;
  }

  // ----- xterm.js instance -----
  const term = new Terminal({
    cursorBlink: true,
    fontFamily: "var(--ff-mono), JetBrains Mono, Menlo, Consolas, monospace",
    fontSize: 13,
    scrollback: 5000,
    convertEol: false,
    // Match the dashboard's dark palette so the terminal doesn't feel pasted-in.
    theme: {
      background: "#0b0f14",
      foreground: "#d8dee9",
      // Dashboard cyan (--accent ≈ #4fcdcd) so the xterm caret speaks the
      // same signal color as the rest of the "Targeting HUD" cursor set.
      cursor: "#4fcdcd",
      selectionBackground: "#3b4252",
      black: "#3b4252",
      red:   "#bf616a",
      green: "#a3be8c",
      yellow:"#ebcb8b",
      blue:  "#81a1c1",
      magenta:"#b48ead",
      cyan:  "#88c0d0",
      white: "#e5e9f0",
      brightBlack: "#4c566a",
      brightRed:   "#bf616a",
      brightGreen: "#a3be8c",
      brightYellow:"#ebcb8b",
      brightBlue:  "#81a1c1",
      brightMagenta:"#b48ead",
      brightCyan:  "#8fbcbb",
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
  // First fit after the next frame so layout has finished.
  const initialFit = () => {
    try { fitAddon.fit(); } catch (_) {}
  };
  requestAnimationFrame(initialFit);

  // ----- WebSocket -----
  // meta.token is set by /api/ptys (POST) for newly-spawned PTYs.
  // _PTY_TOKENS is the runtime cache for restoreOpenPanes / reattach
  // — it survives the page lifetime so refreshing the dashboard keeps
  // existing PTYs reachable as long as the original spawner is the
  // same browser tab.
  const token = (meta && meta.token) || (window._PTY_TOKENS && window._PTY_TOKENS[ptyId]);
  const ws = new WebSocket(termPtyWsUrl(ptyId, token));
  ws.binaryType = "arraybuffer";
  t.source = ws;
  // ``stream: true`` is critical: bytes from the PTY arrive as
  // arbitrary chunks and a single multi-byte char (Portuguese accents,
  // emoji, line-drawing glyphs) may straddle two WebSocket frames.
  // Without streaming mode the decoder emits a U+FFFD replacement for
  // the split char on each end, corrupting the output. With stream:
  // true the incomplete trailing bytes are buffered and joined with
  // the start of the next chunk.
  const decoder = new TextDecoder("utf-8", { fatal: false });
  const statusPill = pane.querySelector(".status-pill");

  ws.onopen = () => {
    termSetPillState(statusPill, "running", "live");
    termSetActivity(t, "live", "busy");
    // Sync the server PTY to our actual rendered geometry.
    sendResize();
    // ``initialCommand`` accepts three shapes:
    //   string     -> sent once, followed by Enter
    //   array      -> sequence of { text, delay, appendCR? } steps
    //   {text,...} -> single object treated as a one-step sequence
    const steps = Array.isArray(initialCommand)
      ? initialCommand
      : (initialCommand
          ? (typeof initialCommand === "string"
              ? [{ text: initialCommand }]
              : [initialCommand])
          : []);
    // Steps go as BINARY frames: text frames are reserved for JSON
    // control messages (resize, etc.) and would be silently dropped
    // by the server's parser. \r at the end fires Enter.
    const enc = new TextEncoder();
    // Track every queued initial-command timer so termClosePty can
    // cancel them. Otherwise a recursive runStep tail keeps ws + term
    // alive until the last delay fires even after the user closed
    // the pane.
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
      // Control frame from server (JSON).
      let msg;
      try { msg = JSON.parse(ev.data); } catch (e) { console.warn("[pane-core] PTY control frame JSON parse failed: " + (e && e.message ? e.message : e)); return; }
      if (msg.type === "exit") {
        termSetPillState(statusPill, "done", "ended");
        termSetActivity(t, "ended", "ended");
        pane.classList.add("dead");
      }
      return;
    }
    // Binary frame: raw bytes from the PTY master. Pass stream:true
    // so the decoder buffers a partial trailing multi-byte sequence
    // until the next chunk completes it.
    const buf = ev.data instanceof ArrayBuffer ? ev.data : new Uint8Array(ev.data);
    const text = decoder.decode(buf, { stream: true });
    if (text) term.write(text);
  };

  ws.onerror = () => {
    termSetPillState(statusPill, "warn", "disconnected");
    termSetActivity(t, "disconnected", "ended");
  };

  ws.onclose = () => {
    if (!pane.classList.contains("dead")) {
      // No state class survived the open->close path — drop into the
      // neutral "cancelled" colour so it visually matches the "this
      // shell is gone" semantics.
      termSetPillState(statusPill, "cancelled", "closed");
      termSetActivity(t, "closed", "ended");
      pane.classList.add("dead");
    }
  };

  // ----- keystroke pipe (xterm -> ws) -----
  term.onData((data) => {
    if (ws.readyState === WebSocket.OPEN) {
      // Send as binary so the server treats it as raw bytes, not
      // a JSON control message.
      ws.send(new TextEncoder().encode(data));
    }
  });

  // ----- resize plumbing -----
  let lastCols = 0, lastRows = 0;
  const sendResize = () => {
    try { fitAddon.fit(); } catch (_) {}
    const cols = term.cols, rows = term.rows;
    if (!cols || !rows) return;
    if (cols === lastCols && rows === lastRows) return;
    lastCols = cols; lastRows = rows;
    if (ws.readyState === WebSocket.OPEN) {
      try {
        ws.send(JSON.stringify({ type: "resize", cols, rows }));
      } catch (e) { console.warn("[pane-core] PTY resize send failed: " + (e && e.message ? e.message : e)); }
    }
  };
  term.onResize(({ cols, rows }) => {
    if (cols === lastCols && rows === lastRows) return;
    lastCols = cols; lastRows = rows;
    if (ws.readyState === WebSocket.OPEN) {
      try {
        ws.send(JSON.stringify({ type: "resize", cols, rows }));
      } catch (e) { console.warn("[pane-core] PTY resize send failed: " + (e && e.message ? e.message : e)); }
    }
  });
  // Debounce so a window-drag burst (~60 Hz of resize events) collapses
  // into a handful of fit.fit() calls instead of one per frame. xterm's
  // fit() reflows + repaints the cell grid, which is the expensive bit —
  // the WS resize message itself is already deduped via lastCols/Rows.
  // 80 ms is short enough that the terminal still feels alive during a
  // drag (user sees a settle within ~5 frames) and long enough to absorb
  // typical burst patterns. Uses window.debounce (defined in core.js,
  // loaded earlier in defer order).
  const debouncedResize = (typeof window.debounce === "function")
    ? window.debounce(sendResize, 80)
    : sendResize;
  if (typeof ResizeObserver !== "undefined") {
    const ro = new ResizeObserver(() => debouncedResize());
    ro.observe(body);
    t._resizeObserver = ro;
  } else {
    // Capture the fallback listener so termClosePty can remove it.
    // Without this, every closed PTY pane leaks a window-level
    // resize handler that keeps the term object alive forever.
    t._resizeFallback = debouncedResize;
    window.addEventListener("resize", debouncedResize);
  }
  // (The expand button's own click listener — wired by the terminals.js
  // layout shim — re-fits xterm on the post-expand frame via handle.fit();
  // the ResizeObserver above catches every other geometry change.)

  return handle;
}

// Build an IDE transcript-mirror pane into ``container``. Read-only mirror
// of a Claude Code IDE session with a one-shot fork-and-send composer.
// LAYOUT-agnostic: the always-collapsed-start, expand/head-toggle
// affordances, empty-state + persistence stay in the terminals.js shim.
//
// ``opts`` = { key, meta }:
//   * key  — pane key ("ide:" + sessionId)
//   * meta — { sessionId } (the raw Claude Code session id)
//
// paneHandle additions: ``.openStream()`` / ``.closeStream()`` lazily
// attach / detach the mirror SSE (also stored on ``t`` so termSetCollapsed
// can drive them on collapse/expand).
function paneCoreMountTranscript(container, opts) {
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
    termClose(paneKey);
  });
  pane.addEventListener("click", () => {
    document.querySelectorAll(".term-pane.focus").forEach((p) => p.classList.remove("focus"));
    pane.classList.add("focus");
  });

  // First send forks the IDE session into a writable dashboard chat
  // (claude --resume <sid>). The mirror pane is KEPT OPEN alongside
  // the fork so the operator can compare the original IDE branch
  // (still owned by the IDE writer) to the new dashboard branch
  // side-by-side. Mirror's composer is disabled after the first fork
  // — additional forks should come from the IDE-side itself.
  // The `forking` flag + immediate UI lock below prevents a double
  // POST when the operator hits Enter twice while the cold-start
  // `claude --resume` is still spawning (which would otherwise create
  // two parallel forks responding to the same prompt).
  let forking = false;
  const forkAndSend = async () => {
    if (forking) return;
    const text = t.input.value.trim();
    if (!text) return;
    forking = true;
    // Lock the composer + clear the text BEFORE the await so the
    // operator gets instant feedback and a second Enter is a no-op.
    t.input.value = "";
    t.input.disabled = true;
    t.sendBtn.disabled = true;
    t.sendBtn.textContent = "forking…";
    try {
      const res = await postJson("/api/jobs", {
        kind: "chat",
        task: text,
        resume_session_id: sessionId,
      });
      // Banner inside the mirror documenting what just happened.
      const banner = document.createElement("div");
      banner.className = "msg system";
      banner.style.color = "var(--warn)";
      banner.textContent = `[forked into dashboard chat ${res.id.slice(0,8)} — new pane opened to the right]`;
      t.body.appendChild(banner);
      // Mirror's composer stays locked; this branch is now history.
      t.input.placeholder = "mirror pane is read-only — continue in the fork pane";
      t.sendBtn.textContent = "forked";
      // Route through termSetPillState so the pill ends up with EXACTLY
      // the warn class (and "forked" text), with every prior state
      // (running/done/bad/queued/cancelling/cancelled) stripped.
      // Direct ``classList.add("warn")`` previously left those stacked
      // and the CSS cascade could resolve to the wrong colour.
      termSetPillState(t.pane.querySelector(".status-pill"), "warn", "forked");
      // Open the writable chat pane next to this one AND expand +
      // scroll it into view so the operator sees their fork land
      // (otherwise list-mode tucks it away as a collapsed row at the
      // bottom of the grid and the mirror banner is the only feedback,
      // which makes the whole flow feel like nothing happened).
      termOpen(res.id, res);
      termFocusNewPane(res.id);
      await loadJobs();
    } catch (e) {
      const err = document.createElement("div");
      err.className = "msg system";
      err.style.color = "var(--bad)";
      err.textContent = `[fork failed: ${e.message}]`;
      t.body.appendChild(err);
      setMsg("#term-msg", "err", "Fork failed: " + e.message, TERM_MSG_DURATION_MS);
      // Restore the composer so the operator can retry.
      t.input.value = text;
      t.input.disabled = false;
      t.sendBtn.disabled = false;
      t.sendBtn.textContent = "fork & send";
      forking = false;
    }
  };
  t.sendBtn.addEventListener("click", forkAndSend);
  t.input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      forkAndSend();
    }
  });
  // Auto-grow the fork prompt up to a sensible cap so multi-line
  // prompts (which the placeholder advertises via Shift+Enter) don't
  // get clipped to one row.
  const transcriptAutosize = () => {
    t.input.style.height = "auto";
    t.input.style.height = Math.min(t.input.scrollHeight, COMPOSER_AUTOSIZE_MAX_PX) + "px";
  };
  t.input.addEventListener("input", transcriptAutosize);

  const statusPill = pane.querySelector(".status-pill");
  // Stream is lazy: only attach the EventSource while the pane is
  // expanded. Browsers cap HTTP/1.1 connections per origin at ~6, so
  // 6 restored transcript panes auto-streaming at once would starve
  // every other AJAX request (jobs polling, sessions, etc.) and make
  // the dashboard appear stuck. Collapsed panes are passive observers
  // — there's nothing to render — so they don't need to hold a slot.
  // termSetCollapsed wires expand -> openStream / collapse -> close.
  t.openStream = () => {
    if (t.source) return;
    const es = new EventSource(`/api/transcripts/${sessionId}/stream`);
    t.source = es;
    es.onopen = () => {
      // ``IDE live`` keeps the "claude" tool-identity class for colour
      // while running. We don't add a state class here — running is the
      // implicit default for an active mirror.
      statusPill.textContent = "IDE live";
      termSetActivity(t, "mirroring…", "busy");
    };
    es.onmessage = (ev) => termHandleTranscriptChunk(t, ev.data + "\n");
    es.addEventListener("end", () => {
      termSetPillState(statusPill, "done", "IDE ended");
      termSetActivity(t, "ended", "ready");
      try { es.close(); } catch (_) {}
      t.source = null;
    });
    es.onerror = () => {
      // Same rationale as the chat-pane onerror: don't paint the pane
      // as "disconnected" while the browser is still trying to reconnect
      // (readyState === CONNECTING). Only flip the status when the
      // browser has given up (CLOSED). Otherwise a momentary network
      // glitch turns a perfectly healthy mirror pane red until F5.
      if (t.pane.classList.contains("dead")) return;
      if (es.readyState !== EventSource.CLOSED) return;
      termSetPillState(statusPill, "warn", "disconnected");
      termSetActivity(t, "disconnected", "ended");
    };
  };
  t.closeStream = () => {
    if (!t.source) return;
    try { t.source.close(); } catch (_) {}
    t.source = null;
    statusPill.textContent = "IDE paused";
    // Reuse the enriched paused-state text the hydrator built (size
    // + relative mtime) so collapsing a pane lands back on the same
    // informative row it had before expand, instead of regressing
    // to the bare "expand to resume" hint.
    termSetActivity(t, t._pausedActivity || "paused (expand to resume)", "ready");
  };
  // Transcript panes always start collapsed (the shim adds the
  // ``collapsed`` class), so we deliberately do NOT openStream here — the
  // lazy path keeps the browser's connection budget free for AJAX until
  // the operator actually expands the pane. Render the same idle state
  // closeStream() uses so a never-opened pane and a collapsed-after-open
  // pane are visually indistinguishable, instead of leaving the stale
  // "mirroring…" template text.
  statusPill.textContent = "IDE paused";
  termSetActivity(t, "paused (expand to resume)", "ready");

  // Hydrate the pane header from /api/transcripts without opening
  // the SSE stream. The shared helper applies title (ai-title
  // preferred → first-user-message fallback), tooltip, last-
  // modified time and size — same code path the 5s poller uses so
  // initial render and subsequent refreshes stay consistent.
  fetchTranscriptsListCached().then((data) => {
    const entry = (data.transcripts || []).find((e) => e.session_id === sessionId);
    if (entry) applyTranscriptStatus(t, entry, sessionId);
  }).catch(() => { /* label stays as the SID placeholder */ });

  // Make sure the periodic status poll is running — restores the
  // "live status bar while collapsed" feel without keeping a per-
  // pane EventSource open. Idempotent; safe to call on every open.
  ensureTranscriptStatusPoll();
  termRefreshTranscriptPicker();

  const activity = paneCoreActivityWiring(pane);

  // Store the disconnect on `t` so termClose (which only has `t`, not
  // the pane handle) can also tear down the observer. Idempotent.
  t._activityDisconnect = function () {
    try { activity.disconnect(); } catch (_) {}
  };

  return {
    t, pane, key: paneKey, kind: "transcript",
    close() { t._activityDisconnect(); termClose(paneKey); },
    onActivity: activity.onActivity,
    openStream: () => t.openStream(),
    closeStream: () => t.closeStream(),
  };
}

// Build a unified session pane into ``container``. Connects to
// /api/sessions/<sid>/stream and renders a live, writable conversation.
// Unlike the read-only transcript mirror, the composer is ALWAYS enabled.
// LAYOUT-agnostic: collapse-on-list, expand/head-toggle, empty-state +
// persistence stay in the terminals.js shim.
//
// ``opts`` = { key, meta }:
//   * key  — pane key ("session:" + sid)
//   * meta — { sid } (raw session UUID)
//
// paneHandle additions: ``.openStream()`` / ``.closeStream()`` lazily
// attach / detach the session SSE (also stored on ``t`` so termSetCollapsed
// can drive them on collapse/expand).
function paneCoreMountSession(container, opts) {
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

  // Auto-grow the textarea up to the shared cap so multi-line
  // prompts don't get clipped (same pattern as chat / transcript).
  const autosize = () => {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, COMPOSER_AUTOSIZE_MAX_PX) + "px";
  };
  input.addEventListener("input", autosize);

  // Build the term object. kind="session" so persistence and layout
  // code distinguishes it from chat / transcript / terminal.
  const t = {
    jobId: paneKey,
    sid,                         // raw session UUID (no "session:" prefix)
    pane, body, input, sendBtn,
    source: null,
    task: "session " + sid,
    kind: "session",
    state: "mirror",             // updated by the first state_change SSE frame
    currentAssistant: null,
    toolUseEls: new Map(),
  };

  termInitAutoFollow(t);

  pane.querySelector(".close-btn").addEventListener("click", (e) => {
    e.stopPropagation();
    termClose(paneKey);
  });
  // Release: ask the server to hand session control back to the engine.
  pane.querySelector(".release-btn")?.addEventListener("click", (e) => {
    e.stopPropagation();
    fetch("/api/sessions/" + encodeURIComponent(sid) + "/release", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    }).catch((err) => {
      setMsg("#term-msg", "err", "Release failed: " + err.message, TERM_MSG_DURATION_MS);
    });
  });
  // Interrupt: signal the current turn to stop processing.
  pane.querySelector(".interrupt-btn")?.addEventListener("click", (e) => {
    e.stopPropagation();
    fetch("/api/sessions/" + encodeURIComponent(sid) + "/interrupt", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    }).catch((err) => {
      setMsg("#term-msg", "err", "Interrupt failed: " + err.message, TERM_MSG_DURATION_MS);
    });
  });
  pane.addEventListener("click", () => {
    document.querySelectorAll(".term-pane.focus").forEach((p) => p.classList.remove("focus"));
    pane.classList.add("focus");
  });

  // Composer wiring — always enabled (no fork gate, no dead-pane check).
  const doSend = () => termSendSession(t, t.input.value);
  sendBtn.addEventListener("click", doSend);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      doSend();
    }
  });

  // Lazy SSE streaming — mirrors the transcript pane pattern so session panes
  // don't consume one of the browser's ~6 HTTP/1.1 connection slots while
  // collapsed. The /api/sessions/<sid>/stream endpoint re-sends a leading
  // state_change frame then re-tails the .jsonl from the start on every new
  // connection, so a naive reopen would duplicate the conversation. We avoid
  // that by clearing the rendered body and resetting currentAssistant / toolUseEls
  // in openStream before subscribing — the fresh replay overwrites nothing.
  // termSetCollapsed wires expand -> openStream / collapse -> closeStream.
  t.openStream = () => {
    if (t.source) return;
    // Clear prior rendered content and reset accumulation state so the
    // server's replay-from-start doesn't duplicate the conversation.
    t.body.innerHTML = "";
    t.currentAssistant = null;
    t.toolUseEls = new Map();
    // Reset auto-follow so the fresh replay scrolls from the top smoothly.
    t.autoFollowBottom = true;
    t.firstScroll = true;
    const es = new EventSource("/api/sessions/" + encodeURIComponent(sid) + "/stream");
    t.source = es;
    es.onopen = () => {
      termSetPillState(statusPill, "running", "connecting");
      termSetActivity(t, "connecting…", "busy");
    };
    es.onmessage = (ev) => {
      let obj;
      try { obj = JSON.parse(ev.data); } catch (_) { return; }
      termHandleSessionEvent(t, obj);
    };
    es.addEventListener("end", () => {
      try { es.close(); } catch (_) {}
      t.source = null;
      termSetPillState(statusPill, "done", "ended");
      termSetActivity(t, "ended", "ready");
      // Keep composer enabled — operator can still send; backend will
      // re-acquire / re-engine on the next /input call.
    });
    es.onerror = () => {
      // Transient disconnect: don't mark the pane dead while the browser
      // is still reconnecting (readyState === CONNECTING). Only surface
      // the disconnect when the browser has given up (CLOSED).
      if (es.readyState !== EventSource.CLOSED) return;
      termSetPillState(statusPill, "warn", "disconnected");
      termSetActivity(t, "disconnected", "ended");
      t.source = null;
    };
  };
  t.closeStream = () => {
    if (!t.source) return;
    try { t.source.close(); } catch (_) {}
    t.source = null;
    termSetPillState(statusPill, "done", "paused");
    termSetActivity(t, "paused (expand to resume)", "ready");
  };

  // Whether the pane starts collapsed is a LAYOUT decision the terminals.js
  // shim computes (from termGetLayout) and passes in as ``opts.collapsed``.
  // PaneCore applies it here so the lazy-stream decision below sees the
  // correct initial state. (The shim still owns the collapse semantics —
  // this is just the seed it hands us so a list-mode session pane doesn't
  // auto-open an SSE only to have the shim collapse it a tick later.)
  if (opts && opts.collapsed) pane.classList.add("collapsed");
  // Open the stream immediately only if NOT collapsed; collapsed panes stay
  // connection-free until the operator expands them.
  if (!pane.classList.contains("collapsed")) {
    t.openStream();
  } else {
    termSetPillState(statusPill, "done", "paused");
    termSetActivity(t, "paused (expand to resume)", "ready");
  }

  const activity = paneCoreActivityWiring(pane);

  // Store the disconnect on `t` so termClose (which only has `t`, not
  // the pane handle) can also tear down the observer. Idempotent.
  t._activityDisconnect = function () {
    try { activity.disconnect(); } catch (_) {}
  };

  return {
    t, pane, key: paneKey, kind: "session",
    close() { t._activityDisconnect(); termClose(paneKey); },
    onActivity: activity.onActivity,
    openStream: () => t.openStream(),
    closeStream: () => t.closeStream(),
  };
}

// Per-kind metadata fetch used by both restore and the list. The pane KEY
// shape decides which endpoint to hit (mirrors the canvas key space + the
// per-kind branches in terminals.js ``restoreOpenPanes``):
//   * "ide:<sid>"     → transcript. /api/transcripts must still list <sid>,
//                       else the session file is gone → null. Returns a
//                       { kind:"transcript", sessionId } shape (the field
//                       paneCoreMountTranscript reads from meta).
//   * "session:<sid>" → session. /api/sessions must still list <sid>, else
//                       null. Returns { kind:"session", sid }.
//   * bare id that is NOT a known prefix → terminal (PTY). /api/ptys/<id>;
//                       null when 404 OR status !== "running" (the shell died,
//                       so restore must skip it — matches restoreOpenPanes).
//   * "job:<id>" or any other bare id → chat. /api/jobs/<id>; null on 404.
//
// Returns null whenever the underlying resource is gone so the canvas restore
// loop can prune the dead key from the tree. Promise-based, no side effects at
// load (safe to extract under the node sidecar).
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
        // /api/sessions merges dashboard + IDE sessions; each item carries
        // both ``sid`` and the back-compat ``session_id``. Match either.
        const items = data.sessions || data || [];
        const entry = (Array.isArray(items) ? items : []).find(
          (e) => e && (e.sid === sid || e.session_id === sid)
        );
        return entry ? { kind: "session", sid } : null;
      })
      .catch(() => null);
  }

  // PTY ids are bare (no prefix) and are NOT job ids. A job id is also bare,
  // so we can't always tell them apart by shape alone — but the canvas stores
  // chat keys with the "job:" prefix, leaving truly-bare keys as PTY ids.
  if (raw.slice(0, 4) !== "job:") {
    // Treat as PTY first; the endpoint 404s for non-PTY ids and we fall to null.
    return fetch("/api/ptys/" + encodeURIComponent(raw), { cache: "no-store" })
      .then((r) => (r.ok ? r.json() : null))
      .then((meta) => {
        if (!meta) return null;
        // A shell that already exited must be skipped on restore.
        if (meta.status && meta.status !== "running") return null;
        return Object.assign({ kind: "terminal" }, meta);
      })
      .catch(() => null);
  }

  // Chat / chat-codex: the job record. Accept a "job:" prefix or a bare id.
  const jobId = raw.slice(0, 4) === "job:" ? raw.slice(4) : raw;
  return fetch("/api/jobs/" + encodeURIComponent(jobId), { cache: "no-store" })
    .then((r) => (r.ok ? r.json() : null))
    .catch(() => null);
}

// Public entry: builds the pane for the given kind into ``container``.
// Handles chat / chat-codex (Task 6) and terminal / transcript / session
// (Task 7). All branches are layout-AGNOSTIC — the caller (terminals.js
// shims or the canvas) owns list/grid placement, collapse/pin, and
// persistence.
function paneCoreMount(container, opts) {
  const kind = (opts && opts.kind) || "";
  if (kind === "chat" || kind === "chat-codex") {
    return paneCoreMountChat(container, opts);
  }
  if (kind === "terminal") {
    return paneCoreMountPty(container, opts);
  }
  if (kind === "transcript") {
    return paneCoreMountTranscript(container, opts);
  }
  if (kind === "session") {
    return paneCoreMountSession(container, opts);
  }
  throw new Error("PaneCore.mount: unsupported kind " + JSON.stringify(kind));
}

window.PaneCore = {
  mount: paneCoreMount,
  fetchMeta: paneCoreFetchMeta,
};
