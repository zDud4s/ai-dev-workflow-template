// .ai/dashboard/app/terminals.js -- extracted from app.js (was lines 1471..3065)
// NOTE: top-level const/let were converted to var so identifiers cross <script> boundaries.

    // ----- terminals (multi-pane real-time view) -----
    // Each entry: { jobId, source, pane, body, input, sendBtn, status, task }
    var TERMS = new Map();

    // ----- pane persistence (survives F5) -----
    //
    // Each open pane (chat job, PTY, IDE transcript mirror) is logged to
    // localStorage with just enough metadata to re-attach on next page
    // load: pane key + kind + collapsed/pinned flags. Everything else is
    // re-fetched server-side on restore — the job/PTY itself is owned by
    // the server process and outlives the browser tab. Drafts (work-in-
    // progress, not yet POSTed) and dispatch trackers (re-spawned by
    // their parent stream) are intentionally excluded.
    // v2 folds Claude chat / IDE transcript panes into unified "session"
    // panes. The legacy v1 key is read once on restore and migrated forward
    // (see migrateOpenPanesV1ToV2), then deleted.
    var PERSIST_KEY = "dash.openPanes.v2";
    var LEGACY_PERSIST_KEY = "dash.openPanes.v1";
    var _persistTimer = null;

    // (Legacy IDE-transcript status-poll machinery removed — Claude
    // conversations are unified session panes that drive their own SSE
    // stream; there is no separate read-only transcript pane to poll.)
    function persistOpenPanes() {
      // Debounce so a burst of mutations (open + collapse + scroll) only
      // serialises once.
      if (_persistTimer) return;
      _persistTimer = setTimeout(() => {
        _persistTimer = null;
        const entries = [];
        for (const [id, t] of TERMS.entries()) {
          if (!t) continue;
          if (t.isDraft) continue;
          if (t.kind === "dispatch") continue;
          entries.push({
            id,
            kind: t.kind,
            collapsed: t.pane && t.pane.classList.contains("collapsed") || false,
            pinned: t.pane && t.pane.classList.contains("pinned") || false,
          });
        }
        // Persist per-PTY tokens alongside the pane list so a page refresh
        // can reattach without losing access. Tokens are same-origin secrets,
        // no weaker than any other localStorage entry the dashboard writes.
        const tokens = {};
        if (window._PTY_TOKENS) {
          for (const e of entries) {
            if (e.kind === "terminal" && window._PTY_TOKENS[e.id]) {
              tokens[e.id] = window._PTY_TOKENS[e.id];
            }
          }
        }
        try { localStorage.setItem(PERSIST_KEY, JSON.stringify({ panes: entries, tokens })); }
        catch (_) { /* quota exceeded? give up silently */ }
      }, 250);
    }

    // One-shot migration of the persisted open-panes store from v1 to v2.
    // v1 logged Claude conversations as either ``transcript`` (id "ide:"+sid)
    // or ``chat`` (id = JOB id) panes; v2 folds both into ``session`` panes
    // (id "session:"+sid). Returns the migrated saved object (or null).
    //
    //   - transcript: sid is in the key -> convert synchronously.
    //   - chat (Claude): the persisted id is the JOB id, not the sid; the sid
    //     is only recoverable via /api/jobs/<id>. Convert only if that fetch
    //     yields a session_id; if the job is gone (common right after the
    //     server restart that triggers migration), DROP the entry rather than
    //     promise reachability.
    //   - chat-codex, terminal: carried over unchanged.
    async function migrateOpenPanesV1ToV2() {
      let legacy;
      try {
        const raw = localStorage.getItem(LEGACY_PERSIST_KEY);
        legacy = raw ? JSON.parse(raw) : null;
      } catch (_) { return null; }
      if (!legacy || !Array.isArray(legacy.panes)) return null;
      // Legacy v1 pane kinds, kept as data so the converged code never has to
      // carry a live ``=== "transcript"``/``=== "chat"`` branch for them.
      const V1_TRANSCRIPT = "transcript";
      const V1_CHAT = "chat";
      const mapped = await Promise.all(legacy.panes.map(async (entry) => {
        if (!entry || !entry.id || !entry.kind) return null;
        const ek = entry.kind;
        if (ek === V1_TRANSCRIPT) {
          const sid = String(entry.id).replace(/^ide:/, "");
          if (!sid) return null;
          return { id: "session:" + sid, kind: "session", collapsed: !!entry.collapsed, pinned: !!entry.pinned };
        }
        if (ek === V1_CHAT) {
          // Recover the sid from the job record; drop if the job is gone.
          try {
            const r = await fetch("/api/jobs/" + encodeURIComponent(entry.id), { cache: "no-store" });
            if (!r.ok) return null;
            const meta = await r.json();
            if (!meta || !meta.session_id) return null;
            return { id: "session:" + meta.session_id, kind: "session", collapsed: !!entry.collapsed, pinned: !!entry.pinned };
          } catch (_) { return null; }
        }
        if (ek === "chat-codex" || ek === "terminal") {
          return { id: entry.id, kind: ek, collapsed: !!entry.collapsed, pinned: !!entry.pinned };
        }
        return null;
      }));
      const panes = mapped.filter(Boolean);
      const migrated = { panes, tokens: (legacy.tokens && typeof legacy.tokens === "object") ? legacy.tokens : {} };
      // Only drop the legacy key once the v2 write has actually succeeded —
      // otherwise a quota failure would lose BOTH keys (all persisted panes)
      // on the next reload, not just the intended sid-less chats.
      try {
        localStorage.setItem(PERSIST_KEY, JSON.stringify(migrated));
        try { localStorage.removeItem(LEGACY_PERSIST_KEY); } catch (_) { /* ignore */ }
      } catch (_) { /* quota: keep v1 so a later reload can retry the migration */ }
      return migrated;
    }

    async function restoreOpenPanes() {
      let saved;
      try {
        const raw = localStorage.getItem(PERSIST_KEY);
        saved = raw ? JSON.parse(raw) : null;
      } catch (_) { return; }
      // No v2 store yet: migrate a v1 store forward (best-effort) before restoring.
      if (!saved) {
        saved = await migrateOpenPanesV1ToV2();
      }
      if (!saved || !Array.isArray(saved.panes) || !saved.panes.length) return;
      // Rehydrate the per-PTY token cache from the previous session so
      // restored terminal panes can pass the WS auth check.
      if (saved.tokens && typeof saved.tokens === "object") {
        window._PTY_TOKENS = window._PTY_TOKENS || {};
        for (const id of Object.keys(saved.tokens)) {
          window._PTY_TOKENS[id] = saved.tokens[id];
        }
      }
      // Fetch every pane's metadata in parallel — sequential awaits made
      // boot scale linearly with the saved-pane count. Each fetch is
      // independent and the open+UI-state pass runs in saved order
      // afterwards so the visual layout is unchanged.
      const fetchers = saved.panes.map(async (entry) => {
        if (!entry || !entry.id || !entry.kind) return null;
        if (TERMS.has(entry.id)) return null;
        try {
          if (entry.kind === "session") {
            // sid is in the key — no metadata fetch needed. Legacy "ide:"
            // transcript ids are migrated to "session:" ids before restore,
            // so only the session prefix reaches here.
            const sid = String(entry.id).replace(/^session:/, "");
            if (!sid) return null;
            return { entry, ready: { kind: "session", sid } };
          }
          if (entry.kind === "terminal") {
            const r = await fetch("/api/ptys/" + encodeURIComponent(entry.id), { cache: "no-store" });
            if (!r.ok) return null;
            const meta = await r.json();
            if (meta.status && meta.status !== "running") return null;
            return { entry, ready: { kind: "terminal", meta } };
          }
          if (entry.kind === "chat" || entry.kind === "chat-codex") {
            const r = await fetch("/api/jobs/" + encodeURIComponent(entry.id), { cache: "no-store" });
            if (!r.ok) return null;
            const meta = await r.json();
            return { entry, ready: { kind: "chat", meta } };
          }
        } catch (_) { /* one pane failed; keep going */ }
        return null;
      });
      const results = await Promise.allSettled(fetchers);
      for (const res of results) {
        if (res.status !== "fulfilled" || !res.value) continue;
        const { entry, ready } = res.value;
        try {
          let paneKey = entry.id;
          if (ready.kind === "session") {
            // Open the unified session pane; suppress under the real pane key
            // so the auto-opener doesn't re-spawn it.
            termOpenSession(ready.sid);
            suppressAutoOpen("session:" + ready.sid);
            paneKey = "session:" + ready.sid;
          } else if (ready.kind === "terminal") {
            termOpenPty(entry.id, ready.meta, null);
          } else if (ready.kind === "chat") {
            termOpen(entry.id, ready.meta);
            suppressAutoOpen(entry.id);
          }
          const t = TERMS.get(paneKey);
          if (t && t.pane) {
            if (entry.pinned) {
              t.pane.classList.add("pinned");
              const btn = t.pane.querySelector(".pin-btn");
              if (btn) { btn.classList.add("active"); btn.textContent = "unpin"; }
            }
            if (entry.collapsed) {
              termSetCollapsed(t, true);
            }
          }
        } catch (_) { /* one pane failed; keep going */ }
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
      // See termOpenSession for the rationale.
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
      // Collapsed/expanded state is part of what we persist so the
      // next F5 restores the same layout the operator left.
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

    // ----- layout control -----
    // "list"  = vertical stack of status rows (collapsed by default).
    // "split" = exactly 2 columns side-by-side, panes expanded.
    // "grid"  = auto-fit multi-column grid, panes expanded.
    // Persisted in localStorage; read at open-time and on every switch.
    var TERM_LAYOUTS = ["list", "split", "grid"];
    function termGetLayout() {
      let v = null;
      try { v = localStorage.getItem("dash.termLayout"); } catch (_) { /* private mode */ }
      return TERM_LAYOUTS.includes(v) ? v : "list";
    }
    function termApplyLayout(mode) {
      const grid = $("#terms-grid");
      if (grid) {
        grid.classList.toggle("layout-split", mode === "split");
        grid.classList.toggle("layout-grid",  mode === "grid");
      }
      // Highlight the active icon button in the layout group.
      document.querySelectorAll(".term-layout-group .layout-btn").forEach((b) => {
        b.classList.toggle("active", b.dataset.layout === mode);
      });
    }
    function termSetLayout(mode) {
      const next = TERM_LAYOUTS.includes(mode) ? mode : "list";
      try { localStorage.setItem("dash.termLayout", next); } catch (_) { /* private mode */ }
      termApplyLayout(next);
      // Whatever the new layout is, collapse every pane to a clean
      // status-row baseline. This avoids every flavour of the "phantom
      // empty body" bug: panes opened expanded by termOpen* in split
      // mode, the grid-stretch issue, content-detection false positives,
      // and stale state from the previous layout. The operator then
      // clicks the panes they actually want to see — explicit and
      // predictable. Drafts are skipped (no expand button means there's
      // no way back). Silent flag stops scrollIntoView from racing.
      for (const t of TERMS.values()) {
        if (t.isDraft) continue;
        termSetCollapsed(t, true, { silent: true });
      }
      // xterm.js panes compute their (cols, rows) from the body's
      // pixel size. Switching layout changes the grid template, which
      // in turn changes pane widths — fit() catches the cases where
      // the ResizeObserver coalesces or fires before the new layout
      // has settled. Defer two frames so the new grid template + any
      // collapse-class changes have both applied.
      requestAnimationFrame(() => requestAnimationFrame(() => {
        for (const t of TERMS.values()) {
          if (t.kind === "terminal" && t._fitAddon && !t.pane.classList.contains("collapsed")) {
            try { t._fitAddon.fit(); } catch (_) {}
          }
        }
      }));
    }

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
      const sel = $("#term-picker");
      if (!sel) return;
      // If the operator is mid-pick (dropdown open) we must NOT replace
      // innerHTML — that closes the dropdown under their cursor. Postpone
      // and retry on the next poll.
      if (document.activeElement === sel) return;
      const prev = sel.value;
      const openKeys = new Set(TERMS.keys());

      // Sessions: IDE + dashboard Claude chats, unified from /api/sessions
      // (deduped by sid, each annotated with its baton state). This single
      // group replaces the old "IDE chats" group AND the chat-kind dashboard
      // jobs — every Claude conversation opens as one writable session pane.
      let sessions = [];
      let totalSessions = 0;
      try {
        const r = await fetch("/api/sessions", { cache: "no-store" });
        if (r.ok) {
          const data = await r.json();
          // Sessions are Claude conversations only. Codex (chat-codex) is NOT a
          // claude --resume session; exclude it here so it stays in the Jobs
          // group and clicking it never spins up a Claude pane on a codex id.
          const all = (data.sessions || []).filter(
            (s) => s.kind !== "chat-codex" && !openKeys.has("session:" + s.sid));
          totalSessions = all.length;
          sessions = all.slice(0, PICKER_MAX_PER_GROUP);
        }
      } catch (_) { /* ignore — picker still works for jobs */ }

      // Jobs spawned by the dashboard that are NOT Claude chats: orchestrate /
      // plan / codex. Claude chats (kind === "chat") are sessions now and live
      // in the Sessions group above, so they are excluded here.
      const nonChatJobs = (jobs || []).filter((j) => j.kind !== "chat" && !openKeys.has(j.id));
      const jobChoices = nonChatJobs.slice(0, PICKER_MAX_PER_GROUP);
      const totalJobs = nonChatJobs.length;

      if (!sessions.length && !jobChoices.length) {
        sel.innerHTML = `<option value="">— nothing to open —</option>`;
        sel.disabled = true;
        const termOpenBtn = $("#term-open");
        if (termOpenBtn) termOpenBtn.disabled = true;
        return;
      }
      const parts = [];
      if (sessions.length) {
        // Label surfaces truncation ("N of M newest") so the cap is never silent.
        const label = totalSessions > sessions.length
          ? `Sessions (${sessions.length} of ${totalSessions} newest)`
          : "Sessions";
        parts.push(`<optgroup label="${escape(label)}">` + sessions.map((s) => {
          const sid = s.sid;
          const state = s.state || "mirror";
          const title = (s.title || s.task || "").replace(/\s+/g, " ").slice(0, 50);
          const when = (s.modified || s.started_at || "").slice(11, 16);
          const shown = title || (sid.slice(0, 8) + "…");
          return `<option value="session:${escape(sid)}">[${escape(state)}] ${escape(shown)}${when ? " (" + escape(when) + ")" : ""}</option>`;
        }).join("") + `</optgroup>`);
      }
      if (jobChoices.length) {
        const label = totalJobs > jobChoices.length
          ? `Jobs (${jobChoices.length} of ${totalJobs} newest)`
          : "Jobs";
        parts.push(`<optgroup label="${escape(label)}">` + jobChoices.map((j) => {
          const preview = (j.task || "").replace(/\s+/g, " ").slice(0, 60);
          return `<option value="job:${escape(j.id)}">[${escape(j.status)}] ${escape(j.kind)} — ${escape(preview)}</option>`;
        }).join("") + `</optgroup>`);
      }
      sel.innerHTML = parts.join("");
      sel.disabled = false;
      const termOpenBtn = $("#term-open");
      if (termOpenBtn) termOpenBtn.disabled = false;
      // Restore by value comparison, not a data-built CSS selector: a `prev`
      // containing selector metacharacters would make querySelector throw a
      // SyntaxError and abort the picker refresh.
      if ([...sel.options].some((o) => o.value === prev)) sel.value = prev;
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
      const grid = $("#terms-grid");
      if (TERMS.size === 0) {
        grid.innerHTML = `<div class="term-empty">No terminal panes open. Pick a job above, click <em>New terminal</em>, or start one in <em>Run</em>.</div>`;
      } else {
        // Drop the empty placeholder if it's still there.
        const empty = grid.querySelector(".term-empty");
        if (empty) empty.remove();
      }
      $("#count-terminals").textContent = TERMS.size || "·";
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
          claude: ["claude-opus-4-7", "claude-opus-4-6", "claude-sonnet-4-6", "claude-haiku-4-5"],
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

    function termOpenDraft() {
      _draftCounter += 1;
      const draftId = "draft:" + Date.now() + ":" + _draftCounter;
      const grid = $("#terms-grid");
      if (!grid) return;

      // Defaults. Editable before sending.
      const defaultTool = "claude";
      const defaultModel = (DRAFT_MODELS_BY_TOOL[defaultTool] || [""])[0] || "claude-sonnet-4-6";
      const defaultType = "ai";

      const pane = document.createElement("div");
      pane.className = "term-pane term-draft focus";
      pane.dataset.jobId = draftId;
      pane.innerHTML = `
        <div class="term-head">
          <span class="pill status-pill" title="not yet started — pick tool, model and type">draft</span>
          <span class="task">New terminal</span>
          <span class="activity waiting" title="will start when you send the first message">unsent</span>
          <span class="id">${escape(draftId.slice(-6))}</span>
          <span class="actions">
            <button class="close-btn" title="Discard this draft">close</button>
          </span>
        </div>
        <div class="term-draft-config">
          <label class="draft-field">
            <span class="draft-label">Tool</span>
            <select class="draft-tool">
              <option value="claude"${defaultTool === "claude" ? " selected" : ""}>Claude</option>
              <option value="codex"${defaultTool === "codex" ? " selected" : ""}>Codex</option>
            </select>
          </label>
          <label class="draft-field">
            <span class="draft-label">Model</span>
            <select class="draft-model">
              ${termDraftModelOptions(defaultTool, defaultModel)}
            </select>
          </label>
          <label class="draft-field">
            <span class="draft-label">Type</span>
            <select class="draft-type">
              ${termDraftTypeOptionsHtml(defaultType)}
            </select>
          </label>
          <span class="draft-hint draft-hint-ai">Conversation starts when you send the first message.</span>
          <span class="draft-hint draft-hint-shell" hidden>Opens a real PTY, runs the chosen tool inside, then types your first message into the running TUI.</span>
        </div>
        <div class="term-body chat" tabindex="0">
          <div class="msg system draft-placeholder">Pick tool, model and type, then send your first message.</div>
        </div>
        <div class="attach-tray" style="display:none"></div>
        <div class="term-foot">
          <textarea class="stdin-input" rows="1" placeholder="type your first message — Enter starts the conversation · Shift+Enter newline"></textarea>
          <button class="send-btn">start</button>
        </div>
      `;
      grid.appendChild(pane);

      const body = pane.querySelector(".term-body");
      const input = pane.querySelector(".stdin-input");
      const sendBtn = pane.querySelector(".send-btn");
      const toolSel = pane.querySelector(".draft-tool");
      const modelSel = pane.querySelector(".draft-model");
      const typeSel = pane.querySelector(".draft-type");
      const hintAi = pane.querySelector(".draft-hint-ai");
      const hintShell = pane.querySelector(".draft-hint-shell");
      const placeholder = body.querySelector(".draft-placeholder");

      // Auto-grow the textarea exactly like real panes do.
      const autosize = () => {
        input.style.height = "auto";
        const next = Math.min(input.scrollHeight, COMPOSER_AUTOSIZE_MAX_PX);
        input.style.height = next + "px";
      };
      input.addEventListener("input", autosize);

      const t = {
        jobId: draftId,
        pane, body, input, sendBtn,
        source: null,
        task: "",
        kind: "draft",
        isDraft: true,
        attached: { images: [], files: [] },
      };
      TERMS.set(draftId, t);

      // Parse the Type select value: "ai" | "shell:<name>".
      const parseType = () => {
        const v = typeSel.value || "ai";
        if (v === "ai") return { kind: "ai" };
        const [k, ...rest] = v.split(":");
        return { kind: k, id: rest.join(":") };
      };

      const refreshType = () => {
        const isShell = parseType().kind === "shell";
        hintAi.hidden = isShell;
        hintShell.hidden = !isShell;
        if (isShell) {
          sendBtn.textContent = "open & send";
          if (placeholder) placeholder.textContent = "Opens the shell, runs the tool with the chosen model, then types your first message into the TUI.";
        } else {
          sendBtn.textContent = "start";
          if (placeholder) placeholder.textContent = "Pick tool, model and type, then send your first message.";
        }
      };
      typeSel.addEventListener("change", refreshType);

      // Tool change repopulates the Model list (claude vs codex models).
      toolSel.addEventListener("change", () => {
        const tool = toolSel.value;
        const list = DRAFT_MODELS_BY_TOOL[tool] || [];
        modelSel.innerHTML = termDraftModelOptions(tool, list[0] || "");
      });

      // Image paste / drop reuses the real-pane plumbing (AI chat only —
      // shell-mode messages are typed into the TUI and don't accept
      // multimodal input from the dashboard composer).
      input.addEventListener("paste", (e) => {
        if (parseType().kind !== "ai") return;
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
        if (parseType().kind !== "ai") return;
        for (const f of e.dataTransfer.files || []) {
          if (f.type.startsWith("image/")) termPasteImage(t, f);
        }
      });

      pane.addEventListener("click", () => {
        document.querySelectorAll(".term-pane.focus").forEach((p) => p.classList.remove("focus"));
        pane.classList.add("focus");
      });

      pane.querySelector(".close-btn").addEventListener("click", (e) => {
        e.stopPropagation();
        TERMS.delete(draftId);
        pane.remove();
        termRenderEmptyState();
      });

      const startConversation = async () => {
        const tool = toolSel.value;
        const model = modelSel.value;
        const typeSelected = parseType();
        const text = input.value.trim();
        if (!model) {
          setMsg("#term-msg", "err", "Pick a model before sending.", TERM_MSG_DURATION_MS);
          return;
        }

        if (typeSelected.kind === "shell") {
          // Shell-with-AI path: spawn the PTY, then launch the chosen
          // tool inside it with the chosen model, then type the user's
          // message into the running TUI.
          const shell = typeSelected.id || "auto";
          sendBtn.disabled = true;
          typeSel.disabled = true;
          toolSel.disabled = true;
          modelSel.disabled = true;
          sendBtn.textContent = "opening…";
          try {
            const res = await postJson("/api/ptys", { shell, cols: 100, rows: 30 });
            TERMS.delete(draftId);
            pane.remove();
            // For Claude: pre-allocate the session-id so we can mark it
            // as already-handled BEFORE the IDE-transcript auto-opener
            // notices the new JSONL file. Without this, opening
            // Type=shell + Tool=Claude spawns a second pane mirroring
            // the same session.
            let preSessionId = null;
            if (tool === "claude") {
              preSessionId = (window.crypto && crypto.randomUUID)
                ? crypto.randomUUID()
                // Fallback for very old browsers without crypto.randomUUID.
                // Math.random is biased and NOT cryptographically random — the
                // session-id collision space is large enough (~10^36) that this
                // is acceptable for a session selector but it must not be used
                // for security tokens. Modern browsers always take the
                // crypto.randomUUID branch above.
                : ("xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
                    const r = Math.random() * 16 | 0;
                    const v = c === "x" ? r : (r & 0x3 | 0x8);
                    return v.toString(16);
                  }));
              suppressAutoOpen("ide:" + preSessionId);
            }
            // Build the sequence the PTY runs once the prompt appears:
            //   1. Launch the tool with the model flag.
            //   2. After a beat (TUI warm-up), type the first message
            //      so the operator sees it land inside the running AI.
            const launchCmd = termDraftLaunchCommand(tool, model, preSessionId);
            const steps = [{ text: launchCmd, delay: 300 }];
            if (text) steps.push({ text: text, delay: 3000 });
            // Stash the per-PTY token so a later restoreOpenPanes()
            // reattach can re-use it without going through /api/ptys/<id>
            // (which now intentionally omits the token from list/get).
            window._PTY_TOKENS = window._PTY_TOKENS || {};
            if (res.token) window._PTY_TOKENS[res.id] = res.token;
            termOpenPty(res.id, res, steps);
          } catch (err) {
            sendBtn.disabled = false;
            typeSel.disabled = false;
            toolSel.disabled = false;
            modelSel.disabled = false;
            sendBtn.textContent = "open & send";
            const note = document.createElement("div");
            note.className = "msg system";
            note.style.color = "var(--bad)";
            note.textContent = "[open shell failed: " + err.message + "]";
            body.appendChild(note);
            setMsg("#term-msg", "err", "Open shell failed: " + err.message, TERM_MSG_DURATION_MS);
          }
          return;
        }

        // AI chat (direct) path: POST /api/jobs with stream-json.
        // The /api/jobs endpoint only accepts a plain-text ``task`` for
        // the first turn (server requires a non-empty ``task``). The
        // previous implementation accepted image paste / file drag into
        // the draft pane but then silently dropped them on POST — the
        // operator saw their attachments in the tray, hit Send, and the
        // first model turn went text-only with no warning. Now: require
        // text on the draft AND carry any tray attachments over to the
        // newly-opened pane so the operator can include them in their
        // very next turn.
        const attached = t.attached || { images: [], files: [] };
        if (!text) {
          if (attached.images.length || attached.files.length) {
            setMsg("#term-msg", "warn",
              "Type a first message — attachments need a text turn to send with.",
              TERM_MSG_DURATION_MS);
          }
          input.focus();
          return;
        }
        sendBtn.disabled = true;
        typeSel.disabled = true;
        toolSel.disabled = true;
        modelSel.disabled = true;
        sendBtn.textContent = "starting…";

        // Claude chats open as unified session panes. Mint a fresh sid and
        // open a session pane on it; the first termSendSession POSTs to
        // /api/sessions/<sid>/input, which the backend create-on-first-turn
        // path materialises (claude --session-id <sid>). No /api/jobs POST.
        if (tool !== "codex") {
          const sid = (window.crypto && crypto.randomUUID)
            ? crypto.randomUUID()
            // Fallback for very old browsers without crypto.randomUUID — a
            // session selector only, never a security token, so the biased
            // Math.random generator is acceptable (modern browsers take the
            // crypto.randomUUID branch above).
            : ("xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
                const r = Math.random() * 16 | 0;
                const v = c === "x" ? r : (r & 0x3 | 0x8);
                return v.toString(16);
              }));
          // Pre-suppress both keys so the IDE-transcript auto-opener doesn't
          // spawn a duplicate pane the moment claude writes its first JSONL.
          suppressAutoOpen("session:" + sid);
          suppressAutoOpen("ide:" + sid);
          TERMS.delete(draftId);
          pane.remove();
          termOpenSession(sid);
          termFocusNewPane("session:" + sid);
          const newT = TERMS.get("session:" + sid);
          if (newT) {
            // Carry over draft attachments so they ride along on the first
            // turn the operator sends (the session composer is always on).
            if (attached.images.length || attached.files.length) {
              newT.attached = attached;
              if (typeof termRenderAttachments === "function") termRenderAttachments(newT);
            }
            // Pin the operator's chosen model so the first turn creates the
            // session on it (the /input body carries it; the engine factory
            // would otherwise fall back to the models.yaml default).
            newT.model = model;
            // Send the first message — this acquires + creates the session.
            termSendSession(newT, text);
          }
          return;
        }

        // Codex chats stay job-based (chat-codex); not migrated to sessions.
        try {
          const payload = { kind: "chat-codex", task: text, model };
          const res = await postJson("/api/jobs", payload);
          TERMS.delete(draftId);
          pane.remove();
          termOpen(res.id, res);
          termFocusNewPane(res.id);
          // Carry over draft attachments to the newly-spawned chat pane.
          // /api/jobs is text-only; the operator will see their files /
          // images still in the tray and they ride along on the next
          // /api/jobs/<id>/input call (which DOES support multimodal).
          const newT = TERMS.get(res.id);
          if (newT && (attached.images.length || attached.files.length)) {
            newT.attached = attached;
            termRenderAttachments(newT);
            setMsg("#term-msg", "warn",
              `${attached.images.length + attached.files.length} attachment(s) carried over — send your next message to deliver them.`,
              5000);
          }
          await loadJobs();
        } catch (err) {
          sendBtn.disabled = false;
          typeSel.disabled = false;
          toolSel.disabled = false;
          modelSel.disabled = false;
          sendBtn.textContent = "start";
          const note = document.createElement("div");
          note.className = "msg system";
          note.style.color = "var(--bad)";
          note.textContent = "[start failed: " + err.message + "]";
          body.appendChild(note);
          setMsg("#term-msg", "err", "Start failed: " + err.message, TERM_MSG_DURATION_MS);
        }
      };

      sendBtn.addEventListener("click", startConversation);
      input.addEventListener("keydown", (e) => {
        // !e.isComposing keeps mid-IME-composition Enter (Japanese, Chinese,
        // Korean, etc.) from sending half-typed text — matches the
        // transcript fork at the other composer site.
        if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); startConversation(); }
      });

      termRenderEmptyState();
      // Drop focus into the message field so the operator can start typing.
      requestAnimationFrame(() => { try { input.focus(); } catch (_) {} });
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

    // ----- Export pane as markdown -----
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
      // PTY panes own additional resources (xterm instance, ResizeObserver,
      // server-side shell) that the generic close path doesn't know about.
      // Delegate so the cleanup is symmetric with the dedicated close-btn.
      if (t.kind === "terminal") {
        termClosePty(jobId);
        return;
      }
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
      // Dispatch-tracker panes register themselves in DISPATCH_TRACKERS so
      // termMarkToolResult can forward results into the pane. The in-pane
      // close button cleans this up itself, but termCloseAllFinished and
      // persistence-driven closes route through termClose directly. Without
      // this sweep, the Map keeps a reference to the (now-detached) pane and
      // future tool_result events appendChild to a node nobody can see.
      if (t.toolUseId && DISPATCH_TRACKERS.get(t.toolUseId) === t) {
        DISPATCH_TRACKERS.delete(t.toolUseId);
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
      // termOpen() time would otherwise point at the FIRST turn's job and
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
        // the closures in termOpen() pass the pane object (`t`) to
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
        // Re-persist immediately so a F5 between turns won't lose the
        // pane (the old job id would 404).
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

    function termCloseAutocomplete(t) {
      const pop = t.pane.querySelector(".composer-pop");
      if (pop) { pop.remove(); t._popOpen = false; }
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

    function termClearThinkingPlaceholder(t) {
      if (!t || !t.body) return;
      t.body.querySelectorAll(".thinking-placeholder").forEach((el) => el.remove());
    }

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

      // If this is a Bash invocation that boots ANOTHER LLM (codex exec /
      // claude -p / claude --print), the dispatched subprocess is what the
      // operator usually wants to watch live. Open it as a tracking pane
      // automatically (unless they disabled auto-open).
      if (name === "Bash" && termIsLLMDispatchCommand(input?.command)) {
        termOpenDispatchTracker(t, toolUseId, input);
      }
    }

    // Heuristic: does this Bash command spawn a Claude or Codex agent?
    function termIsLLMDispatchCommand(cmd) {
      if (!cmd || typeof cmd !== "string") return false;
      // Codex CLI dispatch.
      if (/\bcodex\s+exec(\s|$)/.test(cmd)) return true;
      // Claude CLI dispatch in non-interactive mode.
      if (/\bclaude(\.[a-z]+)?\s+(-p\b|--print\b)/i.test(cmd)) return true;
      // ``/i`` mirrors the -p/--print sibling — Windows shells (cmd.exe)
      // commonly receive flag names in mixed case (``--Input-Format``)
      // and dropping the flag here caused us to miss dispatching.
      if (/\bclaude(\.[a-z]+)?\s+.*--input-format\s+stream-json/i.test(cmd)) return true;
      return false;
    }

    // Map<dispatch tool_use_id, dispatch pane state> so termMarkToolResult
    // can hand the result over to the right tracker pane.
    var DISPATCH_TRACKERS = new Map();

    function termOpenDispatchTracker(parentTerm, toolUseId, input) {
      if (!termAutoOpenEnabled()) return;
      const paneKey = "dispatch:" + toolUseId;
      if (TERMS.has(paneKey)) return;
      // Honour the operator's prior close. Without this, F5 (or a
      // transcript re-mirror) would re-play every Bash tool_use that
      // looks like a Claude/Codex dispatch and re-spawn its tracker
      // pane — exactly what the operator was clearing away.
      if (AUTO_OPENED_ONCE.has(paneKey)) return;
      suppressAutoOpen(paneKey);
      const grid = $("#terms-grid");
      if (!grid) return;
      const cmd = input?.command || "";
      // Anchor with (\s|$) — mirrors the termIsLLMDispatchCommand pattern
      // so the label correctly resolves "Codex" only for ``codex exec``
      // invocations and never matches ``codex executor`` or similar
      // identifier prefixes embedded in a longer Bash command.
      const isCodex = /\bcodex\s+exec(\s|$)/.test(cmd);
      // tool_use_ids look like `toolu_01XXXXX...` — slice past the prefix
      // so the label shows characters that actually distinguish dispatches
      // instead of the literal "toolu_".
      const shortId = toolUseId.replace(/^toolu_/, "").slice(0, 6) || toolUseId.slice(0, 6);
      const label = (isCodex ? "Codex" : "Claude") + " dispatch (" + shortId + ")";
      const pane = document.createElement("div");
      pane.className = "term-pane focus";
      pane.dataset.jobId = paneKey;
      pane.innerHTML = `
        <div class="term-head">
          <span class="pill ${isCodex ? "codex" : "claude"} status-pill">dispatch</span>
          <span class="task" title="${escape(cmd)}">${escape(label)}</span>
          <span class="activity" title="current activity in this pane">queued…</span>
          <span class="id">${escape(shortId)}</span>
          <span class="actions">
            <button class="expand-btn" title="Show or hide this terminal's output">expand</button>
            <button class="close-btn" title="Close this pane">close</button>
          </span>
        </div>
        <div class="term-body chat" tabindex="0"></div>
        <div class="term-foot">
          <textarea class="stdin-input" rows="1" disabled placeholder="read-only — dispatch is owned by the parent orchestrate session"></textarea>
          <button class="send-btn" disabled>send</button>
        </div>
      `;
      grid.appendChild(pane);
      if (termGetLayout() === "list") pane.classList.add("collapsed");
      const body = pane.querySelector(".term-body");
      const t = {
        jobId: paneKey,
        pane, body,
        input: pane.querySelector(".stdin-input"),
        sendBtn: pane.querySelector(".send-btn"),
        source: null,
        task: cmd,
        kind: "dispatch",
        toolUseEls: new Map(),
        currentAssistant: null,
        parentTermId: parentTerm.jobId,
        toolUseId,
      };
      TERMS.set(paneKey, t);
      termInitAutoFollow(t);
      DISPATCH_TRACKERS.set(toolUseId, t);
      pane.querySelector(".close-btn").addEventListener("click", () => {
        DISPATCH_TRACKERS.delete(toolUseId);
        termClose(paneKey);
      });
      pane.querySelector(".expand-btn")?.addEventListener("click", (e) => {
        e.stopPropagation();
        termToggleCollapsed(t);
      });
      pane.querySelector(".term-head").addEventListener("click", (e) => {
        if (e.target.closest("button")) return;
        termToggleCollapsed(t);
      });
      // Render the prompt up-front so the operator sees what's being run.
      // Use the same .bash-cmd treatment as inline Bash tool pills so the
      // command is actually legible — the generic .msg.system style is
      // --text-faint (~48% lightness) and disappears on the dark body.
      body.appendChild(renderBashCommand(cmd));
      const waiting = document.createElement("div");
      waiting.className = "msg system";
      waiting.style.opacity = "0.7";
      waiting.textContent = "(waiting for output…)";
      body.appendChild(waiting);
      t._waitingMsg = waiting;
      termRenderEmptyState();
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
      // If this tool_use_id has a dispatch tracker pane open, forward the
      // result into it so the operator sees the dispatched LLM's output as
      // if it were its own terminal.
      const tracker = DISPATCH_TRACKERS.get(toolUseId);
      if (tracker) {
        if (tracker._waitingMsg) { tracker._waitingMsg.remove(); tracker._waitingMsg = null; }
        const block = document.createElement("div");
        block.className = "msg " + (isError ? "system" : "assistant");
        if (isError) block.style.color = "var(--bad)";
        const role = document.createElement("div");
        role.className = "role";
        role.textContent = isError ? "dispatch failed" : "dispatch result";
        role.dataset.roleLocked = "1";  // protect from termSetPaneModel retro-rename
        block.appendChild(role);
        const text = document.createElement("div");
        text.className = "text";
        // Render the result; for chat-style content arrays surface each
        // element, otherwise dump the JSON / string verbatim.
        const raw = typeof content === "string"
          ? content
          : Array.isArray(content)
            ? content.map((b) => typeof b === "string" ? b : (b?.text ?? JSON.stringify(b))).join("\n")
            : JSON.stringify(content, null, 2);
        try { text.innerHTML = DOMPurify.sanitize(marked.parse(raw)); }
        catch (_) { text.textContent = raw; }
        block.appendChild(text);
        tracker.body.appendChild(block);
        // Header pill goes from "dispatch" to "done" / "failed". Use the
        // helper so the prior "running" / "queued" / cancelling classes are
        // wiped — toggle("done", !isError) on its own left stale classes
        // when the dispatch had been in "running" state.
        termSetPillState(
          tracker.pane.querySelector(".status-pill"),
          isError ? "bad" : "done",
          isError ? "failed" : "done",
        );
        // The activity chip is initialised to "queued…" at pane creation
        // and dispatch panes have no streaming events to advance it. If we
        // don't update it here it stays "queued…" forever even though the
        // pill already reads DONE — see the screenshot bug.
        termSetActivity(tracker, isError ? "failed" : "done", isError ? "ended" : "ready");
        termAutoScroll(tracker);
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

    // ----- PTY (real shell) panes -----
    //
    // Created by termOpenDraft when the operator picks Tool=Shell. A
    // pane hosts an xterm.js instance bound bidirectionally to the
    // server's PTY master via WebSocket (/api/ptys/<id>/io). All
    // chat-bubble plumbing is bypassed: bytes in, bytes out.

    // Reused per pane: when xterm/ResizeObserver aren't available
    // (older browsers, blocked CDN) the pane shows an inline error
    // instead of silently appearing broken.
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

    function termOpenPty(ptyId, meta, initialCommand) {
      if (TERMS.has(ptyId)) return;
      const grid = $("#terms-grid");
      if (!grid) return;
      const shellLabel = (meta?.argv && meta.argv[0]) || meta?.shell || "shell";
      const shortShell = String(shellLabel).split(/[\\/]/).pop() || shellLabel;
      const pane = document.createElement("div");
      pane.className = "term-pane term-pty focus";
      pane.dataset.jobId = ptyId;
      pane.innerHTML = `
        <div class="term-head">
          <span class="pill running status-pill" title="PTY ${escape(ptyId)}">connecting</span>
          <span class="task" title="${escape(meta?.cwd || "")}">${escape(shortShell)} · ${escape(meta?.cwd || "")}</span>
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
      grid.appendChild(pane);

      const body = pane.querySelector(".term-body");
      const t = {
        jobId: ptyId,
        pane, body,
        input: null, sendBtn: null,
        source: null,            // WebSocket goes in here
        task: meta?.cwd || "",
        kind: "terminal",
        shell: meta?.shell || "auto",
        attached: { images: [], files: [] },
      };
      TERMS.set(ptyId, t);

      if (termGetLayout() === "list") pane.classList.add("collapsed");

      pane.addEventListener("click", () => {
        document.querySelectorAll(".term-pane.focus").forEach((p) => p.classList.remove("focus"));
        pane.classList.add("focus");
      });
      pane.querySelector(".term-head").addEventListener("click", (e) => {
        if (e.target.closest("button")) return;
        termToggleCollapsed(t);
      });
      // Single expand-btn listener: toggles collapsed AND re-fits xterm
      // on the next frame. The previous implementation registered TWO
      // separate click listeners on the same button (one toggling
      // collapse, another calling requestAnimationFrame(sendResize)),
      // which fired in sequence on every click — wasteful and made it
      // hard to reason about the order of operations. Consolidated here.
      pane.querySelector(".expand-btn")?.addEventListener("click", (e) => {
        e.stopPropagation();
        termToggleCollapsed(t);
        // The xterm body has display:none while collapsed, so its computed
        // pixel size is 0 → fit() can't run. Re-fit on the next frame so
        // the cols/rows match the new pane geometry once layout settles.
        requestAnimationFrame(() => {
          if (pane.classList.contains("collapsed")) return;
          try { t._fitAddon && t._fitAddon.fit(); } catch (_) {}
        });
      });
      pane.querySelector(".pin-btn")?.addEventListener("click", (e) => {
        e.stopPropagation();
        pane.classList.toggle("pinned");
        const btn = pane.querySelector(".pin-btn");
        btn.classList.toggle("active", pane.classList.contains("pinned"));
        btn.textContent = pane.classList.contains("pinned") ? "unpin" : "pin";
        // Re-fit xterm to the new pane size on the next frame.
        requestAnimationFrame(() => t._fitAddon && t._fitAddon.fit());
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

      if (termPtyMissingDeps()) {
        body.innerHTML = `<div class="msg system" style="color:var(--bad);padding:12px">
          xterm.js failed to load (CDN blocked?). Reload the page or check your network.
        </div>`;
        return;
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
      const fit = new FitAddon.FitAddon();
      term.loadAddon(fit);
      if (typeof WebLinksAddon !== "undefined") {
        try { term.loadAddon(new WebLinksAddon.WebLinksAddon()); } catch (_) {}
      }
      term.open(body);
      t._term = term;
      t._fitAddon = fit;
      // First fit after the next frame so layout has finished.
      const initialFit = () => {
        try { fit.fit(); } catch (_) {}
      };
      requestAnimationFrame(initialFit);

      // ----- WebSocket -----
      // meta?.token is set by /api/ptys (POST) for newly-spawned PTYs.
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
          try { msg = JSON.parse(ev.data); } catch (e) { console.warn("[terminals] PTY control frame JSON parse failed: " + (e && e.message ? e.message : e)); return; }
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
        try { fit.fit(); } catch (_) {}
        const cols = term.cols, rows = term.rows;
        if (!cols || !rows) return;
        if (cols === lastCols && rows === lastRows) return;
        lastCols = cols; lastRows = rows;
        if (ws.readyState === WebSocket.OPEN) {
          try {
            ws.send(JSON.stringify({ type: "resize", cols, rows }));
          } catch (e) { console.warn("[terminals] PTY resize send failed: " + (e && e.message ? e.message : e)); }
        }
      };
      term.onResize(({ cols, rows }) => {
        if (cols === lastCols && rows === lastRows) return;
        lastCols = cols; lastRows = rows;
        if (ws.readyState === WebSocket.OPEN) {
          try {
            ws.send(JSON.stringify({ type: "resize", cols, rows }));
          } catch (e) { console.warn("[terminals] PTY resize send failed: " + (e && e.message ? e.message : e)); }
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
      // (The expand button's own click listener already re-fits xterm on
      // the post-expand frame; the ResizeObserver above catches every
      // other geometry change — no extra listener needed here.)

      termRenderEmptyState();
      persistOpenPanes();
    }

    function termClosePty(ptyId) {
      const t = TERMS.get(ptyId);
      if (!t) return;
      t._closed = true;
      // Cancel any pending initial-command timers so their closures
      // release ws + term references instead of keeping them alive
      // until the last delay fires.
      if (Array.isArray(t._runStepTimers)) {
        for (const tm of t._runStepTimers) {
          try { clearTimeout(tm); } catch (_) {}
        }
        t._runStepTimers = [];
      }
      if (t._costRefreshTimer) { clearTimeout(t._costRefreshTimer); t._costRefreshTimer = null; }
      if (t._autoFollowScrollHandler && t.body) {
        try { t.body.removeEventListener("scroll", t._autoFollowScrollHandler); } catch (_) {}
        t._autoFollowScrollHandler = null;
      }
      try { t._resizeObserver && t._resizeObserver.disconnect(); } catch (_) {}
      // Mirror the ResizeObserver cleanup for the legacy window-resize fallback.
      if (t._resizeFallback) {
        try { window.removeEventListener("resize", t._resizeFallback); } catch (_) {}
        t._resizeFallback = null;
      }
      try { t.source && t.source.close(); } catch (_) {}
      // Fire-and-forget kill on the server side too so we don't leak shells.
      // Both the synchronous throw (rare) AND the Promise rejection (mid-flight
      // network drop, 4xx/5xx response) need to be surfaced — otherwise the
      // operator's "close" appears to succeed locally while the shell keeps
      // running server-side with no diagnostic trail. The earlier guard only
      // caught the synchronous arm.
      try {
        const _killPromise = fetch(`/api/ptys/${ptyId}/kill`, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
        if (_killPromise && typeof _killPromise.catch === "function") {
          _killPromise.catch((e) => console.warn("[terminals] PTY kill fetch rejected: " + (e && e.message ? e.message : e)));
        }
      } catch (e) { console.warn("[terminals] PTY kill fetch failed: " + (e && e.message ? e.message : e)); }
      try { t._term && t._term.dispose(); } catch (_) {}
      t.pane.remove();
      TERMS.delete(ptyId);
      // Prune the in-memory PTY token so the runtime cache mirrors the
      // localStorage pruning (persistOpenPanes rebuilds tokens from open
      // panes only). Otherwise closed-shell secrets linger in
      // window._PTY_TOKENS for the life of the session.
      if (window._PTY_TOKENS) delete window._PTY_TOKENS[ptyId];
      // Same suppression as termClose — a PTY pane the operator closed
      // should stay closed across reloads, even if the shell is still
      // running on the server side.
      suppressAutoOpen(ptyId);
      termRenderEmptyState();
      persistOpenPanes();
    }

    // Bring a freshly-opened pane into the operator's view: expand it
    // (so the body is visible, not just a status row) AND scroll it into
    // sight. Used after every USER-INITIATED termOpen() call — picker
    // "Open", New-terminal first message, fork-and-send, resume-from-
    // dead-chat, etc. Background auto-open (loadJobs polling, restoring
    // previous state) deliberately skips this so the operator can scan
    // a list mode without panes ballooning unsolicited.
    function termFocusNewPane(jobId) {
      const t = TERMS.get(jobId);
      if (!t || !t.pane) return;
      if (t.pane.classList.contains("collapsed")) {
        termSetCollapsed(t, false);
      } else {
        try { t.pane.scrollIntoView({ block: "nearest", behavior: "smooth" }); } catch (_) {}
      }
    }

    function termOpen(jobId, meta) {
      if (TERMS.has(jobId)) return;
      // Claude chats are now unified session panes. Any caller that still
      // hands a kind === "chat" job here (restore, auto-open, picker) is
      // routed to the writable session pane keyed by its session_id. Codex
      // chats (chat-codex) and orchestrate/plan jobs keep the job pane below.
      if (meta && meta.kind === "chat" && meta.session_id) {
        termOpenSession(meta.session_id);
        return;
      }
      const grid = $("#terms-grid");
      const taskPreview = (meta?.task || "").replace(/\s+/g, " ").slice(0, 120);
      const pane = document.createElement("div");
      pane.className = "term-pane";
      pane.dataset.jobId = jobId;
      pane.innerHTML = `
        <div class="term-head">
          <span class="pill running status-pill" title="job ${escape(jobId)}">connecting</span>
          <span class="task" title="${escape(meta?.task || "")}">${escape(taskPreview || jobId)}</span>
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
      grid.appendChild(pane);

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
      const kind = meta?.kind || "orchestrate";
      // Both Claude and Codex chat panes use the same chat-bubble styling.
      // Even though codex is one-shot per subprocess, the pane renders
      // multi-turn conversations via SSE rewiring (see termSendCodexNextTurn).
      if (kind === "chat" || kind === "chat-codex") body.classList.add("chat");
      const t = {
        jobId, pane, body, input, sendBtn,
        source: null,
        task: meta?.task || "",
        kind,
        jsonBuf: [],
        currentAssistant: null,   // element for the in-progress assistant message
        toolUseEls: new Map(),    // tool_use_id -> {pill, detail}
        attached: { images: [], files: [] },
        sessionId: meta?.session_id || "",  // enables resume on dead-pane
        model: meta?.model || "",  // seed from /api/jobs; replaced on first init/assistant frame
      };
      TERMS.set(jobId, t);

      // Initial state depends on the operator's chosen layout. List mode
      // opens collapsed (status-bar reading); grid mode opens expanded
      // (legacy "see every pane at once" view).
      if (termGetLayout() === "list") pane.classList.add("collapsed");

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
      // Clicking the head row toggles expand/collapse. Buttons inside the
      // head call stopPropagation so the toggle doesn't fire when the user
      // is hitting "stop", "close", etc.
      pane.querySelector(".term-head").addEventListener("click", (e) => {
        if (e.target.closest("button")) return;
        termToggleCollapsed(t);
      });
      pane.querySelector(".expand-btn")?.addEventListener("click", (e) => {
        e.stopPropagation();
        termToggleCollapsed(t);
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
      pane.querySelector(".pin-btn")?.addEventListener("click", (e) => {
        e.stopPropagation();
        pane.classList.toggle("pinned");
        const btn = pane.querySelector(".pin-btn");
        btn.classList.toggle("active", pane.classList.contains("pinned"));
        btn.textContent = pane.classList.contains("pinned") ? "unpin" : "pin";
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
        Promise.resolve(loadJobs()).catch((e) => console.warn("[terminals] loadJobs after SSE end failed: " + (e && e.message ? e.message : e)));
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

      termRenderEmptyState();
      persistOpenPanes();
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

    function termOpenSession(sid) {
      const paneKey = "session:" + sid;
      if (TERMS.has(paneKey)) return;
      const grid = $("#terms-grid");
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
            <button class="branch-btn" title="create a copy to explore an alternative without touching this one">branch</button>
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
      grid.appendChild(pane);

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
      TERMS.set(paneKey, t);

      // Start collapsed in list layout; expanded in others (matches chat-pane behaviour).
      if (termGetLayout() === "list") pane.classList.add("collapsed");

      termInitAutoFollow(t);

      // Header click toggles expand/collapse.
      pane.querySelector(".term-head").addEventListener("click", (e) => {
        if (e.target.closest("button")) return;
        termToggleCollapsed(t);
      });
      pane.querySelector(".expand-btn")?.addEventListener("click", (e) => {
        e.stopPropagation();
        termToggleCollapsed(t);
      });
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
      // Branch: fork this session into an independent copy. The backend
      // mints a new sid by copying this session's transcript under it; on
      // success we open that sid as a fresh session pane so the operator can
      // explore an alternative without disturbing this conversation.
      pane.querySelector(".branch-btn")?.addEventListener("click", async (e) => {
        e.stopPropagation();
        const btn = e.currentTarget;
        if (btn._branchInFlight) return;
        btn._branchInFlight = true;
        btn.disabled = true;
        try {
          const r = await fetch("/api/sessions/" + encodeURIComponent(sid) + "/branch", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: "{}",
          });
          if (!r.ok) {
            const body = await r.text().catch(() => "");
            throw new Error("HTTP " + r.status + (body ? ": " + body.slice(0, 120) : ""));
          }
          const resp = await r.json();
          if (resp && resp.sid) {
            termOpenSession(resp.sid);
            termFocusNewPane("session:" + resp.sid);
          } else {
            throw new Error("no sid in branch response");
          }
        } catch (err) {
          setMsg("#term-msg", "err", "Branch failed: " + err.message, TERM_MSG_DURATION_MS);
        } finally {
          btn._branchInFlight = false;
          btn.disabled = false;
        }
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

      // Open stream immediately only if the pane is NOT starting collapsed.
      // Collapsed panes stay connection-free until expanded by the operator.
      if (!pane.classList.contains("collapsed")) {
        t.openStream();
      } else {
        termSetPillState(statusPill, "done", "paused");
        termSetActivity(t, "paused (expand to resume)", "ready");
      }

      termRenderEmptyState();
      persistOpenPanes();
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

    // POST text to /api/sessions/<sid>/input. The SSE stream will echo back
    // the user turn + the assistant reply, so we intentionally do NOT render
    // the bubble here — the stream renders it. On error we surface a toast
    // and an inline note (matches the termSend error path).
    async function termSendSession(t, text) {
      if (!t || !t.sid) return;
      const trimmed = (text || "").trim();
      if (!trimmed) return;
      // In-flight latch: Enter keydown can race with a click; first one wins.
      if (t._sessionSendInFlight) return;
      t._sessionSendInFlight = true;
      t.sendBtn.disabled = true;
      try {
        // Use fetch directly so we can inspect the HTTP status code.
        // postJson throws on non-ok but we need to distinguish 202/409.
        const payload = { text: trimmed, owner: termClientId() };
        // Carry the pane's pinned model (set when a new chat is started) so the
        // backend creates/runs the session on the chosen model, not the default.
        if (t.model) payload.model = t.model;
        const r = await fetch(
          "/api/sessions/" + encodeURIComponent(t.sid) + "/input",
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
          }
        );
        if (r.status === 202) {
          // Accepted and queued — clear the composer; surface a brief notice.
          t.input.value = "";
          if (t.input.tagName === "TEXTAREA") t.input.style.height = "";
          // Flip the chip to its queued state immediately rather than waiting
          // for the next SSE state frame (~1s); the stream stays the source of truth.
          t.pending = true;
          termSessionChipUpdate(t);
          const note = document.createElement("div");
          note.className = "msg system";
          note.textContent = "[queued — turn will be processed shortly]";
          t.body.appendChild(note);
          termAutoScroll(t);
        } else if (r.status === 409) {
          // Already queued — keep the composer text so the operator can
          // retry once the current queued turn has been processed.
          const note = document.createElement("div");
          note.className = "msg system";
          note.style.color = "var(--warn, #e6a817)";
          note.textContent = "[already queued — please wait before sending again]";
          t.body.appendChild(note);
          termAutoScroll(t);
          setMsg("#term-msg", "warn", "Already queued — text preserved.", TERM_MSG_DURATION_MS);
        } else if (r.ok) {
          // 200 accepted — clear the composer. The SSE stream renders the turn.
          t.input.value = "";
          if (t.input.tagName === "TEXTAREA") t.input.style.height = "";
        } else {
          // Other HTTP errors: surface them like network failures.
          const body = await r.text().catch(() => "");
          throw new Error("HTTP " + r.status + (body ? ": " + body.slice(0, 120) : ""));
        }
      } catch (e) {
        const err = document.createElement("div");
        err.className = "msg system";
        err.style.color = "var(--bad)";
        err.textContent = "[send failed: " + e.message + "]";
        t.body.appendChild(err);
        termAutoScroll(t);
        setMsg("#term-msg", "err", "Send failed: " + e.message, TERM_MSG_DURATION_MS);
      } finally {
        t._sessionSendInFlight = false;
        t.sendBtn.disabled = false;
        try { t.input.focus(); } catch (_) {}
      }
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
    // User preference: if disabled, auto-open does nothing.
    function termAutoOpenEnabled() {
      return localStorage.getItem("dash.autoOpenChats") !== "0";
    }
    function termSetAutoOpen(enabled) {
      localStorage.setItem("dash.autoOpenChats", enabled ? "1" : "0");
      const btn = $("#term-autoopen-toggle");
      if (!btn) return;
      btn.classList.toggle("active", !!enabled);
      btn.setAttribute("aria-pressed", enabled ? "true" : "false");
      btn.setAttribute(
        "title",
        enabled
          ? "Auto-open new chats: ON (click to disable)"
          : "Auto-open new chats: OFF (click to enable)",
      );
    }
    function termAutoOpenActive(jobs) {
      if (!termAutoOpenEnabled()) return;
      // Only auto-open when the operator is actually on the Terminals view,
      // so we don't yank focus while they're reading Memory or Decisions.
      if (!$("#view-terminals").classList.contains("active")) return;
      for (const j of (jobs || [])) {
        if (j.kind !== "chat" && j.kind !== "chat-codex") continue;
        if (j.status !== "running" && j.status !== "queued") continue;
        if (TERMS.has(j.id)) continue;
        if (AUTO_OPENED_ONCE.has(j.id)) continue;
        suppressAutoOpen(j.id);
        termOpen(j.id, j);
      }
      // Also mirror live IDE Claude Code sessions running outside the
      // dashboard (any transcript file written-to in the last 5 minutes).
      termAutoOpenActiveTranscripts();
    }

    // IDE transcript files touched within this window count as "live".
    // Claude Code writes to the JSONL on every user/assistant turn, so a
    // few minutes of silence is a safe "abandoned" threshold.
    var TRANSCRIPT_ACTIVE_WINDOW_MS = 5 * 60 * 1000;
    async function termAutoOpenActiveTranscripts() {
      if (!termAutoOpenEnabled()) return;
      if (!$("#view-terminals").classList.contains("active")) return;
      try {
        const r = await fetch("/api/transcripts", { cache: "no-store" });
        if (!r.ok) return;
        const data = await r.json();
        const now = Date.now();
        for (const t of (data.transcripts || [])) {
          const sid = t.session_id;
          if (!sid) continue;
          // Claude transcripts open as session panes now. Dedup + suppress on
          // the actual pane key ("session:"+sid) — the legacy "ide:"+sid key
          // is also checked so an entry suppressed before the convergence is
          // still honoured and we never double-open.
          const sessionKey = "session:" + sid;
          const ideKey = "ide:" + sid;
          if (TERMS.has(sessionKey) || TERMS.has(ideKey)) continue;
          if (AUTO_OPENED_ONCE.has(sessionKey) || AUTO_OPENED_ONCE.has(ideKey)) continue;
          const mtime = t.modified ? Date.parse(t.modified) : 0;
          if (!Number.isFinite(mtime) || mtime <= 0) continue;
          if ((now - mtime) > TRANSCRIPT_ACTIVE_WINDOW_MS) continue;
          suppressAutoOpen(sessionKey);
          termOpenSession(sid);
        }
      } catch (_) { /* ignore - we'll retry next poll */ }
    }

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
            if (TERMS.has(j.id)) { already++; continue; }
            termOpen(j.id, j);
            suppressAutoOpen(j.id);
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
            if (TERMS.has(sessionKey) || TERMS.has(ideKey)) { already++; continue; }
            suppressAutoOpen(sessionKey);
            termOpenSession(sid);
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
      $("#term-open")?.addEventListener("click", async () => {
        const raw = $("#term-picker").value;
        if (!raw) return;
        const sep = raw.indexOf(":");
        const source = raw.slice(0, sep);
        const id = raw.slice(sep + 1);
        // Picker click = "I explicitly want this pane". Lift any prior
        // suppression so the pane behaves like a fresh open: re-closes
        // re-suppress, auto-open paths take the id back into account.
        if (source === "session") {
          unsuppressAutoOpen("session:" + id);
          unsuppressAutoOpen("ide:" + id);  // a session may have been auto-suppressed under its ide key
          termOpenSession(id);
          termFocusNewPane("session:" + id);
          return;
        }
        if (source === "ide") {
          // Legacy "ide:" picker values now open the unified session pane.
          unsuppressAutoOpen("session:" + id);
          unsuppressAutoOpen("ide:" + id);
          termOpenSession(id);
          termFocusNewPane("session:" + id);
          return;
        }
        // Default: dashboard-spawned job.
        try {
          unsuppressAutoOpen(id);
          const r = await fetch("/api/jobs", { cache: "no-store" });
          const data = await r.json();
          const meta = (data.jobs || []).find((j) => j.id === id);
          termOpen(id, meta || { task: "" });
          termFocusNewPane(id);
          await loadJobs();
        } catch (e) {
          setMsg("#term-msg", "err", e.message, TERM_MSG_DURATION_MS);
        }
      });
      $("#term-new")?.addEventListener("click", termOpenDraft);
      $("#term-open-all")?.addEventListener("click", termOpenAllRunning);
      // Restore the auto-open preference and wire its toggle.
      termSetAutoOpen(termAutoOpenEnabled());
      $("#term-autoopen-toggle")?.addEventListener("click", () => {
        termSetAutoOpen(!termAutoOpenEnabled());
      });
      $("#term-close-all")?.addEventListener("click", termCloseAllFinished);
      $("#term-collapse-all")?.addEventListener("click", termCollapseAll);
      // Apply the persisted layout to the grid container, then wire the
      // icon button group — each button carries its target layout in
      // data-layout and we delegate via the group's click event.
      termApplyLayout(termGetLayout());
      document.querySelector(".term-layout-group")?.addEventListener("click", (e) => {
        const btn = e.target.closest(".layout-btn");
        if (!btn || !btn.dataset.layout) return;
        termSetLayout(btn.dataset.layout);
      });
      // Initial picker fill (single unified source).
      termRefreshTranscriptPicker();
      // Restore panes that were open at the last unload. Fires once on
      // boot; rebuilds chat / PTY / IDE-transcript panes from
      // localStorage by re-fetching their server-side state. Drafts and
      // dispatch trackers are intentionally NOT restored.
      restoreOpenPanes();
    });

