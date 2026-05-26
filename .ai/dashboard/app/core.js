// .ai/dashboard/app/core.js -- extracted from app.js (was lines 1..572)
// NOTE: top-level const/let were converted to var so identifiers cross <script> boundaries.

    var $ = (sel) => document.querySelector(sel);
    var $$ = (sel) => Array.from(document.querySelectorAll(sel));

    // `marked.setOptions` runs at script-parse time. If the CDN script that
    // defines `marked` failed to load, accessing it here throws synchronously
    // and aborts the rest of core.js — every later module fails to load and
    // the dashboard renders blank. Guard the call so a missing markdown
    // library degrades gracefully instead of nuking the whole UI.
    if (typeof marked !== "undefined") {
      marked.setOptions({ gfm: true, breaks: false });
    } else {
      console.warn("[dashboard] marked library not loaded; markdown rendering disabled");
    }
    // Same defensive probe for DOMPurify — every sink that renders
    // user/server markdown to innerHTML wraps the parsed output in
    // DOMPurify before assignment. If DOMPurify failed to load (CDN
    // issue, CSP block, offline), the try/catch around those sinks
    // falls back to textContent (safe) but the user sees raw markdown
    // source. Warn at boot so the regression is diagnosable.
    if (typeof DOMPurify === "undefined") {
      console.warn("[dashboard] DOMPurify not loaded; markdown sinks will fall back to plain text");
    }

    // ----- nav switching -----
    $$("nav button").forEach((btn) => {
      btn.addEventListener("click", () => {
        $$("nav button").forEach((b) => b.classList.remove("active"));
        $$(".view").forEach((v) => v.classList.remove("active"));
        btn.classList.add("active");
        $("#view-" + btn.dataset.view).classList.add("active");
        try { localStorage.setItem("dash.view", btn.dataset.view); } catch (_) { /* ignore */ }
        if (btn.dataset.view === "run" || btn.dataset.view === "terminals") loadJobs();
        if (btn.dataset.view === "terminals") termRefreshTranscriptPicker();
        if (btn.dataset.view === "timeline") loadTimeline();
        if (btn.dataset.view === "auto-select" && typeof loadAutoSelect === "function") loadAutoSelect();
      });
    });

    // Restore last opened view across reloads. The click handler above
    // calls into loadJobs / termRefreshTranscriptPicker / loadTimeline /
    // loadAutoSelect — all defined in later <script> tags. Running this
    // at core.js parse time crashes with "loadJobs is not defined"
    // (pageerror in the console) and leaves the dashboard half-loaded.
    // Defer to DOMContentLoaded so every app/*.js has finished hoisting.
    document.addEventListener("DOMContentLoaded", function restoreView() {
      let saved = null;
      // Private browsing / restricted contexts throw on localStorage access.
      // Swallow and default to the "overview" view so the DOMContentLoaded
      // handler doesn't abort the rest of the dashboard boot.
      try {
        saved = localStorage.getItem("dash.view");
      } catch (e) {
        console.warn("[dashboard] localStorage unavailable: " + (e && e.message ? e.message : e));
        saved = "overview";
      }
      if (!saved || saved === "overview") return;
      const btn = document.querySelector(`nav button[data-view="${saved}"]`);
      if (btn) btn.click();
    });

    // ----- form button wiring -----
    document.addEventListener("DOMContentLoaded", () => {
      // Guard every `$()` lookup in this handler — any one being renamed
      // or removed from index.html otherwise aborts the rest of boot.
      $("#mem-submit")?.addEventListener("click", submitMemory);
      $("#dec-submit")?.addEventListener("click", submitDecision);
      $("#dispatch-toggle")?.addEventListener("click", toggleDispatchMode);
      $("#run-submit")?.addEventListener("click", submitJob);
      $("#run-task")?.addEventListener("keydown", (e) => {
        if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); submitJob(); }
      });
      // Default decision date = today
      const today = new Date().toISOString().slice(0, 10);
      const decDate = $("#dec-date");
      if (decDate) decDate.value = today;
      // Enter-to-submit on Memory form
      ["#mem-topic", "#mem-fact"].forEach((s) => {
        $(s)?.addEventListener("keydown", (e) => { if (e.key === "Enter") submitMemory(); });
      });
      $("#dec-decision")?.addEventListener("keydown", (e) => { if (e.key === "Enter") submitDecision(); });
      // Skills search input
      $("#skills-search")?.addEventListener("input", (e) => {
        _skillsState.query = e.target.value;
        renderSkillsGrid();
      });
      // Agents search input
      $("#agents-search")?.addEventListener("input", (e) => {
        _agentsState.query = e.target.value;
        renderAgentsGrid();
      });
      // Proposal modal wiring
      $("#proposal-modal-close")?.addEventListener("click", closeProposalModal);
      $("#proposal-modal")?.addEventListener("click", (e) => {
        if (e.target.id === "proposal-modal") closeProposalModal();
      });
      $("#proposal-accept")?.addEventListener("click", () => decideProposal("accept"));
      $("#proposal-reject")?.addEventListener("click", () => decideProposal("reject"));
      // Skill detail modal wiring
      $("#skill-detail-close")?.addEventListener("click", closeSkillDetail);
      $("#skill-detail-modal")?.addEventListener("click", (e) => {
        if (e.target.id === "skill-detail-modal") closeSkillDetail();
      });
      // Agent detail modal wiring
      $("#agent-detail-close")?.addEventListener("click", closeAgentDetail);
      $("#agent-detail-modal")?.addEventListener("click", (e) => {
        if (e.target.id === "agent-detail-modal") closeAgentDetail();
      });
      $("#agent-proposal-close")?.addEventListener("click", closeAgentProposalModal);
      $("#agent-proposal-modal")?.addEventListener("click", (e) => {
        if (e.target.id === "agent-proposal-modal") closeAgentProposalModal();
      });
      document.addEventListener("keydown", (e) => {
        if (e.key !== "Escape") return;
        const propModal = $("#proposal-modal");
        if (propModal && !propModal.hidden) { closeProposalModal(); return; }
        const skillModal = $("#skill-detail-modal");
        if (skillModal && !skillModal.hidden) { closeSkillDetail(); return; }
        const agentPropModal = $("#agent-proposal-modal");
        if (agentPropModal && !agentPropModal.hidden) { closeAgentProposalModal(); return; }
        const agentModal = $("#agent-detail-modal");
        if (agentModal && !agentModal.hidden) { closeAgentDetail(); return; }
      });
      // Density toggle: persist preference in localStorage and apply on boot.
      const applyDensity = (mode) => {
        document.body.classList.toggle("density-compact", mode === "compact");
        const btn = $("#density-toggle");
        if (btn) btn.textContent = mode === "compact" ? "comfortable" : "compact";
      };
      // In private browsing / restricted contexts localStorage.getItem
      // throws synchronously. Catch so the rest of DOMContentLoaded keeps
      // running and default to "comfortable" density.
      let savedDensity = "comfortable";
      try {
        savedDensity = localStorage.getItem("dash.density") || "comfortable";
      } catch (e) {
        console.warn("[dashboard] localStorage unavailable: " + (e && e.message ? e.message : e));
      }
      applyDensity(savedDensity);
      $("#density-toggle")?.addEventListener("click", () => {
        const isCompact = document.body.classList.contains("density-compact");
        const next = isCompact ? "comfortable" : "compact";
        try { localStorage.setItem("dash.density", next); } catch (_) { /* ignore */ }
        applyDensity(next);
      });
    });

    // ----- fetch helpers -----
    // 30s AbortController guards every request — a hung server otherwise
    // stalls dashboard load forever (spinner spins indefinitely).
    async function getText(path) {
      const ctrl = new AbortController();
      const timer = setTimeout(() => ctrl.abort(), 30000);
      try {
        const r = await fetch("/" + path.replace(/^\/+/, ""), { cache: "no-store", signal: ctrl.signal });
        if (!r.ok) throw new Error(path + " -> HTTP " + r.status);
        return await r.text();
      } catch (err) {
        if (err && err.name === "AbortError") throw new Error(path + " -> timeout after 30s");
        throw err;
      } finally {
        clearTimeout(timer);
      }
    }
    async function getYaml(path) {
      return jsyaml.load(await getText(path));
    }
    async function listDir(path) {
      const ctrl = new AbortController();
      const timer = setTimeout(() => ctrl.abort(), 30000);
      try {
        const r = await fetch("/api/list?path=" + encodeURIComponent(path), { cache: "no-store", signal: ctrl.signal });
        if (!r.ok) {
          // Non-2xx: log so operators see issues, but return [] so chained
          // callers like `.then(xs => xs.filter(...))` don't blow up cold boot.
          console.warn("[dashboard] listDir " + path + " -> HTTP " + r.status);
          return [];
        }
        const data = await r.json();
        return data.entries || [];
      } catch (err) {
        if (err && err.name === "AbortError") {
          console.warn("[dashboard] listDir " + path + " -> timeout after 30s");
          return [];
        }
        console.warn("[dashboard] listDir " + path + " -> " + (err && err.message ? err.message : err));
        return [];
      } finally {
        clearTimeout(timer);
      }
    }

    // ----- renderers -----
    function pillTool(tool) {
      const cls = tool === "claude" ? "claude" : tool === "codex" ? "codex" : "";
      return `<span class="pill ${cls}">${tool || "?"}</span>`;
    }

    function formatTokens(n) {
      // Explicit empty-string check first — `isNaN("")` is false because ""
      // coerces to 0, so without this guard formatTokens("") returned "0"
      // instead of the intended em-dash placeholder.
      if (n === "" || n === null || n === undefined) return "—";
      if (isNaN(n)) return "—";
      n = Number(n);
      if (n < 1000) return String(n);
      if (n < 1_000_000) return (n / 1000).toFixed(1) + "K";
      if (n < 1_000_000_000) return (n / 1_000_000).toFixed(2) + "M";
      return (n / 1_000_000_000).toFixed(2) + "B";
    }

    function shortModelLabel(name) {
      if (!name) return "?";
      return String(name).replace(/^claude-/, "").replace(/-codex$/, "-cdx");
    }

    function formatModelShares(byModel, opts = {}) {
      if (!byModel || typeof byModel !== "object") return "—";
      const entries = Object.entries(byModel)
        .filter(([, v]) => v && v.total > 0)
        .sort((a, b) => (b[1].total || 0) - (a[1].total || 0));
      if (!entries.length) return "—";
      const topN = opts.topN ?? 3;
      const head = entries.slice(0, topN).map(([m, v]) =>
        `${escape(shortModelLabel(m))} ${(v.percent ?? 0).toFixed(0)}%`
      );
      const rest = entries.length - topN;
      if (rest > 0) head.push(`+${rest}`);
      return head.join(" · ");
    }

    function formatResetIn(when) {
      // Accepts either unix-seconds (number) or an ISO datetime string.
      if (!when) return "";
      let target;
      if (typeof when === "number") target = when * 1000;
      else if (typeof when === "string") {
        const t = Date.parse(when);
        if (isNaN(t)) return "";
        target = t;
      } else return "";
      const ms = target - Date.now();
      if (ms <= 0) return "now";
      const totalMin = Math.floor(ms / 60000);
      const days = Math.floor(totalMin / 1440);
      const hours = Math.floor((totalMin % 1440) / 60);
      const minutes = totalMin % 60;
      if (days > 0) return `in ${days}d ${hours}h`;
      if (hours > 0) return `in ${hours}h ${minutes}m`;
      return `in ${minutes}m`;
    }

    function formatPct(v) {
      // v is 0..100 (claude) or 0..1 (codex used_percent). Caller picks scale.
      if (v == null || isNaN(v)) return "—";
      const n = Number(v);
      if (n === 0) return "0%";
      // Sub-1% rounds to "<1%" so we don't claim 0%, but no decimals shown.
      if (n < 1) return "<1%";
      return Math.round(n) + "%";
    }

    function renderWindowLine(prefix, win) {
      if (!win || !win.total) return `${prefix}: <span style="opacity:0.6">none</span>`;
      return `${prefix}: ${formatTokens(win.total)} · ${formatModelShares(win.by_model)}`;
    }

    function renderOverview(project, models, memoryEntries, plansCount, specsCount) {
      // Single lookup — the previous shape called `$()` once for the guard
      // then again for the assignment, so a DOM mutation between the two
      // would silently null-deref `overviewCards.dataset`.
      const overviewCards = $("#overview-cards");
      if (!overviewCards) return;
      const stack = (project.stack || []).join(", ") || "—";
      const pms = (project.package_managers || []).join(", ") || "—";
      const dispatchMode = models.dispatch_mode || "manual";
      const dispatchPill = dispatchMode === "auto"
        ? `<span class="pill good">${dispatchMode}</span>`
        : `<span class="pill warn">${dispatchMode}</span>`;
      delete overviewCards.dataset.skeletoned;
      overviewCards.innerHTML = `
        <div class="card"><h3>Stack</h3><div class="val">${escape(stack)}</div></div>
        <div class="card"><h3>Package managers</h3><div class="val">${escape(pms)}</div></div>
        <div class="card"><h3>Dispatch mode</h3><div class="val">${dispatchPill}</div></div>
        <div class="card"><h3>Memory entries</h3><div class="val big">${memoryEntries}</div></div>
        <div class="card"><h3>Plans / Specs</h3><div class="val big">${plansCount} / ${specsCount}</div></div>
        <div class="card" title="Sum of input + output + cache tokens in Claude transcripts for this repo, with per-model share for the last 5 hours and last 7 days">
          <h3>Claude tokens</h3>
          <div class="val big" id="ov-claude-total">—</div>
          <div class="val" id="ov-claude-5h"  style="color:var(--fg-dim);margin-top:4px;font-size:11px">5h: —</div>
          <div class="val" id="ov-claude-7d"  style="color:var(--fg-dim);margin-top:2px;font-size:11px">7d: —</div>
          <div class="val" id="ov-claude-all" style="color:var(--fg-dim);margin-top:2px;font-size:11px">all: —</div>
        </div>
        <div class="card" title="Sum of Codex per-turn tokens for sessions whose cwd matches this repo, with per-model share">
          <h3>Codex tokens</h3>
          <div class="val big" id="ov-codex-total">—</div>
          <div class="val" id="ov-codex-5h"  style="color:var(--fg-dim);margin-top:4px;font-size:11px">5h: —</div>
          <div class="val" id="ov-codex-7d"  style="color:var(--fg-dim);margin-top:2px;font-size:11px">7d: —</div>
          <div class="val" id="ov-codex-all" style="color:var(--fg-dim);margin-top:2px;font-size:11px">all: —</div>
        </div>
        <div class="card" title="Account-wide rate-limit utilization. Claude values come from /api/oauth/usage using the OAuth token in ~/.claude/.credentials.json; Codex values come from the most recent token_count event across any session.">
          <h3>Limits</h3>
          <div class="val" style="display:grid;grid-template-columns:auto 1fr;gap:2px 10px;font-size:13px">
            <span style="color:var(--fg-dim)">Claude 5h</span><span id="ov-rl-claude-5h">—</span>
            <span style="color:var(--fg-dim)">Claude week</span><span id="ov-rl-claude-week">—</span>
            <span style="color:var(--fg-dim)">Codex 5h</span><span id="ov-rl-codex-5h">—</span>
            <span style="color:var(--fg-dim)">Codex week</span><span id="ov-rl-codex-week">—</span>
          </div>
          <div class="val" id="ov-rl-claude-models" style="color:var(--fg-dim);margin-top:4px;font-size:11px"></div>
          <div class="val" id="ov-rl-meta" style="color:var(--fg-dim);margin-top:4px;font-size:11px">—</div>
        </div>
      `;
    }

    var _tokenUsageInFlight = false;
    async function loadTokenUsage() {
      if (!$("#ov-claude-total")) return;
      // In-flight sentinel: token usage is also fetched on every tab focus
      // and a 5s poll. Without this guard a slow /api/usage/total would
      // stack callers and overwrite each other's results.
      if (_tokenUsageInFlight) return;
      _tokenUsageInFlight = true;
      const ctrl = (typeof AbortController !== "undefined") ? new AbortController() : null;
      const timer = ctrl ? setTimeout(() => { try { ctrl.abort(); } catch (_) {} }, 30000) : null;
      try {
        const r = await fetch("/api/usage/total", { cache: "no-store", signal: ctrl ? ctrl.signal : undefined });
        if (!r.ok) {
          // Surface the failure so operators can correlate "—" cards with the
          // underlying 500/404. A silent return was the previous behaviour.
          console.warn("[dashboard] /api/usage/total -> HTTP " + r.status);
          return;
        }
        const u = await r.json();
        const c = u.claude || {};
        const setText = (id, val) => {
          const el = document.getElementById(id);
          if (el) el.textContent = val;
        };
        const setHTML = (id, val) => {
          const el = document.getElementById(id);
          if (el) el.innerHTML = val;
        };
        setText("ov-claude-total", formatTokens(c.all && c.all.total));
        setHTML("ov-claude-5h",  renderWindowLine("5h",  c["5h"]));
        setHTML("ov-claude-7d",  renderWindowLine("7d",  c["7d"]));
        setHTML("ov-claude-all", renderWindowLine("all", c.all));

        const x = u.codex || {};
        setText("ov-codex-total", formatTokens(x.all && x.all.total));
        setHTML("ov-codex-5h",  renderWindowLine("5h",  x["5h"]));
        setHTML("ov-codex-7d",  renderWindowLine("7d",  x["7d"]));
        setHTML("ov-codex-all", renderWindowLine("all", x.all));

        const codex5hEl   = document.getElementById("ov-rl-codex-5h");
        const codexWeekEl = document.getElementById("ov-rl-codex-week");
        const claude5hEl  = document.getElementById("ov-rl-claude-5h");
        const claudeWeekEl = document.getElementById("ov-rl-claude-week");
        const claudeModelsEl = document.getElementById("ov-rl-claude-models");
        const metaEl = document.getElementById("ov-rl-meta");
        const metaBits = [];

        // Topbar usage bars. The cards above show the full breakdown; the
        // header shows just the 5h utilization per tool as an at-a-glance
        // strip. Coerce nullish/NaN to "na" so the bar dims rather than
        // pinning to 0% and looking like a healthy quota.
        function setHeaderUsage(tool, pct) {
          const item = document.querySelector(`.usage-item[data-tool="${tool}"]`);
          const bar = document.getElementById(`usage-bar-${tool}`);
          const fill = bar ? bar.querySelector(".usage-bar-fill") : null;
          const pctEl = document.getElementById(`usage-pct-${tool}`);
          if (!item || !bar || !fill || !pctEl) return;
          const n = pct == null || isNaN(pct) ? null : Math.max(0, Math.min(100, Number(pct)));
          if (n === null) {
            item.dataset.state = "na";
            fill.style.width = "0%";
            pctEl.textContent = "—";
            bar.setAttribute("aria-valuenow", "0");
            return;
          }
          item.dataset.state = n >= 90 ? "crit" : n >= 70 ? "warn" : "ok";
          fill.style.width = n + "%";
          pctEl.textContent = formatPct(n);
          bar.setAttribute("aria-valuenow", String(Math.round(n)));
        }

        // ----- Claude (OAuth /api/oauth/usage) -----
        const claudeRL = (u.claude || {}).rate_limits || null;
        if (claudeRL && claudeRL.available && claudeRL.data) {
          const d = claudeRL.data;
          const fh = d.five_hour;
          const sd = d.seven_day;
          if (fh) {
            claude5hEl.innerHTML = `<strong>${formatPct(fh.utilization)}</strong> <span style="color:var(--fg-dim);font-size:11px">${formatResetIn(fh.resets_at)}</span>`;
            setHeaderUsage("claude", fh.utilization);
          } else {
            claude5hEl.textContent = "—";
            setHeaderUsage("claude", null);
          }
          if (sd) {
            claudeWeekEl.innerHTML = `<strong>${formatPct(sd.utilization)}</strong> <span style="color:var(--fg-dim);font-size:11px">${formatResetIn(sd.resets_at)}</span>`;
          } else {
            claudeWeekEl.textContent = "—";
          }
          const perModel = [];
          if (d.seven_day_opus && d.seven_day_opus.utilization != null) {
            perModel.push(`opus ${formatPct(d.seven_day_opus.utilization)}`);
          }
          if (d.seven_day_sonnet && d.seven_day_sonnet.utilization != null) {
            perModel.push(`sonnet ${formatPct(d.seven_day_sonnet.utilization)}`);
          }
          claudeModelsEl.textContent = perModel.length ? `weekly per-model: ${perModel.join(" · ")}` : "";
          if (claudeRL.tier) metaBits.push(`Claude tier: ${claudeRL.tier.replace(/^default_claude_/, "")}`);
        } else {
          const reason = claudeRL && claudeRL.error ? claudeRL.error : "no claude oauth data";
          const errMarkup = `<span style="opacity:0.55" title="${escape(reason)}">n/a</span>`;
          claude5hEl.innerHTML = errMarkup;
          claudeWeekEl.innerHTML = errMarkup;
          claudeModelsEl.textContent = "";
          setHeaderUsage("claude", null);
        }

        // ----- Codex (rate_limits from latest token_count event) -----
        // Dashboard reads local rollout files; the IDE queries OpenAI's API
        // live. When a window's resets_at has passed the server marks it
        // stale — render dimmed so users don't mistake an old snapshot for
        // current state.
        const codexRL = x.rate_limits;
        function renderCodexWindow(win) {
          if (!win || win.used_percent == null) return "no data";
          const pct = formatPct(win.used_percent);
          const reset = formatResetIn(win.resets_at);
          if (win.stale) {
            return `<span style="opacity:0.55" title="Window rolled over since last Codex run — value is historical. The IDE shows live API data; the dashboard only sees what's in local rollout files. Run codex once to refresh.">${pct} <span style="font-size:11px">(stale)</span></span>`;
          }
          return `<strong>${pct}</strong> <span style="color:var(--fg-dim);font-size:11px">${reset}</span>`;
        }
        if (!codexRL) {
          codex5hEl.textContent = "no data";
          codexWeekEl.textContent = "no data";
          metaBits.push("run codex once to populate");
          setHeaderUsage("codex", null);
        } else {
          codex5hEl.innerHTML   = renderCodexWindow(codexRL.primary);
          codexWeekEl.innerHTML = renderCodexWindow(codexRL.secondary);
          const p = codexRL.primary;
          // Stale snapshots (resets_at in the past) are dimmed in the card
          // strip; mirror that here by treating them as no-data rather than
          // pretending the old number reflects current quota.
          setHeaderUsage("codex", (p && !p.stale) ? p.used_percent : null);
          if (codexRL.plan_type) metaBits.push(`Codex plan: ${codexRL.plan_type}`);
          if (codexRL.last_event_at) {
            try {
              metaBits.push(`Codex seen ${new Date(codexRL.last_event_at).toLocaleString()}`);
            } catch (e) {
              // Locale formatting of a bad timestamp shouldn't kill the rest
              // of the meta strip, but operators should see why the "Codex
              // seen …" string is missing.
              console.warn("[dashboard] codex last_event_at format failed: " + (e && e.message ? e.message : e));
            }
          }
        }
        metaEl.textContent = metaBits.join(" · ");
      } catch (err) {
        console.error(err);
        // Surface the failure to the operator via the toast stack if the
        // dashboard provides a #token-usage-msg channel; otherwise fall
        // back to a console.warn (no msg element available in markup).
        const msg = "token usage failed: " + (err && err.message ? err.message : err);
        if ($("#token-usage-msg")) {
          setMsg("#token-usage-msg", "warn", msg, 5000);
        } else {
          console.warn("[dashboard] " + msg + " (no msg element available)");
        }
      } finally {
        if (timer) clearTimeout(timer);
        _tokenUsageInFlight = false;
      }
    }

    function renderActivity(plans, specs) {
      // Single lookup — see renderOverview for the rationale.
      const activity = $("#overview-activity");
      if (!activity) return;
      const items = [
        ...plans.map((p) => ({ kind: "plan", name: p })),
        ...specs.map((s) => ({ kind: "spec", name: s })),
      ]
        // Explicit String() coercion — the names are normally strings from
        // listDir(), but if a future caller hands us numeric or null entries
        // `localeCompare` throws. Coerce defensively to keep the sort stable.
        .sort((a, b) => String(b.name).localeCompare(String(a.name)))
        .slice(0, 8);
      delete activity.dataset.skeletoned;
      if (!items.length) {
        activity.innerHTML = `<div class="empty">No plans or specs yet. Run the planner.</div>`;
        return;
      }
      activity.innerHTML = `<table><thead><tr><th>Kind</th><th>Name</th></tr></thead><tbody>${
        items.map((it) => `<tr><td><span class="pill ${it.kind === "plan" ? "claude" : "codex"}">${it.kind}</span></td><td class="mono">${escape(it.name)}</td></tr>`).join("")
      }</tbody></table>`;
    }

    function renderModels(models) {
      // Single lookups + null-guards on every dataset/innerHTML target.
      // The previous shape guarded only #dispatch-toggle and #models-table,
      // leaving `dispatchCards.dataset.skeletoned` to throw if the dispatch
      // cards container was removed by markup edits.
      const tBtn = $("#dispatch-toggle");
      const modelsTable = $("#models-table");
      if (!tBtn || !modelsTable) return;
      const mode = models.dispatch_mode || "manual";
      const session = models.session || {};
      const dispatchCards = $("#dispatch-cards");
      if (dispatchCards) {
        delete dispatchCards.dataset.skeletoned;
        dispatchCards.innerHTML = `
          <div class="card"><h3>Dispatch mode</h3><div class="val big">${escape(mode)}</div></div>
          <div class="card"><h3>Session tool</h3><div class="val">${pillTool(session.tool)}</div></div>
          <div class="card"><h3>Session model</h3><div class="val mono">${escape(session.model || "—")}</div></div>
        `;
      }
      tBtn.dataset.current = mode;
      tBtn.textContent = mode === "auto" ? "Switch to manual" : "Switch to auto";
      const phases = ["session", "plan", "execute", "review", "rescue", "maintenance", "bootstrap"];
      const rows = phases.map((ph) => {
        const cfg = models[ph] || {};
        let resolved = "—";
        if (ph === "session") {
          resolved = "n/a";
        } else if (cfg.mode) resolved = cfg.mode;
        else if (mode === "auto" && session.tool && session.model) {
          if (cfg.tool !== session.tool) resolved = "dispatcher";
          else if (cfg.model !== session.model) resolved = "agent";
          else resolved = "inline";
        } else if (mode === "manual") resolved = "dispatcher";
        const pillCls = resolved === "inline" ? "good" : resolved === "agent" ? "warn" : (resolved === "n/a" ? "" : "claude");
        const showMode = ph !== "session";
        return `<tr data-phase="${ph}">
          <td class="mono"><strong>${ph}</strong></td>
          <td data-field="tool">${pillTool(cfg.tool)}</td>
          <td class="mono" data-field="model">${escape(cfg.model || "—")}</td>
          <td data-field="mode">${showMode ? (cfg.mode ? `<span class="pill warn">${cfg.mode}</span>` : `<span class="pill" style="color:var(--fg-dim)">auto</span>`) : "—"}</td>
          <td><span class="pill ${pillCls}">${resolved}</span></td>
          <td style="text-align:right"><button class="btn secondary" style="padding:3px 10px;font-size:11px" data-edit-phase="${escape(ph)}">Edit</button></td>
        </tr>`;
      }).join("");
      delete modelsTable.dataset.skeletoned;
      modelsTable.innerHTML = `<table>
        <thead><tr><th>Phase</th><th>Tool</th><th>Model</th><th>Override</th><th>Resolved</th><th></th></tr></thead>
        <tbody>${rows}</tbody>
      </table>`;
      _modelsCache = models;
      // Wire ONE delegated click listener on the models table for Edit/Save
      // buttons (idempotent via module flag — canonical pattern from jobs.js).
      // Avoids inline onclick="" handlers which are a latent XSS pattern.
      if (!_modelsTableDelegationWired) {
        modelsTable.addEventListener("click", (e) => {
          const editBtn = e.target.closest("[data-edit-phase]");
          if (editBtn) { editPhaseRow(editBtn.dataset.editPhase); return; }
          const saveBtn = e.target.closest("[data-save-phase]");
          if (saveBtn) { savePhaseRow(saveBtn.dataset.savePhase); return; }
          const cancelBtn = e.target.closest("[data-phase-cancel]");
          if (cancelBtn) { loadAll(); return; }
        });
        _modelsTableDelegationWired = true;
      }
    }

    var _modelsCache = null;
    var _modelsTableDelegationWired = false;

    // Mirror the comments at the top of .ai/models.yaml.
    // Catalog of available models per tool. Listed newest-first within each family.
    // Last refreshed: May 2026.
    //   Claude — https://platform.claude.com/docs/en/about-claude/models/overview
    //   Codex  — https://developers.openai.com/codex/models
    var MODELS_BY_TOOL = {
      claude: [
        "claude-opus-4-7",     // most capable, agentic coding flagship
        "claude-opus-4-6",
        "claude-sonnet-4-6",   // 1M ctx, balanced speed/intelligence
        "claude-haiku-4-5",    // fastest / cheapest
      ],
      codex: [
        "gpt-5.5",             // current frontier
        "gpt-5.4",             // computer-use, 1M ctx
        "gpt-5.4-mini",        // fast subagent
        "gpt-5.3-codex",       // previous codex generation
      ],
    };

    function modelOptionsHtml(tool, currentModel) {
      const list = MODELS_BY_TOOL[tool] || [];
      const opts = list.slice();
      if (currentModel && !opts.includes(currentModel)) opts.unshift(currentModel);
      return opts.map((m) => `<option value="${escape(m)}"${m === currentModel ? " selected" : ""}>${escape(m)}${list.includes(m) ? "" : " (custom)"}</option>`).join("");
    }

    function editPhaseRow(phase) {
      if (!$("#models-table")) return;
      const tr = document.querySelector(`#models-table tr[data-phase="${phase}"]`);
      if (!tr) return;
      const cfg = (_modelsCache && _modelsCache[phase]) || {};
      const showMode = phase !== "session";
      const initialTool = cfg.tool || "claude";
      tr.innerHTML = `
        <td class="mono"><strong>${phase}</strong></td>
        <td>
          <select id="pe-tool" class="cmp-select">
            <option value="claude" ${initialTool === "claude" ? "selected" : ""}>claude</option>
            <option value="codex" ${initialTool === "codex" ? "selected" : ""}>codex</option>
          </select>
        </td>
        <td>
          <select id="pe-model" class="cmp-select mono">
            ${modelOptionsHtml(initialTool, cfg.model || "")}
          </select>
        </td>
        <td>
          ${showMode ? `<select id="pe-mode" class="cmp-select">
            <option value=""${!cfg.mode ? " selected" : ""}>(auto)</option>
            <option value="inline"${cfg.mode === "inline" ? " selected" : ""}>inline</option>
            <option value="agent"${cfg.mode === "agent" ? " selected" : ""}>agent</option>
            <option value="dispatcher"${cfg.mode === "dispatcher" ? " selected" : ""}>dispatcher</option>
          </select>` : "—"}
        </td>
        <td>
          ${showMode ? `<select id="pe-reff" class="cmp-select" title="reasoning_effort (codex only)">
            <option value=""${!cfg.reasoning_effort ? " selected" : ""}>(default)</option>
            <option value="xhigh"${cfg.reasoning_effort === "xhigh" ? " selected" : ""}>xhigh</option>
            <option value="high"${cfg.reasoning_effort === "high" ? " selected" : ""}>high</option>
            <option value="medium"${cfg.reasoning_effort === "medium" ? " selected" : ""}>medium</option>
            <option value="low"${cfg.reasoning_effort === "low" ? " selected" : ""}>low</option>
          </select>` : "—"}
        </td>
        <td style="text-align:right;white-space:nowrap">
          <button class="btn" style="padding:3px 10px;font-size:11px" data-save-phase="${escape(phase)}">Save</button>
          <button class="btn secondary" style="padding:3px 10px;font-size:11px" data-phase-cancel="1">Cancel</button>
        </td>
      `;
      // When tool changes, repopulate the model dropdown for that tool.
      $("#pe-tool").addEventListener("change", (e) => {
        const newTool = e.target.value;
        const sel = $("#pe-model");
        const keep = sel.value;  // try to preserve user's current pick if compatible
        sel.innerHTML = modelOptionsHtml(newTool, MODELS_BY_TOOL[newTool]?.includes(keep) ? keep : "");
      });
    }

    async function savePhaseRow(phase) {
      if (!$("#pe-tool")) return;
      const tool = $("#pe-tool")?.value;
      const model = $("#pe-model")?.value.trim();
      const showMode = phase !== "session";
      const payload = { phase, tool, model };
      if (showMode) {
        payload.mode = $("#pe-mode")?.value || "";
        payload.reasoning_effort = $("#pe-reff")?.value || "";
      }
      if (!model) {
        setMsg("#models-phase-msg", "err", "Model is required");
        return;
      }
      try {
        await postJson("/api/models/phase", payload);
        setMsg("#models-phase-msg", "ok", "Saved " + phase, 4000);
        await loadAll();
      } catch (e) {
        setMsg("#models-phase-msg", "err", "Save failed: " + e.message);
      }
    }

    function renderProject(project, rawText) {
      // Resolve every target up front and null-guard each independently.
      // The previous shape only checked #project-stack; if a future markup
      // edit drops #project-boundaries or #project-raw, the unguarded
      // dataset access aborted the rest of the loader.
      const stack = $("#project-stack");
      if (!stack) return;
      const cmds = project.commands || {};
      const cmdRows = Object.entries(cmds).map(([k, v]) => {
        const arr = Array.isArray(v) ? v : [v];
        const val = arr.length && arr[0] ? arr.join(" && ") : "—";
        return `<tr><td class="mono">${k}</td><td class="mono">${escape(val)}</td></tr>`;
      }).join("");
      delete stack.dataset.skeletoned;
      stack.innerHTML = cmdRows
        ? `<table><thead><tr><th>Command</th><th>Value</th></tr></thead><tbody>${cmdRows}</tbody></table>`
        : `<div class="empty">No commands declared. Run bootstrap.</div>`;

      const b = project.boundaries || {};
      const boundaryRows = Object.entries(b).map(([k, v]) => {
        const arr = Array.isArray(v) ? v : [];
        const val = arr.length ? arr.map((x) => `<span class="pill">${escape(x)}</span>`).join(" ") : "—";
        return `<tr><td class="mono">${k}</td><td>${val}</td></tr>`;
      }).join("");
      const boundaries = $("#project-boundaries");
      if (boundaries) {
        delete boundaries.dataset.skeletoned;
        boundaries.innerHTML = boundaryRows
          ? `<table><thead><tr><th>Category</th><th>Entries</th></tr></thead><tbody>${boundaryRows}</tbody></table>`
          : `<div class="empty">No boundaries declared.</div>`;
      }

      const raw = $("#project-raw");
      if (raw) {
        delete raw.dataset.skeletoned;
        raw.textContent = rawText;
      }
    }

