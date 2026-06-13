// .ai/dashboard/app/jobs.js -- extracted from app.js (was lines 1103..1470)
// NOTE: top-level const/let were converted to var so identifiers cross <script> boundaries.

    // ----- Events clear -----
    async function clearEvents() {
      if (!confirm("Clear .ai/ledgers/events.jsonl ? This deletes the file.")) return;
      // Pause the auto-refresh timer for the duration of the clear+reload —
      // otherwise a 5s tick can fire mid-await and re-populate _eventsCache
      // from a partially-deleted file, making the operator see entries
      // they thought they just wiped.
      const wasTimer = _eventsTimer;
      if (wasTimer) { clearInterval(_eventsTimer); _eventsTimer = null; }
      try {
        await postJson("/api/events/clear", {});
        _eventsCache = [];
        if (_eventsState && _eventsState.expanded) _eventsState.expanded.clear();
        await loadEvents();
        setMsg("#events-clear", "ok", "Events log cleared", 4000);
      } catch (e) {
        // Null-guard #events-meta — if the markup omits this status element,
        // the unguarded write would mask the underlying error with a fresh
        // TypeError and the setMsg toast (more visible) wouldn't run.
        const meta = $("#events-meta");
        if (meta) meta.textContent = "clear failed: " + e.message;
        setMsg("#events-clear", "err", "Clear failed: " + e.message);
      } finally {
        // Only re-arm if (a) we were the ones who paused a timer, (b) the
        // user still wants auto-refresh, and (c) no handler created a fresh
        // timer during our awaits. Without (c) we'd overwrite (and orphan) a
        // live interval created by the visibilitychange/checkbox handlers;
        // without (b) we'd re-arm against the user's mid-clear opt-out.
        const cb = $("#events-autorefresh");
        if (wasTimer && cb && cb.checked && !_eventsTimer) {
          _eventsTimer = setInterval(loadEvents, EVENTS_AUTOREFRESH_MS);
        }
      }
    }

    // ----- Dispatch mode toggle -----
    async function toggleDispatchMode() {
      const btn = $("#dispatch-toggle");
      // Bail if the dispatch toggle button is missing — the click handler is
      // wired via `$("#dispatch-toggle")?.addEventListener` in core.js, but a
      // bare `toggleDispatchMode()` invocation against a stripped shell would
      // otherwise null-deref `.dataset` / `.disabled`.
      if (!btn) return;
      if (!window._modelsCache) {
        setMsg("#toast-root", "warn", "Models still loading...", 4000);
        return;
      }
      const current = btn.dataset.current || "auto";
      const next = current === "auto" ? "manual" : "auto";
      btn.disabled = true;
      setMsg("#dispatch-msg", "", "saving…");
      try {
        await postJson("/api/models/dispatch_mode", { mode: next });
        setMsg("#dispatch-msg", "ok", "switched to " + next, 4000);
        await loadAll();  // re-renders cards and resolved phase modes
      } catch (e) {
        setMsg("#dispatch-msg", "err", e.message);
      } finally {
        btn.disabled = false;
      }
    }

    // ----- jobs -----
    var _selectedJobId = null;
    var _jobsTimer = null;
    var _jobsLoadInFlight = false;
    var _jobsListDelegationWired = false;
    var _jobsDocDelegationWired = false;
    // Truncate task previews in the row strip so a single 10KB task doesn't
    // blow out the list width. The actual job task is still rendered in
    // full in the job-detail panel.
    var JOB_TASK_PREVIEW_LEN = 80;
    // Default `?tail=N` on /api/jobs/<id> — tunes how much log history the
    // detail panel renders.
    var JOB_LOG_TAIL_LINES = 400;
    // Pixel slop for "is the log scrolled to the bottom?" before we
    // re-pin scrollTop to scrollHeight after a refresh.
    var JOB_LOG_BOTTOM_SLOP_PX = 50;

    // Local defensive whitelist for tool names before they reach
    // pillTool() (owned by core.js, which interpolates raw). Anything
    // not in the known set collapses to a safe sentinel so a hostile
    // server JSON `tool` field can't smuggle attributes/markup into
    // the resulting <span class="pill ...">${tool}</span>. Mirrors
    // _safeTool in skills.js (defined there but kept local here to
    // stay robust against script-load-order changes).
    function _jobsSafeTool(t) {
      return ({ "claude": "claude", "codex": "codex" }[t] || "unknown");
    }

    function statusPill(status) {
      const cls = ["running","queued","cancelling","cancelled","done"].includes(status)
        ? status
        : (status === "failed" ? "bad" : "");
      return `<span class="pill ${cls}">${escape(status)}</span>`;
    }

    // Module-local: parse an ISO timestamp string, returning NaN for any
    // invalid / missing input. Avoids NaN-propagation noise spreading
    // from Date.parse(undefined) or Date.parse("garbage") downstream.
    function _safeParseDate(s) {
      if (!s) return NaN;
      const n = Date.parse(s);
      return Number.isFinite(n) ? n : NaN;
    }

    async function loadJobs(opts) {
      if (_jobsLoadInFlight) { _jobsLoadInFlight = "pending"; return; }
      _jobsLoadInFlight = true;
      opts = opts || {};
      // 30s timeout — a hung backend would otherwise wedge the in-flight
      // guard forever and block every subsequent poll.
      const ctrl = (typeof AbortController !== "undefined") ? new AbortController() : null;
      const timer = ctrl ? setTimeout(() => { try { ctrl.abort(); } catch (_) {} }, 30000) : null;
      try {
        const r = await fetch("/api/jobs", { cache: "no-store", signal: ctrl ? ctrl.signal : undefined });
        if (!r.ok) throw new Error("HTTP " + r.status);
        const data = await r.json();
        const jobs = data.jobs || [];
        const countJobsEl = $("#count-jobs");
        if (countJobsEl) countJobsEl.textContent = jobs.length;
        // If the previously selected job vanished (pruned, cleared events,
        // server restart), clear local state so we don't keep fetching a
        // dead id and rendering "HTTP 404" into the doc panel.
        if (_selectedJobId && !jobs.find((j) => j.id === _selectedJobId)) {
          _selectedJobId = null;
          const doc = $("#jobs-doc");
          if (doc) doc.innerHTML = `<div class="empty">(select a job)</div>`;
        }
        const el = $("#jobs-list");
        // A11Y: announce dynamic list changes politely to screen readers.
        // Idempotent — set once; subsequent polls won't re-touch attributes.
        if (el && !el.getAttribute("aria-live")) {
          el.setAttribute("aria-live", "polite");
          el.setAttribute("aria-relevant", "additions");
        }
        // Early-return if #jobs-list is missing — every later branch derefs `el`
        // (`.dataset`, `.innerHTML`, `.children`, `.addEventListener`).
        // The previous shape only guarded the aria-live block, then null-derefed
        // `delete el.dataset.skeletoned` on the next line.
        if (!el) return;
        delete el.dataset.skeletoned;
        if (!jobs.length) {
          el.innerHTML = `<div class="empty"><strong>No jobs yet.</strong><br><span class="empty-sub">Pick <em>Chat</em> for an interactive Claude/Codex session, or <em>Workflow</em> to run plan/orchestrate in the background.</span></div>`;
        } else {
          // Build row HTML strings (same template as before).
          const rows = jobs.map((j) => {
            const taskPreview = (j.task || "").replace(/\s+/g, " ");
            const tool = j.tool || (j.kind === "chat-codex" ? "codex" : "claude");
            const ts = j.created_at ? relativeTime(j.created_at) : "";
            let dur = "";
            if (j.status === "running" && j.started_at) {
              dur = tlFormatDuration(Date.now() - _safeParseDate(j.started_at));
            } else if (j.ended_at && j.started_at) {
              dur = tlFormatDuration(_safeParseDate(j.ended_at) - _safeParseDate(j.started_at));
            }
            const metaParts = [];
            if (ts) metaParts.push(`<span>${escape(ts)}</span>`);
            if (dur) metaParts.push(`<span>${escape(dur)}</span>`);
            const inner = `<div class="job-row-head">${statusPill(j.status)} ${pillTool(_jobsSafeTool(tool))} <span class="job-row-kind">${escape((j.kind || "").toUpperCase())}</span></div>
              <div class="sub" style="margin-top:4px;white-space:normal">${escape(taskPreview.slice(0, JOB_TASK_PREVIEW_LEN))}${taskPreview.length > JOB_TASK_PREVIEW_LEN ? "…" : ""}</div>
              ${metaParts.length ? `<div class="job-row-meta">${metaParts.join(" · ")}</div>` : ""}`;
            return { id: j.id, inner };
          });
          // Compare current child id sequence with new ids.
          let existing = null;
          let sameSet = false;
          if (el.children.length === rows.length) {
            sameSet = rows.every((row, i) => {
              const node = el.children[i];
              return node?.classList?.contains("list-item") && node.dataset.id === row.id;
            });
            if (sameSet) existing = el.children;
          }
          if (!sameSet) {
            existing = Array.from(el.children).filter((c) => c.classList.contains("list-item"));
            sameSet = existing.length === rows.length
              && existing.every((node, i) => node.dataset.id === rows[i].id);
          }

          if (sameSet) {
            // Update only inner content of each row - preserves the outer DIVs,
            // their focus/scroll state, and the .active class.
            rows.forEach((r, i) => { existing[i].innerHTML = r.inner; });
          } else {
            // ID set changed (jobs added/removed/reordered) - rebuild.
            const tpl = document.createElement("template");
            // A11Y: tabindex + role="button" so keyboard users can focus
            // and activate rows (Enter / Space — handled in delegation).
            tpl.innerHTML = rows.map((r) => `<div class="list-item" data-id="${escape(r.id)}" tabindex="0" role="button">${r.inner}</div>`).join("");
            el.replaceChildren(...tpl.content.children);
          }

          // Re-apply active class to the selected row (if any).
          el.querySelectorAll(".list-item").forEach((li) => {
            li.classList.toggle("active", li.dataset.id === _selectedJobId);
          });

          // Wire ONE delegated click + keydown listener (idempotent).
          if (!_jobsListDelegationWired) {
            el.addEventListener("click", (e) => {
              const li = e.target.closest(".list-item");
              if (!li || !el.contains(li)) return;
              _selectedJobId = li.dataset.id;
              el.querySelectorAll(".list-item").forEach((x) => x.classList.remove("active"));
              li.classList.add("active");
              loadJobDetail();
            });
            // A11Y: Enter / Space on a focused .list-item re-triggers the
            // click handler above. preventDefault on Space avoids the page
            // scrolling while the row has focus.
            el.addEventListener("keydown", (e) => {
              if (e.key !== "Enter" && e.key !== " ") return;
              const li = e.target.closest(".list-item");
              if (!li || !el.contains(li)) return;
              e.preventDefault();
              li.click();
            });
            _jobsListDelegationWired = true;
          }
        }
        // Feed the Terminals status list (no auto-open — the operator launches
        // and opens panes manually; the canvas owns interactive panes).
        termRefreshPicker(jobs);

        // Background poll if any job is running and a relevant tab is visible.
        const anyRunning = jobs.some((j) => j.status === "running" || j.status === "queued" || j.status === "cancelling");
        // Optional chaining so a stripped shell (no #view-run / #view-terminals)
        // doesn't null-deref `.classList` and abort the polling-scheduler block.
        const runTabActive = !!$("#view-run")?.classList.contains("active");
        const termsTabActive = !!$("#view-terminals")?.classList.contains("active");
        if (_jobsTimer) { clearTimeout(_jobsTimer); _jobsTimer = null; }
        if (anyRunning && (runTabActive || termsTabActive)) {
          _jobsTimer = setTimeout(loadJobs, 2000);
        } else if (termsTabActive) {
          // Even with nothing running, keep polling on the Terminals view so
          // newly-launched sessions / externally-started chats appear in the
          // status list without a manual reload.
          _jobsTimer = setTimeout(loadJobs, 4000);
        } else if (runTabActive) {
          // Background poll at slower cadence so externally-started jobs
          // appear on the Run tab without requiring a manual reload.
          _jobsTimer = setTimeout(loadJobs, 15000);
        }
        // Refresh open job's log too
        if (_selectedJobId && runTabActive) loadJobDetail();
      } catch (e) {
        // Null-guard #jobs-list — a missing element would mask the real
        // failure with a fresh TypeError and the operator-visible toast
        // below would never fire.
        const jobsListEl = $("#jobs-list");
        if (jobsListEl) jobsListEl.innerHTML = `<div class="err">${escape(e.message)}</div>`;
        setMsg("#jobs-load", "err", "Jobs load failed: " + e.message);
      } finally {
        if (timer) clearTimeout(timer);
        const wasPending = _jobsLoadInFlight === "pending";
        _jobsLoadInFlight = false;
        if (wasPending) {
          // Cancel the freshly-armed background poll before scheduling
          // the immediate retry — otherwise both timers fire and we
          // race two concurrent loadJobs calls (the in-flight guard
          // catches it but only after two awaits already started).
          if (_jobsTimer) { clearTimeout(_jobsTimer); _jobsTimer = null; }
          setTimeout(loadJobs, 0);
        }
      }
    }

    async function loadJobDetail() {
      if (!_selectedJobId) return;
      // Every branch derefs #jobs-doc; bail when the run view is stripped so
      // missing-DOM doesn't bury the actual HTTP error.
      const docEl = $("#jobs-doc");
      if (!docEl) return;
      // Pin the selection at fetch start so a fresh click on a different job
      // while we're awaiting can't write stale content into the panel.
      const requestedJobId = _selectedJobId;
      try {
        const r = await fetch("/api/jobs/" + requestedJobId + "?tail=" + JOB_LOG_TAIL_LINES, { cache: "no-store" });
        if (requestedJobId !== _selectedJobId) return;
        if (!r.ok) {
          docEl.innerHTML = `<div class="err">HTTP ${r.status}</div>`;
          return;
        }
        const j = await r.json();
        if (requestedJobId !== _selectedJobId) return;
        const cancelable = j.status === "running" || j.status === "queued";
        const timeParts = [];
        if (j.created_at) timeParts.push(`<span class="job-time-k">created</span> ${escape(j.created_at)}`);
        if (j.started_at) timeParts.push(`<span class="job-time-k">started</span> ${escape(j.started_at)}`);
        if (j.ended_at)   timeParts.push(`<span class="job-time-k">ended</span> ${escape(j.ended_at)}`);
        const prevLog = $("#job-log");
        const wasAtBottom = prevLog ? (prevLog.scrollHeight - prevLog.scrollTop - prevLog.clientHeight < JOB_LOG_BOTTOM_SLOP_PX) : true;
        docEl.innerHTML = `
          <div class="job-head">
            <div class="job-status">${statusPill(j.status)} ${j.exit_code != null ? `<span class="job-exit">exit ${j.exit_code}</span>` : ""} <span class="job-row-kind">${escape((j.kind || "").toUpperCase())}</span></div>
            <h3 class="job-task">${escape(j.task || "(no task)")}</h3>
            <div class="job-times">${timeParts.join(" · ") || "—"}</div>
          </div>
          <details class="job-cmd">
            <summary>command</summary>
            <code class="mono">${escape(j.command || "—")}</code>
          </details>
          <div class="job-id-row"><span class="job-time-k">id</span> <span class="mono">${escape(j.id)}</span></div>
          <div style="margin-bottom:6px;font-size:11px;color:var(--fg-dim);text-transform:uppercase;letter-spacing:0.5px">log (last ${JOB_LOG_TAIL_LINES} lines)</div>
          <pre class="log" id="job-log">${escape(j.log_tail || "(no output yet)")}</pre>
          <div class="form-actions" style="margin-top:10px">
            ${cancelable ? `<button class="btn danger" data-job-cancel="${escape(j.id)}" aria-label="Cancel job ${escape(j.id)}" title="Cancel this job">Cancel job</button>` : ""}
            <span class="form-msg" id="job-action-msg"></span>
          </div>
        `;
        if (!_jobsDocDelegationWired) {
          // docEl resolved at function top; null-check already enforced via early-return.
          docEl.addEventListener("click", (e) => {
            const btn = e.target.closest("[data-job-cancel]");
            if (!btn) return;
            cancelJob(btn.dataset.jobCancel, e);
          });
          _jobsDocDelegationWired = true;
        }
        // Auto-scroll log
        const log = $("#job-log");
        if (log && wasAtBottom) log.scrollTop = log.scrollHeight;
      } catch (e) {
        // docEl resolved at top of the function — reuse it here.
        if (requestedJobId !== _selectedJobId) return;
        docEl.innerHTML = `<div class="err">${escape(e.message)}</div>`;
      }
    }

    async function submitJob() {
      const btn = $("#run-submit");
      const kindEl = $("#run-kind");
      const taskEl = $("#run-task");
      // Bail when any required form element is missing — the previous shape
      // null-derefed `.value` on a fresh lookup and aborted whichever caller
      // invoked us.
      if (!btn || !kindEl || !taskEl) return;
      const kind = kindEl.value;
      const task = taskEl.value.trim();
      const sessionPick = ($("#run-resume")?.value || "").trim() || undefined;
      const wantFork = !!$("#run-fork")?.checked;
      const resume_session_id = sessionPick && !wantFork ? sessionPick : undefined;
      const fork_session_id = sessionPick && wantFork ? sessionPick : undefined;
      const permission_mode = ($("#run-permission")?.value || "").trim() || undefined;
      const tagsRaw = ($("#run-tags")?.value || "").trim();
      const tags = tagsRaw
        ? tagsRaw.split(",").map((t) => t.trim().toLowerCase()).filter(Boolean)
        : undefined;
      const compare = !!$("#run-compare")?.checked;
      if (!task) { setMsg("#run-msg", "err", "task is required"); return; }
      btn.disabled = true;
      setMsg("#run-msg", "", "starting…");
      const basePayload = { kind, task };
      if (resume_session_id) basePayload.resume_session_id = resume_session_id;
      if (fork_session_id) basePayload.fork_session_id = fork_session_id;
      if (permission_mode) basePayload.permission_mode = permission_mode;
      if (tags) basePayload.tags = tags;
      try {
        const res = await postJson("/api/jobs", basePayload);
        setMsg("#run-msg", "ok", "job " + res.id.slice(0, 8) + " started", 4000);
        _selectedJobId = res.id;
        // Compare side-by-side: also spin up the same task on the other tool.
        // The result is intentionally not bound — the Terminals tab is a status
        // list (Chunk 5b-1), so the compare job surfaces as its own row via the
        // loadJobs()/loadSessions() refresh below; nothing reads the response.
        if (compare && (kind === "chat" || kind === "chat-codex")) {
          const otherKind = kind === "chat" ? "chat-codex" : "chat";
          try {
            await postJson("/api/jobs", { ...basePayload, kind: otherKind, resume_session_id: undefined });
          } catch (cmpErr) {
            setMsg("#run-msg", "warn", "compare job failed: " + cmpErr.message, 4000);
          }
        }
        taskEl.value = "";
        // Chat jobs are most useful in the Terminals view. Switch tabs
        // BEFORE refreshing the job list so loadJobs() sees runTab inactive
        // and skips loadJobDetail() — otherwise we render the doc panel
        // into a now-hidden Run tab (flash + wasted work + race).
        const isChat = kind === "chat" || kind === "chat-codex";
        if (isChat) {
          const navBtn = document.querySelector('nav button[data-view="terminals"]');
          if (navBtn) navBtn.click();
        }
        await loadJobs();
        await loadSessions();
        // The Terminals tab is a status list now (Chunk 5b-1): there is no
        // inline pane to open. Switching tabs + the loadJobs()/loadSessions()
        // refresh above already surface the new chat as a status row. The
        // operator routes it to the canvas via the row's send-to-canvas (⊞)
        // control. We deliberately do NOT auto-open the canvas popup — a
        // window.open() after an await is commonly blocked and surprising.
      } catch (e) {
        setMsg("#run-msg", "err", e.message);
      } finally {
        btn.disabled = false;
      }
    }

    async function loadSessions() {
      const sel = $("#run-resume");
      if (!sel) return;
      const prev = sel.value;
      const kind = $("#run-kind")?.value || "chat";
      try {
        // Dashboard-spawned sessions and IDE-spawned transcripts live in
        // two different stores. The Resume picker should offer both so the
        // user can continue any chat — including ones started from the
        // VSCode/Cursor extension.
        const [dashRes, ideRes] = await Promise.all([
          fetch("/api/sessions", { cache: "no-store" }),
          // Only Claude transcripts exist on disk; if Codex is selected we
          // don't need transcript data, but the fetch is cheap.
          fetch("/api/transcripts", { cache: "no-store" }),
        ]);
        const dashData = dashRes.ok ? await dashRes.json() : { sessions: [] };
        const ideData = ideRes.ok ? await ideRes.json() : { transcripts: [] };
        const dashSessions = (dashData.sessions || []).filter((s) => s.kind === kind);
        // IDE transcripts are Claude-only; only relevant when kind === "chat".
        const ideSessions = (kind === "chat") ? (ideData.transcripts || []) : [];

        // Fingerprint so polls that return the same session list skip the
        // innerHTML rebuild. Key fields: id + ended_at for dashboard chats
        // (started_at doesn't change but ended_at flips when the chat
        // finishes) and id + modified for IDE transcripts (mtime advances
        // when the user replies, so the timestamp captures activity).
        const fp = kind + "|" + String(dashSessions.length) + ":"
          + dashSessions.map((s) => (s.session_id || "") + "@" + (s.ended_at || "")).join(",")
          + "|" + String(ideSessions.length) + ":"
          + ideSessions.map((s) => (s.session_id || "") + "@" + (s.modified || "")).join(",");
        const changed = (typeof window.renderIfChanged === "function")
          ? window.renderIfChanged(sel, fp, () => {
              const parts = [`<option value="">— new session —</option>`];
              if (dashSessions.length) {
                parts.push(`<optgroup label="Dashboard chats">`);
                for (const s of dashSessions) {
                  const preview = (s.task || "").replace(/\s+/g, " ").slice(0, 60);
                  const when = s.started_at ? s.started_at.slice(11, 16) : "—";
                  parts.push(`<option value="${escape(s.session_id)}">[${escape(when)}] ${escape(preview)}</option>`);
                }
                parts.push(`</optgroup>`);
              }
              if (ideSessions.length) {
                parts.push(`<optgroup label="IDE chats (this repo)">`);
                for (const s of ideSessions) {
                  const when = s.modified ? s.modified.slice(5, 16).replace("T", " ") : "—";
                  const preview = (s.task || "").replace(/\s+/g, " ").slice(0, 60)
                    || `(${(s.session_id || "").slice(0, 8)})`;
                  parts.push(`<option value="${escape(s.session_id)}">[${escape(when)}] ${escape(preview)}</option>`);
                }
                parts.push(`</optgroup>`);
              }
              sel.innerHTML = parts.join("");
            })
          : true;
        // Preserve selection if it survived the refresh. Always re-apply
        // even on skip, because some other code path (e.g. the user
        // clicking an option) may have changed sel.value since last poll.
        const allIds = new Set([
          ...dashSessions.map((s) => s.session_id),
          ...ideSessions.map((s) => s.session_id),
        ]);
        if (prev && allIds.has(prev)) sel.value = prev;
        void changed;  // keep symbol for future telemetry hooks
      } catch (e) {
        // Network failure or malformed response — surface to the console so
        // operators can diagnose, and drop an "(error)" placeholder into the
        // dropdown so users don't keep staring at stale options.
        console.warn("[dashboard] loadSessions failed:", e.message || e);
        sel.innerHTML = `<option value="">— (error) —</option>`;
      }
    }

    // ----- Run-mode UI helpers -----
    function updateRunHint() {
      const hint = document.getElementById("run-hint");
      if (!hint) return;
      const kind = document.getElementById("run-kind")?.value || "chat";
      const map = {
        "chat":        "Opens an interactive Claude terminal panel — you can chat back and forth.",
        "chat-codex":  "Opens an interactive Codex panel — each follow-up spawns <code>codex exec resume</code> behind the scenes.",
        "orchestrate": "Runs the orchestrate skill in the background: plan → execute → review. The subprocess cannot prompt — it must emit <code>## Escalation</code> if blocked.",
        "plan":        "Runs the planner skill in the background. Produces an execution packet but does not implement.",
      };
      hint.innerHTML = map[kind] || map["chat"];
    }

    function applyRunMode(mode) {
      mode = mode || "chat";
      const sel = document.getElementById("run-kind");
      if (!sel) return;
      let firstVisible = null;
      Array.from(sel.options).forEach((opt) => {
        const optMode = opt.dataset.mode || "chat";
        opt.hidden = optMode !== mode;
        if (!opt.hidden && firstVisible == null) firstVisible = opt;
      });
      const cur = sel.options[sel.selectedIndex];
      if ((!cur || cur.hidden) && firstVisible) sel.value = firstVisible.value;
      const form = document.querySelector("#view-run .run-form");
      if (form) form.classList.toggle("is-workflow", mode === "workflow");
      document.querySelectorAll(".run-mode-tab").forEach((b) => {
        const active = b.dataset.runMode === mode;
        b.classList.toggle("active", active);
        b.setAttribute("aria-pressed", active ? "true" : "false");
      });
      updateRunHint();
      if (typeof loadSessions === "function") loadSessions();
    }

    function applyResumeState() {
      const resume = document.getElementById("run-resume")?.value || "";
      const wrap = document.getElementById("run-checks-wrap");
      if (wrap) wrap.classList.toggle("has-resume", !!resume);
    }

    document.addEventListener("DOMContentLoaded", () => {
      $("#run-kind")?.addEventListener("change", () => { loadSessions(); updateRunHint(); });
      document.querySelectorAll(".run-mode-tab").forEach((btn) => {
        btn.addEventListener("click", () => applyRunMode(btn.dataset.runMode));
      });
      $("#run-resume")?.addEventListener("change", applyResumeState);
      applyRunMode("chat");
      applyResumeState();
    });

    async function cancelJob(jobId, event) {
      if (!confirm("Cancel job " + String(jobId).slice(0, 8) + "? This sends SIGTERM to the subprocess.")) return;
      const btn = (typeof event !== "undefined" && event?.target?.closest?.("button[data-job-cancel]")) || null;
      if (btn) btn.disabled = true;
      try {
        await postJson("/api/jobs/" + jobId + "/cancel", {});
        setMsg("#job-action-msg", "ok", "cancellation requested", 4000);
        await loadJobs();
      } catch (e) {
        setMsg("#job-action-msg", "err", e.message);
      } finally {
        if (btn) btn.disabled = false;
      }
    }

    // ----- events -----
    function relativeTime(iso) {
      // Guard against future-dated stamps (clock skew between client
      // and server, or jobs spawned with a forward-clock event log).
      // Without Math.max(0, ...) we render "-3s ago" or similar — and
      // negative input also confuses the cadence buckets below. NaN
      // (from `new Date("garbage")`) also collapses to 0 / "just now".
      const raw = (Date.now() - new Date(iso).getTime()) / 1000;
      const diff = Math.max(0, Number.isFinite(raw) ? raw : 0);
      if (diff < 60) return Math.floor(diff) + "s ago";
      if (diff < 3600) return Math.floor(diff / 60) + "m ago";
      if (diff < 86400) return Math.floor(diff / 3600) + "h ago";
      return Math.floor(diff / 86400) + "d ago";
    }

    function tlFormatDuration(ms) {
      if (ms == null || isNaN(ms) || ms < 0) return "—";
      if (ms < 1000) return `${ms}ms`;
      if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`;
      const m = Math.floor(ms / 60_000);
      const s = Math.round((ms % 60_000) / 1000);
      if (ms < 3_600_000) return s ? `${m}m ${s}s` : `${m}m`;
      const h = Math.floor(ms / 3_600_000);
      const mr = Math.floor((ms % 3_600_000) / 60_000);
      return mr ? `${h}h ${mr}m` : `${h}h`;
    }

    function _tlBannerHtml(sid) {
      if (!sid) return "";
      return `<div class="tl-filter-banner">Filtered to session <code>${escape(sid.slice(0, 8))}</code> · <button id="tl-clear-filter" type="button">clear</button></div>`;
    }

    function renderTimelineSkeletons() {
      const chart = $("#timeline-chart");
      if (!chart || chart.dataset.skeletoned) return;
      chart.innerHTML = Array.from({ length: 5 }).map(() => `
        <div class="skeleton-timeline-row">
          <div>
            <span class="skeleton skeleton-tl-label"></span>
            <span class="skeleton skeleton-tl-meta"></span>
          </div>
          <span class="skeleton skeleton-tl-track"></span>
        </div>
      `).join("");
      chart.dataset.skeletoned = "1";
    }

    var TL_STATE_KEY = "dashboard.timeline.state.v1";
    var _tlCache = [];
    var _tlState = _tlDefaultState();

    function _tlDefaultState() {
      return { range: "24h", phases: new Set(), tool: "", status: "", sort: "newest", search: "" };
    }

    function _tlReadState() {
      const state = _tlDefaultState();
      try {
        const raw = localStorage.getItem(TL_STATE_KEY);
        if (!raw) return state;
        const saved = JSON.parse(raw);
        if (["24h", "7d", "all"].includes(saved.range)) state.range = saved.range;
        if (Array.isArray(saved.phases)) state.phases = new Set(saved.phases.filter(Boolean));
        if (typeof saved.tool === "string") state.tool = saved.tool;
        if (typeof saved.status === "string") state.status = saved.status;
        if (["newest", "oldest", "duration", "failures"].includes(saved.sort)) state.sort = saved.sort;
        if (typeof saved.search === "string") state.search = saved.search;
      } catch (_) {}
      return state;
    }

    function _tlPersistState() {
      try {
        localStorage.setItem(TL_STATE_KEY, JSON.stringify({
          range: _tlState.range,
          phases: Array.from(_tlState.phases),
          tool: _tlState.tool,
          status: _tlState.status,
          sort: _tlState.sort,
          search: _tlState.search,
        }));
      } catch (_) {}
    }

    function _tlRangeMs(range) {
      if (range === "24h") return 24 * 3600 * 1000;
      if (range === "7d") return 7 * 24 * 3600 * 1000;
      return Infinity;
    }

    function _tlPhaseName(phase) {
      return (!phase || phase === "unknown") ? "untagged" : phase;
    }

    function _tlPhaseStatus(ph) {
      if (["success", "failure", "pending"].includes(ph.status)) return ph.status;
      if (ph.exit_code === 0) return "success";
      if (ph.exit_code == null) return "pending";
      return "failure";
    }

    function _tlRunBounds(run) {
      let start = _safeParseDate(run.started_at);
      let end = _safeParseDate(run.ended_at);
      const phases = Array.isArray(run.phases) ? run.phases : [];
      for (const ph of phases) {
        const phEnd = _safeParseDate(ph.end_ts);
        const dur = (typeof ph.duration_ms === "number" && ph.duration_ms >= 0) ? ph.duration_ms : 0;
        if (Number.isFinite(phEnd)) {
          const phStart = phEnd - dur;
          start = Number.isFinite(start) ? Math.min(start, phStart) : phStart;
          end = Number.isFinite(end) ? Math.max(end, phEnd) : phEnd;
        }
      }
      if (!Number.isFinite(start)) start = Date.now();
      if (!Number.isFinite(end) || end < start) {
        const fallback = (typeof run.total_duration_ms === "number" && run.total_duration_ms >= 0) ? run.total_duration_ms : 1000;
        end = start + Math.max(1000, fallback);
      }
      return { start, end, span: Math.max(1000, end - start) };
    }

    function _tlAllPhases(runs) {
      const names = new Set();
      for (const run of runs) {
        for (const ph of (run.phases || [])) names.add(_tlPhaseName(ph.phase));
      }
      return Array.from(names).sort();
    }

    function _tlRunMatchesFilters(run) {
      const phases = run.phases || [];
      const bounds = _tlRunBounds(run);
      const rangeMs = _tlRangeMs(_tlState.range);
      if (rangeMs !== Infinity && (Date.now() - bounds.end) > rangeMs) return false;
      if (_tlState.phases.size && !phases.some((ph) => _tlState.phases.has(_tlPhaseName(ph.phase)))) return false;
      if (_tlState.tool && !phases.some((ph) => ph.tool === _tlState.tool)) return false;
      if (_tlState.status && !phases.some((ph) => _tlPhaseStatus(ph) === _tlState.status)) return false;
      if (_tlState.search) {
        const needle = _tlState.search.toLowerCase();
        const hay = [
          run.session_id || "",
          run.task || "",
          run.tag || "",
          phases.map((ph) => `${_tlPhaseName(ph.phase)} ${ph.tool || ""} ${ph.model || ""}`).join(" "),
        ].join(" ").toLowerCase();
        if (!hay.includes(needle)) return false;
      }
      return true;
    }

    function _tlSortRuns(runs) {
      const sorted = runs.slice();
      sorted.sort((a, b) => {
        if (_tlState.sort === "oldest") return _tlRunBounds(a).start - _tlRunBounds(b).start;
        if (_tlState.sort === "duration") return _tlRunBounds(b).span - _tlRunBounds(a).span;
        if (_tlState.sort === "failures") {
          const af = (a.phases || []).filter((ph) => _tlPhaseStatus(ph) === "failure").length;
          const bf = (b.phases || []).filter((ph) => _tlPhaseStatus(ph) === "failure").length;
          return bf - af || (_tlRunBounds(b).start - _tlRunBounds(a).start);
        }
        return _tlRunBounds(b).start - _tlRunBounds(a).start;
      });
      return sorted;
    }

    function _tlSetText(id, value) {
      const el = $(id);
      if (el) el.textContent = value;
    }

    function _tlRenderAxis(start, span) {
      const axis = $("#tl-axis");
      if (!axis) return;
      axis.innerHTML = [0, 0.25, 0.5, 0.75, 1].map((pct) => {
        const d = new Date(start + span * pct);
        const label = span > 24 * 3600 * 1000
          ? d.toLocaleDateString(undefined, { month: "short", day: "numeric" })
          : d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
        return `<span style="left:${(pct * 100).toFixed(2)}%">${escape(label)}</span>`;
      }).join("");
    }

    function _tlRenderKpi(runs, start, span) {
      const phases = runs.flatMap((run) => run.phases || []);
      const failed = phases.filter((ph) => _tlPhaseStatus(ph) === "failure").length;
      const pending = phases.filter((ph) => _tlPhaseStatus(ph) === "pending").length;
      const totalMs = runs.reduce((sum, run) => sum + Math.max(0, _tlRunBounds(run).span), 0);
      _tlSetText("#tl-kpi-sessions", String(runs.length));
      _tlSetText("#tl-kpi-phases", String(phases.length));
      _tlSetText("#tl-kpi-failed", String(failed));
      _tlSetText("#tl-kpi-pending", String(pending));
      _tlSetText("#tl-kpi-duration", tlFormatDuration(totalMs));
      _tlSetText("#tl-kpi-window", runs.length ? tlFormatDuration(span) : "0s");
    }

    function _tlRenderSparkline(runs, start, span) {
      const svg = $("#tl-sparkline");
      if (!svg) return;
      const bucketCount = 24;
      const buckets = Array.from({ length: bucketCount }, () => ({ total: 0, failed: 0 }));
      for (const run of runs) {
        for (const ph of (run.phases || [])) {
          const end = _safeParseDate(ph.end_ts);
          if (!Number.isFinite(end)) continue;
          const idx = Math.max(0, Math.min(bucketCount - 1, Math.floor(((end - start) / Math.max(1, span)) * bucketCount)));
          buckets[idx].total += 1;
          if (_tlPhaseStatus(ph) === "failure") buckets[idx].failed += 1;
        }
      }
      const max = Math.max(1, ...buckets.map((b) => b.total));
      const w = 240 / bucketCount;
      svg.innerHTML = buckets.map((b, i) => {
        if (b.total === 0) return "";
        const h = Math.max(2, (b.total / max) * 34);
        const failH = b.failed ? Math.max(2, (b.failed / max) * 34) : 0;
        const x = i * w;
        return `<rect class="tl-spark-total" x="${x.toFixed(2)}" y="${(38 - h).toFixed(2)}" width="${Math.max(1, w - 2).toFixed(2)}" height="${h.toFixed(2)}"></rect>`
          + (failH ? `<rect class="tl-spark-fail" x="${x.toFixed(2)}" y="${(38 - failH).toFixed(2)}" width="${Math.max(1, w - 2).toFixed(2)}" height="${failH.toFixed(2)}"></rect>` : "");
      }).filter(Boolean).join("");
    }

    function _tlRenderPhaseStrip(phases) {
      const icons = { plan: "P", execute: "E", review: "R", rescue: "!", maintenance: "M", unknown: "?", untagged: "?" };
      const names = phases.map(_tlPhaseName);
      const tagged = names.filter((p) => p !== "untagged");
      const untagged = names.length - tagged.length;
      const visible = tagged.slice(0, 4).map((p) => {
        const icon = icons[p] || p.slice(0, 1).toUpperCase();
        return `<span title="${escape(p)}"><b>${escape(icon)}</b>${escape(p)}</span>`;
      });
      if (tagged.length > 4) visible.push(`<span title="${escape(tagged.slice(4).join(", "))}">+${tagged.length - 4}</span>`);
      if (untagged) visible.push(`<span title="phase not detected">+${untagged} untagged</span>`);
      if (!visible.length) return "";
      return `<div class="tl-phase-strip">${visible.join("")}</div>`;
    }

    function _tlRenderRows(runs, globalStart, globalSpan, bannerHtml) {
      const chart = $("#timeline-chart");
      if (!chart) return;
      const filterSid = window._timelineSessionFilter || null;
      chart.classList.toggle("tl-many", runs.length > 8);
      if (!runs.length) {
        const emptyFp = "empty|" + (filterSid || "") + "|" + bannerHtml.length;
        if (typeof window.renderIfChanged === "function") {
          window.renderIfChanged(chart, emptyFp, () => {
            chart.innerHTML = bannerHtml + (filterSid
              ? `<div class="tl-empty">No runs match session <code>${escape(filterSid.slice(0, 8))}</code>. Clear the filter to see all runs.</div>`
              : `<div class="tl-empty">No pipeline runs match the current filters.</div>`);
          });
        } else {
          chart.innerHTML = bannerHtml + (filterSid
            ? `<div class="tl-empty">No runs match session <code>${escape(filterSid.slice(0, 8))}</code>. Clear the filter to see all runs.</div>`
            : `<div class="tl-empty">No pipeline runs match the current filters.</div>`);
        }
        return;
      }
      // Fingerprint built from identity-bearing fields + per-row durations,
      // so a pure auto-refresh of an idle pipeline (no running phases)
      // skips the entire SVG/innerHTML rebuild. Includes phase count +
      // total_duration_ms so live runs (whose duration ticks each poll)
      // correctly invalidate. globalStart/globalSpan are folded in because
      // an axis rescale must always re-render the bars.
      const fp = "rows|" + (filterSid || "") + "|" + globalStart + ":" + globalSpan
        + "|" + runs.length + ":"
        + runs.map((r) => (r.session_id || "")
            + "/" + (r.ended_at || "live")
            + "/" + ((r.phases || []).length)
            + "/" + (r.total_duration_ms || 0)).join(",");
      if (typeof window.renderIfChanged === "function" && !window.renderIfChanged(chart, fp, () => _tlBuildRowsHtml(runs, globalStart, globalSpan, bannerHtml, filterSid, chart))) {
        return;
      }
      if (typeof window.renderIfChanged !== "function") {
        _tlBuildRowsHtml(runs, globalStart, globalSpan, bannerHtml, filterSid, chart);
      }
    }

    function _tlBuildRowsHtml(runs, globalStart, globalSpan, bannerHtml, filterSid, chart) {
      chart.innerHTML = bannerHtml + runs.map((run) => {
        const phases = run.phases || [];
        const bars = phases.map((ph) => {
          const status = _tlPhaseStatus(ph);
          const phEnd = _safeParseDate(ph.end_ts);
          const phDur = (typeof ph.duration_ms === "number" && ph.duration_ms >= 0) ? ph.duration_ms : 0;
          const phStart = Number.isFinite(phEnd) ? phEnd - phDur : _tlRunBounds(run).start;
          const leftPct = Math.max(0, Math.min(100, ((phStart - globalStart) / globalSpan) * 100));
          const widthPct = Math.max(0.8, Math.min(100 - leftPct, (Math.max(1, phDur) / globalSpan) * 100));
          const displayLabel = _tlPhaseName(ph.phase);
          const cls = displayLabel === "untagged" ? `${status} unknown-phase` : status;
          const exitText = ph.exit_code == null ? "?" : String(ph.exit_code);
          const tip = `${displayLabel} - ${ph.tool || "unknown"}/${ph.model || "unknown"} - exit ${exitText} - ${tlFormatDuration(ph.duration_ms)}`;
          const narrow = widthPct < 6 ? ` data-narrow="1"` : "";
          return `<div class="tl-bar ${cls}" title="${escape(tip)}"${narrow} `
            + `style="left:${leftPct.toFixed(2)}%;width:${widthPct.toFixed(2)}%">`
            + `${escape(displayLabel)}</div>`;
        }).join("");
        const sid = run.session_id || "unknown";
        const sidShort = sid.slice(0, 8);
        const when = run.started_at ? new Date(run.started_at).toLocaleString() : "";
        const totalDur = tlFormatDuration(run.total_duration_ms);
        const rowClasses = ["tl-row"];
        if (filterSid && run.session_id === filterSid) rowClasses.push("tl-row-highlight");
        if (phases.some((ph) => _tlPhaseStatus(ph) === "pending") || !run.ended_at) rowClasses.push("live");
        const taskHtml = run.task
          ? `<div class="tl-task" title="${escape(run.task)}">${escape(run.task)}</div>`
          : `<div class="tl-task dim">(no transcript - session file unavailable)</div>`;
        return `<div class="${rowClasses.join(" ")}">`
          + `<div class="tl-label">`
          +   `<button class="tl-row-copy" type="button" data-tl-copy-sid="${escape(sid)}" aria-label="Copy session id ${escape(sidShort)}" title="copy session id">copy</button>`
          +   taskHtml
          +   _tlRenderPhaseStrip(phases.map((ph) => ph.phase))
          +   `<div class="tl-meta">`
          +     `<span class="tl-tag" title="primary tool/model for this session">${escape(run.tag || "-")}</span>`
          +     `<span class="tl-dur" title="total wall-clock duration">${escape(totalDur)}</span>`
          +     `<span>${phases.length} bar${phases.length === 1 ? "" : "s"}</span>`
          +     `<span>${escape(when)}</span>`
          +     `<span class="tl-sid" title="${escape(sid)}">${escape(sidShort)}</span>`
          +   `</div>`
          + `</div>`
          + `<div class="tl-track">${bars}</div>`
          + `</div>`;
      }).join("");
    }

    function _tlRefreshPhaseChips(phases) {
      const el = $("#tl-phase-chips");
      if (!el) return;
      el.innerHTML = phases.map((phase) => {
        const on = _tlState.phases.has(phase);
        return `<button type="button" class="tl-chip${on ? " active" : ""}" aria-pressed="${on ? "true" : "false"}" data-tl-phase="${escape(phase)}">${escape(phase)}</button>`;
      }).join("") || `<span class="tl-chip-empty">no phases</span>`;
    }

    function _tlSyncToolbar(phases) {
      const range = $("#tl-range");
      const tool = $("#tl-tool");
      const status = $("#tl-status");
      const sort = $("#tl-sort");
      const search = $("#tl-search");
      if (range) range.value = _tlState.range;
      if (tool) tool.value = _tlState.tool;
      if (status) status.value = _tlState.status;
      if (sort) sort.value = _tlState.sort;
      // Skip overwriting the search input while the user is typing in it —
      // otherwise a background poll mid-keystroke moves the cursor to the end.
      if (search && search !== document.activeElement && search.value !== _tlState.search) {
        search.value = _tlState.search;
      }
      _tlRefreshPhaseChips(phases);
    }

    function _tlCopySid(sid) {
      if (!sid || !navigator.clipboard) return;
      navigator.clipboard.writeText(sid).catch(() => {});
    }

    function _tlWireToolbar() {
      const toolbar = $("#tl-toolbar");
      if (!toolbar || toolbar.dataset.wired === "1") return;
      toolbar.addEventListener("change", (e) => {
        const id = e.target && e.target.id;
        if (id === "tl-range") _tlState.range = e.target.value;
        else if (id === "tl-tool") _tlState.tool = e.target.value;
        else if (id === "tl-status") _tlState.status = e.target.value;
        else if (id === "tl-sort") _tlState.sort = e.target.value;
        else return;
        _tlPersistState();
        _tlApplyTimeline();
      });
      toolbar.addEventListener("input", (e) => {
        if (!e.target || e.target.id !== "tl-search") return;
        _tlState.search = e.target.value || "";
        _tlPersistState();
        _tlApplyTimeline();
      });
      toolbar.addEventListener("click", (e) => {
        const btn = e.target && e.target.closest ? e.target.closest("[data-tl-phase]") : null;
        if (!btn) return;
        const phase = btn.dataset.tlPhase;
        if (_tlState.phases.has(phase)) _tlState.phases.delete(phase);
        else _tlState.phases.add(phase);
        _tlPersistState();
        _tlApplyTimeline();
      });
      toolbar.dataset.wired = "1";
    }

    function _tlApplyTimeline() {
      const meta = $("#timeline-meta");
      const chart = $("#timeline-chart");
      const view = $("#view-timeline");
      if (!chart) return;
      const filterSid = window._timelineSessionFilter || null;
      let scoped = _tlCache.slice();
      if (filterSid) scoped = scoped.filter((run) => run.session_id === filterSid);
      const phaseOptions = _tlAllPhases(scoped);
      _tlSyncToolbar(phaseOptions);
      const visible = _tlSortRuns(scoped.filter(_tlRunMatchesFilters));
      if (meta) meta.textContent = `${visible.length} / ${_tlCache.length} session${visible.length === 1 ? "" : "s"}${filterSid ? " (filtered)" : ""}`;
      const bannerHtml = _tlBannerHtml(filterSid);
      let globalStart = Date.now();
      let globalEnd = globalStart + 1000;
      if (visible.length) {
        const bounds = visible.map(_tlRunBounds);
        globalStart = Math.min(...bounds.map((b) => b.start));
        globalEnd = Math.max(...bounds.map((b) => b.end));
        if (visible.length === 1) {
          globalStart = bounds[0].start;
          globalEnd = bounds[0].end;
        }
      }
      const globalSpan = Math.max(1000, globalEnd - globalStart);
      _tlRenderAxis(globalStart, globalSpan);
      _tlRenderKpi(visible, globalStart, globalSpan);
      _tlRenderSparkline(visible, globalStart, globalSpan);
      _tlRenderRows(visible, globalStart, globalSpan, bannerHtml);
      const markMounted = () => {
        chart.dataset.tlMounted = "1";
        if (view) view.dataset.tlMounted = "1";
      };
      if (chart.dataset.tlMounted === "1") markMounted();
      else if (typeof requestAnimationFrame === "function") requestAnimationFrame(markMounted);
      else setTimeout(markMounted, 0);
    }

    async function loadTimeline() {
      const meta = $("#timeline-meta");
      const chart = $("#timeline-chart");
      // The success and error branches both deref `chart` (.dataset, .innerHTML)
      // — bail early when the timeline view is stripped from markup so a missing
      // element doesn't mask the underlying load failure with a TypeError.
      if (!chart) return;
      renderTimelineSkeletons();
      _tlWireToolbar();
      try {
        const r = await fetch("/api/timeline", { cache: "no-store" });
        if (!r.ok) throw new Error("HTTP " + r.status);
        const data = await r.json();
        _tlCache = data.runs || [];
        _tlState = _tlReadState();
        const countEl = $("#count-timeline");
        if (countEl) countEl.textContent = _tlCache.length;
        delete chart.dataset.skeletoned;
        if (!_tlCache.length) {
          const now = Date.now();
          const filterSid = window._timelineSessionFilter || null;
          _tlSyncToolbar([]);
          _tlRenderAxis(now, 1000);
          _tlRenderKpi([], now, 1000);
          _tlRenderSparkline([], now, 1000);
          chart.innerHTML = _tlBannerHtml(filterSid) + `<div class="tl-empty">No pipeline runs yet. Dispatch a phase via <em>Run</em> or invoke the orchestrate skill; the dashboard hook logs subprocess dispatches automatically. Inline phases are not captured.</div>`;
          if (meta) meta.textContent = filterSid ? "0 runs (filtered)" : "0 runs";
          return;
        }
        _tlApplyTimeline();
      } catch (err) {
        if (meta) meta.textContent = "error";
        delete chart.dataset.skeletoned;
        chart.innerHTML = `<div class="err">${escape(err.message)}</div>`;
        setMsg("#timeline-load", "err", "Timeline load failed: " + err.message);
      }
    }

    // ----- Events state -----
    var _eventsCache = [];
    // Identity of the newest event seen last poll. Used to detect new rows
    // even once the server tail cap pins events.length steady — comparing
    // lengths would stop firing the token-usage nudge on busy ledgers.
    var _eventsNewestKey = null;
    // `expanded` is a Set keyed by `${ts}|${session_id}|${phase}` (see
    // _evRenderFlat). The previous shape initialised it to null then ran a
    // typeof-object || === null check that was always true — dead branch.
    var _eventsState = { phase: "", exit: "", search: "", range: "24h", group: false, expanded: new Set() };

    function _evRangeMs(range) {
      if (range === "24h") return 24 * 3600 * 1000;
      if (range === "7d") return 7 * 24 * 3600 * 1000;
      return Infinity;
    }

    function _evMatchesFilters(e) {
      if (_eventsState.phase && e.phase !== _eventsState.phase) return false;
      if (_eventsState.exit === "ok" && e.exit_code !== 0) return false;
      if (_eventsState.exit === "fail" && (e.exit_code === 0 || e.exit_code == null)) return false;
      if (_eventsState.search) {
        const needle = _eventsState.search.toLowerCase();
        if (!String(e.command_preview || "").toLowerCase().includes(needle)) return false;
      }
      const span = _evRangeMs(_eventsState.range);
      if (span !== Infinity) {
        const t = Date.parse(e.ts);
        if (isNaN(t) || (Date.now() - t) > span) return false;
      }
      return true;
    }

    function _evExitPill(code) {
      if (code === 0) return `<span class="pill good">${code}</span>`;
      if (code == null) return `<span class="pill">—</span>`;
      return `<span class="pill bad">${code}</span>`;
    }

    function _evFormatStats(filtered) {
      const total = filtered.length;
      const failed = filtered.filter((e) => e.exit_code != null && e.exit_code !== 0).length;
      const durations = filtered.map((e) => e.duration_ms).filter((d) => typeof d === "number" && d >= 0);
      const avgMs = durations.length ? durations.reduce((a, b) => a + b, 0) / durations.length : null;
      const failRate = total ? Math.round((failed / total) * 100) : 0;
      const avgTxt = avgMs == null ? "—" : tlFormatDuration(Math.round(avgMs));
      return `
        <span><strong>${total}</strong> event${total === 1 ? "" : "s"}</span>
        <span><strong class="${failed ? "stat-bad" : ""}">${failed}</strong> failed${total ? ` (${failRate}%)` : ""}</span>
        <span>avg duration <strong>${escape(avgTxt)}</strong></span>
      `;
    }

    function _evRenderGrouped(filtered) {
      const groups = new Map();
      for (const e of filtered) {
        const sid = e.session_id || "unknown";
        if (!groups.has(sid)) groups.set(sid, []);
        groups.get(sid).push(e);
      }
      const rows = Array.from(groups.entries()).map(([sid, evs]) => {
        evs.sort((a, b) => _safeParseDate(a.ts) - _safeParseDate(b.ts));
        const first = evs[0];
        const last = evs[evs.length - 1];
        const firstMs = _safeParseDate(first.ts);
        const lastMs = _safeParseDate(last.ts);
        const spanMs = (Number.isFinite(firstMs) && Number.isFinite(lastMs))
          ? Math.max(0, lastMs - firstMs)
          : 0;
        const lastExit = last.exit_code;
        const phases = Array.from(new Set(evs.map((e) => e.phase || "?"))).join(", ");
        const sidShort = sid.slice(0, 8);
        // Guard against missing first.ts -- avoids "Invalid Date" rendering.
        const tsStr = first.ts || "";
        const tsRel = tsStr ? relativeTime(tsStr) : "—";
        const tsAbs = tsStr ? new Date(tsStr).toLocaleTimeString() : "—";
        return `<tr data-sid="${escape(sid)}">
          <td class="ts" title="${escape(tsStr)}">${escape(tsRel)}<div class="ts-abs">${escape(tsAbs)}</div></td>
          <td class="mono"><a class="link-mini" data-sid="${escape(sid)}" data-action="view-timeline">${escape(sidShort)}</a></td>
          <td><span class="pill">${evs.length}</span> <span class="ev-phases">${escape(phases)}</span></td>
          <td>${escape(tlFormatDuration(spanMs))}</td>
          <td>${_evExitPill(lastExit)}</td>
        </tr>`;
      }).join("");
      return `<table class="events-table events-grouped">
        <thead><tr><th>First seen</th><th>Session</th><th>Phases</th><th>Span</th><th>Last exit</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>`;
    }

    function _evRenderFlat(filtered) {
      // expandKey shape: `${ts}|${session_id}|${phase}` -- content-stable so
      // the persisted `_eventsState.expanded` set survives auto-refresh.
      // Previously used the array index, which shifted as new events
      // arrived, causing the wrong row to appear expanded after refresh.
      // Rare collisions on (ts, session_id, phase) are accepted as a much
      // smaller harm than the per-refresh shift.
      const rows = filtered.map((e) => {
        const isBad = e.exit_code != null && e.exit_code !== 0;
        const ts = e.ts || "";
        const tsRel = ts ? relativeTime(ts) : "—";
        const tsAbs = ts ? new Date(ts).toLocaleTimeString() : "—";
        const expandKey = `${ts}|${e.session_id || ""}|${e.phase || ""}`;
        const isOpen = _eventsState.expanded.has(expandKey);
        const sid = e.session_id || "";
        const sidShort = sid ? sid.slice(0, 8) : "—";
        const tlLink = sid
          ? `<a class="link-mini" data-sid="${escape(sid)}" data-action="view-timeline" title="filter timeline to this session">${escape(sidShort)} ↗</a>`
          : `<span class="ev-sid-dim">—</span>`;
        const main = `<tr class="ev-row ${isBad ? "bad-row" : ""}${isOpen ? " is-open" : ""}" data-expand-key="${escape(expandKey)}">
          <td class="ts" title="${escape(ts)}">${escape(tsRel)}<div class="ts-abs">${escape(tsAbs)}</div></td>
          <td><span class="pill">${escape(e.phase || "?")}</span></td>
          <td>${pillTool(_jobsSafeTool(e.tool))}</td>
          <td class="mono">${escape(e.model || "—")}</td>
          <td>${_evExitPill(e.exit_code)}</td>
          <td class="cmd" title="${escape(e.command_preview || "")}">${escape(e.command_preview || "")}</td>
          <td class="ev-sid">${tlLink}</td>
        </tr>`;
        const expanded = isOpen
          ? `<tr class="ev-expand"><td colspan="7"><pre>${escape(JSON.stringify(e, null, 2))}</pre></td></tr>`
          : "";
        return main + expanded;
      }).join("");
      return `<table class="events-table">
        <thead><tr><th>When</th><th>Phase</th><th>Tool</th><th>Model</th><th>Exit</th><th>Command</th><th>Session</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>`;
    }

    function renderEvents() {
      const meta = $("#events-meta");
      const body = $("#events-body");
      const stats = $("#events-stats");
      if (!body || !stats) return;
      delete stats.dataset.skeletoned;
      delete body.dataset.skeletoned;
      const filtered = _eventsCache.filter(_evMatchesFilters);
      stats.innerHTML = _evFormatStats(filtered);
      if (!filtered.length) {
        body.innerHTML = `<div class="empty">No events match the current filters.</div>`;
        if (meta) meta.textContent = `0 / ${_eventsCache.length}`;
        return;
      }
      // Fingerprint: filter state (so filter/search changes invalidate) +
      // the per-row identity tuple (ts + session_id + phase) + the open-row
      // set (expanding/collapsing must re-render that row's <tr.ev-expand>).
      // On a 5s auto-refresh with no new events and no UI change, this skips
      // the full ~2000-row innerHTML rebuild entirely.
      const fp = "ev|"
        + (_eventsState.group ? "g" : "f") + "|"
        + (_eventsState.search || "") + "|"
        + (_eventsState.phase || "") + "|"
        + (_eventsState.exit || "") + "|"
        + (_eventsState.range || "") + "|"
        + filtered.length + ":"
        + filtered.map((e) => (e.ts || "") + ":" + (e.session_id || "") + ":" + (e.phase || "")).join(",")
        + "|"
        + Array.from(_eventsState.expanded || []).sort().join(",");
      const doRender = () => {
        body.innerHTML = _eventsState.group ? _evRenderGrouped(filtered) : _evRenderFlat(filtered);
      };
      if (typeof window.renderIfChanged === "function") {
        window.renderIfChanged(body, fp, doRender);
      } else {
        doRender();
      }
      // Meta always refreshes its timestamp — the "updated HH:MM:SS" line
      // is the visible signal that the auto-refresh actually ran, even
      // when no rows changed.
      if (meta) meta.textContent = `${filtered.length} / ${_eventsCache.length} · updated ${new Date().toLocaleTimeString()}`;
    }

    function _evRefreshPhaseOptions() {
      const sel = $("#ev-phase");
      if (!sel) return;
      const current = sel.value;
      const phases = Array.from(new Set(_eventsCache.map((e) => e.phase).filter(Boolean))).sort();
      // The phase set rarely changes between 5s polls — skip the rebuild
      // when it's identical to last render. Sorted join is the fingerprint.
      const fp = phases.join(",");
      if (typeof window.renderIfChanged === "function") {
        window.renderIfChanged(sel, fp, () => {
          const opts = [`<option value="">all phases</option>`].concat(
            phases.map((p) => `<option value="${escape(p)}">${escape(p)}</option>`)
          );
          sel.innerHTML = opts.join("");
        });
      } else {
        const opts = [`<option value="">all phases</option>`].concat(
          phases.map((p) => `<option value="${escape(p)}">${escape(p)}</option>`)
        );
        sel.innerHTML = opts.join("");
      }
      if (current && phases.includes(current)) sel.value = current;
    }

    // Skeleton placeholders for the events table and the stats strip.
    // Paints once per page-load — the auto-refresh and filter handlers
    // re-render real content into the same containers, clearing the flag.
    function renderEventsSkeletons() {
      const stats = $("#events-stats");
      if (stats && !stats.dataset.skeletoned) {
        stats.innerHTML = `<div class="skeleton-events-stats">
          <span class="skeleton skeleton-stat"></span>
          <span class="skeleton skeleton-stat"></span>
          <span class="skeleton skeleton-stat"></span>
        </div>`;
        stats.dataset.skeletoned = "1";
      }
      const body = $("#events-body");
      if (body && !body.dataset.skeletoned) {
        body.innerHTML = Array.from({ length: 6 }).map(() => `
          <div class="skeleton-table-row">
            <span class="skeleton skeleton-cell narrow"></span>
            <span class="skeleton skeleton-cell narrow"></span>
            <span class="skeleton skeleton-cell narrow"></span>
            <span class="skeleton skeleton-cell"></span>
            <span class="skeleton skeleton-cell narrow"></span>
            <span class="skeleton skeleton-cell wide"></span>
          </div>
        `).join("");
        body.dataset.skeletoned = "1";
      }
    }

    async function loadEvents() {
      const meta = $("#events-meta");
      const body = $("#events-body");
      const stats = $("#events-stats");
      // Every branch below derefs `body` (.innerHTML, .dataset); bail when the
      // events view markup is missing so missing-DOM doesn't masquerade as a
      // load error in the catch block.
      if (!body) return;
      // Match the loadJobs 30s timeout so a wedged backend can't pin the
      // 5s auto-refresh against a never-resolving fetch.
      const ctrl = (typeof AbortController !== "undefined") ? new AbortController() : null;
      const timer = ctrl ? setTimeout(() => { try { ctrl.abort(); } catch (_) {} }, 30000) : null;
      renderEventsSkeletons();
      try {
        // /api/events?tail=2000 returns parsed events without re-shipping
        // the entire JSONL ledger on every 5s refresh. Server caps at
        // 5000; default 2000 keeps the UI responsive on huge histories.
        const r = await fetch("/api/events?tail=2000", { cache: "no-store", signal: ctrl ? ctrl.signal : undefined });
        if (r.status === 404) {
          _eventsCache = [];
          if (stats) { stats.innerHTML = ""; delete stats.dataset.skeletoned; }
          delete body.dataset.skeletoned;
          body.innerHTML = `<div class="empty">No events yet.<br><br>The hook is registered in <code>.claude/settings.json</code> and will start logging dispatches on the next Claude Code session that runs workflow phases.</div>`;
          const countEl404 = $("#count-events");
          if (countEl404) countEl404.textContent = "0";
          if (meta) meta.textContent = "no events";
          _evRefreshPhaseOptions();
          return;
        }
        if (!r.ok) throw new Error("HTTP " + r.status);
        const data = await r.json();
        const events = (data.events || []).slice().reverse();
        // The server caps at tail=N (default 2000); warn so an operator
        // hunting old events knows the dashboard isn't showing all of them.
        // The "dropped" wording matches the prior per-line parse warning
        // (test_jobs_high_fixes pins on the literal substring) — same
        // operator-visible meaning: events that exist but didn't render.
        if (data.truncated) {
          const dropped = (data.total || 0) - (data.returned || events.length);
          console.warn("[dashboard] events: dropped " + dropped + " older rows (server tail cap; refresh shows newest)");
        }
        // New ledger rows since the last poll → a workflow phase just
        // completed somewhere (typically an external Claude/Codex session
        // outside the dashboard's terminals view). Nudge the topbar usage
        // bars; the schedule helper rate-limits the actual fetch.
        // Compare newest-event identity (+ server total) rather than array
        // length: once the tail cap is hit, length stays pinned at the cap
        // even as brand-new rows arrive, so a length check would stop nudging.
        const _newest = events[0];
        const _newestKey = _newest
          ? `${_newest.ts || ""}|${_newest.session_id || ""}|${_newest.phase || ""}|${data.total ?? events.length}`
          : null;
        if (_newestKey && _newestKey !== _eventsNewestKey) {
          try { window.scheduleTokenUsageRefresh?.(); } catch (_) {}
        }
        _eventsNewestKey = _newestKey;
        _eventsCache = events;
        const totalCount = (typeof data.total === "number") ? data.total : events.length;
        const countEvEl = $("#count-events");
        if (countEvEl) {
          countEvEl.textContent = data.truncated
            ? `${events.length} of ${totalCount}`
            : String(totalCount);
        }
        _evRefreshPhaseOptions();
        if (!events.length) {
          if (stats) { stats.innerHTML = ""; delete stats.dataset.skeletoned; }
          delete body.dataset.skeletoned;
          body.innerHTML = `<div class="empty">No events yet.</div>`;
          if (meta) meta.textContent = "0 events";
          return;
        }
        renderEvents();
      } catch (err) {
        delete body.dataset.skeletoned;
        body.innerHTML = `<div class="err">${escape(err.message)}</div>`;
        setMsg("#events-load", "err", "Events load failed: " + err.message);
      } finally {
        if (timer) clearTimeout(timer);
      }
    }

    var _eventsTimer = null;
    // Auto-refresh cadence for the Events table when the user opts in via
    // the "auto-refresh" checkbox. Same interval is reused after a
    // visibilitychange wake-up below.
    var EVENTS_AUTOREFRESH_MS = 5000;
    document.addEventListener("change", (e) => {
      if (!e.target) return;
      if (e.target.id === "events-autorefresh") {
        if (_eventsTimer) { clearInterval(_eventsTimer); _eventsTimer = null; }
        if (e.target.checked) {
          loadEvents();
          _eventsTimer = setInterval(loadEvents, EVENTS_AUTOREFRESH_MS);
        }
      } else if (e.target.id === "ev-phase") {
        _eventsState.phase = e.target.value; renderEvents();
      } else if (e.target.id === "ev-exit") {
        _eventsState.exit = e.target.value; renderEvents();
      } else if (e.target.id === "ev-range") {
        _eventsState.range = e.target.value; renderEvents();
      } else if (e.target.id === "ev-group") {
        _eventsState.group = !!e.target.checked; renderEvents();
      }
    });
    var _evSearchTimer = null;
    document.addEventListener("input", (e) => {
      if (e.target && e.target.id === "ev-search") {
        clearTimeout(_evSearchTimer);
        _evSearchTimer = setTimeout(() => {
          _eventsState.search = e.target.value || "";
          renderEvents();
        }, 150);
      }
    });
    document.addEventListener("click", (e) => {
      const t = e.target;
      if (!t) return;
      if (t.id === "ev-reload") { loadEvents(); return; }
      if (t.id === "ev-clear") { clearEvents(); return; }
      if (t.id === "tl-clear-filter") {
        window._timelineSessionFilter = null;
        loadTimeline();
        return;
      }
      const tlCopy = t.closest && t.closest("[data-tl-copy-sid]");
      if (tlCopy) {
        _tlCopySid(tlCopy.dataset.tlCopySid || "");
        return;
      }
      if (t.classList && t.classList.contains("link-mini") && t.dataset.action === "view-timeline") {
        e.preventDefault();
        window._timelineSessionFilter = t.dataset.sid || null;
        const navBtn = document.querySelector('nav button[data-view="timeline"]');
        if (navBtn) navBtn.click();
        return;
      }
      // Row click → toggle expand (only inside flat events table, not on links)
      const row = t.closest && t.closest(".ev-row");
      if (row && !t.closest("a")) {
        const key = row.dataset.expandKey;
        if (!key) return;
        if (_eventsState.expanded.has(key)) _eventsState.expanded.delete(key);
        else _eventsState.expanded.add(key);
        renderEvents();
      }
    });


    // ----- Cross-cutting: pause polling when tab hidden (packet C) -----
    // Dedupe / debounce: some browsers (notably Safari + older Chrome
    // with bfcache) fire `visibilitychange` twice in quick succession
    // when a tab is focused, which would trigger two parallel
    // loadJobs/loadEvents/loadTimeline storms. We coalesce calls
    // within a short window by tracking the last-handled visibility
    // state + timestamp; identical transitions inside the window
    // are dropped.
    var _lastVisibilityState = null;
    var _lastVisibilityAt = 0;
    // Debounce window for back-to-back visibilitychange events (Safari +
    // older Chrome with bfcache fire twice). 250ms is short enough to
    // ignore the dup pair without delaying a real tab-switch refresh.
    var VISIBILITY_DEDUPE_MS = 250;
    document.addEventListener("visibilitychange", () => {
      const state = document.hidden ? "hidden" : "visible";
      const now = Date.now();
      if (_lastVisibilityState === state && (now - _lastVisibilityAt) < VISIBILITY_DEDUPE_MS) {
        // Duplicate fire within the debounce window — ignore.
        return;
      }
      _lastVisibilityState = state;
      _lastVisibilityAt = now;
      if (document.hidden) {
        if (_jobsTimer) { clearTimeout(_jobsTimer); _jobsTimer = null; }
        if (_eventsTimer) { clearInterval(_eventsTimer); _eventsTimer = null; }
        // Also stop per-pane SSE heartbeats — they were the only background-
        // tab-active timer that kept firing through document.hidden.
        // termRestoreHeartbeats() below rewires them on tab focus.
        if (typeof TERMS !== "undefined") {
          for (const t of TERMS.values()) {
            if (t && t._sseHeartbeat) {
              clearInterval(t._sseHeartbeat);
              t._sseHeartbeat = null;
              t._sseHeartbeatPaused = true;
            }
          }
        }
        return;
      }
      if (typeof TERMS !== "undefined") {
        for (const t of TERMS.values()) {
          if (t && t._sseHeartbeatPaused && typeof t._restartSseHeartbeat === "function") {
            t._sseHeartbeatPaused = false;
            try { t._restartSseHeartbeat(); } catch (_) {}
          }
        }
      }
      const runActive = document.getElementById("view-run")?.classList.contains("active");
      const termsActive = document.getElementById("view-terminals")?.classList.contains("active");
      const evActive = document.getElementById("view-events")?.classList.contains("active");
      const tlActive = document.getElementById("view-timeline")?.classList.contains("active");
      if (runActive || termsActive) loadJobs();
      if (evActive) {
        loadEvents();
        const cb = document.getElementById("events-autorefresh");
        if (cb && cb.checked && !_eventsTimer) {
          _eventsTimer = setInterval(loadEvents, EVENTS_AUTOREFRESH_MS);
        }
      }
      if (tlActive && typeof loadTimeline === "function") loadTimeline();
    });
