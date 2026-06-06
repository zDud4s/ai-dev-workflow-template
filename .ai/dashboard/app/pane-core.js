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
  // ({label, cls}). PaneCore drives this by observing the chip element's
  // mutations, so any code path that flips activity (termSetActivity,
  // dead-state, codex turns) propagates without each call site knowing
  // about subscribers. The canvas uses this to broadcast `activity` to
  // the status list.
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
      if (_activityObserver) { try { _activityObserver.disconnect(); } catch (_) {} _activityObserver = null; }
      termClose(t.jobId);
    },
    // Subscribe to activity changes ({label, cls}). Returns an
    // unsubscribe function.
    onActivity(cb) {
      if (typeof cb !== "function") return () => {};
      activitySubs.push(cb);
      ensureObserver();
      return () => {
        const i = activitySubs.indexOf(cb);
        if (i >= 0) activitySubs.splice(i, 1);
      };
    },
  };
  return handle;
}

// Per-kind metadata fetch used by both restore and the list. For chat
// kinds this is the job record at /api/jobs/<id>. (PTY/transcript/session
// fetches join in Task 7.)
function paneCoreFetchMeta(key) {
  return fetch("/api/jobs/" + encodeURIComponent(key), { cache: "no-store" })
    .then((r) => (r.ok ? r.json() : null));
}

// Public entry: builds the pane for the given kind into ``container``.
// Only chat / chat-codex are handled here (Task 6); other kinds delegate
// back to their existing terminals.js openers until Task 7 moves them in.
function paneCoreMount(container, opts) {
  const kind = (opts && opts.kind) || "";
  if (kind === "chat" || kind === "chat-codex") {
    return paneCoreMountChat(container, opts);
  }
  throw new Error("PaneCore.mount: unsupported kind " + JSON.stringify(kind));
}

window.PaneCore = {
  mount: paneCoreMount,
  fetchMeta: paneCoreFetchMeta,
};