function renderMarkdown(el, text) {
  // Bail if the caller passed a missing element — `el.innerHTML = ...` would
  // otherwise throw and stop whichever loader chain invoked us.
  if (!el) return;
  if (el.dataset) delete el.dataset.skeletoned;
  el.innerHTML = DOMPurify.sanitize(marked.parse(text || ""));
}

    function countMemoryEntries(text) {
      // Defensive coerce — callers default to "" today but a future caller
      // passing null/undefined would throw on `.match`.
      text = text || "";
      const m = text.match(/^- \d{4}-\d{2}-\d{2}/gm);
      return m ? m.length : 0;
    }

    function buildList(containerSel, items, onSelect) {
      const el = $(containerSel);
      // Caller may run before the target container is in the DOM (or against
      // a stripped page). Bail rather than crashing on dataset access.
      if (!el) return;
      delete el.dataset.skeletoned;
      if (!items.length) {
        el.innerHTML = `<div class="empty">Empty</div>`;
        return;
      }
      el.innerHTML = items.map((it) => {
        const date = (it.match(/^(\d{4}-\d{2}-\d{2})/) || [])[1] || "";
        return `<div class="list-item" data-name="${escape(it)}">
          <div>${escape(it.replace(/\.md$/, ""))}</div>
          ${date ? `<div class="sub">${date}</div>` : ""}
        </div>`;
      }).join("");
      el.querySelectorAll(".list-item").forEach((li) => {
        li.addEventListener("click", () => {
          el.querySelectorAll(".list-item").forEach((x) => x.classList.remove("active"));
          li.classList.add("active");
          onSelect(li.dataset.name);
        });
      });
    }

    // Shared helper: pre-render N list-item skeletons into a `.list` container
    // so the page doesn't snap from empty to populated. Idempotent via
    // dataset.skeletoned, matching the pattern in agents.js/skills.js.
    function renderListSkeletons(containerSel, n) {
      const el = $(containerSel);
      if (!el || el.dataset.skeletoned) return;
      el.innerHTML = Array.from({ length: n || 6 }).map(() => `
        <div class="skeleton-list-item">
          <span class="skeleton skeleton-row-h"></span>
          <span class="skeleton skeleton-row-sub"></span>
        </div>
      `).join("");
      el.dataset.skeletoned = "1";
    }

    function escape(s) {
      return String(s ?? "").replace(/[&<>"']/g, (c) => ({
        "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
      }[c]));
    }
    // Preferred alias for new code — `escape` shadows the legacy
    // `window.escape` (URL escaping) which is a footgun for readers
    // skimming for HTML escaping. Existing call sites keep working; the
    // IIFE-local escape helpers in other files can be migrated to escHtml
    // incrementally without a sweeping rename.
    var escHtml = escape;

    // ----- Modal focus trap -----
    // aria-modal="true" tells AT to treat outside as inert, but
    // keyboard users can still Tab into the background. trapFocusInModal()
    // captures the previously-focused element, moves focus into the modal
    // pane (or first focusable child), and intercepts Tab/Shift+Tab so
    // focus stays inside until releaseFocusTrap() runs. Escape also fires
    // an optional onEscape callback so callers can close + restore in
    // one step. Idempotent: a second activate replaces the prior trap.
    var _focusTrapState = null;
    function _focusableInside(root) {
      if (!root) return [];
      var sel = [
        'a[href]', 'button:not([disabled])', 'input:not([disabled])',
        'select:not([disabled])', 'textarea:not([disabled])',
        '[tabindex]:not([tabindex="-1"])',
      ].join(",");
      return Array.from(root.querySelectorAll(sel))
        .filter(function (el) { return el.offsetParent !== null; });
    }
    function trapFocusInModal(modal, onEscape) {
      if (!modal) return;
      releaseFocusTrap();
      var prev = document.activeElement;
      var pane = modal.querySelector('[tabindex="-1"]') || modal;
      // Move focus inside on the next frame so the modal's display:flex
      // transition has settled before we start measuring focusable nodes.
      requestAnimationFrame(function () {
        var nodes = _focusableInside(modal);
        (nodes[0] || pane).focus();
      });
      function onKey(e) {
        if (e.key === "Escape" && typeof onEscape === "function") {
          e.preventDefault();
          onEscape();
          return;
        }
        if (e.key !== "Tab") return;
        var nodes = _focusableInside(modal);
        if (!nodes.length) { e.preventDefault(); pane.focus(); return; }
        var first = nodes[0], last = nodes[nodes.length - 1];
        if (e.shiftKey && document.activeElement === first) {
          e.preventDefault(); last.focus();
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault(); first.focus();
        }
      }
      modal.addEventListener("keydown", onKey);
      _focusTrapState = { modal: modal, prev: prev, onKey: onKey };
    }
    function releaseFocusTrap() {
      if (!_focusTrapState) return;
      try { _focusTrapState.modal.removeEventListener("keydown", _focusTrapState.onKey); } catch (_) {}
      var prev = _focusTrapState.prev;
      _focusTrapState = null;
      // Restore prior focus if it's still in the DOM and focusable.
      if (prev && typeof prev.focus === "function" && document.contains(prev)) {
        try { prev.focus(); } catch (_) {}
      }
    }
    window.trapFocusInModal = trapFocusInModal;
    window.releaseFocusTrap = releaseFocusTrap;

    // ----- POST helper -----
    async function postJson(path, body) {
      const r = await fetch(path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body || {}),
      });
      let data = null;
      try { data = await r.json(); } catch (_) { /* no body */ }
      if (!r.ok) {
        const msg = (data && data.error) || ("HTTP " + r.status);
        throw new Error(msg);
      }
      return data || {};
    }

    // setMsg now routes ALL status messages through the central toast
    // stack. The `sel` argument is treated as a channel key so repeated
    // calls to the same form (e.g. "saving..." -> "saved") replace the
    // existing toast instead of stacking. Empty text clears the channel.
    function setMsg(sel, kind, text, timeoutMs) {
      showToast(sel, kind || "", text || "", timeoutMs);
    }

    // ----- Toast stack -----
    // Each channel holds at most one toast; new calls on the same channel
    // reuse the element so updates read as one continuous message.
    // position:fixed at top-center of the viewport so layouts never shift.
    var TOASTS = new Map();   // channel -> { el, timer }
    // Named constants for the toast dismiss cadences. Errors stay on screen
    // longer because operators want to read them; warnings get a medium
    // window; everything else (ok / info) auto-dismisses quickest. The
    // 220ms exit transition matches the CSS `.toast.out` keyframe.
    var TOAST_DISMISS_MS_OK = 3500;
    var TOAST_DISMISS_MS_WARN = 4500;
    var TOAST_DISMISS_MS_ERR = 6000;
    var TOAST_EXIT_ANIM_MS = 220;

    function _toastRoot() {
      // index.html declares <div id="toast-root"> already, so on the normal
      // dashboard page this short-circuit returns immediately. The
      // createElement fallback below only runs in unusual injection scenarios
      // (tests, embeds, stripped shells of the page) where the host HTML
      // omitted the root.
      if ($("#toast-root")) return $("#toast-root");
      const root = document.createElement("div");
      root.id = "toast-root";
      root.setAttribute("aria-live", "polite");
      document.body.appendChild(root);
      return root;
    }

    function hideToast(channel) {
      const entry = TOASTS.get(channel);
      if (!entry) return;
      clearTimeout(entry.timer);
      entry.timer = null;
      // Drop the Map entry up-front so a second hideToast for the same
      // channel (or a replacement showToast that races the 220ms exit
      // animation) doesn't keep the orphan element/timer alive in the Map.
      // The DOM removal still completes via the setTimeout below.
      TOASTS.delete(channel);
      entry.el.classList.remove("in");
      entry.el.classList.add("out");
      setTimeout(() => {
        if (entry.el.parentNode) entry.el.parentNode.removeChild(entry.el);
      }, TOAST_EXIT_ANIM_MS);
    }

    function showToast(channel, kind, text, timeoutMs) {
      if (!text) { hideToast(channel); return; }
      let entry = TOASTS.get(channel);
      if (entry) {
        clearTimeout(entry.timer);
        entry.el.className = "toast " + (kind || "") + " in";
        entry.el.querySelector(".toast-text").textContent = text;
      } else {
        const root = _toastRoot();
        const el = document.createElement("div");
        el.className = "toast " + (kind || "");
        // Info icon on the left + text on the right. The icon's colour is
        // driven by the severity class on .toast (see styles.css).
        el.innerHTML =
          '<span class="toast-ico" aria-hidden="true">'
          + '<svg viewBox="0 0 20 20" width="18" height="18">'
          +   '<circle cx="10" cy="10" r="9" fill="none" stroke="currentColor" stroke-width="1.4"/>'
          +   '<circle cx="10" cy="5.5" r="1.2" fill="currentColor"/>'
          +   '<path d="M10 9 V14.5" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/>'
          + '</svg>'
          + '</span>'
          + '<span class="toast-text"></span>';
        el.querySelector(".toast-text").textContent = text;
        root.appendChild(el);
        // Two RAFs so the initial styles paint before the .in class
        // triggers the transition (otherwise it would just snap in).
        requestAnimationFrame(() => requestAnimationFrame(() => el.classList.add("in")));
        entry = { el, timer: null };
        TOASTS.set(channel, entry);
      }
      // Auto-dismiss. Explicit timeoutMs wins. Otherwise, give errors more
      // time on screen — operator wants to read those, not just "saved".
      const dismissAfter = (timeoutMs != null)
        ? timeoutMs
        : (kind === "err" ? TOAST_DISMISS_MS_ERR : kind === "warn" ? TOAST_DISMISS_MS_WARN : TOAST_DISMISS_MS_OK);
      if (dismissAfter > 0) {
        entry.timer = setTimeout(() => hideToast(channel), dismissAfter);
      }
    }

    // ----- Memory form -----
    async function submitMemory() {
      if (!$("#mem-submit")) return;
      const btn = $("#mem-submit");
      // Resolve input refs up-front + null-guard together — guards against
      // a partial-DOM variant where the submit button exists but the inputs
      // were stripped (would otherwise crash with TypeError on .value).
      const topicEl = $("#mem-topic"); const factEl = $("#mem-fact");
      if (!topicEl || !factEl) return;
      const topic = topicEl.value.trim();
      const fact = factEl.value.trim();
      if (!topic || !fact) { setMsg("#mem-msg", "err", "topic and fact required"); return; }
      btn.disabled = true;
      setMsg("#mem-msg", "", "saving…");
      try {
        const res = await postJson("/api/memory", { topic, fact });
        topicEl.value = "";
        factEl.value = "";
        setMsg("#mem-msg", "ok", "added: " + res.line, 4000);
        const memText = await getText(".ai/memory.md").catch(() => "");
        renderMarkdown($("#memory-doc"), memText);
        const countMemoryEl = $("#count-memory");
        if (countMemoryEl) countMemoryEl.textContent = countMemoryEntries(memText);
      } catch (e) {
        setMsg("#mem-msg", "err", e.message);
      } finally {
        btn.disabled = false;
      }
    }

    // ----- Decisions form -----
    async function submitDecision() {
      if (!$("#dec-submit")) return;
      const btn = $("#dec-submit");
      // Required fields resolved + null-guarded together; optional fields
      // use optional chaining so a partial form (e.g. minimal-DOM variant)
      // degrades gracefully instead of TypeError'ing on `.value`.
      const decisionEl = $("#dec-decision"); const whyEl = $("#dec-why");
      if (!decisionEl || !whyEl) return;
      const payload = {
        date: $("#dec-date")?.value || undefined,
        decision: decisionEl.value.trim(),
        why: whyEl.value.trim(),
        consequence: $("#dec-consequence")?.value.trim() || "",
        revisit: $("#dec-revisit")?.value.trim() || "",
      };
      if (!payload.decision || !payload.why) {
        setMsg("#dec-msg", "err", "decision and why required");
        return;
      }
      btn.disabled = true;
      setMsg("#dec-msg", "", "saving…");
      try {
        await postJson("/api/decisions", payload);
        ["#dec-decision", "#dec-why", "#dec-consequence", "#dec-revisit"].forEach((s) => {
          const el = $(s); if (el) el.value = "";
        });
        setMsg("#dec-msg", "ok", "decision added", 4000);
        const txt = await getText(".ai/decisions.md").catch(() => "");
        renderMarkdown($("#decisions-doc"), txt);
      } catch (e) {
        setMsg("#dec-msg", "err", e.message);
      } finally {
        btn.disabled = false;
      }
    }
