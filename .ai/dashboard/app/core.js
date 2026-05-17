// .ai/dashboard/app/core.js -- extracted from app.js (was lines 1..572)
// NOTE: top-level const/let were converted to var so identifiers cross <script> boundaries.

    var $ = (sel) => document.querySelector(sel);
    var $$ = (sel) => Array.from(document.querySelectorAll(sel));

    marked.setOptions({ gfm: true, breaks: false });

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
      });
    });

    // Restore last opened view across reloads.
    (function restoreView() {
      let saved = null;
      try { saved = localStorage.getItem("dash.view"); } catch (_) { /* ignore */ }
      if (!saved || saved === "overview") return;
      const btn = document.querySelector(`nav button[data-view="${saved}"]`);
      if (btn) btn.click();
    })();

    // ----- form button wiring -----
    document.addEventListener("DOMContentLoaded", () => {
      $("#mem-submit").addEventListener("click", submitMemory);
      $("#dec-submit").addEventListener("click", submitDecision);
      $("#dispatch-toggle").addEventListener("click", toggleDispatchMode);
      $("#run-submit").addEventListener("click", submitJob);
      $("#run-task").addEventListener("keydown", (e) => {
        if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); submitJob(); }
      });
      // Default decision date = today
      const today = new Date().toISOString().slice(0, 10);
      $("#dec-date").value = today;
      // Enter-to-submit on Memory form
      ["#mem-topic", "#mem-fact"].forEach((s) => {
        $(s).addEventListener("keydown", (e) => { if (e.key === "Enter") submitMemory(); });
      });
      // Skills search input
      $("#skills-search")?.addEventListener("input", (e) => {
        _skillsState.query = e.target.value;
        renderSkillsGrid();
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
      document.addEventListener("keydown", (e) => {
        if (e.key !== "Escape") return;
        if (!$("#proposal-modal").hidden) closeProposalModal();
        else if (!$("#skill-detail-modal").hidden) closeSkillDetail();
      });
      // Density toggle: persist preference in localStorage and apply on boot.
      const applyDensity = (mode) => {
        document.body.classList.toggle("density-compact", mode === "compact");
        const btn = $("#density-toggle");
        if (btn) btn.textContent = mode === "compact" ? "comfortable" : "compact";
      };
      const savedDensity = localStorage.getItem("dash.density") || "comfortable";
      applyDensity(savedDensity);
      $("#density-toggle")?.addEventListener("click", () => {
        const isCompact = document.body.classList.contains("density-compact");
        const next = isCompact ? "comfortable" : "compact";
        localStorage.setItem("dash.density", next);
        applyDensity(next);
      });
    });

    // ----- fetch helpers -----
    async function getText(path) {
      const r = await fetch("/" + path.replace(/^\/+/, ""), { cache: "no-store" });
      if (!r.ok) throw new Error(path + " -> HTTP " + r.status);
      return r.text();
    }
    async function getYaml(path) {
      return jsyaml.load(await getText(path));
    }
    async function listDir(path) {
      const r = await fetch("/api/list?path=" + encodeURIComponent(path), { cache: "no-store" });
      if (!r.ok) return [];
      const data = await r.json();
      return data.entries || [];
    }

    // ----- renderers -----
    function pillTool(tool) {
      const cls = tool === "claude" ? "claude" : tool === "codex" ? "codex" : "";
      return `<span class="pill ${cls}">${tool || "?"}</span>`;
    }

    function formatTokens(n) {
      if (n == null || isNaN(n)) return "—";
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
      if (n < 1) return n.toFixed(2) + "%";
      if (n < 10) return n.toFixed(1) + "%";
      return Math.round(n) + "%";
    }

    function renderWindowLine(prefix, win) {
      if (!win || !win.total) return `${prefix}: <span style="opacity:0.6">none</span>`;
      return `${prefix}: ${formatTokens(win.total)} · ${formatModelShares(win.by_model)}`;
    }

    function renderOverview(project, models, memoryEntries, plansCount, specsCount) {
      const stack = (project.stack || []).join(", ") || "—";
      const pms = (project.package_managers || []).join(", ") || "—";
      const dispatchMode = models.dispatch_mode || "manual";
      const dispatchPill = dispatchMode === "auto"
        ? `<span class="pill good">${dispatchMode}</span>`
        : `<span class="pill warn">${dispatchMode}</span>`;
      $("#overview-cards").innerHTML = `
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

    async function loadTokenUsage() {
      if (!document.getElementById("ov-claude-total")) return;
      try {
        const r = await fetch("/api/usage/total", { cache: "no-store" });
        if (!r.ok) return;
        const u = await r.json();
        const c = u.claude || {};
        document.getElementById("ov-claude-total").textContent = formatTokens(c.all && c.all.total);
        document.getElementById("ov-claude-5h").innerHTML  = renderWindowLine("5h",  c["5h"]);
        document.getElementById("ov-claude-7d").innerHTML  = renderWindowLine("7d",  c["7d"]);
        document.getElementById("ov-claude-all").innerHTML = renderWindowLine("all", c.all);

        const x = u.codex || {};
        document.getElementById("ov-codex-total").textContent = formatTokens(x.all && x.all.total);
        document.getElementById("ov-codex-5h").innerHTML  = renderWindowLine("5h",  x["5h"]);
        document.getElementById("ov-codex-7d").innerHTML  = renderWindowLine("7d",  x["7d"]);
        document.getElementById("ov-codex-all").innerHTML = renderWindowLine("all", x.all);

        const codex5hEl   = document.getElementById("ov-rl-codex-5h");
        const codexWeekEl = document.getElementById("ov-rl-codex-week");
        const claude5hEl  = document.getElementById("ov-rl-claude-5h");
        const claudeWeekEl = document.getElementById("ov-rl-claude-week");
        const claudeModelsEl = document.getElementById("ov-rl-claude-models");
        const metaEl = document.getElementById("ov-rl-meta");
        const metaBits = [];

        // ----- Claude (OAuth /api/oauth/usage) -----
        const claudeRL = (u.claude || {}).rate_limits || null;
        if (claudeRL && claudeRL.available && claudeRL.data) {
          const d = claudeRL.data;
          const fh = d.five_hour;
          const sd = d.seven_day;
          if (fh) {
            claude5hEl.innerHTML = `<strong>${formatPct(fh.utilization)}</strong> <span style="color:var(--fg-dim);font-size:11px">${formatResetIn(fh.resets_at)}</span>`;
          } else {
            claude5hEl.textContent = "—";
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
        }

        // ----- Codex (rate_limits from latest token_count event) -----
        const codexRL = x.rate_limits;
        if (!codexRL) {
          codex5hEl.textContent = "no data";
          codexWeekEl.textContent = "no data";
          metaBits.push("run codex once to populate");
        } else {
          const p = codexRL.primary || {};
          const s = codexRL.secondary || {};
          codex5hEl.innerHTML   = `<strong>${formatPct(p.used_percent)}</strong> <span style="color:var(--fg-dim);font-size:11px">${formatResetIn(p.resets_at)}</span>`;
          codexWeekEl.innerHTML = `<strong>${formatPct(s.used_percent)}</strong> <span style="color:var(--fg-dim);font-size:11px">${formatResetIn(s.resets_at)}</span>`;
          if (codexRL.plan_type) metaBits.push(`Codex plan: ${codexRL.plan_type}`);
          if (codexRL.last_event_at) {
            try { metaBits.push(`Codex seen ${new Date(codexRL.last_event_at).toLocaleString()}`); } catch (_) { /* ignore */ }
          }
        }
        metaEl.textContent = metaBits.join(" · ");
      } catch (err) {
        console.error(err);
      }
    }

    function renderActivity(plans, specs) {
      const items = [
        ...plans.map((p) => ({ kind: "plan", name: p })),
        ...specs.map((s) => ({ kind: "spec", name: s })),
      ].sort((a, b) => b.name.localeCompare(a.name)).slice(0, 8);
      if (!items.length) {
        $("#overview-activity").innerHTML = `<div class="empty">No plans or specs yet. Run the planner.</div>`;
        return;
      }
      $("#overview-activity").innerHTML = `<table><thead><tr><th>Kind</th><th>Name</th></tr></thead><tbody>${
        items.map((it) => `<tr><td><span class="pill ${it.kind === "plan" ? "claude" : "codex"}">${it.kind}</span></td><td class="mono">${escape(it.name)}</td></tr>`).join("")
      }</tbody></table>`;
    }

    function renderModels(models) {
      const mode = models.dispatch_mode || "manual";
      const session = models.session || {};
      $("#dispatch-cards").innerHTML = `
        <div class="card"><h3>Dispatch mode</h3><div class="val big">${escape(mode)}</div></div>
        <div class="card"><h3>Session tool</h3><div class="val">${pillTool(session.tool)}</div></div>
        <div class="card"><h3>Session model</h3><div class="val mono">${escape(session.model || "—")}</div></div>
      `;
      const tBtn = $("#dispatch-toggle");
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
          <td style="text-align:right"><button class="btn secondary" style="padding:3px 10px;font-size:11px" onclick="editPhaseRow('${ph}')">Edit</button></td>
        </tr>`;
      }).join("");
      $("#models-table").innerHTML = `<table>
        <thead><tr><th>Phase</th><th>Tool</th><th>Model</th><th>Override</th><th>Resolved</th><th></th></tr></thead>
        <tbody>${rows}</tbody>
      </table>`;
      _modelsCache = models;
    }

    var _modelsCache = null;

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
          <button class="btn" style="padding:3px 10px;font-size:11px" onclick="savePhaseRow('${phase}')">Save</button>
          <button class="btn secondary" style="padding:3px 10px;font-size:11px" onclick="loadAll()">Cancel</button>
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
      const tool = $("#pe-tool")?.value;
      const model = $("#pe-model")?.value.trim();
      const showMode = phase !== "session";
      const payload = { phase, tool, model };
      if (showMode) {
        payload.mode = $("#pe-mode")?.value || "";
        payload.reasoning_effort = $("#pe-reff")?.value || "";
      }
      if (!model) {
        alert("model is required");
        return;
      }
      try {
        await postJson("/api/models/phase", payload);
        await loadAll();
      } catch (e) {
        alert("save failed: " + e.message);
      }
    }

    function renderProject(project, rawText) {
      const cmds = project.commands || {};
      const cmdRows = Object.entries(cmds).map(([k, v]) => {
        const arr = Array.isArray(v) ? v : [v];
        const val = arr.length && arr[0] ? arr.join(" && ") : "—";
        return `<tr><td class="mono">${k}</td><td class="mono">${escape(val)}</td></tr>`;
      }).join("");
      $("#project-stack").innerHTML = cmdRows
        ? `<table><thead><tr><th>Command</th><th>Value</th></tr></thead><tbody>${cmdRows}</tbody></table>`
        : `<div class="empty">No commands declared. Run bootstrap.</div>`;

      const b = project.boundaries || {};
      const boundaryRows = Object.entries(b).map(([k, v]) => {
        const arr = Array.isArray(v) ? v : [];
        const val = arr.length ? arr.map((x) => `<span class="pill">${escape(x)}</span>`).join(" ") : "—";
        return `<tr><td class="mono">${k}</td><td>${val}</td></tr>`;
      }).join("");
      $("#project-boundaries").innerHTML = boundaryRows
        ? `<table><thead><tr><th>Category</th><th>Entries</th></tr></thead><tbody>${boundaryRows}</tbody></table>`
        : `<div class="empty">No boundaries declared.</div>`;

      $("#project-raw").textContent = rawText;
    }

    function renderMarkdown(el, text) { el.innerHTML = marked.parse(text || ""); }

    function countMemoryEntries(text) {
      const m = text.match(/^- \d{4}-\d{2}-\d{2}/gm);
      return m ? m.length : 0;
    }

    function buildList(containerSel, items, onSelect) {
      const el = $(containerSel);
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

    function escape(s) {
      return String(s ?? "").replace(/[&<>"']/g, (c) => ({
        "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
      }[c]));
    }

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

    function setMsg(sel, kind, text, timeoutMs) {
      const el = $(sel);
      el.className = "form-msg " + (kind || "");
      el.textContent = text || "";
      if (timeoutMs) {
        clearTimeout(el._t);
        el._t = setTimeout(() => { el.textContent = ""; el.className = "form-msg"; }, timeoutMs);
      }
    }

    // ----- Memory form -----
    async function submitMemory() {
      const btn = $("#mem-submit");
      const topic = $("#mem-topic").value.trim();
      const fact = $("#mem-fact").value.trim();
      if (!topic || !fact) { setMsg("#mem-msg", "err", "topic and fact required"); return; }
      btn.disabled = true;
      setMsg("#mem-msg", "", "saving…");
      try {
        const res = await postJson("/api/memory", { topic, fact });
        $("#mem-topic").value = "";
        $("#mem-fact").value = "";
        setMsg("#mem-msg", "ok", "added: " + res.line, 4000);
        const memText = await getText(".ai/memory.md").catch(() => "");
        renderMarkdown($("#memory-doc"), memText);
        $("#count-memory").textContent = countMemoryEntries(memText);
      } catch (e) {
        setMsg("#mem-msg", "err", e.message);
      } finally {
        btn.disabled = false;
      }
    }

    // ----- Decisions form -----
    async function submitDecision() {
      const btn = $("#dec-submit");
      const payload = {
        date: $("#dec-date").value || undefined,
        decision: $("#dec-decision").value.trim(),
        why: $("#dec-why").value.trim(),
        consequence: $("#dec-consequence").value.trim(),
        revisit: $("#dec-revisit").value.trim(),
      };
      if (!payload.decision || !payload.why) {
        setMsg("#dec-msg", "err", "decision and why required");
        return;
      }
      btn.disabled = true;
      setMsg("#dec-msg", "", "saving…");
      try {
        await postJson("/api/decisions", payload);
        ["#dec-decision", "#dec-why", "#dec-consequence", "#dec-revisit"].forEach((s) => { $(s).value = ""; });
        setMsg("#dec-msg", "ok", "decision added", 4000);
        const txt = await getText(".ai/decisions.md").catch(() => "");
        renderMarkdown($("#decisions-doc"), txt);
      } catch (e) {
        setMsg("#dec-msg", "err", e.message);
      } finally {
        btn.disabled = false;
      }
    }
