// .ai/dashboard/app/terminals.js -- extracted from app.js (was lines 1471..3065)
// NOTE: top-level const/let were converted to var so identifiers cross <script> boundaries.

    // ----- terminals (multi-pane real-time view) -----
    // Each entry: { jobId, source, pane, body, input, sendBtn, status, task }
    var TERMS = new Map();

    // ----- shared PTY token cache (survives F5) -----
    //
    // The canvas window owns PTY rendering and layout persistence. This
    // dashboard-side legacy key is read only now, used only to migrate tokens
    // written before PTYs moved fully into the canvas.
    var PERSIST_KEY = "dash.openPanes.v2";
    var PTY_TOKEN_CACHE_KEY = "dash.ptyTokens.v1";

    function ptyTokenCacheRead() {
      try {
        if (typeof localStorage === "undefined") return {};
        const raw = localStorage.getItem(PTY_TOKEN_CACHE_KEY);
        const data = raw ? JSON.parse(raw) : {};
        return data && typeof data === "object" && !Array.isArray(data) ? data : {};
      } catch (_) {
        return {};
      }
    }

    function ptyTokenCacheWrite(data) {
      try {
        if (typeof localStorage === "undefined") return;
        localStorage.setItem(PTY_TOKEN_CACHE_KEY, JSON.stringify(data || {}));
      } catch (_) { /* ignore quota / security errors */ }
    }

    window.PtyTokens = window.PtyTokens || {
      get: function (id) {
        const key = String(id || "");
        if (!key) return "";
        return ptyTokenCacheRead()[key] || "";
      },
      set: function (id, token) {
        const key = String(id || "");
        if (!key || !token) return;
        const data = ptyTokenCacheRead();
        data[key] = String(token);
        ptyTokenCacheWrite(data);
      },
      remove: function (id) {
        const key = String(id || "");
        if (!key) return;
        const data = ptyTokenCacheRead();
        if (!Object.prototype.hasOwnProperty.call(data, key)) return;
        delete data[key];
        ptyTokenCacheWrite(data);
      },
    };

    function termRememberPtyToken(id, token) {
      if (!id || !token) return;
      window._PTY_TOKENS = window._PTY_TOKENS || {};
      window._PTY_TOKENS[id] = token;
      if (window.PtyTokens && typeof window.PtyTokens.set === "function") {
        window.PtyTokens.set(id, token);
      }
    }

    function termLookupPtyToken(id) {
      if (!id) return "";
      if (window._PTY_TOKENS && window._PTY_TOKENS[id]) return window._PTY_TOKENS[id];
      if (window.PtyTokens && typeof window.PtyTokens.get === "function") {
        return window.PtyTokens.get(id) || "";
      }
      return "";
    }

    // (Legacy IDE-transcript status-poll machinery removed — Claude
    // conversations are unified session panes that drive their own SSE
    // stream; there is no separate read-only transcript pane to poll.)
    function persistOpenPanes() {
      // Canvas owns durable pane layout now; keep this as a no-op for older
      // dashboard call sites until the remaining inline job helpers are retired.
    }

    // ----- send-to-canvas bridge (additive) -----
    //
    // A pane can be "sent to" the standalone canvas window (app/canvas.html),
    // which tiles panes via window.PaneCore. The dashboard and the canvas talk
    // over the cross-window CanvasBus (app/canvas-bus.js, channel/storage key
    // "dash.canvas.v1"). This block owns the dashboard side of that protocol:
    //   * a single bus client (created lazily, guarded against double-init),
    //   * a queue-until-ready buffer so an `open` posted right after we
    //     window.open()'d the canvas isn't dropped before the canvas boots,
    //   * inbound handling of opened / closed / ready / activity to paint an
    //     "on canvas" badge on the matching pane header.
    // Outbound message shapes (must match canvas.js dispatchBusMessage):
    //   {type:"open", key, kind}  {type:"focus", key}  {type:"hello"}
    var _CANVAS_BUS = null;          // CanvasBus.create handle ({post, close})
    var _CANVAS_QUEUE = null;        // CanvasBus.makeQueue() — buffers until ready
    var _CANVAS_ON_KEYS = new Set(); // keys currently mounted on the canvas
    var CANVAS_STALE_INTERVAL_MS = 3000; // mirrors canvas.js heartbeat cadence
    var _CANVAS_WIN = null;          // handle to the opened canvas window

    // Open (or focus) the single named canvas window WITHOUT ever reloading it.
    //
    // CRITICAL: calling window.open("app/canvas.html", "dash-canvas") when the
    // window already exists RE-NAVIGATES it — reloading canvas.html, tearing
    // down every mounted pane and dropping the in-flight `open` message. So
    // adding a terminal would reload the whole canvas. We must NEVER pass the
    // canvas URL to an existing window.
    //
    // Instead always reacquire via an EMPTY url: window.open("", "dash-canvas")
    // returns the existing window WITHOUT navigating it (verified: no reload),
    // or a fresh blank window if none exists. We only navigate to canvas.html
    // when the window is genuinely blank (i.e. we just created it) — detected
    // by reading its same-origin location. A reused live canvas is left exactly
    // as it is. (We don't rely on the heartbeat for liveness here: a backgrounded
    // canvas throttles its heartbeat and would look "dead", which previously
    // forced the reloading URL path.)
    function canvasOpenWindow() {
      try {
        if (_CANVAS_WIN && !_CANVAS_WIN.closed) {
          try { _CANVAS_WIN.focus(); } catch (_) {}
          return _CANVAS_WIN;
        }
        var w = window.open("", "dash-canvas");
        if (!w) {
          // Empty-url open can be blocked on the very first user gesture in some
          // browsers; fall back to a real open so the canvas still appears.
          _CANVAS_WIN = window.open("app/canvas.html", "dash-canvas");
          return _CANVAS_WIN;
        }
        try {
          var href = (w.location && w.location.href) || "";
          // Navigate ONLY when the window isn't already our canvas (a freshly
          // created blank window). A live canvas keeps its panes — no reload.
          if (href === "" || href === "about:blank" || href.indexOf("canvas.html") === -1) {
            w.location.href = "app/canvas.html";
          }
        } catch (_) { /* same-origin popup; shouldn't throw */ }
        try { w.focus(); } catch (_) {}
        _CANVAS_WIN = w;
      } catch (_) {
        _CANVAS_WIN = null;
      }
      return _CANVAS_WIN;
    }

    // The pane's term-object → the {key, kind} the bus speaks. Key is
    // normalized the SAME way the canvas normalizes inbound keys (pty: prefix
    // stripped) so badge bookkeeping lines up across windows. kind is the
    // pane's own kind; the canvas refines meta itself via PaneCore.fetchMeta,
    // so we never send meta.
    function canvasKeyForTerm(t) {
      if (!t) return null;
      var raw = t.jobId;
      if (typeof raw !== "string" || !raw) return null;
      if (window.CanvasBus && typeof window.CanvasBus.normalizeKey === "function") {
        return window.CanvasBus.normalizeKey(raw);
      }
      return raw;
    }

    // Lazily create the dashboard's CanvasBus client + queue. Idempotent: a
    // second call returns the existing handle. Returns null when CanvasBus
    // isn't loaded (defensive — index.html loads canvas-bus.js before us).
    function canvasEnsureBus() {
      if (_CANVAS_BUS) return _CANVAS_BUS;
      if (!window.CanvasBus || typeof window.CanvasBus.create !== "function") return null;
      _CANVAS_QUEUE = window.CanvasBus.makeQueue();
      _CANVAS_BUS = window.CanvasBus.create({ onMessage: handleCanvasBusMessage });
      return _CANVAS_BUS;
    }

    // Reflect "is this key on the canvas?" onto its pane header: toggle the
    // pane's .on-canvas class and show/hide the badge label. Tolerates a
    // missing pane (key the dashboard doesn't have open).
    function setCanvasBadge(key, on) {
      if (!key) return;
      if (on) _CANVAS_ON_KEYS.add(key); else _CANVAS_ON_KEYS.delete(key);
      // Status rows (the Terminals tab's primary content) mirror the badge too.
      setStatusRowCanvasBadge(key, on);
      // The dashboard keys panes by jobId; for terminals the bus key is the
      // bare id, which already equals jobId. session:/job: keys match directly.
      var t = TERMS.get(key);
      if (!t || !t.pane) return;
      t.pane.classList.toggle("on-canvas", !!on);
      var head = t.pane.querySelector(".term-head");
      if (!head) return;
      var badge = head.querySelector(".on-canvas-badge");
      if (on) {
        if (!badge) {
          badge = document.createElement("span");
          badge.className = "on-canvas-badge";
          badge.textContent = "on canvas";
          badge.title = "This pane is mirrored on the canvas window";
          // Park it just before the actions cluster so it reads with the head.
          var actions = head.querySelector(".actions");
          if (actions) head.insertBefore(badge, actions); else head.appendChild(badge);
        }
      } else if (badge) {
        badge.remove();
      }
    }

    // Toggle the "on canvas" badge on a matching status row (if rendered).
    // Rows key off data-key, which is the same normalized bus key.
    function setStatusRowCanvasBadge(key, on) {
      var grid = document.getElementById("terms-grid");
      if (!grid) return;
      var row = grid.querySelector('.term-status-row[data-key="' + (window.CSS && CSS.escape ? CSS.escape(key) : key) + '"]');
      if (!row) return;
      row.classList.toggle("on-canvas", !!on);
      var actions = row.querySelector(".row-actions");
      if (!actions) return;
      var badge = actions.querySelector(".on-canvas-badge");
      if (on) {
        if (!badge) {
          badge = document.createElement("span");
          badge.className = "on-canvas-badge";
          badge.textContent = "on canvas";
          badge.title = "Mirrored on the canvas window";
          actions.insertBefore(badge, actions.querySelector(".send-to-canvas"));
        }
      } else if (badge) {
        badge.remove();
      }
    }

    // Inbound bus messages from the canvas window. Routed through the queue's
    // ready gate only for ordering parity with the canvas; badge updates are
    // idempotent so immediate handling is also safe.
    function handleCanvasBusMessage(msg) {
      if (!msg || typeof msg !== "object") return;
      if (msg.type === "opened") { setCanvasBadge(window.CanvasBus.normalizeKey(msg.key), true); return; }
      if (msg.type === "closed") { setCanvasBadge(window.CanvasBus.normalizeKey(msg.key), false); return; }
      if (msg.type === "ready") {
        // The canvas has booted → flush any `open`/`focus` we queued while it
        // was starting up. flush() also marks the queue ready, so subsequent
        // pushes post immediately.
        if (_CANVAS_QUEUE && !_CANVAS_QUEUE.ready()) {
          _CANVAS_QUEUE.flush(function (queued) { if (_CANVAS_BUS) _CANVAS_BUS.post(queued); });
        }
        // Authoritative open set from the canvas — clear stale badges, set the
        // listed ones. Snapshot current badges so we can diff-clear.
        var next = new Set((msg.open || []).map(function (k) { return window.CanvasBus.normalizeKey(k); }));
        _CANVAS_ON_KEYS.forEach(function (k) { if (!next.has(k)) setCanvasBadge(k, false); });
        next.forEach(function (k) { setCanvasBadge(k, true); });
        return;
      }
      if (msg.type === "activity") {
        // Optional: reflect a one-shot activity hint on the badge title and,
        // for status rows, surface the live label on the row's activity chip.
        var key = window.CanvasBus.normalizeKey(msg.key);
        var t = TERMS.get(key);
        var badge = t && t.pane && t.pane.querySelector(".on-canvas-badge");
        if (badge && msg.label) badge.title = "on canvas · " + msg.label;
        if (msg.label) {
          var grid = document.getElementById("terms-grid");
          var row = grid && grid.querySelector('.term-status-row[data-key="' + (window.CSS && CSS.escape ? CSS.escape(key) : key) + '"]');
          var chip = row && row.querySelector(".activity");
          if (chip) chip.textContent = msg.label;
        }
        return;
      }
    }

    // If the canvas window's persisted heartbeat (lastSeen) has gone stale,
    // the canvas window is gone → clear every badge. Called on load before we
    // post `hello`, so a crashed/closed canvas doesn't leave ghost badges.
    function canvasClearStaleBadges() {
      if (!window.CanvasBus || typeof window.CanvasBus.loadState !== "function") return;
      var state = window.CanvasBus.loadState();
      if (!state) return;
      if (window.CanvasBus.isStale(state, Date.now(), CANVAS_STALE_INTERVAL_MS)) {
        var keys = [..._CANVAS_ON_KEYS];
        for (var i = 0; i < keys.length; i++) setCanvasBadge(keys[i], false);
      }
    }

    // The send-to-canvas click handler for a pane's term object. Opens (or
    // focuses) the named canvas window, then posts open/focus over the bus.
    function termSendToCanvas(t, opts) {
      opts = opts || {};
      var key = canvasKeyForTerm(t);
      if (!key) return;
      var bus = canvasEnsureBus();
      if (!bus) {
        setMsg("#term-msg", "err", "Canvas bridge unavailable (canvas-bus.js not loaded)", TERM_MSG_DURATION_MS);
        return;
      }
      // Already on the canvas → focus it instead of opening a duplicate pane.
      if (_CANVAS_ON_KEYS.has(key)) {
        bus.post({ type: "focus", key: key });
        // Bring the window forward WITHOUT reloading it (see canvasOpenWindow).
        canvasOpenWindow();
        return;
      }
      // Ensure the canvas window exists (named target ⇒ single instance) and
      // bring it forward without ever reloading an already-open canvas.
      var win = canvasOpenWindow();
      if (!win) {
        setMsg("#term-msg", "warn",
          "Canvas popup blocked — allow popups for this site, then click canvas again",
          TERM_MSG_DURATION_MS);
        return;
      }
      var kind = opts.kind || (t && t.kind) || undefined;
      var meta = opts.meta || null;
      if (kind === "terminal") {
        var token = (meta && meta.token) || termLookupPtyToken(key);
        if (token) meta = Object.assign({}, meta || {}, { token: token });
      }
      var openMsg = { type: "open", key: key, kind: kind };
      if (meta) openMsg.meta = meta;
      if (Object.prototype.hasOwnProperty.call(opts, "initialCommand")) {
        openMsg.initialCommand = opts.initialCommand;
      }
      // If the canvas just opened it hasn't sent `ready` yet → queue the open
      // and flush when ready arrives. If it's already ready (badges live),
      // post immediately.
      if (_CANVAS_QUEUE && !_CANVAS_QUEUE.ready()) {
        _CANVAS_QUEUE.push(openMsg);
      } else {
        bus.post(openMsg);
      }
    }

    // Append the send-to-canvas button to a freshly-built pane header and wire
    // its click. Additive: it slots into the existing .actions cluster without
    // touching any sibling control. Re-paints the badge if the key is already
    // known to be on the canvas (e.g. pane restored while canvas open).
    function termWireCanvasButton(t) {
      if (!t || !t.pane) return;
      var head = t.pane.querySelector(".term-head");
      if (!head) return;
      var actions = head.querySelector(".actions");
      if (!actions || actions.querySelector(".send-to-canvas")) return;
      var btn = document.createElement("button");
      btn.className = "send-to-canvas";
      btn.type = "button";
      btn.dataset.action = "send-canvas";
      btn.title = "Send this pane to the canvas window";
      btn.textContent = "⊞";
      btn.addEventListener("click", function (e) {
        e.stopPropagation();
        termSendToCanvas(t);
      });
      actions.appendChild(btn);
      // If the canvas already announced this key, show the badge immediately.
      var key = canvasKeyForTerm(t);
      if (key && _CANVAS_ON_KEYS.has(key)) setCanvasBadge(key, true);
    }

    async function restoreOpenPanes() {
      let saved;
      try {
        const raw = localStorage.getItem(PERSIST_KEY);
        saved = raw ? JSON.parse(raw) : null;
      } catch (_) { return; }
      if (!saved || typeof saved !== "object") return;
      // Migrate tokens from retired dashboard pane persistence. The canvas
      // restores its own PTY layout and authenticates from this shared cache.
      if (saved.tokens && typeof saved.tokens === "object") {
        for (const id of Object.keys(saved.tokens)) {
          termRememberPtyToken(id, saved.tokens[id]);
        }
      }
    }

    // ----- status-bar helpers -----
    // Each pane opens collapsed (one-line status row). The body+composer
    // only render when the operator expands it explicitly OR when input is
    // required (e.g. a dead chat pane that can be resumed). The "activity"
    // chip tracks what the pane is doing right now so the operator can scan
    // the list without expanding every pane.

    // Status pills carry exactly ONE state class at a time (running /
    // done / bad / warn / queued / cancelling / cancelled). The old code
    // sprinkled ad-hoc ``classList.add("done")`` calls without removing
    // the previous state, so a PTY that ended kept its "running" green
    // alongside the new "done" — done wins for done (it's later in CSS)
    // but warn/bad were left under running (which comes AFTER them in
    // the cascade) and the pill stayed green for a "disconnected" or
    // failed shell. This helper normalises every transition.
    // Tool-identity classes ("claude", "codex") are preserved.
    // termSetPillState + _PILL_STATE_CLASSES moved to pane-helpers.js
    // (pure render leaf; loaded before terminals.js, resolves as a global).

    function termSetActivity(t, label, cls) {
      if (!t || !t.pane) return;
      const el = t.pane.querySelector(".term-head .activity");
      if (!el) return;
      const prevCls = t._activityCls || "";
      el.textContent = label || "";
      el.classList.remove("busy", "waiting", "ready", "ended");
      if (cls) el.classList.add(cls);
      t._activityCls = cls || "";
      // "Waiting" on the pane (NOT the chip) means "operator's turn has
      // come up" — it floats the row to the top of list mode and keeps
      // it expanded across layout switches.
      const operatorWaiting = cls === "waiting";
      t.pane.classList.toggle("is-waiting", operatorWaiting);
      // If the pane is collapsed and we're showing fresh activity, mark
      // it so the row gets an accent edge until the operator opens it.
      if (t.pane.classList.contains("collapsed") && cls === "busy") {
        t.pane.classList.add("has-update");
      }
      // Auto-expand on the transition INTO operator-waiting — that's
      // the cue the operator's turn has come up. Fires once per
      // transition (not on every redraw), so manually re-collapsing
      // during a long idle is sticky until the pane goes busy again.
      if (operatorWaiting && prevCls !== "waiting"
          && t.pane.classList.contains("collapsed")) {
        termSetCollapsed(t, false);
      }
    }

    function termSetCollapsed(t, collapsed, opts) {
      if (!t || !t.pane) return;
      t.pane.classList.toggle("collapsed", !!collapsed);
      const btn = t.pane.querySelector(".expand-btn");
      if (btn) btn.textContent = collapsed ? "expand" : "collapse";
      // Lazy streaming: collapsed session panes don't hold an EventSource
      // (which would consume one of the browser's ~6 HTTP/1.1 connection
      // slots per origin). Expand attaches the stream; collapse releases it.
      // Session panes moved to the canvas; keep this hook defensive for any
      // restored object that still exposes lazy stream controls.
      if (t.kind === "session") {
        if (collapsed) {
          if (typeof t.closeStream === "function") t.closeStream();
        } else {
          if (typeof t.openStream === "function") t.openStream();
        }
      }
      if (!collapsed) {
        // Operator opened the pane — clear the "new activity" indicator
        // and pulse so the row isn't shouting anymore.
        t.pane.classList.remove("has-update", "needs-action");
        // Scroll to bottom once expanded so they see the latest output.
        if (t.body) {
          requestAnimationFrame(() => {
            try { t.body.scrollTop = t.body.scrollHeight; } catch (_) {}
          });
        }
        // Bring the pane into view if it's offscreen. Suppressed for
        // bulk callers (layout switch) — otherwise every pane in the
        // grid races to scrollIntoView and the viewport jumps to
        // whichever one resolved last.
        if (!opts || !opts.silent) {
          try { t.pane.scrollIntoView({ block: "nearest", behavior: "smooth" }); } catch (_) {}
        }
      }
      // Legacy compatibility hook; canvas owns durable pane layout.
      persistOpenPanes();
    }

    function termToggleCollapsed(t) {
      if (!t || !t.pane) return;
      termSetCollapsed(t, !t.pane.classList.contains("collapsed"));
    }

    function termCollapseAll() {
      // Drafts are skipped — they have no expand button, so collapsing
      // them would hide the composer with no way to bring it back.
      for (const t of TERMS.values()) {
        if (t.isDraft) continue;
        termSetCollapsed(t, true);
      }
    }

    // True when the pane has meaningful rendered content. Used by layout
    // switching to decide whether to auto-expand: a pane with content is
    // worth a column slot in split/grid; an empty pane just becomes a
    // dark rectangle, so it stays collapsed until the operator opens it.
    //
    // We anchor on .msg.user / .bash-cmd specifically because those carry
    // OPERATOR-AUTHORED text that's always rendered. .msg.assistant alone
    // is NOT enough — it matches the thinking-placeholder and the empty
    // in-progress assistant block during streaming, both of which have
    // no visible text and produce the "expanded empty rectangle" bug
    // when split/grid switching evaluates them.
    function termPaneHasContent(t) {
      if (!t || !t.body) return false;
      if (t.kind === "terminal") return true;
      return t.body.querySelector(".msg.user, .bash-cmd") !== null;
    }

    // ----- layout control: REMOVED (Chunk 5b-1) -----
    // The Terminals tab is a pure status list now; multi-pane geometry
    // (side-by-side / tiled / resize) is owned by the standalone canvas
    // window (app/canvas.html). The old layout selector, its persisted
    // preference, and the apply/get/set-layout helpers + their grid CSS
    // classes were deleted with the inline panes. Any still-live inline pane
    // that a legacy path (restore / picker) opens always starts collapsed as
    // a status row; the operator routes it to the canvas via send-to-canvas.

    // Hard cap on options per group. A long-running project accumulates
    // thousands of jobs/transcripts; jamming them all into a single
    // <select> makes the picker unusable (the dropdown becomes a wall of
    // UUIDs) AND rebuilding the same 1000+ <option> elements every
    // loadJobs poll thrashes the DOM. The newest N are what the operator
    // ever actually wants to reopen; older sessions are still reachable
    // via the Run / Sessions tabs.
    var PICKER_MAX_PER_GROUP = 50;

    // Auto-grow composer textareas up to this many CSS pixels so multi-
    // line prompts don't get clipped to one row but the composer also
    // doesn't eat the entire pane. Previously hardcoded as the literal
    // ``220`` at five sites (draft, regular chat, transcript fork,
    // resume reset, codex-rekey reset).
    var COMPOSER_AUTOSIZE_MAX_PX = 220;

    // Default duration for #term-msg toast messages. The pre-existing
    // call sites all used the literal ``4000``; this single source of
    // truth keeps subsequent UX tweaks (e.g. lower to 3000 for snappier
    // feedback) one-line affairs and prevents the linter-eye from
    // mistaking 4000-ms toasts for a stream-timeout / network constant.
    var TERM_MSG_DURATION_MS = 4000;

    async function termRefreshPicker(jobs) {
      // The Terminals tab is a launcher + status list now — there is no picker
      // <select> to populate. We still fetch the unified session snapshot and
      // feed the status renderer the FULL session + job set (the renderer drops
      // kind === "chat" itself, since chats are sessions). Kept named
      // termRefreshPicker for the existing call sites (loadJobs, the shim).
      let allSessions = [];
      try {
        const r = await fetch("/api/sessions", { cache: "no-store" });
        if (r.ok) {
          const data = await r.json();
          // Codex (chat-codex) is not a claude --resume session — keep it out of
          // the session set so it renders as a job row, not a Claude pane target.
          allSessions = (data.sessions || []).filter((s) => s.kind !== "chat-codex" && s.sid);
        }
      } catch (_) { /* ignore — the status list still renders the job rows */ }
      termRenderStatusList(allSessions, jobs || []);
    }

    // Back-compat shim so existing call sites that only refresh the
    // transcripts side end up reusing the unified refresh.
    async function termRefreshTranscriptPicker() {
      // Replay loadJobs's tail using whatever was returned last time so we
      // don't double-fetch. If no cached jobs, just refresh empty.
      try {
        const r = await fetch("/api/jobs", { cache: "no-store" });
        const data = await r.json();
        await termRefreshPicker(data.jobs || []);
      } catch (_) {
        await termRefreshPicker([]);
      }
    }

    function termRenderEmptyState() {
      // Inline panes (drafts, legacy restore/picker opens) still live in
      // #terms-grid alongside the status rows. The empty placeholder is only
      // shown when there are NEITHER inline panes NOR status rows; the status
      // renderer owns its own placeholder, so here we just drop the legacy
      // placeholder once any inline pane exists.
      const grid = $("#terms-grid");
      if (!grid) return;
      if (TERMS.size > 0) {
        const empty = grid.querySelector(".term-empty");
        if (empty) empty.remove();
      }
      termUpdateTerminalsCount();
    }

    // ----- status list (Chunk 5b-1) -----
    // The Terminals tab is a read-only status list: one row per Claude
    // session and per dashboard job, summarising state + activity + tool/model
    // + task. The actual interactive pane lives in the canvas window; each row
    // carries a send-to-canvas control (⊞) and an on-canvas badge that lights
    // up while the canvas mirrors that key. Rows are sorted active-first; the
    // finished ones collapse into a disclosure group so the live work stays
    // at the top. This renderer NEVER mounts an interactive pane inline.
    var _STATUS_ROWS_KEY = "terms-status-rows";
    var _STATUS_FINISHED_OPEN_KEY = "dash.terms.finishedOpen";
    var _statusFinishedOpen = false;
    try { _statusFinishedOpen = localStorage.getItem(_STATUS_FINISHED_OPEN_KEY) === "1"; } catch (_) {}

    // Last data we rendered from, so the bus listener can re-paint badges
    // (on-canvas state) without re-fetching.
    var _STATUS_LAST = { sessions: [], jobs: [] };

    // ----- launched-but-not-yet-opened resources -----
    // "New terminal" is a pure launcher: the operator picks what to launch
    // (AI chat / shell), tool and model, hits Launch, and the resource is
    // CREATED but NOT shown on the canvas. It lands here and renders as a
    // status row with a ⊞ so the operator opens it on the canvas when they
    // want. Persisted so a dashboard reload doesn't lose the launch.
    //   entry = { id, kind:"session"|"terminal", tool, model, label, ts,
    //             token?, steps? }   (token/steps only for kind:"terminal")
    var LAUNCHED_KEY = "dash.launched.v1";
    var _LAUNCHED = [];
    (function loadLaunched() {
      try {
        var raw = localStorage.getItem(LAUNCHED_KEY);
        var arr = raw ? JSON.parse(raw) : [];
        if (Array.isArray(arr)) _LAUNCHED = arr.filter((e) => e && e.id && e.kind);
      } catch (_) { _LAUNCHED = []; }
    })();
    function persistLaunched() {
      try { localStorage.setItem(LAUNCHED_KEY, JSON.stringify(_LAUNCHED)); } catch (_) {}
    }
    function addLaunched(entry) {
      _LAUNCHED.unshift(entry);
      persistLaunched();
      termRenderStatusList();
    }
    function removeLaunched(id) {
      var n = _LAUNCHED.length;
      _LAUNCHED = _LAUNCHED.filter((e) => e.id !== id);
      if (_LAUNCHED.length !== n) persistLaunched();
    }
    // Open a launched entry on the canvas, then drop it from the pending list
    // (the canvas owns it now; a session also reappears via /api/sessions).
    function openLaunched(id) {
      var e = _LAUNCHED.find((x) => x.id === id);
      if (!e) return;
      if (e.kind === "terminal") {
        if (e.token) termRememberPtyToken(e.id, e.token);
        var opts = { meta: e.token ? { token: e.token } : null };
        if (e.steps && e.steps.length) opts.initialCommand = e.steps;
        termSendToCanvas(_statusRowTerm("terminal", e.id), opts);
      } else {
        // Claude session: route the (still transcript-less) sid to the canvas;
        // the operator types the first message in the canvas pane, which
        // create-on-first-turn materialises.
        termRouteSessionToCanvas(e.id);
      }
      // Keep the launched entry in the list so the row stays visible (now with
      // an "on canvas" badge) and re-openable after the canvas pane is closed.
      // A launched SESSION is auto-dropped once it materialises in /api/sessions
      // (dedup in termRenderStatusList); a launched TERMINAL stays until the
      // operator dismisses it (the ✕ on its row). Re-render to paint the badge.
      termRenderStatusList();
    }

    // Active vs finished partition. A session/job is "active" while it is
    // running / queued / cancelling / mirror (a live IDE session) — anything
    // terminal (done / failed / cancelled) drops to the finished group.
    var _STATUS_ACTIVE_JOB = new Set(["running", "queued", "cancelling"]);
    function _statusIsActiveJob(j) { return _STATUS_ACTIVE_JOB.has(j.status); }
    // A passive IDE transcript counts as "active" (top of the list) only if it
    // was touched within this window — otherwise the Terminals tab fills with
    // every Claude conversation ever recorded in the project dir. The archive
    // is still reachable in the collapsed "inactive" group below.
    var _STATUS_SESSION_FRESH_MS = 15 * 60 * 1000;
    function _statusModifiedWithinMs(s, ms) {
      var iso = s.modified || s.started_at || "";
      if (!iso) return false;
      var t = Date.parse(iso);
      if (isNaN(t)) return false;
      return (Date.now() - t) <= ms;
    }
    function _statusIsActiveSession(s) {
      // "Active" = work the operator is genuinely driving right now, not the
      // whole transcript archive. A session is active when an engine is
      // attached (dashboard/canvas is live on it), the registry baton is held
      // (owned/acquiring/engine), a dashboard-launched chat is still running,
      // or the transcript was touched very recently. Everything else — the
      // passive IDE mirrors and anything explicitly done — drops to the
      // collapsed "inactive" group so the live work stays at the top.
      var st = (s.state || "mirror").toLowerCase();
      if (st === "done") return false;
      if (s.has_engine) return true;
      if (st === "owned" || st === "acquiring" || st === "engine") return true;
      if (s.source === "dashboard" && _STATUS_ACTIVE_JOB.has(s.status)) return true;
      return _statusModifiedWithinMs(s, _STATUS_SESSION_FRESH_MS);
    }

    // Pseudo-term for a status row so the existing canvas bridge
    // (termSendToCanvas / canvasKeyForTerm) works unchanged: it only reads
    // .jobId + .kind. For sessions the bus key is "session:<sid>"; for jobs
    // it is the bare job id (matches how the inline panes key themselves).
    function _statusRowTerm(kind, key) { return { jobId: key, kind: kind }; }

    function termSessionCanvasKey(sidOrKey) {
      const raw = String(sidOrKey || "");
      if (!raw) return "";
      return raw.startsWith("session:") ? raw : ("session:" + raw);
    }

    function termRouteSessionToCanvas(sidOrKey) {
      const key = termSessionCanvasKey(sidOrKey);
      if (!key) return "";
      termSendToCanvas(_statusRowTerm("session", key));
      return key;
    }

    function termJobCanvasTarget(jobId, meta) {
      const j = meta || {};
      if (j.kind === "chat" && j.session_id) {
        return { key: "session:" + j.session_id, kind: "session" };
      }
      return { key: jobId, kind: j.kind || "job" };
    }

    function termRouteJobToCanvas(jobId, meta) {
      const target = termJobCanvasTarget(jobId, meta);
      if (!target.key) return target;
      termSendToCanvas(_statusRowTerm(target.kind, target.key));
      return target;
    }

    function _statusRowEl(opts) {
      // opts: { key, kind, pill, pillState, activity, tool, preview, title,
      //         onOpen? }  — onOpen overrides the default ⊞ behavior (used by
      //         launched-but-not-opened rows, which materialise on open).
      const row = document.createElement("div");
      row.className = "term-status-row";
      row.dataset.key = opts.key;
      row.dataset.kind = opts.kind;
      // escHtml (not the ambiguous `escape`, which reads like window.escape's
      // URL-encoder) HTML-escapes the server-supplied fields below — a session
      // title / job task can carry arbitrary text, so this is the XSS guard.
      const pillCls = opts.pillState ? " " + escHtml(opts.pillState) : "";
      row.innerHTML =
        `<span class="pill status-pill${pillCls}">${escHtml(opts.pill || "")}</span>` +
        `<span class="activity${opts.activityCls ? " " + escHtml(opts.activityCls) : ""}">${escHtml(opts.activity || "")}</span>` +
        `<span class="row-tool" title="${escHtml(opts.toolTitle || "")}">${escHtml(opts.tool || "")}</span>` +
        `<span class="row-task" title="${escHtml(opts.title || "")}">${escHtml(opts.preview || "")}</span>` +
        `<span class="row-actions">` +
          `<button class="send-to-canvas" type="button" data-action="send-canvas" title="Open this in the canvas window">⊞</button>` +
        `</span>`;
      // Reflect any live on-canvas badge for this key immediately.
      const onCanvas = _CANVAS_ON_KEYS.has(opts.key);
      if (onCanvas) {
        row.classList.add("on-canvas");
        const badge = document.createElement("span");
        badge.className = "on-canvas-badge";
        badge.textContent = "on canvas";
        badge.title = "Mirrored on the canvas window";
        row.querySelector(".row-actions").insertBefore(badge, row.querySelector(".send-to-canvas"));
      }
      const sendBtn = row.querySelector(".send-to-canvas");
      sendBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        if (typeof opts.onOpen === "function") { opts.onOpen(); return; }
        termSendToCanvas(_statusRowTerm(opts.kind, opts.key));
      });
      // Optional dismiss control (launched rows): a ✕ that removes the row.
      if (typeof opts.onDismiss === "function") {
        const x = document.createElement("button");
        x.className = "row-dismiss";
        x.type = "button";
        x.dataset.action = "dismiss";
        x.title = "Dismiss this launch";
        x.textContent = "✕";
        x.addEventListener("click", (e) => { e.stopPropagation(); opts.onDismiss(); });
        row.querySelector(".row-actions").appendChild(x);
      }
      return row;
    }

    // Build the rows from a sessions[] + jobs[] snapshot. Sessions are Claude
    // conversations (key "session:<sid>"); jobs are non-chat dashboard jobs
    // (orchestrate / plan / codex). Chat jobs (kind === "chat") are sessions
    // now, so they are excluded from the jobs side to avoid double rows.
    function termRenderStatusList(sessions, jobs) {
      const grid = $("#terms-grid");
      if (!grid) return;
      if (Array.isArray(sessions)) _STATUS_LAST.sessions = sessions;
      if (Array.isArray(jobs)) _STATUS_LAST.jobs = jobs;
      sessions = _STATUS_LAST.sessions || [];
      jobs = (_STATUS_LAST.jobs || []).filter((j) => j.kind !== "chat");

      let container = grid.querySelector("." + _STATUS_ROWS_KEY);
      if (!container) {
        container = document.createElement("div");
        container.className = _STATUS_ROWS_KEY;
        // Status rows render before any legacy inline pane in the grid.
        grid.insertBefore(container, grid.firstChild);
      }
      container.innerHTML = "";

      // Drop launched SESSIONS that have since materialised in /api/sessions
      // (first message sent) — the real row takes over, so we don't show both.
      const _sessionSids = new Set((sessions || []).map((s) => s && s.sid).filter(Boolean));
      const _dropped = _LAUNCHED.filter((e) => e.kind === "session" && _sessionSids.has(e.id));
      if (_dropped.length) { _dropped.forEach((e) => removeLaunched(e.id)); }

      // Launched rows render first (the freshest operator intent). Their ⊞
      // materialises + opens on the canvas; the ✕ dismisses the launch.
      const launchedEls = [];
      for (const e of _LAUNCHED) {
        const isTerm = e.kind === "terminal";
        const onCanvas = _CANVAS_ON_KEYS.has(e.id);
        const el = _statusRowEl({
          key: e.id,
          kind: e.kind,
          pill: "launched",
          pillState: "queued",
          activity: onCanvas ? "on canvas" : "open on canvas →",
          activityCls: "waiting",
          tool: e.tool || (isTerm ? "shell" : "claude"),
          toolTitle: e.model || e.tool || "",
          preview: e.label || (isTerm ? "shell" : "AI chat"),
          title: e.label || e.id,
          onOpen: () => openLaunched(e.id),
          onDismiss: () => { removeLaunched(e.id); termRenderStatusList(); },
        });
        launchedEls.push(el);
      }

      const active = [];
      const finished = [];
      for (const s of sessions) {
        const sid = s.sid;
        if (!sid) continue;
        const state = (s.state || "mirror");
        const preview = (s.title || s.task || "").replace(/\s+/g, " ").slice(0, 80);
        const el = _statusRowEl({
          key: "session:" + sid,
          kind: "session",
          pill: state,
          pillState: _statusIsActiveSession(s) ? "running" : "done",
          activity: _statusIsActiveSession(s) ? "live" : "ended",
          activityCls: _statusIsActiveSession(s) ? "busy" : "ended",
          tool: "claude",
          toolTitle: s.model || "claude",
          preview: preview || (sid.slice(0, 8) + "…"),
          title: "session " + sid,
        });
        (_statusIsActiveSession(s) ? active : finished).push(el);
      }
      for (const j of jobs) {
        const preview = (j.task || "").replace(/\s+/g, " ").slice(0, 80);
        const tool = j.kind === "chat-codex" ? "codex" : (j.kind || "job");
        const el = _statusRowEl({
          key: j.id,
          kind: j.kind || "job",
          pill: j.status || "?",
          pillState: _statusIsActiveJob(j) ? "running" : (j.status === "done" ? "done" : "bad"),
          activity: _statusIsActiveJob(j) ? "running" : (j.status || "ended"),
          activityCls: _statusIsActiveJob(j) ? "busy" : "ended",
          tool: tool,
          toolTitle: j.model || tool,
          preview: preview || j.id.slice(0, 8),
          title: j.task || j.id,
        });
        (_statusIsActiveJob(j) ? active : finished).push(el);
      }

      if (!launchedEls.length && !active.length && !finished.length) {
        // No rows AND no inline panes → show the placeholder.
        if (TERMS.size === 0 && !grid.querySelector(".term-empty")) {
          const empty = document.createElement("div");
          empty.className = "term-empty";
          empty.innerHTML = "Nothing launched yet. Click <em>New terminal</em> to launch an AI chat or a shell, then open it on the canvas with ⊞.";
          container.appendChild(empty);
        }
        termUpdateTerminalsCount();
        return;
      }

      for (const el of launchedEls) container.appendChild(el);
      for (const el of active) container.appendChild(el);

      if (finished.length) {
        const details = document.createElement("details");
        details.className = "terms-finished-group";
        details.open = _statusFinishedOpen;
        const summary = document.createElement("summary");
        summary.textContent = "inactive (" + finished.length + ")";
        details.appendChild(summary);
        for (const el of finished) details.appendChild(el);
        details.addEventListener("toggle", () => {
          _statusFinishedOpen = details.open;
          try { localStorage.setItem(_STATUS_FINISHED_OPEN_KEY, details.open ? "1" : "0"); } catch (_) {}
        });
        container.appendChild(details);
      }

      termUpdateTerminalsCount();
    }

    // Count badge: inline panes + status rows.
    function termUpdateTerminalsCount() {
      const grid = $("#terms-grid");
      const rows = grid ? grid.querySelectorAll(".term-status-row").length : 0;
      const total = TERMS.size + rows;
      const el = $("#count-terminals");
      if (el) el.textContent = total || "·";
    }

    // ----- Draft terminal (created via "New terminal") -----
    // A draft pane is an EMPTY terminal: no job exists on the server yet.
    // The operator picks tool (claude / codex) + model and only when they
    // hit send does the dashboard POST /api/jobs and turn the draft into
    // a real, connected pane.

    // Mirrors the MODELS_BY_TOOL catalog in core.js. Kept here as a local
    // fallback so this file doesn't depend on script load order.
    var DRAFT_MODELS_BY_TOOL = (typeof MODELS_BY_TOOL === "object" && MODELS_BY_TOOL)
      ? MODELS_BY_TOOL
      : {
          claude: ["claude-opus-4-8", "claude-opus-4-7", "claude-sonnet-4-6", "claude-haiku-4-5"],
          codex:  ["gpt-5.5", "gpt-5.4", "gpt-5.4-mini", "gpt-5.3-codex"],
        };

    var _draftCounter = 0;

    function termDraftModelOptions(tool, selected) {
      const list = DRAFT_MODELS_BY_TOOL[tool] || [];
      return list.map((m) =>
        `<option value="${escape(m)}"${m === selected ? " selected" : ""}>${escape(m)}</option>`
      ).join("");
    }

    // Shell options for the "New shell" draft pane. "auto" lets the
    // server pick the platform default (pwsh / cmd on Windows, $SHELL
    // or zsh / bash on POSIX). The explicit ids are passed through to
    // /api/ptys as the ``shell`` field and resolved server-side.
    var DRAFT_SHELLS = [
      { id: "auto",       label: "Auto (platform default)" },
      { id: "bash",       label: "bash" },
      { id: "zsh",        label: "zsh" },
      { id: "fish",       label: "fish" },
      { id: "pwsh",       label: "PowerShell 7+ (pwsh)" },
      { id: "powershell", label: "Windows PowerShell" },
      { id: "cmd",        label: "Command Prompt (cmd)" },
      { id: "sh",         label: "sh" },
    ];

    function termDraftShellOptions(selected) {
      return DRAFT_SHELLS.map((s) =>
        `<option value="${escape(s.id)}"${s.id === selected ? " selected" : ""}>${escape(s.label)}</option>`
      ).join("");
    }

    // Type dropdown: how to present the AI session.
    //   "ai"        -> stream-json chat pane (current chat-pane flow)
    //   "shell:<x>" -> open a real PTY (shell <x>) AND launch the chosen
    //                  tool with the chosen model inside it, then type
    //                  the first message into the running AI so the
    //                  operator sees the full TUI experience.
    function termDraftTypeOptionsHtml(selected) {
      const opts = [`<option value="ai"${"ai" === selected ? " selected" : ""}>AI chat (direct)</option>`];
      const shellHtml = DRAFT_SHELLS.map((s) => {
        const v = "shell:" + s.id;
        return `<option value="${escape(v)}"${v === selected ? " selected" : ""}>${escape(s.label)}</option>`;
      }).join("");
      opts.push(`<optgroup label="Run AI inside a real terminal">${shellHtml}</optgroup>`);
      return opts.join("");
    }

    // Build the argv/command line that launches the chosen AI tool in
    // a shell, taking the model into account. Both binaries accept the
    // prompt as a positional arg in interactive mode, but we'd rather
    // send the message AFTER launch so the operator sees the TUI come
    // up first — this returns just the launcher; the message follows.
    //
    // For Claude we also pin a ``--session-id`` so the caller can
    // pre-mark that uuid as already-handled in AUTO_OPENED_ONCE — this
    // prevents the IDE-transcript auto-opener from spawning a duplicate
    // mirror pane the moment claude writes its first JSONL line.
    function termDraftLaunchCommand(tool, model, sessionId) {
      // Strict allowlist to defeat shell-metachar injection. `safeModel`
      // is typed verbatim into a running PTY via WebSocket — any
      // metachar (; $() && backtick newline space) would be evaluated
      // by the shell. Legitimate model names contain only
      // [A-Za-z0-9._-]; anything else falls back to an empty string
      // so claude/codex surface a clear "model required" error instead
      // of executing attacker payloads.
      const rawModel = String(model || "");
      const safeModel = /^[A-Za-z0-9._-]+$/.test(rawModel) ? rawModel : "";
      if (tool === "codex") {
        return `codex -m ${safeModel}`;
      }
      const sid = sessionId ? ` --session-id ${sessionId}` : "";
      return `claude${sid} --model ${safeModel}`;
    }

    // Mint a client-side session id. crypto.randomUUID where available; the
    // biased Math.random fallback is fine for a session SELECTOR (never a
    // security token) on the rare browser without crypto.randomUUID.
    function termMintSid() {
      if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
      return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
        const r = Math.random() * 16 | 0;
        const v = c === "x" ? r : (r & 0x3 | 0x8);
        return v.toString(16);
      });
    }

    // "New terminal" → a compact LAUNCHER (not a chat). The operator picks what
    // to launch (AI chat / Terminal), the tool and the model, then clicks
    // Launch. The resource is CREATED and added to the status list as a
    // launched row — it is NOT shown on the canvas. The operator opens it on
    // the canvas with ⊞ and interacts there (first message / shell). Decoupling
    // launch from open matches the converged model: the dashboard launches, the
    // canvas hosts the interactive panes.
    // Wire the launcher controls that live in the Terminals toolbar (#term-
    // launchtype / #term-tool / #term-model / #term-shell). Populates the model
    // list, shows the Shell field only for terminals, and constrains AI chat to
    // Claude (Codex's native UX is its CLI — launch it as a shell). Called once
    // on boot.
    function wireLauncherToolbar() {
      const launchSel = $("#term-launchtype");
      const toolSel = $("#term-tool");
      const modelSel = $("#term-model");
      const shellSel = $("#term-shell");
      const shellField = document.querySelector(".term-shell-field");
      const hint = $("#term-launch-hint");
      if (!launchSel || !toolSel || !modelSel) return;
      if (shellSel) shellSel.innerHTML = termDraftShellOptions("auto");
      const codexOpt = [...toolSel.options].find((o) => o.value === "codex");

      const refresh = () => {
        const isShell = launchSel.value === "shell";
        if (shellField) shellField.hidden = !isShell;
        if (codexOpt) codexOpt.disabled = !isShell;
        if (!isShell && toolSel.value === "codex") toolSel.value = "claude";
        if (hint) {
          hint.textContent = isShell
            ? "Launches a real shell" + (toolSel.value === "claude" || toolSel.value === "codex" ? " running " + toolSel.value : "") + " — open it on the canvas with ⊞."
            : "Launches a Claude chat — open it on the canvas with ⊞ and send your first message there.";
        }
      };
      const repopulateModels = () => {
        const tool = toolSel.value;
        modelSel.innerHTML = termDraftModelOptions(tool, (DRAFT_MODELS_BY_TOOL[tool] || [""])[0] || "");
      };
      repopulateModels();
      refresh();
      launchSel.addEventListener("change", refresh);
      toolSel.addEventListener("change", () => { repopulateModels(); refresh(); });
    }

    // "New terminal" = launch the resource configured in the toolbar selects.
    // It is CREATED and added to the status list as a launched row — NOT shown
    // on the canvas. The operator opens it on the canvas with ⊞ (and, for an AI
    // chat, types the first message there → create-on-first-turn).
    async function termOpenDraft() {
      const launchType = ($("#term-launchtype") || {}).value || "ai";
      const tool = ($("#term-tool") || {}).value || "claude";
      const model = ($("#term-model") || {}).value || "";

      if (launchType === "ai") {
        // Direct Claude session. Nothing is sent now — the conversation
        // materialises (create-on-first-turn) when the operator types the first
        // message in the canvas session pane.
        if (!model) { setMsg("#term-msg", "err", "Pick a model before launching.", TERM_MSG_DURATION_MS); return; }
        const sid = termMintSid();
        addLaunched({ id: sid, kind: "session", tool: "claude", model,
                      label: "Claude · " + model, ts: Date.now() });
        setMsg("#term-msg", "ok", "Claude chat launched — open it on the canvas (⊞) and send your first message there.", TERM_MSG_DURATION_MS);
        return;
      }

      // Terminal (shell): create the PTY now; the tool launch command (if any)
      // runs when the operator opens the row on the canvas.
      if ((tool === "claude" || tool === "codex") && !model) {
        setMsg("#term-msg", "err", "Pick a model before launching.", TERM_MSG_DURATION_MS); return;
      }
      const shell = ($("#term-shell") || {}).value || "auto";
      const btn = $("#term-new");
      if (btn) { btn.disabled = true; }
      try {
        const res = await postJson("/api/ptys", { shell, cols: 100, rows: 30 });
        termRememberPtyToken(res.id, res.token);
        let steps = [];
        let label = "Shell (" + shell + ")";
        if (tool === "claude" || tool === "codex") {
          // Pin a session-id for Claude so its transcript is identifiable.
          const preSid = tool === "claude" ? termMintSid() : null;
          // 600ms (was 300) so the shell prompt + PSReadLine are fully ready
          // before the launch line is typed — avoids a mangled command on a
          // still-initialising PowerShell.
          steps = [{ text: termDraftLaunchCommand(tool, model, preSid), delay: 600 }];
          label = (tool === "claude" ? "Claude" : "Codex") + " · " + model + " (shell)";
        }
        addLaunched({ id: res.id, kind: "terminal", tool, model, token: res.token, steps, label, ts: Date.now() });
        setMsg("#term-msg", "ok", "Terminal launched — open it on the canvas (⊞).", TERM_MSG_DURATION_MS);
      } catch (err) {
        setMsg("#term-msg", "err", "Launch failed: " + err.message, TERM_MSG_DURATION_MS);
      } finally {
        if (btn) btn.disabled = false;
      }
    }

    function termAppendChunk(t, chunk) {
      if (!chunk) return;
      // Cap pane buffer at ~200 KB to keep DOM responsive.
      const MAX = 200_000;
      const node = document.createTextNode(chunk);
      t.body.appendChild(node);
      if (t.body.textContent.length > MAX) {
        t.body.textContent = t.body.textContent.slice(-MAX);
      }
      termSetActivity(t, "streaming…", "busy");
      termAutoScroll(t);
    }

    // Classic chat-pane scroll behaviour: stick to bottom unless the user has
    // scrolled up manually. Reset to "follow" when they scroll back near the
    // bottom. The FIRST scroll after a pane opens uses smooth behaviour so
    // big catch-up dumps slide down rather than snapping.
    // ----- In-pane search (Ctrl+F) -----
    function termToggleSearch(t, open) {
      const bar = t.pane.querySelector(".term-search");
      const wantOpen = open === undefined ? !bar.classList.contains("open") : open;
      bar.classList.toggle("open", wantOpen);
      if (wantOpen) {
        bar.querySelector("input").focus();
        termRunSearch(t);
      } else {
        termClearSearchHighlights(t);
      }
    }

    function termClearSearchHighlights(t) {
      t.body.querySelectorAll("mark.term-hit").forEach((m) => {
        const txt = document.createTextNode(m.textContent);
        m.parentNode.replaceChild(txt, m);
      });
      // normalize() walks the entire body subtree to merge adjacent text
      // nodes — O(n) per call. The previous search input handler ran this
      // on every keystroke even when no highlights existed. Gate the call
      // behind a flag so we only pay the cost on the active→inactive
      // transition, i.e. when the previous run actually placed marks.
      if (t._searchActive) {
        t.body.normalize();
        t._searchActive = false;
      }
      t._searchHits = [];
      t._searchIdx = 0;
      const m = t.pane.querySelector(".term-search .matches");
      if (m) m.textContent = "0 / 0";
    }

    // Defensive cap on text-node scan per termRunSearch invocation. A
    // chat pane that's been streaming for an hour can accumulate tens of
    // thousands of text nodes; combined with the existing 150ms debounce
    // this bound keeps a single search call bounded even on enormous
    // panes. The previous text scrolls off-screen but stays in DOM so
    // anchoring on the most-recent N keeps the search responsive.
    var TERM_SEARCH_NODE_CAP = 20000;

    function termRunSearch(t) {
      termClearSearchHighlights(t);
      const q = t.pane.querySelector(".term-search input").value;
      if (!q) return;
      const lower = q.toLowerCase();
      const walker = document.createTreeWalker(t.body, NodeFilter.SHOW_TEXT, null);
      const targets = [];
      // Bound the walk so panes with extremely large DOM trees don't
      // burn 100ms per search call even after the input debounce.
      let scanned = 0;
      while (walker.nextNode()) {
        if (++scanned > TERM_SEARCH_NODE_CAP) break;
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
      // Track whether the body currently has highlight marks so the next
      // clear call can skip the O(n) normalize() walk when none exist.
      t._searchActive = hits.length > 0;
      const matches = t.pane.querySelector(".term-search .matches");
      if (matches) matches.textContent = hits.length ? "1 / " + hits.length : "0 / 0";
      if (hits.length) {
        hits[0].classList.add("current");
        try { hits[0].scrollIntoView({block: "center", behavior: "smooth"}); } catch (_) {}
      }
    }

    function termSearchStep(t, delta) {
      const hits = t._searchHits || [];
      if (!hits.length) return;
      hits[t._searchIdx]?.classList.remove("current");
      t._searchIdx = (t._searchIdx + delta + hits.length) % hits.length;
      const next = hits[t._searchIdx];
      next.classList.add("current");
      try { next.scrollIntoView({block: "center", behavior: "smooth"}); } catch (_) {}
      const m = t.pane.querySelector(".term-search .matches");
      if (m) m.textContent = (t._searchIdx + 1) + " / " + hits.length;
    }

    // termExportMarkdown + termInitAutoFollow moved to pane-helpers.js
    // (pure render leaves; loaded before terminals.js, resolve as globals).

    function termSetDead(t, label) {
      // If the subprocess died mid-turn, the placeholder has no streaming
      // event coming to replace it — clear it explicitly.
      termClearThinkingPlaceholder(t);
      // Cancel pending autocomplete debounce so it doesn't fire against a
      // pane the operator has visually moved on from (would spam /api/skills
      // and /api/files/list with stale prefix lookups for a dead session).
      if (t._composerTimer) {
        clearTimeout(t._composerTimer);
        t._composerTimer = null;
      }
      // Close any open autocomplete popup defensively — a click on a stale
      // entry would otherwise splice into a composer about to be repurposed
      // as the resume-input.
      if (t._popOpen) {
        termCloseAutocomplete(t);
      }
      // Drop tool-use DOM ref Map so late tool_result frames (e.g. from a
      // slow PostToolUse hook posting after the parent claude exited) can't
      // mutate cached nodes on a pane the operator now treats as history.
      // `.clear()` instead of reassign — other code may hold the same Map ref.
      if (t.toolUseEls && typeof t.toolUseEls.clear === "function") {
        t.toolUseEls.clear();
      }
      // Dispatch-tracker placeholder ref → null so the strong ref releases
      // and the placeholder node can be collected once the pane is closed.
      if (t._waitingMsg) {
        t._waitingMsg = null;
      }
      t.pane.classList.add("dead");
      const status = t.pane.querySelector(".status-pill");
      if (status && label) {
        // Mutate the pill IN PLACE — never replace the node via outerHTML.
        // PTY closures (ws.onopen/onmessage/onerror/onclose) and the SSE
        // wiring capture the original .status-pill reference at pane-open
        // time. Replacing the element via ``outerHTML = ...`` detaches
        // the captured node from the DOM, so every subsequent
        // termSetPillState(statusPill, ...) call from those closures
        // mutates an orphan and the user sees stale state forever.
        // Clear the state classes we know about, then apply the new one
        // and update the visible text. Same node, same listeners, fresh
        // appearance.
        status.classList.remove("running", "done", "bad", "warn", "queued", "cancelling", "cancelled");
        status.className = "pill " + (label === "done" ? "done" : "bad") + " status-pill";
        status.textContent = label;
      }
      // A dead pane is history: surface the terminal state and lock the
      // composer. Claude chats are unified session panes now; the old
      // job-based "resume on next message" affordance was removed when its
      // send pipeline (termSendResumeChat) was retired, so there is no
      // chat-specific dead state left to special-case here.
      termSetActivity(t, label || "ended", label === "done" ? "ready" : "ended");
      t.input.disabled = true;
      t.sendBtn.disabled = true;
    }

    function termClose(jobId) {
      const t = TERMS.get(jobId);
      if (!t) return;
      try { t.source && t.source.close(); } catch (e) { console.warn("[terminals] termClose: SSE close failed: " + (e && e.message ? e.message : e)); }
      // Stop the SSE heartbeat watchdog so the closed pane doesn't keep
      // a timer alive that calls termSetDead on an already-removed pane.
      if (t._sseHeartbeat) {
        clearInterval(t._sseHeartbeat);
        t._sseHeartbeat = null;
      }
      // Cost-refresh debounce + auto-follow scroll listener cleanup.
      if (t._costRefreshTimer) { clearTimeout(t._costRefreshTimer); t._costRefreshTimer = null; }
      if (t._autoFollowScrollHandler && t.body) {
        try { t.body.removeEventListener("scroll", t._autoFollowScrollHandler); } catch (_) {}
        t._autoFollowScrollHandler = null;
      }
      t.pane.remove();
      TERMS.delete(jobId);
      // Closing a pane is the operator's explicit "I don't want to see
      // this anymore" signal — suppress auto-open for this id so it
      // doesn't come back on the next poll or after F5.
      suppressAutoOpen(jobId);
      termRenderEmptyState();
      persistOpenPanes();
      // Fire-and-forget refresh — async loadJobs failures (network blip,
      // server transient 5xx) must not become unhandled rejections that
      // pollute the browser console with cryptic "Uncaught (in promise)"
      // stacks. Surface via warn so transient failures are diagnosable
      // but don't interrupt the close flow.
      Promise.resolve(loadJobs()).catch((e) => console.warn("[terminals] loadJobs after termClose failed: " + (e && e.message ? e.message : e)));
    }

    async function termSend(arg) {
      // Accepts either a jobId string (legacy callers) or the term object
      // itself. The object form is required for chat-codex panes whose
      // ``t.jobId`` is re-keyed across turns — closures captured at
      // pane creation time would otherwise point at the FIRST turn's job and
      // fail with 404 from turn 2 onwards.
      const t = typeof arg === "object" && arg ? arg : TERMS.get(arg);
      if (!t) return;
      const text = t.input.value;
      const attached = t.attached || { images: [], files: [] };
      if (!text.trim() && !attached.images.length && !attached.files.length) return;
      // Codex chat is one subprocess per turn (``codex exec`` exits after
      // emitting its answer). To present continuous multi-turn UX in the
      // same pane, every follow-up message spawns a fresh
      // ``codex exec resume <sid>`` job and the pane's SSE is rewired
      // in place — see termSendCodexNextTurn.
      if (t.kind === "chat-codex") {
        await termSendCodexNextTurn(t, text, attached);
        return;
      }
      t.sendBtn.disabled = true;
      try {
        const payload = { text };
        if (attached.images.length) payload.images = attached.images;
        if (attached.files.length) payload.files = attached.files;
        await postJson(`/api/jobs/${t.jobId}/input`, payload);
        // termSend now only drives generic job panes (orchestrate / plan);
        // Claude chats are session panes and chat-codex returns above. Echo
        // the operator's input into the pane body.
        const echo = document.createElement("span");
        echo.className = "stdin-echo";
        echo.textContent = `\n> ${text}\n`;
        t.body.appendChild(echo);
        t.body.scrollTop = t.body.scrollHeight;
        t.input.value = "";
        // Reset textarea auto-grown height so the next prompt starts at one row.
        if (t.input.tagName === "TEXTAREA") t.input.style.height = "";
        t.attached = { images: [], files: [] };
        termRenderAttachments(t);
      } catch (e) {
        const err = document.createElement("span");
        err.style.color = "var(--bad)";
        err.textContent = `\n[input failed: ${e.message}]\n`;
        t.body.appendChild(err);
        // Also surface to the global toast: when the chat scroll is well
        // past the bottom or the pane is collapsed in list mode, the
        // inline ``[input failed]`` line is invisible. setMsg is the only
        // reliable signal that the operator's click did not land.
        setMsg("#term-msg", "err", "Send failed: " + e.message, TERM_MSG_DURATION_MS);
        if (/not running|409/i.test(e.message)) termSetDead(t, "ended");
      } finally {
        t.sendBtn.disabled = false;
        t.input.focus();
      }
    }

    // ----- chat-codex: multi-turn (one job per turn, SSE rewired in-place)
    //
    // The codex CLI exits after one turn. To make a chat-codex pane feel
    // continuous, we:
    //   1. capture session_id from the codex stream (server-side, in
    //      _start_subprocess_job, looking for ``type=session_meta``);
    //   2. on SSE 'end' for a chat-codex pane, mark the pane idle instead
    //      of dead (termCodexAwaitNextTurn);
    //   3. on the next user send, POST a fresh chat-codex job with
    //      ``resume_session_id=<sid>``, then close the old EventSource and
    //      open a new one bound to the new job id — re-keying TERMS so the
    //      same pane object owns both job ids over its lifetime.

    function termCodexBeginTurn(t) {
      if (t.input) t.input.disabled = true;
      if (t.sendBtn) { t.sendBtn.disabled = true; t.sendBtn.textContent = "running…"; }
      termSetActivity(t, "running…", "busy");
    }

    async function termCodexAwaitNextTurn(t) {
      // Re-entry guard. Multiple async sources fire this concurrently for
      // the same pane: SSE 'end', onerror (CLOSED), the 15s heartbeat
      // watchdog (for the chat-codex codepath), and (post-rekey) the
      // previous job's lingering events. Without serialisation each
      // caller fetches /api/jobs/<id> in parallel and the last write of
      // session_id/model wins — sometimes overwriting a captured
      // session_id with an empty string for the rekeyed turn. Drop
      // overlapping calls; the first one wins, the rest no-op.
      if (t._codexAwaitInFlight) return;
      t._codexAwaitInFlight = true;
      try {
        termClearThinkingPlaceholder(t);
        termSetActivity(t, "waiting", "waiting");
        // Route through termSetPillState so every prior state class
        // (running/queued/cancelling/bad/warn) is cleared before "done" lands.
        // The old toggle/add cocktail left stale classes that the CSS cascade
        // could resolve in the wrong order.
        termSetPillState(t.pane.querySelector(".status-pill"), "done", "ready");
        if (t.input) {
          t.input.disabled = false;
          t.input.placeholder = "type, /skill, @file — Enter sends next turn (Codex resumes session)";
        }
        if (t.sendBtn) {
          t.sendBtn.disabled = false;
          t.sendBtn.textContent = "send";
        }
        // Fetch the latest job summary so we pick up the session_id the
        // server captured from the codex stream. Without it the next turn
        // can't resume.
        try {
          const r = await fetch(`/api/jobs/${t.jobId}`, { cache: "no-store" });
          if (r.ok) {
            const j = await r.json();
            if (j.session_id) t.sessionId = j.session_id;
            if (j.model) t.model = j.model;
          }
        } catch (_) { /* the operator can retry — we'll try again on send */ }
        termRefreshCost(t);
      } finally {
        t._codexAwaitInFlight = false;
      }
    }

    async function termSendCodexNextTurn(t, text, attached) {
      // The first turn happens via the draft/run flow with initial_stdin;
      // termSend should only fire for follow-up turns. If we somehow got
      // here without a captured session_id, try one last fetch — then bail.
      if (!t.sessionId) {
        try {
          const r = await fetch(`/api/jobs/${t.jobId}`, { cache: "no-store" });
          if (r.ok) {
            const j = await r.json();
            if (j.session_id) t.sessionId = j.session_id;
          }
        } catch (_) { /* fall through */ }
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
      // Render the operator's message locally before the new job spawns
      // so the chat reads naturally, and put up a "thinking" bubble.
      termRenderUserMessage(t, text);
      termShowThinkingPlaceholder(t);
      t.input.value = "";
      if (t.input.tagName === "TEXTAREA") t.input.style.height = "";
      t.attached = { images: [], files: [] };
      termRenderAttachments(t);
      termCodexBeginTurn(t);
      try {
        const payload = { kind: "chat-codex", task: text, resume_session_id: t.sessionId };
        if (t.model) payload.model = t.model;
        const res = await postJson("/api/jobs", payload);
        // Re-key TERMS so the same pane is reachable by its NEW job id —
        // legacy inline-pane closures passed the pane object (`t`) to
        // termSend(), not a string, so they keep working across rekeys.
        // Park the old jobId -> new jobId mapping so a late cancel/close
        // call keyed on the previous id (e.g. a fast button click that
        // fired against the in-flight pane) still resolves to the same
        // pane instead of silently no-op'ing on a removed map entry.
        var oldJobId = t.jobId;
        TERMS.delete(t.jobId);
        t.jobId = res.id;
        t.pane.dataset.jobId = res.id;
        TERMS.set(res.id, t);
        if (oldJobId && oldJobId !== res.id) {
          window._JOB_ID_ALIASES = window._JOB_ID_ALIASES || {};
          window._JOB_ID_ALIASES[oldJobId] = res.id;
        }
        // Legacy compatibility hook; canvas-owned panes persist elsewhere.
        persistOpenPanes();
        const idEl = t.pane.querySelector(".id");
        if (idEl) idEl.textContent = res.id.slice(0, 8);
        // Normalize pill state through the helper — clears queued, done,
        // warn, bad, cancelling so the new "running" doesn't compose with
        // a stale class from the previous turn.
        termSetPillState(t.pane.querySelector(".status-pill"), "running", "connecting");
        // Tear down the previous job's SSE and bind a new one to the
        // freshly-spawned resume job.
        try { t.source && t.source.close(); } catch (_) {}
        t.jsonBuf = [];
        t.currentAssistant = null;
        const es = new EventSource(`/api/jobs/${res.id}/stream`);
        t.source = es;
        es.onopen = () => {
          // Route through termSetPillState so the pill state is normalised
          // — the ad-hoc ``textContent = "live" + classList.remove("queued")``
          // here left other prior classes (warn/bad/done from a previous
          // turn that flipped through them) stacked under "running", and
          // the cascade resolved colours unpredictably. The helper clears
          // every known state class first, then applies the new one.
          termSetPillState(t.pane.querySelector(".status-pill"), "running", "live");
          termSetActivity(t, "live", "busy");
        };
        es.onmessage = (ev) => termHandleCodexChunk(t, ev.data);
        es.addEventListener("end", () => {
          try { es.close(); } catch (_) {}
          termCodexAwaitNextTurn(t);
          Promise.resolve(loadJobs()).catch((e) => console.warn("[terminals] loadJobs after codex end failed: " + (e && e.message ? e.message : e)));
        });
        es.onerror = () => {
          // Same readyState gate as the chat onerror — only react when the
          // browser has given up; let CONNECTING-state retries pass.
          if (t.pane.classList.contains("dead")) return;
          if (es.readyState !== EventSource.CLOSED) return;
          termCodexAwaitNextTurn(t);
        };
        Promise.resolve(loadJobs()).catch((e) => console.warn("[terminals] loadJobs after codex rekey failed: " + (e && e.message ? e.message : e)));
      } catch (e) {
        termClearThinkingPlaceholder(t);
        const err = document.createElement("div");
        err.className = "msg system";
        err.style.color = "var(--bad)";
        err.textContent = `[next turn failed: ${e.message}]`;
        t.body.appendChild(err);
        setMsg("#term-msg", "err", "next turn failed: " + e.message, TERM_MSG_DURATION_MS);
        if (t.input) t.input.disabled = false;
        if (t.sendBtn) { t.sendBtn.disabled = false; t.sendBtn.textContent = "send"; }
        termSetActivity(t, "error", "ended");
      }
    }

    // Minimal renderer for the codex JSON event stream. We deliberately
    // surface only assistant text, reasoning blocks, and tool calls — the
    // session_meta / turn_context / event_msg noise stays out of the
    // chat body. The operator's own messages are rendered locally on
    // send, so we skip the user/developer ``response_item`` records that
    // codex injects for system prompts and AGENTS.md context.
    function termHandleCodexChunk(t, chunk) {
      if (!chunk) return;
      // Buffer deltas in an array and join once per chunk to avoid the
      // O(n²) repeated string allocation that the old `+=` pattern caused
      // on long streams. The trailing partial line (if any) is preserved
      // as the sole remaining buffer entry for the next chunk to extend.
      if (!Array.isArray(t.jsonBuf)) t.jsonBuf = t.jsonBuf ? [t.jsonBuf] : [];
      t.jsonBuf.push(chunk);
      const joined = t.jsonBuf.join("");
      const lastNl = joined.lastIndexOf("\n");
      if (lastNl === -1) { termAutoScroll(t); return; }
      const complete = joined.slice(0, lastNl);
      const remnant = joined.slice(lastNl + 1);
      t.jsonBuf = remnant ? [remnant] : [];
      for (const line of complete.split("\n")) {
        const trimmed = line.trim();
        if (!trimmed) continue;
        let obj;
        try { obj = JSON.parse(trimmed); }
        catch (_) { continue; }
        termRenderCodexEvent(t, obj);
      }
      termAutoScroll(t);
    }

    function termRenderCodexEvent(t, obj) {
      if (!obj || typeof obj !== "object") return;
      const type = obj.type;
      const payload = obj.payload || {};
      if (type === "session_meta") {
        if (payload.id && !t.sessionId) t.sessionId = payload.id;
        if (payload.model_provider) {
          // We don't have a single model id here, but the role chip falls
          // back to "codex" via termAssistantRoleLabel which is fine.
        }
        return;
      }
      if (type === "turn_context") {
        const m = payload.model;
        if (m && typeof m === "string") termSetPaneModel(t, m);
        return;
      }
      if (type === "response_item") {
        const role = payload.role;
        const kind = payload.type;
        if (kind === "message" && role === "assistant" && Array.isArray(payload.content)) {
          // Discrete assistant message — close any in-progress block, then
          // render the full text as a fresh assistant bubble.
          t.currentAssistant = null;
          const text = payload.content.map((c) => c.text || c.output_text || "").join("");
          if (text) termAppendAssistantText(t, text);
          return;
        }
        if (kind === "reasoning" && Array.isArray(payload.content)) {
          const block = termAssistantBlock(t);
          const txtEl = block.querySelector(".text");
          const det = document.createElement("details");
          det.className = "thinking-block";
          const sum = document.createElement("summary");
          const txt = payload.content.map((c) => c.text || "").join("\n");
          sum.textContent = `reasoning · ${txt.length} chars`;
          const pre = document.createElement("pre");
          pre.textContent = txt;
          det.appendChild(sum); det.appendChild(pre);
          txtEl.appendChild(det);
          return;
        }
        if (kind === "function_call") {
          const name = payload.name || "(tool)";
          let args = {};
          try { args = JSON.parse(payload.arguments || "{}"); } catch (e) { console.warn("[terminals] codex function_call args parse failed: " + (e && e.message ? e.message : e)); }
          // No synthetic fallback id: a function_call_output keys off the real
          // call_id/id only, so a synthetic id could never be matched back
          // (and two within the same ms could collide). Pass null → the pill
          // renders but isn't registered for result-matching it can't receive.
          const callId = payload.call_id || payload.id || null;
          termAddToolPill(t, callId, name, args);
          return;
        }
        if (kind === "function_call_output") {
          const callId = payload.call_id || payload.id;
          if (callId) termMarkToolResult(t, callId, false, payload.output || "");
          return;
        }
        // user / developer response_items are codex's own system context;
        // the operator already saw their prompt as a local user bubble.
        return;
      }
      if (type === "event_msg") {
        const sub = payload.type;
        if (sub === "agent_message_delta") {
          termAppendAssistantText(t, payload.delta || "");
          return;
        }
        if (sub === "task_started") {
          termSetActivity(t, "thinking…", "busy");
          return;
        }
        if (sub === "task_complete") {
          termSetActivity(t, "responding…", "busy");
          return;
        }
        return;
      }
    }

    // ----- composer: image paste/drop + @/  autocomplete -----

    // termCloseAutocomplete moved to pane-helpers.js (pure render leaf;
    // loaded before terminals.js, resolves as a global).
    //
    // termRenderAttachments + termPasteImage (+ _IMAGE_PASTE_MAX_BYTES) are
    // PURE leaves too, but they are PINNED to terminals.js by a static-lint
    // sanitization test (tests/test_dashboard_sanitization.py asserts the
    // image-mime allowlist regex and the termRenderAttachments/
    // _IMAGE_PASTE_MAX_BYTES ordering live in THIS file). Kept here so the
    // guard stays green; revisit when that test is taught the new location.
    function termRenderAttachments(t) {
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
        // a11y: chips are clickable to remove. role/tabindex/keydown so
        // keyboard-only users can reach + activate them.
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
        wireChipKeyboard(chip, () => { a.files.splice(i, 1); termRenderAttachments(t); });
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
        wireChipKeyboard(chip, () => { a.images.splice(i, 1); termRenderAttachments(t); });
        tray.appendChild(chip);
      });
    }

    var _IMAGE_PASTE_MAX_BYTES = 5 * 1024 * 1024;
    function termPasteImage(t, file) {
      // Cap pasted/dropped images so a misclick on a 50 MB phone photo
      // doesn't balloon into ~67 MB base64 in the composer (which then
      // round-trips as part of every turn). 5 MB raw is generous for
      // screenshots / dashboard captures.
      if (file && typeof file.size === "number" && file.size > _IMAGE_PASTE_MAX_BYTES) {
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
        termRenderAttachments(t);
      };
      reader.readAsDataURL(file);
    }

    function termOpenAutocomplete(t, items, onPick) {
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

    // Composer autocomplete: typing "/sk" hits /api/skills, typing
    // "@src/" hits /api/files/list. Previous impl fired one fetch per
    // keystroke — fast typists got 5-10 in flight, the latest fetch
    // could resolve BEFORE an earlier one (showing stale items in the
    // popup), and the server got hammered. We now:
    //   1) Debounce: at most one fetch every 120ms of typing,
    //   2) Race-protect: stamp each fetch with a sequence number per
    //      term object and ignore results that aren't the latest one.
    // Skills are also cached for 5s so consecutive "/" prefixes share
    // one network round-trip.
    var _SKILLS_CACHE = { at: 0, data: null };
    var _SKILLS_TTL_MS = 5000;

    function termScheduleComposerInput(t) {
      if (t._composerTimer) clearTimeout(t._composerTimer);
      t._composerTimer = setTimeout(() => {
        t._composerTimer = null;
        termHandleComposerInput(t);
      }, 120);
    }

    async function termHandleComposerInput(t) {
      const input = t.input;
      const val = input.value;
      const caret = input.selectionStart || val.length;
      // Token under caret starting with @ or /.
      const before = val.slice(0, caret);
      const m = before.match(/([@/])([^\s]*)$/);
      if (!m) { termCloseAutocomplete(t); return; }
      const trigger = m[1];
      const prefix = m[2];
      // Stamp this invocation. termOpenAutocomplete only applies the
      // result if the seq matches the latest (a fetch that finishes after
      // the user kept typing is discarded).
      t._composerSeq = (t._composerSeq || 0) + 1;
      const seq = t._composerSeq;
      const isLatest = () => t._composerSeq === seq && document.activeElement === input;
      if (trigger === "/") {
        try {
          let skills;
          if (_SKILLS_CACHE.data && (Date.now() - _SKILLS_CACHE.at) < _SKILLS_TTL_MS) {
            skills = _SKILLS_CACHE.data;
          } else {
            let r;
            try {
              r = await fetch("/api/skills", { cache: "no-store" });
            } catch (netErr) {
              // Network failure (offline, server restart, DNS): invalidate the
              // cache so the next attempt re-fetches instead of replaying a
              // previous successful response that may now be stale.
              _SKILLS_CACHE = { at: 0, data: null };
              return;
            }
            if (!r.ok) {
              // Same reasoning as the network-failure branch: a 4xx/5xx
              // signals the cached snapshot may no longer reflect what the
              // server would return today. Drop the cache so we re-query
              // the next time the operator hits "/".
              _SKILLS_CACHE = { at: 0, data: null };
              return;
            }
            const data = await r.json();
            skills = data.skills || [];
            // Surface debug visibility when the skill set diverges from
            // what we previously served — helps spot "why is /foo missing
            // from the popup?" when a SKILL.md was just added on disk.
            if (_SKILLS_CACHE.data) {
              const prevNames = (_SKILLS_CACHE.data || []).map((s) => s.name).sort().join(",");
              const nextNames = skills.map((s) => s.name).sort().join(",");
              if (prevNames !== nextNames) {
                try { console.debug("[terminals] /api/skills set changed since last cache hit"); } catch (_) {}
              }
            }
            _SKILLS_CACHE = { at: Date.now(), data: skills };
          }
          if (!isLatest()) return;
          const items = skills
            .filter((s) => s.name.toLowerCase().includes(prefix.toLowerCase()))
            .map((s) => ({ label: "/" + s.name, detail: s.description || "", pick: "/" + s.name }));
          termOpenAutocomplete(t, items, (it) => {
            // TOCTOU guard: between popup-open and the click, the operator
            // may have kept typing, moved the caret, or cleared the field.
            // Re-read the live textarea state and abort the splice if the
            // captured ``val``/``caret`` no longer matches reality —
            // otherwise we'd slice using stale offsets and corrupt whatever
            // the operator wrote in the meantime.
            const curVal = input.value;
            const curCaret = input.selectionStart || curVal.length;
            if (curVal !== val || curCaret !== caret) {
              termCloseAutocomplete(t);
              return;
            }
            const newVal = val.slice(0, caret - prefix.length - 1) + it.pick + val.slice(caret);
            input.value = newVal;
            input.focus();
            const pos = caret - prefix.length - 1 + it.pick.length;
            input.setSelectionRange(pos, pos);
          });
        } catch (e) { console.warn("[terminals] /api/skills autocomplete failed:", e); }
      } else {
        try {
          const r = await fetch("/api/files/list?prefix=" + encodeURIComponent(prefix), { cache: "no-store" });
          if (!r.ok) return;
          const data = await r.json();
          if (!isLatest()) return;
          const items = (data.files || []).map((f) => ({ label: "@" + f, detail: "", pick: f }));
          termOpenAutocomplete(t, items, (it) => {
            // Same TOCTOU guard as the /-skills branch — re-read the live
            // textarea before splicing so a fast typist who kept editing
            // after the popup opened doesn't get their text mangled.
            const curVal = input.value;
            const curCaret = input.selectionStart || curVal.length;
            if (curVal !== val || curCaret !== caret) {
              termCloseAutocomplete(t);
              return;
            }
            // Attach the file (don't paste path into the text). Remove the @prefix from the input.
            t.attached = t.attached || { images: [], files: [] };
            t.attached.files.push(it.pick);
            const newVal = val.slice(0, caret - prefix.length - 1) + val.slice(caret);
            input.value = newVal;
            input.focus();
            const pos = caret - prefix.length - 1;
            input.setSelectionRange(pos, pos);
            termRenderAttachments(t);
          });
        } catch (e) { console.warn("[terminals] /api/files/list autocomplete failed:", e); }
      }
    }

    // ----- chat rendering (stream-json -> structured DOM) -----

    // termFormatCostCompact / termFormatCost / _termRefreshCostNow /
    // termRefreshCost (+ _COST_REFRESH_DEBOUNCE_MS) moved to pane-helpers.js
    // (pure render leaves; loaded before terminals.js, resolve as globals).

    function termAutoScroll(t) {
      // Honour the "follow bottom" flag set by user scroll behaviour.
      // First call after open uses smooth scroll so the catch-up content
      // slides into view; subsequent calls snap (lower latency for live
      // streaming text).
      if (!t.autoFollowBottom) return;
      if (t._markProgrammaticScroll) t._markProgrammaticScroll();
      if (t.firstScroll) {
        t.firstScroll = false;
        // Defer to next frame so the freshly-appended DOM has been laid out.
        requestAnimationFrame(() => {
          try {
            t.body.scrollTo({ top: t.body.scrollHeight, behavior: "smooth" });
          } catch (_) {
            t.body.scrollTop = t.body.scrollHeight;
          }
        });
        return;
      }
      t.body.scrollTop = t.body.scrollHeight;
    }

    function termHandleChatChunk(t, chunk) {
      // Array buffer pattern: push the delta, then join+split once per
      // chunk rather than concatenating strings on every delta. Keeps the
      // trailing partial line as the only buffer entry for the next call.
      if (!Array.isArray(t.jsonBuf)) t.jsonBuf = t.jsonBuf ? [t.jsonBuf] : [];
      t.jsonBuf.push(chunk);
      const joined = t.jsonBuf.join("");
      const lastNl = joined.lastIndexOf("\n");
      if (lastNl === -1) { termAutoScroll(t); return; }
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
          // A line that opens with `{` but didn't parse is almost certainly
          // a stream-json object whose tail landed in the NEXT delta — the
          // server emits one JSON object per newline-terminated line so a
          // partial `{...` here will be completed by the next chunk. Carry
          // it forward instead of dumping it through termRenderRaw (which
          // would create a phantom system message and the real completion
          // would later parse standalone).
          if (trimmed.startsWith("{") && !trimmed.endsWith("}") && i === lines.length - 1) {
            carry.push(line);
            continue;
          }
          termRenderRaw(t, line);
          continue;
        }
        termRenderJsonObject(t, obj);
      }
      if (carry.length) {
        t.jsonBuf = (t.jsonBuf || []).concat(carry);
      }
      termAutoScroll(t);
    }

    // termRenderRaw (+ RAW_NOISE_PATTERNS) moved to pane-helpers.js (pure
    // render leaf; loaded before terminals.js, resolves as a global).

    function termRenderUserMessage(t, text) {
      const msg = document.createElement("div");
      msg.className = "msg user";
      msg.innerHTML = `<div class="role">user</div><div class="text"></div>`;
      msg.querySelector(".text").textContent = text;
      t.body.appendChild(msg);
      // After a user turn, prepare for a fresh assistant block on the next
      // assistant event.
      t.currentAssistant = null;
    }

    // Convert a model id into a human-friendly label for the role chip:
    //   claude-sonnet-4-6              -> CLAUDE SONNET 4.6
    //   claude-opus-4-7                -> CLAUDE OPUS 4.7
    //   claude-haiku-4-5-20251001      -> CLAUDE HAIKU 4.5  (drops YYYYMMDD)
    //   o4-mini                        -> O4 MINI
    //   gpt-5                          -> GPT 5
    // The tooltip on the role chip carries the unmodified id so power users
    // can still read the exact version.
    function termFormatModel(model) {
      if (!model) return "";
      return String(model)
        .replace(/-\d{8}$/, "")
        .replace(/-(\d+)-(\d+)(?=$|-)/, " $1.$2")
        .replace(/-/g, " ")
        .toUpperCase();
    }

    // Resolve the best label for an assistant role chip, given what we know
    // about the pane: explicit model wins; otherwise fall back to the tool
    // identity ("claude" / "codex") implied by the job kind; otherwise the
    // generic "assistant".
    function termAssistantRoleLabel(t) {
      if (t.model) return termFormatModel(t.model);
      if (t.kind === "chat") return "claude";
      if (t.kind === "chat-codex") return "codex";
      return "assistant";
    }

    // Record a model id for this pane and retro-update any assistant role
    // chips that were created before the model was known (chat-mode panes
    // create the block on the first text_delta, but stream-json's `init`
    // frame arrives just before that — they race).
    function termSetPaneModel(t, model) {
      if (!model || t.model === model) return;
      t.model = model;
      const label = termFormatModel(model);
      const title = "model: " + model;
      t.body.querySelectorAll(".msg.assistant:not(.thinking-placeholder) .role")
        .forEach((r) => {
          // Skip chips that were locked by another caller (e.g. the dispatch
          // tracker pane renames its role to "dispatch result").
          if (r.dataset.roleLocked === "1") return;
          r.textContent = label;
          r.title = title;
        });
    }

    // Show an animated "thinking" bubble while we wait for the first
    // assistant event. Replaced in-place as soon as text/tool_use starts
    // streaming (see termAssistantBlock and termRenderResult below).
    function termShowThinkingPlaceholder(t) {
      if (!t || !t.body) return;
      if (t.kind !== "chat") return;
      termClearThinkingPlaceholder(t);  // de-dupe
      const msg = document.createElement("div");
      msg.className = "msg assistant thinking-placeholder";
      msg.innerHTML = `<div class="role">thinking</div>`
        + `<div class="thinking-dots" aria-label="generating response">`
        + `<span class="dot"></span><span class="dot"></span><span class="dot"></span>`
        + `</div>`;
      t.body.appendChild(msg);
      termSetActivity(t, "thinking…", "busy");
      termAutoScroll(t);
    }

    // termClearThinkingPlaceholder moved to pane-helpers.js (pure render
    // leaf; loaded before terminals.js, resolves as a global).

    function termAssistantBlock(t) {
      if (t.currentAssistant && t.currentAssistant.isConnected) return t.currentAssistant;
      // First real content for this turn — drop the thinking placeholder.
      termClearThinkingPlaceholder(t);
      const msg = document.createElement("div");
      msg.className = "msg assistant";
      const label = termAssistantRoleLabel(t);
      const titleAttr = t.model ? ` title="model: ${escape(t.model)}"` : "";
      msg.innerHTML = `<div class="role"${titleAttr}>${escape(label)}</div><div class="text"></div>`;
      t.body.appendChild(msg);
      t.currentAssistant = msg;
      return msg;
    }

    function termAppendAssistantText(t, text) {
      if (!text) return;
      const block = termAssistantBlock(t);
      const textEl = block.querySelector(".text");
      // Accumulate raw deltas in an internal array buffer rather than
      // re-reading/writing dataset.raw on every chunk. The dataset write
      // incurs a DOM-attribute round-trip and the string concat is O(n²)
      // across many small deltas. We materialise the joined string once
      // per rAF flush (when we re-render markdown anyway) and mirror it
      // to dataset.raw there so external readers (see termRenderJsonObject
      // dedupe check) still observe the latest text.
      if (!Array.isArray(textEl._rawBuf)) {
        textEl._rawBuf = textEl.dataset.raw ? [textEl.dataset.raw] : [];
      }
      textEl._rawBuf.push(text);
      // Streaming responses arrive as MANY small deltas (a typical 5KB
      // answer can be 50+ chunks of 100 chars). Re-parsing the full
      // accumulated markdown on EVERY delta is O(N²) total work and
      // visibly janks the pane on long answers. Coalesce repaints onto
      // animation frames: at most one parse per frame regardless of how
      // many deltas land in between. The final result is identical; only
      // the intermediate frames are skipped.
      if (textEl._renderPending) {
        termSetActivity(t, "responding…", "busy");
        return;
      }
      textEl._renderPending = true;
      requestAnimationFrame(() => {
        textEl._renderPending = false;
        // The pane (and therefore this textEl) may have been removed from
        // the DOM between the delta arriving and the rAF callback firing
        // — operator clicked close, termCloseAllFinished swept it, the
        // chat-codex rekey replaced the block, etc. Writing into a
        // detached node leaks the dataset payload + queued buffer for
        // GC's lifetime and pointlessly re-parses markdown. Bail out
        // cleanly when the node is no longer connected.
        if (!textEl.isConnected) {
          textEl._rawBuf = [];
          return;
        }
        const latest = (textEl._rawBuf || []).join("");
        textEl.dataset.raw = latest;
        try { textEl.innerHTML = DOMPurify.sanitize(marked.parse(latest)); }
        catch (_) { textEl.textContent = latest; }
      });
      termSetActivity(t, "responding…", "busy");
    }

    function termAddToolPill(t, toolUseId, name, input) {
      termSetActivity(t, "tool: " + (name || "?"), "busy");
      // Some tools deserve inline rich rendering instead of a collapsed pill.
      if (name === "TodoWrite") return termRenderTodoWrite(t, toolUseId, input);

      const block = termAssistantBlock(t);
      const textEl = block.querySelector(".text");
      const wrap = document.createElement("div");
      const pill = document.createElement("span");
      pill.className = "tool-pill";
      // a11y: pill is a clickable disclosure trigger. role=button +
      // tabindex makes it keyboard-reachable; keydown handles Enter/Space
      // since native <button> would inherit submit-form semantics that
      // we don't want here.
      pill.setAttribute("role", "button");
      pill.setAttribute("tabindex", "0");
      pill.setAttribute("aria-expanded", "false");
      const argSummary = termSummariseToolInput(input);
      pill.textContent = name + (argSummary ? "  " + argSummary : "");

      // Pick a tool-specific inline renderer so file edits look like a
      // proper diff view, not a JSON dump.
      let detail;
      if (name === "Edit" && typeof input?.old_string === "string" && typeof input?.new_string === "string") {
        detail = renderEditDiff(input.file_path, input.old_string, input.new_string);
      } else if (name === "Write" && typeof input?.content === "string") {
        detail = renderNewFile(input.file_path, input.content);
      } else if (name === "Read" && input?.file_path) {
        detail = renderReadIntent(input.file_path, input.offset, input.limit);
      } else if (name === "Bash" && typeof input?.command === "string") {
        detail = renderBashCommand(input.command, input.description);
      } else if (name === "Grep" && typeof input?.pattern === "string") {
        detail = renderGrep(input);
      } else if (name === "Glob" && typeof input?.pattern === "string") {
        detail = renderGlob(input);
      } else if ((name === "WebFetch" || name === "WebSearch") && (input?.url || input?.query)) {
        detail = renderWebTool(name, input);
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
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          togglePill();
        }
      });
      wrap.appendChild(pill);
      wrap.appendChild(detail);
      textEl.appendChild(wrap);
      // Only register interactive pills that carry a real id — an id-less call
      // (e.g. a codex function_call with no call_id) can never be matched by a
      // result, and registering under a falsy key would collide across calls.
      if (toolUseId) t.toolUseEls.set(toolUseId, { pill, detail });

    }

    // ----- Inline tool-detail renderers -----

    function renderEditDiff(filePath, oldStr, newStr) {
      const wrap = document.createElement("div");
      wrap.className = "tool-detail diff-view";
      if (filePath) {
        const h = document.createElement("div");
        h.className = "diff-header";
        h.textContent = filePath;
        wrap.appendChild(h);
      }
      for (const part of simpleLineDiff(oldStr || "", newStr || "")) {
        const line = document.createElement("div");
        line.className = "diff-line " + part.kind;
        const prefix = part.kind === "removed" ? "- " : part.kind === "added" ? "+ " : "  ";
        line.textContent = prefix + part.text;
        wrap.appendChild(line);
      }
      return wrap;
    }

    function renderNewFile(filePath, content) {
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

    function renderReadIntent(filePath, offset, limit) {
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

    // renderBashCommand moved to pane-helpers.js (pure render leaf; loaded
    // before terminals.js, resolves as a global).

    function renderGrep(input) {
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

    function renderGlob(input) {
      const wrap = document.createElement("div");
      wrap.className = "tool-detail bash-view";
      const h = document.createElement("div");
      h.className = "diff-header";
      const where = input.path ? " in " + input.path : "";
      h.textContent = "Glob: " + input.pattern + where;
      wrap.appendChild(h);
      return wrap;
    }

    function renderWebTool(name, input) {
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

    // Fallback for diffs above the LCS size cliff. Returns marker entries
    // the renderer can display without paying the O(n*m) DP cost.
    function _fallbackDiffStub(oldLines, newLines) {
      const n = oldLines.length, m = newLines.length;
      return [
        { kind: "common", text: "(diff too large to display inline; " + n + " old / " + m + " new lines)" },
        ...oldLines.map((ln) => ({ kind: "removed", text: ln })),
        ...newLines.map((ln) => ({ kind: "added",   text: ln })),
      ];
    }

    // LCS DP grid cliff. Above this cell count we bail out to
    // _fallbackDiffStub before allocating (n+1) Int32Arrays of size
    // (m+1) — browsers freeze noticeably above ~100k cells.
    var SIMPLE_LINE_DIFF_CELL_CAP = 100_000;

    // Line-level diff using LCS backtrace. Falls back to a stub for huge
    // edits to bound memory. The cliff is tightened to
    // oldLines.length * newLines.length > 100_000 (was 200_000) — at
    // 500×500 the LCS allocates (n+1) Int32Arrays of size (m+1), and
    // browsers freeze noticeably above ~100k cells.
    function simpleLineDiff(oldStr, newStr) {
      const oldLines = oldStr.split("\n");
      const newLines = newStr.split("\n");
      const n = oldLines.length, m = newLines.length;
      if (oldLines.length * newLines.length > SIMPLE_LINE_DIFF_CELL_CAP) {
        return _fallbackDiffStub(oldLines, newLines);
      }
      const a = oldLines, b = newLines;
      const dp = new Array(n + 1);
      for (let i = 0; i <= n; i++) dp[i] = new Int32Array(m + 1);
      for (let i = n - 1; i >= 0; i--) {
        for (let j = m - 1; j >= 0; j--) {
          dp[i][j] = a[i] === b[j] ? dp[i+1][j+1] + 1 : Math.max(dp[i+1][j], dp[i][j+1]);
        }
      }
      const out = [];
      let i = 0, j = 0;
      while (i < n && j < m) {
        if (a[i] === b[j]) { out.push({ kind: "common",  text: a[i] }); i++; j++; }
        else if (dp[i+1][j] >= dp[i][j+1]) { out.push({ kind: "removed", text: a[i] }); i++; }
        else { out.push({ kind: "added",   text: b[j] }); j++; }
      }
      while (i < n) out.push({ kind: "removed", text: a[i++] });
      while (j < m) out.push({ kind: "added",   text: b[j++] });
      return out;
    }

    // Diff algorithm correctness is covered by tests/test_terminals_fixes.py
    // and the inline diff renderer's behaviour in the dashboard. The original
    // DOMContentLoaded self-test logged on every page load (production noise)
    // so it was removed; gate any future probe behind `window.DEBUG_DIFF_SELFTEST`.

    function termRenderTodoWrite(t, toolUseId, input) {
      const block = termAssistantBlock(t);
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
        // While a task is in progress show its activeForm; otherwise show
        // the imperative content. Falls back gracefully on either field.
        const label = (status === "in_progress" && todo?.activeForm) ? todo.activeForm : (todo?.content ?? todo?.activeForm ?? "(unnamed)");
        // Wrap the label in its own span so that line-through on
        // completed items only crosses the text — not the status icon.
        const labelEl = document.createElement("span");
        labelEl.className = "todo-label";
        labelEl.textContent = label;
        li.appendChild(labelEl);
        ul.appendChild(li);
      }
      wrap.appendChild(ul);
      textEl.appendChild(wrap);
      // Still register so a tool_result event can mark it succeeded/failed.
      t.toolUseEls.set(toolUseId, { pill: wrap, detail: null });
    }

    function termSummariseToolInput(input) {
      if (!input || typeof input !== "object") return "";
      const keys = Object.keys(input);
      if (!keys.length) return "";
      // Prefer a recognised summary key.
      const candidate = ["command", "file_path", "path", "pattern", "url", "query"]
        .find((k) => typeof input[k] === "string" && input[k]);
      if (candidate) {
        const v = String(input[candidate]);
        return v.length > 60 ? v.slice(0, 57) + "…" : v;
      }
      return "(" + keys.slice(0, 3).join(", ") + (keys.length > 3 ? "…" : "") + ")";
    }

    function termMarkToolResult(t, toolUseId, isError, content) {
      const entry = t.toolUseEls.get(toolUseId);
      if (entry) {
        entry.pill.classList.add(isError ? "error" : "result");
        // Inline-rendered tools (like TodoWrite) don't have a detail panel
        // to dump raw JSON into - the rich widget already shows the state.
        if (entry.detail) {
          const result = "\n--- result ---\n" + (typeof content === "string" ? content : JSON.stringify(content, null, 2));
          entry.detail.textContent += result;
        }
      }
    }

    function termRenderSystem(t, obj) {
      const sub = obj.subtype || obj.type;
      const div = document.createElement("div");
      div.className = "msg system";
      div.textContent = `[${obj.type}${sub && sub !== obj.type ? ":" + sub : ""}]`;
      // Don't show every system frame — only init / shutdown / errors.
      if (sub === "init" || sub === "shutdown" || /error/i.test(String(sub))) {
        t.body.appendChild(div);
      }
    }

    function termRenderResult(t, obj) {
      // Result frames close out a turn — drop any lingering thinking
      // placeholder (e.g. when the turn finishes without any assistant
      // text, the placeholder would otherwise persist forever).
      termClearThinkingPlaceholder(t);
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
      // Reflect the actual outcome instead of hardcoding "done". Result
      // frames CAN carry ``error_max_turns`` / ``error_during_execution`` /
      // ``interrupted`` — painting them as "[done]" lied to the operator.
      const label = isError ? (subtype || "error") : "done";
      div.textContent = `[${label}${meta ? "  " + meta : ""}]`;
      if (isError) div.style.color = "var(--bad)";
      t.body.appendChild(div);
      t.currentAssistant = null;  // next assistant goes in a fresh block
      termRefreshCost(t);          // bring the header pill up to date
      // A turn just consumed quota — nudge the topbar usage bars to
      // refresh. The schedule helper coalesces bursts (multiple panes
      // finishing simultaneously → one fetch) and enforces a cooldown so
      // we don't hammer /api/usage/total.
      try { window.scheduleTokenUsageRefresh?.(); } catch (_) {}
      // Turn finished — the pane is now idle and the operator can take
      // the next turn. Warn-colored chip makes it easy to scan the list
      // for "what wants my attention". On error we mark the activity as
      // ended (red) so the chip matches the body line.
      termSetActivity(t, isError ? label : "waiting", isError ? "ended" : "waiting");
      // Notify the operator if the tab is in the background.
      termNotifyTurnComplete(t, meta);
    }

    // Browser-notification on turn complete, when this tab isn't focused.
    // We ask permission lazily on the first notification opportunity per
    // session - never pop up a permission dialog out of nowhere.
    var _notifyPermAsked = false;
    function termNotifyTurnComplete(t, metaStr) {
      if (typeof Notification === "undefined") return;
      if (document.visibilityState === "visible" && document.hasFocus()) return;
      const fire = () => {
        try {
          const title = (t.task || "Chat").slice(0, 80);
          const body = "Turn finished" + (metaStr ? "  ·  " + metaStr : "");
          const n = new Notification(title, { body, tag: "term-" + t.jobId, silent: false });
          n.onclick = () => { window.focus(); try { t.pane.scrollIntoView({behavior:"smooth"}); } catch (_) {} n.close(); };
          setTimeout(() => { try { n.close(); } catch (_) {} }, 8000);
        } catch (_) { /* notifications can throw in some browsers */ }
      };
      if (Notification.permission === "granted") return fire();
      if (Notification.permission === "denied") return;
      if (_notifyPermAsked) return;
      _notifyPermAsked = true;
      Notification.requestPermission().then((p) => { if (p === "granted") fire(); }).catch(() => {});
    }

    // Transcript-format meta records that the IDE writes for plumbing
    // (hooks, queue, file backups). They are noise from the operator's POV.
    var TRANSCRIPT_META_NOISE = new Set([
      "attachment",
      "queue-operation",
      "file-history-snapshot",
      "summary",
      "compaction",
      "last-prompt",   // duplicate of the latest user message
    ]);

    function termRenderJsonObject(t, obj) {
      if (!obj || typeof obj !== "object") return;
      const type = obj.type;

      // Silence transcript-format meta noise (hooks, queue ops, snapshots).
      if (TRANSCRIPT_META_NOISE.has(type)) return;

      // Capture the model identifier early so the assistant role chip can
      // render with the real model name (e.g. "CLAUDE SONNET 4.6") instead
      // of the generic "assistant". stream-json carries it on the `init`
      // frame and on every `assistant` message; transcripts only on the
      // assistant record. First one wins, but later updates retro-apply.
      const declaredModel = obj.model || (obj.message && obj.message.model);
      if (declaredModel) termSetPaneModel(t, declaredModel);

      // Transcript-format ai-title: rename the pane.
      // Scope the selector to ``.term-head .task`` (NOT a bare ``.task``):
      // tool results / markdown rendered into the body can introduce
      // nested ``.task`` elements, and an unscoped query would grab the
      // FIRST one and rename whatever-the-first-match-happens-to-be
      // instead of the header title. Always pin the lookup to the head row.
      if (type === "ai-title" && typeof obj.aiTitle === "string") {
        const head = t.pane.querySelector(".term-head .task");
        if (head) head.textContent = obj.aiTitle;
        return;
      }

      if (type === "system") return termRenderSystem(t, obj);
      if (type === "result") return termRenderResult(t, obj);

      if (type === "assistant" && obj.message) {
        const content = obj.message.content;
        if (Array.isArray(content)) {
          // The final assistant message arrives AFTER the stream_event deltas
          // that already painted the same text/tool_use into the current
          // block. Re-appending duplicates the answer ("Hi!Hi!" syndrome) and
          // re-renders the same tool pills twice. Dedupe by checking what we
          // already have in the live block.
          for (const blk of content) {
            if (blk.type === "text" && typeof blk.text === "string") {
              const cur = t.currentAssistant;
              const accSoFar = cur ? (cur.querySelector(".text").dataset.raw || "") : "";
              // If deltas already streamed (any) text into this block, the
              // final text is a copy — skip. If the block is empty, this IS
              // the first text we've seen (e.g. transcript replay where no
              // deltas exist) — append normally.
              if (!accSoFar) termAppendAssistantText(t, blk.text);
            } else if (blk.type === "tool_use") {
              // stream_event/content_block_start may have already created the
              // pill; don't duplicate it here.
              if (!t.toolUseEls.has(blk.id)) {
                termAddToolPill(t, blk.id, blk.name, blk.input);
              }
            } else if (blk.type === "thinking" && typeof blk.thinking === "string") {
              const block = termAssistantBlock(t);
              const t2 = block.querySelector(".text");
              // Render thinking as a collapsed <details> so long internal
              // monologues don't drown the actual answer. Click summary to
              // expand. The char-count gives a sense of how much thinking
              // happened without forcing the user to read all of it.
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
          // Transcript shape: assistant message as a plain string.
          t.currentAssistant = null;
          termAppendAssistantText(t, content);
        }
        // Safety net for streams that emit a complete `assistant` frame
        // without the closing `result` (e.g. transcript-style records fed
        // through chat mode). Without this the chip gets stuck reading
        // "responding…" forever even though the model is idle.
        termSetActivity(t, "waiting", "waiting");
        return;
      }

      if (type === "user" && obj.message) {
        const content = obj.message.content;
        if (typeof content === "string") {
          // Strip system/loader wrappers; if nothing real is left, skip.
          const cleaned = termCleanUserPrompt(content);
          if (cleaned) termRenderUserMessage(t, cleaned);
        } else if (Array.isArray(content)) {
          for (const blk of content) {
            if (blk.type === "tool_result") {
              termMarkToolResult(t, blk.tool_use_id, !!blk.is_error, blk.content);
            } else if (blk.type === "text" && typeof blk.text === "string") {
              const cleaned = termCleanUserPrompt(blk.text);
              if (cleaned) termRenderUserMessage(t, cleaned);
            }
          }
        }
        return;
      }

      if (type === "stream_event") {
        // Partial deltas - extract text and append to the current assistant block.
        const ev = obj.event || {};
        if (ev.type === "content_block_delta" && ev.delta && ev.delta.type === "text_delta") {
          termAppendAssistantText(t, ev.delta.text || "");
        } else if (ev.type === "content_block_start" && ev.content_block) {
          const cb = ev.content_block;
          if (cb.type === "tool_use") {
            termAddToolPill(t, cb.id, cb.name, cb.input || {});
          }
        }
        return;
      }

      // Genuinely unknown — dump as a small dim line so we notice it but
      // it doesn't dominate the pane.
      const pre = document.createElement("pre");
      pre.style.color = "var(--text-dim)";
      pre.style.fontSize = "11px";
      pre.style.margin = "4px 0";
      pre.textContent = "[unhandled " + (type || "?") + "]";
      t.body.appendChild(pre);
    }

    // ----- Unified session panes -----
    // A session pane connects directly to the /api/sessions/<sid>/stream SSE
    // endpoint and renders a live, writable conversation. The composer is
    // ALWAYS enabled — the whole point of the unified session API is that the
    // operator can type at any time regardless of which process "owns" the
    // session (mirror / acquiring / engine states from the backend).
    //
    // SessionEvent schema (one JSON object per SSE data: frame):
    //   { seq, kind, role, text, partial, state }
    //   kind values: "message" | "tool_use" | "tool_result" | "system" | "state_change"
    //   The FIRST frame is always a state_change carrying the current state.
    //   "message" frames carry role ("user" | "assistant" | "system") + text.

    // Update the session-state chip in the pane header.
    // State strings from the backend: "mirror" | "acquiring" | "engine".
    // Map them to English labels and pill CSS state classes so the operator
    // can see at a glance who is driving the session.
    function termSessionChipUpdate(t) {
      const pill = t.pane && t.pane.querySelector(".status-pill");
      if (!pill) return;
      const state = t.state || "mirror";
      let label, pillCls, activityCls;
      if (state === "mirror") {
        label = "mirror"; pillCls = "done"; activityCls = "ready";
        termSetActivity(t, "idle", "ready");
      } else if (state === "acquiring") {
        label = "acquiring…"; pillCls = "running"; activityCls = "busy";
        termSetActivity(t, "acquiring…", "busy");
      } else if (state === "engine") {
        label = "live"; pillCls = "running"; activityCls = "busy";
        termSetActivity(t, "live", "busy");
      } else if (state === "foreign") {
        // Session is driven by an external agent — show as busy/warn so
        // the operator knows they are not the active driver.
        label = "external"; pillCls = "warn"; activityCls = "busy";
        termSetActivity(t, "external", "busy");
      } else {
        // Unknown future state — show it as-is with a neutral style.
        label = state; pillCls = "warn"; activityCls = "ready";
        termSetActivity(t, state, "ready");
      }
      // When a turn is queued (t.pending true) append a visible suffix so
      // the operator knows a message is waiting to be processed.
      if (t.pending) label = label + " · queued";
      termSetPillState(pill, pillCls, label);
    }

    // Handle one parsed SessionEvent object from the SSE stream.
    // Reuses the existing chat-pane rendering helpers so session bubbles
    // look identical to regular chat turns.
    function termHandleSessionEvent(t, ev) {
      if (!ev || typeof ev !== "object") return;
      const kind = ev.kind;
      if (kind === "state_change") {
        // Store current backend state and refresh the header chip.
        // Do NOT render as a bubble — state transitions are metadata,
        // not conversation content.
        t.state = ev.state || t.state;
        t.pending = !!ev.pending;
        termSessionChipUpdate(t);
        return;
      }
      if (kind === "warning") {
        // Surface server-side warnings as inline system notices so the
        // operator sees them without leaving the pane.
        const warn = document.createElement("div");
        warn.className = "msg system";
        warn.style.color = "var(--warn, #e6a817)";
        warn.textContent = "[warning] " + (ev.text || "");
        t.body.appendChild(warn);
        termAutoScroll(t);
        return;
      }
      if (kind === "message") {
        const role = ev.role || "system";
        const text = ev.text || "";
        if (role === "user") {
          termRenderUserMessage(t, text);
        } else if (role === "assistant") {
          // Partial frames arrive during streaming; accumulate them into
          // the current assistant block exactly like the chat-pane path.
          termAppendAssistantText(t, text);
          if (!ev.partial) {
            // Turn boundary: next assistant frame starts a fresh block.
            t.currentAssistant = null;
          }
        } else {
          // system / unknown roles get a neutral system note.
          const note = document.createElement("div");
          note.className = "msg system";
          note.textContent = text;
          t.body.appendChild(note);
        }
        termAutoScroll(t);
        return;
      }
      if (kind === "tool_use") {
        // Render a collapsible tool pill via the shared helper.
        // ev.id / ev.name / ev.input mirror the transcript record shape.
        termAddToolPill(t, ev.id || "", ev.name || "tool", ev.input || {});
        termAutoScroll(t);
        return;
      }
      if (kind === "tool_result") {
        // Mark the matching pill done/error.
        termMarkToolResult(t, ev.tool_use_id || ev.id || "", !!ev.is_error, ev.content || ev.output || "");
        termAutoScroll(t);
        return;
      }
      if (kind === "system") {
        const note = document.createElement("div");
        note.className = "msg system";
        note.textContent = ev.text || "";
        t.body.appendChild(note);
        termAutoScroll(t);
        return;
      }
      // Unknown event kinds: silently ignore to stay forward-compatible.
    }

    // Stable per-tab id sent as the `owner` of session turns so the backend
    // registry can tell turns from different browser tabs apart. sessionStorage
    // is per-tab and survives reloads, which is exactly the lifetime we want.
    // Memoized so every send in this tab reuses the same id.
    function termClientId() {
      if (termClientId._id) return termClientId._id;
      let id = null;
      try { id = sessionStorage.getItem("dash.sessionOwnerId"); } catch (_) {}
      if (!id) {
        id = (window.crypto && crypto.randomUUID)
          ? crypto.randomUUID()
          // Fallback for very old browsers without crypto.randomUUID. This is
          // only a tab discriminator, not a security token, so Math.random is
          // fine here.
          : ("xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
              const r = Math.random() * 16 | 0;
              const v = c === "x" ? r : (r & 0x3 | 0x8);
              return v.toString(16);
            }));
        try { sessionStorage.setItem("dash.sessionOwnerId", id); } catch (_) {}
      }
      termClientId._id = id;
      return id;
    }

    // Strip IDE/system wrapper blocks from a user message. If nothing
    // meaningful is left after stripping (i.e. the message was ONLY
    // wrappers), return null so the caller can skip rendering entirely.
    // Slash-command invocations get collapsed to "/name args" so the
    // pane shows what the operator typed instead of the whole expansion
    // envelope (and, for SessionStart hooks, the full skill body the
    // platform injects via EXTREMELY_IMPORTANT blocks).
    function termCleanUserPrompt(text) {
      if (!text) return null;
      let s = String(text);
      s = s.replace(/<system-reminder>[\s\S]*?<\/system-reminder>/g, "");
      // EXTREMELY_IMPORTANT / EXTREMELY-IMPORTANT blocks are injected by
      // SessionStart hooks (e.g. the superpowers skill loader) and carry
      // entire SKILL.md bodies — operator never typed them.
      s = s.replace(/<EXTREMELY[_-]IMPORTANT>[\s\S]*?<\/EXTREMELY[_-]IMPORTANT>/g, "");
      s = s.replace(/<ide_opened_file>[\s\S]*?<\/ide_opened_file>/g, "");
      s = s.replace(/<ide_selection>[\s\S]*?<\/ide_selection>/g, "");
      s = s.replace(/<task-notification>[\s\S]*?<\/task-notification>/g, "");
      s = s.replace(/<local-command-stdout>[\s\S]*?<\/local-command-stdout>/g, "");
      s = s.replace(/<local-command-stderr>[\s\S]*?<\/local-command-stderr>/g, "");
      // Slash-command envelope: collapse <command-name>/X</command-name>
      // + <command-args>Y</command-args> to "/X Y". The platform also wraps
      // the SKILL.md body in <command-source> / <command-stdout> /
      // <command-instructions> blocks alongside; drop those too so the
      // operator sees their command, not the loader output.
      const nameMatch = s.match(/<command-name>([^<]*)<\/command-name>/);
      const argsMatch = s.match(/<command-args>([\s\S]*?)<\/command-args>/);
      if (nameMatch) {
        const name = (nameMatch[1] || "").trim();
        const args = (argsMatch ? (argsMatch[1] || "") : "").trim();
        // Drop EVERY <command-*>...</command-*> block (loader envelope).
        s = s.replace(/<command-[\w-]+>[\s\S]*?<\/command-[\w-]+>/g, "");
        const compact = (name + (args ? " " + args : "")).trim();
        const rest = s.trim();
        return rest ? `${compact}\n\n${rest}` : (compact || null);
      }
      s = s.trim();
      return s || null;
    }

    // Suppression set for auto-open. Two semantics in one bag:
    //   1) "we've already auto-opened this id in this tab" — prevents
    //      the poll loop from flapping a pane open every few seconds;
    //   2) "the operator explicitly closed this id" — keeps the pane
    //      gone across F5, even if the underlying job/transcript is
    //      still active on the server.
    //
    // Persisted to localStorage so closed panes don't come back via the
    // next poll cycle (or after a hard reload). Capped at the most
    // recent 2000 entries to avoid unbounded growth in long-running
    // projects — pane ids are small (UUIDs / ide:<sid> / dispatch:<id>)
    // so 2000 fits comfortably under the localStorage budget.
    var AUTO_OPENED_KEY = "dash.suppressAutoOpen.v1";
    var AUTO_OPENED_MAX = 2000;
    var AUTO_OPENED_ONCE = new Set();
    (function loadSuppressAutoOpen() {
      try {
        const raw = localStorage.getItem(AUTO_OPENED_KEY);
        const arr = raw ? JSON.parse(raw) : [];
        if (Array.isArray(arr)) for (const id of arr) {
          if (typeof id === "string" && id) AUTO_OPENED_ONCE.add(id);
        }
      } catch (_) { /* corrupt / quota / private mode — start empty */ }
    })();

    var _suppressPersistTimer = null;
    function persistSuppressAutoOpen() {
      // Debounce so a close-finished sweep that touches 50 panes only
      // writes localStorage once.
      if (_suppressPersistTimer) return;
      _suppressPersistTimer = setTimeout(() => {
        _suppressPersistTimer = null;
        try {
          localStorage.setItem(AUTO_OPENED_KEY, JSON.stringify([...AUTO_OPENED_ONCE]));
        } catch (_) { /* quota — best-effort */ }
      }, 250);
    }

    // Move-to-front insert with cap. Re-adding an existing id bumps it
    // to the most-recent position so the cap evicts truly old ids
    // first, not the one we just touched.
    function suppressAutoOpen(id) {
      if (!id || typeof id !== "string") return;
      // Draft pane ids ("draft:<ts>:<n>") are local-only — the server
      // never sees them, so persisting them just wastes the cap.
      if (id.startsWith("draft:")) return;
      if (AUTO_OPENED_ONCE.has(id)) AUTO_OPENED_ONCE.delete(id);
      AUTO_OPENED_ONCE.add(id);
      while (AUTO_OPENED_ONCE.size > AUTO_OPENED_MAX) {
        const oldest = AUTO_OPENED_ONCE.values().next().value;
        AUTO_OPENED_ONCE.delete(oldest);
      }
      persistSuppressAutoOpen();
    }

    // Inverse of suppressAutoOpen — used by explicit user "open this"
    // paths (picker + Open-all-running) so a previously-closed pane
    // can come back if the operator asks for it.
    function unsuppressAutoOpen(id) {
      if (!id) return;
      if (AUTO_OPENED_ONCE.delete(id)) persistSuppressAutoOpen();
    }
    // Auto-open is GONE. The dashboard never pushes panes onto the canvas by
    // itself — not dashboard chat jobs, not external IDE transcripts. The
    // Terminals tab is a manual launcher + status list: the operator launches
    // what they want and opens each row on the canvas with the ⊞ control. The
    // suppressAutoOpen bookkeeping above is retained only so the launch paths
    // can mark freshly-minted ids without re-introducing any auto-open.

    // IDE transcript files touched within this window count as "live" for the
    // explicit "open all running" button below. (Auto-open no longer uses this —
    // the poll-driven transcript mirror was removed; see termAutoOpenActive.)
    var TRANSCRIPT_ACTIVE_WINDOW_MS = 5 * 60 * 1000;
    async function termOpenAllRunning() {
      // Combines two sources of "active chat":
      //   1. Dashboard chat jobs (running / queued / cancelling).
      //   2. IDE Claude Code transcripts whose JSONL was written-to in the
      //      last 5 minutes — those are live sessions running in your IDE.
      let opened = 0, already = 0, scanned = 0;
      try {
        const r = await fetch("/api/jobs", { cache: "no-store" });
        if (r.ok) {
          const data = await r.json();
          const all = data.jobs || [];
          scanned += all.length;
          const candidates = all.filter((j) =>
            (j.kind === "chat" || j.kind === "chat-codex") &&
            ["running", "queued", "cancelling"].includes(j.status)
          );
          for (const j of candidates) {
            const target = termJobCanvasTarget(j.id, j);
            if (TERMS.has(j.id) || TERMS.has(target.key) || _CANVAS_ON_KEYS.has(target.key)) { already++; continue; }
            termSendToCanvas(_statusRowTerm(target.kind, target.key));
            suppressAutoOpen(j.id);
            if (target.key !== j.id) suppressAutoOpen(target.key);
            if (j.kind === "chat" && j.session_id) suppressAutoOpen("ide:" + j.session_id);
            opened++;
          }
        }
      } catch (e) {
        setMsg("#term-msg", "err", "jobs: " + e.message, TERM_MSG_DURATION_MS);
      }
      try {
        const r = await fetch("/api/transcripts", { cache: "no-store" });
        if (r.ok) {
          const data = await r.json();
          const tx = data.transcripts || [];
          scanned += tx.length;
          const now = Date.now();
          for (const t of tx) {
            const sid = t.session_id;
            if (!sid) continue;
            // Claude transcripts open as session panes; key dedup + suppress
            // on the real pane key ("session:"+sid) and honour any legacy
            // "ide:"+sid bookkeeping so we never double-open.
            const sessionKey = "session:" + sid;
            const ideKey = "ide:" + sid;
            const mtime = t.modified ? Date.parse(t.modified) : 0;
            if (!Number.isFinite(mtime) || mtime <= 0) continue;
            if ((now - mtime) > TRANSCRIPT_ACTIVE_WINDOW_MS) continue;
            if (TERMS.has(sessionKey) || TERMS.has(ideKey) || _CANVAS_ON_KEYS.has(sessionKey)) { already++; continue; }
            suppressAutoOpen(sessionKey);
            termRouteSessionToCanvas(sid);
            opened++;
          }
        }
      } catch (e) {
        setMsg("#term-msg", "err", "transcripts: " + e.message, TERM_MSG_DURATION_MS);
      }
      if (!opened && !already) {
        setMsg("#term-msg", "warn", `nothing active (scanned ${scanned} job/transcript entr(ies))`, TERM_MSG_DURATION_MS);
        return;
      }
      const msg = opened
        ? `opened ${opened}${already ? `, ${already} already open` : ""}`
        : `${already} already open — nothing to do`;
      setMsg("#term-msg", opened ? "ok" : "warn", msg, TERM_MSG_DURATION_MS);
    }

    // Status-pill texts that mean "this pane is finished and can be
    // swept away by Close-finished". The button name promises to close
    // panes that look done — anchor on the visible label so the
    // criterion tracks whatever the operator actually reads on the pill
    // (DONE / ENDED / FAILED in the screenshot). "ready" is deliberately
    // NOT in this set: chat-codex between turns shows "ready" but the
    // operator may still send the next prompt, so the sweep leaves it
    // alone.
    var _FINISHED_PILL_TEXTS = new Set(["ended", "done", "failed"]);

    function termPaneIsFinished(t) {
      if (!t || !t.pane) return false;
      // Drafts have no "finished" state by design. Session panes keep an
      // always-on composer (the operator can resume an "ended" session at
      // any time), so the sweep leaves them alone too.
      if (t.isDraft) return false;
      if (t.kind === "session") return false;
      // Gold-standard signal: chat SSE ended / PTY exited explicitly
      // dropped the pane into dead state.
      if (t.pane.classList.contains("dead")) return true;
      // Visible-label fallback for kinds that never go dead (dispatch
      // trackers, chat-codex between turns). Pill text is exactly what
      // the operator scans when deciding "should this close?", so
      // anchoring on it keeps the button predictable.
      const pill = t.pane.querySelector(".status-pill");
      if (pill) {
        // Prefer dataset.pillText (mirrored by termSetPillState) over the
        // rendered textContent so i18n / "ending…" spinners don't desync
        // the sweep from the real state.
        const txt = (
          pill.dataset.pillText
          || (pill.textContent || "").trim().toLowerCase()
        );
        if (_FINISHED_PILL_TEXTS.has(txt)) return true;
      }
      return false;
    }

    function termCloseAllFinished() {
      let closed = 0;
      // Snapshot the keys before iterating — termClose() deletes the entry
      // for the closing pane from TERMS, and on some panes (chat-codex
      // rekeys, transcript companions) the close can cascade and remove
      // another entry. Iterating TERMS.entries() live can skip a sibling
      // entry mid-cascade; the snapshot decouples the iteration from
      // mutation and a stale jobId is just a no-op in termClose.
      for (const jobId of [...TERMS.keys()]) {
        const t = TERMS.get(jobId);
        if (!t) continue;
        if (termPaneIsFinished(t)) { termClose(jobId); closed++; }
      }
      if (!closed) {
        setMsg("#term-msg", "warn", "no finished panes to close", 3000);
      }
    }

    document.addEventListener("DOMContentLoaded", () => {
      // The Terminals tab is now a launcher + status list. "New terminal" opens
      // the launcher; the old "open a pane" picker and the inline-pane controls
      // (open-all / collapse-all / auto-open / close-all) were removed when
      // panes moved entirely into the canvas window.
      // Launcher controls now live in the toolbar; "New terminal" launches the
      // configured resource into the status list (open it on the canvas later).
      wireLauncherToolbar();
      $("#term-new")?.addEventListener("click", termOpenDraft);
      // Render the status rows from the current job/session state.
      termRenderStatusList();
      // Initial status-list fill (single unified source).
      termRefreshTranscriptPicker();
      // Restore panes that were open at the last unload. Fires once on
      // boot; rebuilds chat / PTY / IDE-transcript panes from
      // localStorage by re-fetching their server-side state. Drafts and
      // dispatch trackers are intentionally NOT restored.
      restoreOpenPanes();

      // Wire the dashboard-side CanvasBus client once. On load: clear any
      // ghost badges if the canvas window's heartbeat is stale, then say
      // `hello` so an already-open canvas re-announces its `ready` open set
      // (which repaints badges for panes mirrored there).
      var bus = canvasEnsureBus();
      if (bus) {
        canvasClearStaleBadges();
        bus.post({ type: "hello" });
      }
    });

