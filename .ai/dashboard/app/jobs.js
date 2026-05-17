// .ai/dashboard/app/jobs.js -- extracted from app.js (was lines 1103..1470)
// NOTE: top-level const/let were converted to var so identifiers cross <script> boundaries.

    // ----- Events clear -----
    async function clearEvents() {
      if (!confirm("Clear .ai/events.jsonl ? This deletes the file.")) return;
      try {
        await postJson("/api/events/clear", {});
        await loadEvents();
      } catch (e) {
        $("#events-meta").textContent = "clear failed: " + e.message;
      }
    }

    // ----- Dispatch mode toggle -----
    async function toggleDispatchMode() {
      const btn = $("#dispatch-toggle");
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

    function statusPill(status) {
      const cls = ["running","queued","cancelling","cancelled","done"].includes(status)
        ? status
        : (status === "failed" ? "bad" : "");
      return `<span class="pill ${cls}">${escape(status)}</span>`;
    }

    async function loadJobs(opts) {
      opts = opts || {};
      try {
        const r = await fetch("/api/jobs", { cache: "no-store" });
        const data = await r.json();
        const jobs = data.jobs || [];
        $("#count-jobs").textContent = jobs.length;
        const el = $("#jobs-list");
        if (!jobs.length) {
          el.innerHTML = `<div class="empty">No jobs yet</div>`;
        } else {
          el.innerHTML = jobs.map((j) => {
            const taskPreview = (j.task || "").replace(/\s+/g, " ");
            return `<div class="list-item" data-id="${escape(j.id)}">
              <div>${statusPill(j.status)} <span style="color:var(--fg-dim);font-size:11px">${escape((j.kind || "").toUpperCase())}</span></div>
              <div class="sub" style="margin-top:4px;white-space:normal">${escape(taskPreview.slice(0, 80))}${taskPreview.length > 80 ? "…" : ""}</div>
            </div>`;
          }).join("");
          el.querySelectorAll(".list-item").forEach((li) => {
            if (li.dataset.id === _selectedJobId) li.classList.add("active");
            li.addEventListener("click", () => {
              _selectedJobId = li.dataset.id;
              el.querySelectorAll(".list-item").forEach((x) => x.classList.remove("active"));
              li.classList.add("active");
              loadJobDetail();
            });
          });
        }
        // Feed the terminals picker.
        termRefreshPicker(jobs);
        // Auto-open every active chat / chat-codex job that we haven't
        // touched before. Once the operator closes a pane, its id stays
        // in AUTO_OPENED_ONCE so we don't keep popping it back open.
        termAutoOpenActive(jobs);

        // Background poll if any job is running and a relevant tab is visible.
        const anyRunning = jobs.some((j) => j.status === "running" || j.status === "queued" || j.status === "cancelling");
        const runTabActive = $("#view-run").classList.contains("active");
        const termsTabActive = $("#view-terminals").classList.contains("active");
        if (_jobsTimer) { clearTimeout(_jobsTimer); _jobsTimer = null; }
        if (anyRunning && (runTabActive || termsTabActive)) {
          _jobsTimer = setTimeout(loadJobs, 2000);
        } else if (termsTabActive && termAutoOpenEnabled()) {
          // Even with nothing running, keep polling on the Terminals view so
          // newly-created chats (e.g. spawned externally) pop into view.
          _jobsTimer = setTimeout(loadJobs, 4000);
        }
        // Refresh open job's log too
        if (_selectedJobId && runTabActive) loadJobDetail();
      } catch (e) {
        $("#jobs-list").innerHTML = `<div class="err">${escape(e.message)}</div>`;
      }
    }

    async function loadJobDetail() {
      if (!_selectedJobId) return;
      try {
        const r = await fetch("/api/jobs/" + _selectedJobId + "?tail=400", { cache: "no-store" });
        if (!r.ok) {
          $("#jobs-doc").innerHTML = `<div class="err">HTTP ${r.status}</div>`;
          return;
        }
        const j = await r.json();
        const cancelable = j.status === "running" || j.status === "queued";
        $("#jobs-doc").innerHTML = `
          <div class="job-meta">
            <div class="k">id</div>      <div class="v">${escape(j.id)}</div>
            <div class="k">kind</div>    <div class="v">${escape(j.kind)}</div>
            <div class="k">status</div>  <div class="v">${statusPill(j.status)} ${j.exit_code != null ? `exit ${j.exit_code}` : ""}</div>
            <div class="k">created</div> <div class="v">${escape(j.created_at || "—")}</div>
            <div class="k">started</div> <div class="v">${escape(j.started_at || "—")}</div>
            <div class="k">ended</div>   <div class="v">${escape(j.ended_at || "—")}</div>
            <div class="k">command</div> <div class="v">${escape(j.command || "—")}</div>
            <div class="k">task</div>    <div class="v">${escape(j.task || "—")}</div>
          </div>
          <div style="margin-bottom:6px;font-size:11px;color:var(--fg-dim);text-transform:uppercase;letter-spacing:0.5px">log (last 400 lines)</div>
          <pre class="log" id="job-log">${escape(j.log_tail || "(no output yet)")}</pre>
          <div class="form-actions" style="margin-top:10px">
            ${cancelable ? `<button class="btn danger" onclick="cancelJob('${escape(j.id)}')">Cancel job</button>` : ""}
            <span class="form-msg" id="job-action-msg"></span>
          </div>
        `;
        // Auto-scroll log
        const log = $("#job-log");
        if (log) log.scrollTop = log.scrollHeight;
      } catch (e) {
        $("#jobs-doc").innerHTML = `<div class="err">${escape(e.message)}</div>`;
      }
    }

    async function submitJob() {
      const btn = $("#run-submit");
      const kind = $("#run-kind").value;
      const task = $("#run-task").value.trim();
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
        let compareRes = null;
        if (compare && (kind === "chat" || kind === "chat-codex")) {
          const otherKind = kind === "chat" ? "chat-codex" : "chat";
          try {
            compareRes = await postJson("/api/jobs", { ...basePayload, kind: otherKind, resume_session_id: undefined });
          } catch (cmpErr) {
            setMsg("#run-msg", "warn", "compare job failed: " + cmpErr.message, 4000);
          }
        }
        $("#run-task").value = "";
        await loadJobs();
        await loadSessions();
        // Chat jobs are most useful in the Terminals view — jump there and
        // open the pane(s) right away so the operator can start typing.
        if (kind === "chat" || kind === "chat-codex") {
          const navBtn = document.querySelector('nav button[data-view="terminals"]');
          if (navBtn) navBtn.click();
          termOpen(res.id, res);
          if (compareRes) termOpen(compareRes.id, compareRes);
        }
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

        // Preserve selection if it survived the refresh.
        const allIds = new Set([
          ...dashSessions.map((s) => s.session_id),
          ...ideSessions.map((s) => s.session_id),
        ]);
        if (prev && allIds.has(prev)) sel.value = prev;
      } catch (_) { /* ignore */ }
    }

    document.addEventListener("DOMContentLoaded", () => {
      $("#run-kind")?.addEventListener("change", loadSessions);
    });

    async function cancelJob(jobId) {
      try {
        await postJson("/api/jobs/" + jobId + "/cancel", {});
        setMsg("#job-action-msg", "ok", "cancellation requested", 4000);
        await loadJobs();
      } catch (e) {
        setMsg("#job-action-msg", "err", e.message);
      }
    }

    // ----- events -----
    function relativeTime(iso) {
      const diff = (Date.now() - new Date(iso).getTime()) / 1000;
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

    async function loadTimeline() {
      const meta = $("#timeline-meta");
      const chart = $("#timeline-chart");
      try {
        const r = await fetch("/api/timeline", { cache: "no-store" });
        if (!r.ok) throw new Error("HTTP " + r.status);
        const data = await r.json();
        const runs = data.runs || [];
        $("#count-timeline").textContent = runs.length;
        if (!runs.length) {
          chart.innerHTML = `<div class="tl-empty">No pipeline runs yet. Dispatch a phase via <em>Run</em> or invoke the orchestrate skill — the <code>PostToolUse</code> hook logs subprocess dispatches to <code>.ai/events.jsonl</code> automatically. Inline phases (orchestrator running a phase in its own session) are not captured.</div>`;
          meta.textContent = "0 runs";
          return;
        }
        meta.textContent = `${runs.length} session${runs.length === 1 ? "" : "s"}`;
        chart.innerHTML = runs.map((run) => {
          const start = Date.parse(run.started_at) || 0;
          const end = Date.parse(run.ended_at) || start;
          // Pad span so the last phase has a visible bar even when its duration is 0.
          const minSpanMs = 1000;
          const span = Math.max(minSpanMs, end - start);
          const bars = run.phases.map((ph) => {
            const phEnd = Date.parse(ph.end_ts) || start;
            const phStart = phEnd - (ph.duration_ms || 0);
            const leftPct = span > 0 ? Math.max(0, Math.min(100, ((phStart - start) / span) * 100)) : 0;
            const widthPct = span > 0
              ? Math.max(0.8, Math.min(100 - leftPct, ((ph.duration_ms || 0) / span) * 100))
              : 100 / run.phases.length;
            const exitText = ph.exit_code == null ? "?" : String(ph.exit_code);
            const isUnknownPhase = ph.phase === "unknown";
            const displayLabel = isUnknownPhase ? "ad-hoc" : ph.phase;
            const cls = isUnknownPhase ? `${ph.status} unknown-phase` : ph.status;
            const tip = `${displayLabel} · ${ph.tool}/${ph.model} · exit ${exitText} · ${tlFormatDuration(ph.duration_ms)}`;
            return `<div class="tl-bar ${cls}" title="${escape(tip)}" `
              + `style="left:${leftPct.toFixed(2)}%;width:${widthPct.toFixed(2)}%">`
              + `${escape(displayLabel)}</div>`;
          }).join("");
          const sidShort = (run.session_id || "unknown").slice(0, 8);
          const when = run.started_at ? new Date(run.started_at).toLocaleString() : "";
          const totalDur = tlFormatDuration(run.total_duration_ms);
          const phaseCount = run.phases.length;
          const taskHtml = run.task
            ? `<div class="tl-task" title="${escape(run.task)}">${escape(run.task)}</div>`
            : `<div class="tl-task dim">(no transcript — session file unavailable)</div>`;
          return `<div class="tl-row">`
            + `<div class="tl-label">`
            +   taskHtml
            +   `<div class="tl-meta">`
            +     `<span class="tl-tag" title="primary tool/model for this session">${escape(run.tag || "—")}</span>`
            +     `<span class="tl-dur" title="total wall-clock duration">${escape(totalDur)}</span>`
            +     `<span>· ${phaseCount} bar${phaseCount === 1 ? "" : "s"}</span>`
            +     `<span>· ${escape(when)}</span>`
            +     `<span class="tl-sid" title="${escape(run.session_id)}">· ${escape(sidShort)}</span>`
            +   `</div>`
            + `</div>`
            + `<div class="tl-track">${bars}</div>`
            + `</div>`;
        }).join("");
      } catch (err) {
        meta.textContent = "error";
        chart.innerHTML = `<div class="err">${escape(err.message)}</div>`;
      }
    }

    async function loadEvents() {
      const meta = $("#events-meta");
      const body = $("#events-body");
      try {
        const r = await fetch("/.ai/events.jsonl", { cache: "no-store" });
        if (r.status === 404) {
          body.innerHTML = `<div class="empty">No events yet.<br><br>The hook is registered in <code>.claude/settings.json</code> and will start logging dispatches on the next Claude Code session that runs workflow phases.</div>`;
          $("#count-events").textContent = "0";
          meta.textContent = "no events";
          return;
        }
        if (!r.ok) throw new Error("HTTP " + r.status);
        const text = await r.text();
        const lines = text.split("\n").filter((l) => l.trim());
        const events = [];
        for (const line of lines) {
          try { events.push(JSON.parse(line)); } catch (_) {}
        }
        events.reverse();
        $("#count-events").textContent = events.length;
        if (!events.length) {
          body.innerHTML = `<div class="empty">No events yet.</div>`;
          meta.textContent = "0 events";
          return;
        }
        const rows = events.map((e) => {
          const exitPill = e.exit_code === 0
            ? `<span class="pill good">${e.exit_code}</span>`
            : e.exit_code == null
              ? `<span class="pill">—</span>`
              : `<span class="pill bad">${e.exit_code}</span>`;
          return `<tr>
            <td class="ts" title="${escape(e.ts)}">${escape(relativeTime(e.ts))}</td>
            <td><span class="pill">${escape(e.phase || "?")}</span></td>
            <td>${pillTool(e.tool)}</td>
            <td class="mono">${escape(e.model || "—")}</td>
            <td>${exitPill}</td>
            <td class="cmd" title="${escape(e.command_preview || "")}">${escape(e.command_preview || "")}</td>
          </tr>`;
        }).join("");
        body.innerHTML = `<table class="events-table">
          <thead><tr><th>When</th><th>Phase</th><th>Tool</th><th>Model</th><th>Exit</th><th>Command</th></tr></thead>
          <tbody>${rows}</tbody>
        </table>`;
        meta.textContent = `${events.length} event${events.length === 1 ? "" : "s"} · updated ${new Date().toLocaleTimeString()}`;
      } catch (err) {
        body.innerHTML = `<div class="err">${escape(err.message)}</div>`;
      }
    }

    var _eventsTimer = null;
    document.addEventListener("change", (e) => {
      if (e.target && e.target.id === "events-autorefresh") {
        if (_eventsTimer) { clearInterval(_eventsTimer); _eventsTimer = null; }
        if (e.target.checked) {
          loadEvents();
          _eventsTimer = setInterval(loadEvents, 5000);
        }
      }
    });

