// .ai/dashboard/app/agents.js -- catalog view for .claude/agents/*.md across
// project + user (editable) and plugin marketplaces + cache (read-only).
// Pattern mirrors skills.js but is intentionally simpler: no proposals,
// no telemetry, no auto-improver hooks. Agents are catalog + detail only.

    var _agentsState = { all: [], sources: {}, filter: "all", query: "" };

    // Shorten an absolute path by collapsing the project root to "<repo>"
    // and the user's home to "~". Both values are derived dynamically from
    // the .claude/agents paths the backend reports per scope — never
    // hardcode usernames or project names in shared frontend code. Backend
    // may use \ while agent paths use /, so we compare normalised (always /).
    function shortPath(p) {
      const raw = String(p || "").replace(/[\\\/]+$/, "");
      if (!raw) return raw;
      const norm = (s) => String(s || "").replace(/\\/g, "/");
      const src = _agentsState.sources || {};
      const stripClaudeAgents = (s) =>
        norm(s).replace(/\/+\.claude\/+agents\/*$/, "");
      const repoRoot = stripClaudeAgents(src.project && src.project.path);
      const userHome = stripClaudeAgents(src.user && src.user.path);
      const sep = raw.indexOf("\\") >= 0 ? "\\" : "/";
      const normRaw = norm(raw);
      // Longest-prefix-first: repo root usually contains user home as a
      // prefix (e.g. ~/Documents/<repo>), so check repo first.
      if (repoRoot && normRaw.startsWith(repoRoot)) {
        const rest = normRaw.slice(repoRoot.length).replace(/^\/+/, "").replace(/\//g, sep);
        return rest ? "<repo>" + sep + rest : "<repo>";
      }
      if (userHome && normRaw.startsWith(userHome)) {
        const rest = normRaw.slice(userHome.length).replace(/^\/+/, "").replace(/\//g, sep);
        return rest ? "~" + sep + rest : "~";
      }
      return raw;
    }

    function parseAgentTools(raw) {
      let items = null;
      try {
        const parsed = JSON.parse(raw);
        if (Array.isArray(parsed)) items = parsed;
      } catch (_) {
        items = null;
      }
      if (!items) items = String(raw || "").split(/\s*,\s*/);
      return items
        .map((t) => String(t).replace(/^[\s\[\]"']+|[\s\[\]"']+$/g, ""))
        .filter(Boolean);
    }

    function agentFilterLabel(id) {
      if (id === "all") return "All";
      const src = _agentsState.sources || {};
      return (src[id] && src[id].label) || id;
    }

    // Pre-render skeleton placeholders so the page does not snap from
    // empty to fully-populated. The skeleton shapes match the real
    // card layout so there is no layout shift when data lands.
    function renderAgentsSkeletons() {
      const summary = $("#agents-summary");
      if (summary && !summary.dataset.skeletoned) {
        const labels = ["Total agents", "Project", "User (global)", "Plugin (market)", "Plugin (cache)"];
        summary.innerHTML = labels.map(() => `
          <div class="card skeleton-summary-card">
            <span class="skeleton skeleton-title"></span>
            <span class="skeleton skeleton-big"></span>
            <span class="skeleton skeleton-sub"></span>
          </div>
        `).join("");
        summary.dataset.skeletoned = "1";
      }
      const grid = $("#agents-grid");
      if (grid && !grid.dataset.skeletoned) {
        grid.innerHTML = Array.from({ length: 12 }).map(() => `
          <div class="card skill-card agent-card skeleton-agent-card">
            <span class="skeleton skeleton-h"></span>
            <span class="skeleton skeleton-desc-1"></span>
            <span class="skeleton skeleton-desc-2"></span>
            <span class="skeleton skeleton-desc-3"></span>
            <span class="skeleton skeleton-tools"></span>
            <span class="skeleton skeleton-path"></span>
            <span class="skeleton skeleton-meta"></span>
          </div>
        `).join("");
        grid.dataset.skeletoned = "1";
      }
    }

    async function loadAgents() {
      renderAgentsSkeletons();
      try {
        const r = await fetch("/api/agents/all", { cache: "no-store" });
        if (!r.ok) throw new Error("HTTP " + r.status);
        const data = await r.json();
        _agentsState.all = data.agents || [];
        _agentsState.sources = data.sources || {};
        $("#count-agents").textContent = _agentsState.all.length;
        renderAgentsSummary();
        renderAgentsFilters();
        renderAgentsGrid();
        loadAgentProposals();  // surface any pending proposals alongside the catalog
      } catch (e) {
        $("#count-agents").textContent = "!";
        const grid = $("#agents-grid");
        if (grid) grid.innerHTML = `<div class="err">${escape(e.message)}</div>`;
        setMsg("#agents-load", "err", "Agents load failed: " + e.message);
      }
    }

    function renderAgentsSummary() {
      const total = _agentsState.all.length;
      const src = _agentsState.sources;
      const card = (label, s) => {
        const status = s.exists ? "" : `<div class="val" style="color:var(--warn);font-size:11px">no files yet</div>`;
        return `<div class="card" data-editable="${s.editable ? "1" : "0"}">
          <h3>${escape(label)}</h3>
          <div class="val big">${s.count}</div>
          <div class="path" title="${escape(s.path || "")}">${escape(shortPath(s.path || ""))}</div>
          ${status}
        </div>`;
      };
      const entries = [
        ["project",       src.project],
        ["user",          src.user],
        ["plugin_market", src.plugin_market],
        ["plugin_cache",  src.plugin_cache],
      ];
      const html = [
        `<div class="card"><h3>Total agents</h3><div class="val big">${total}</div><div class="path">across all scopes</div></div>`,
        ...entries
          .filter(([, s]) => s)
          .map(([, s]) => card(s.label, s)),
      ].join("");
      const summary = $("#agents-summary");
      summary.innerHTML = html;
      delete summary.dataset.skeletoned;
    }

    function renderAgentsFilters() {
      const wrap = $("#agents-filters");
      const active = _agentsState.filter;
      const opts = [
        { id: "all", label: "All", count: _agentsState.all.length },
      ];
      const src = _agentsState.sources;
      ["project", "user", "plugin_market", "plugin_cache"].forEach((id) => {
        if (src[id]) opts.push({ id, label: src[id].label, count: src[id].count });
      });
      wrap.innerHTML = opts.map((o) =>
        `<button class="refresh agents-filter${o.id === active ? " active" : ""}${o.count === 0 ? " disabled" : ""}" data-source="${escape(o.id)}"><span class="label">${escape(o.label)}</span><span class="count">${o.count}</span></button>`
      ).join("");
      wrap.querySelectorAll(".agents-filter").forEach((b) => {
        if (b.classList.contains("disabled")) return;
        b.addEventListener("click", () => {
          _agentsState.filter = b.dataset.source;
          renderAgentsFilters();
          renderAgentsGrid();
        });
      });
    }

    function renderAgentsGrid() {
      const q = (_agentsState.query || "").trim().toLowerCase();
      const filter = _agentsState.filter;
      const filtered = _agentsState.all.filter((a) => {
        if (filter !== "all" && a.source !== filter) return false;
        if (!q) return true;
        return (a.name || "").toLowerCase().includes(q)
            || (a.description || "").toLowerCase().includes(q);
      });
      $("#agents-meta").textContent = `${filtered.length} of ${_agentsState.all.length} shown`;
      const grid = $("#agents-grid");
      delete grid.dataset.skeletoned;
      if (!filtered.length) {
        const srcCount = filter === "all"
          ? _agentsState.all.length
          : ((_agentsState.sources[filter] || {}).count || 0);
        const msg = _agentsState.all.length && srcCount === 0 && filter !== "all"
          ? `No agents in ${agentFilterLabel(filter)} scope.`
          : _agentsState.all.length
          ? "No agents match the current filter."
          : "No agents in this project yet. Use the agent-creator skill to add one.";
        grid.innerHTML = `<p class="agents-empty">${escape(msg)}</p>`;
        return;
      }
      grid.innerHTML = filtered.map((a) => {
        const sourcePill = `<span class="pill">${escape(a.source_label || a.source)}</span>`;
        const modelPill = a.model ? `<span class="metric-pill">${escape(a.model)}</span>` : "";
        const dupPill = a.duplicate ? `<span class="metric-pill warn" title="another agent shares this name in a different scope">duplicate name</span>` : "";
        const tools = parseAgentTools(a.tools || "");
        const toolsRow = tools.length
          ? `<div class="metrics-row" title="${escape(a.tools)}">${
              tools.slice(0, 6)
                .map((t) => `<span class="metric-pill">${escape(t)}</span>`)
                .join("")
            }</div>`
          : "";
        return `<div class="card skill-card agent-card" data-source="${escape(a.source)}" data-name="${escape(a.name)}" data-path="${escape(a.path)}" title="Click for details">
          <h3>${escape(a.name)} ${modelPill} ${dupPill}</h3>
          <div class="desc">${escape(a.description || "-")}</div>
          ${toolsRow}
          <div class="path" title="${escape(a.path)}">${escape(shortPath(a.path))}</div>
          <div class="meta-row">${sourcePill}</div>
        </div>`;
      }).join("");
      grid.querySelectorAll(".skill-card[data-name]").forEach((card) => {
        card.addEventListener("click", () => {
          openAgentDetail(card.dataset.path, card.dataset.name, card.dataset.source);
        });
      });
    }

    async function openAgentDetail(path, name, source) {
      const modal = $("#agent-detail-modal");
      modal.hidden = false;
      const cached = _agentsState.all.find((x) => x.path === path);
      $("#agent-detail-title").textContent = name + (cached ? ` - ${cached.source_label}` : "");
      $("#agent-detail-content").textContent = "loading...";
      const meta = [];
      if (cached) {
        meta.push(`source: ${escape(cached.source_label || cached.source)}`);
        meta.push(`editable: ${cached.editable ? "yes" : "no (plugin)"}`);
        if (cached.model) meta.push(`model: ${escape(cached.model)}`);
        if (cached.tools) meta.push(`tools: ${escape(cached.tools)}`);
        meta.push(`path: ${escape(cached.path)}`);
      }
      const rationaleHtml = cached && cached.description
        ? `<div style="margin-top:6px;color:var(--text-2)">${escape(cached.description)}</div>`
        : "";
      $("#agent-detail-meta").innerHTML =
        meta.map((s) => `<span>${s}</span>`).join("") + rationaleHtml;
      try {
        const r = await fetch("/api/agents/content?path=" + encodeURIComponent(path), { cache: "no-store" });
        if (!r.ok) {
          const errJson = await r.json().catch(() => ({}));
          throw new Error(errJson.error || ("HTTP " + r.status));
        }
        const data = await r.json();
        const text = data.content || "";
        const el = $("#agent-detail-content");
        try { el.innerHTML = DOMPurify.sanitize(marked.parse(text)); }
        catch (_) { el.textContent = text; }
        if (data.truncated) {
          el.insertAdjacentHTML("beforeend",
            `<div style="margin-top:8px;color:var(--warn)">...content truncated at 256 KB</div>`);
        }
      } catch (e) {
        $("#agent-detail-content").innerHTML =
          `<div class="err">Failed to load agent file: ${escape(e.message)}</div>`;
      }
    }

    function closeAgentDetail() {
      $("#agent-detail-modal").hidden = true;
    }

    // ----- Agent suggestions (POST /api/agents/suggest -> proposals) -----
    // Backend mirrors the skill-improver flow but for agents: one click runs
    // a one-shot LLM that proposes 0..N new agent files. Each proposal is
    // persisted as {id}.json + {id}.body.md under .ai/dashboard/agent_proposals
    // and surfaced here until the user accepts (materialises the agent at
    // .claude/agents/<slug>.md) or rejects (kept on disk, status=rejected).
    var _currentAgentProposalId = null;

    async function loadAgentProposals() {
      const wrap  = $("#agent-suggestions-wrap");
      const block = $("#agent-suggestions-block");
      const countEl = $("#agent-suggestions-count");
      if (!wrap || !block) return;
      try {
        const r = await fetch("/api/agents/proposals", { cache: "no-store" });
        if (!r.ok) throw new Error("HTTP " + r.status);
        const data = await r.json();
        // Only show pending proposals; accepted/rejected drop out of the
        // active block (they're auditable on disk).
        const visible = (data.proposals || []).filter(
          (p) => (p.status || "pending") === "pending"
        );
        // Guard countEl: the block/wrap can exist without the count badge
        // (e.g. partial DOM during teardown), so unconditionally writing
        // textContent would throw on null.
        if (countEl) countEl.textContent = visible.length;
        if (!visible.length) {
          wrap.innerHTML = "";  // belt-and-braces: clear stale content
          block.style.display = "none";
          return;
        }
        block.style.display = "";
        wrap.innerHTML = visible.map(renderAgentProposalCard).join("");
        wrap.querySelectorAll(".agent-prop-open").forEach((b) => {
          b.addEventListener("click", () => openAgentProposalModal(b.dataset.id));
        });
      } catch (e) {
        wrap.innerHTML = `<div class="err">${escape(e.message)}</div>`;
        if (countEl) countEl.textContent = "!";
        block.style.display = "";
        setMsg("#agent-suggest-msg", "err", "Proposals load failed: " + e.message);
      }
    }

    function renderAgentProposalCard(p) {
      const triggers = (p.trigger_phrasings || []).slice(0, 3)
        .map((t) => `<span class="metric-pill">${escape(t)}</span>`).join("");
      const conf = (p.confidence != null && p.confidence !== "")
        ? `<span class="metric-pill" title="agent-improver confidence">conf ${escape(String(p.confidence))}</span>`
        : "";
      const when = p.ts ? new Date(p.ts).toLocaleString() : "—";
      const targetShort = p.target_path ? shortPath(p.target_path) : "";
      return `<div class="card skill-card suggestion-card" data-id="${escape(p.id)}">
        <h3>${escape(p.name || p.slug || p.id)} ${conf}</h3>
        <div class="desc">${escape(p.description || "—")}</div>
        ${triggers ? `<div class="metrics-row">${triggers}</div>` : ""}
        <div class="path" title="${escape(p.target_path || "")}">${escape(targetShort)}</div>
        <div class="meta-row">
          <span class="metric-pill" title="${escape(when)}">${escape(when)}</span>
          <button class="refresh agent-prop-open" data-id="${escape(p.id)}">Open</button>
        </div>
      </div>`;
    }

    async function suggestAgents() {
      const btn = $("#btn-suggest-agents");
      const msg = $("#agent-suggest-msg");
      if (!btn) return;
      btn.disabled = true;
      btn.textContent = "Thinking…";
      // Guard msg too: the button can exist without the inline message slot
      // (e.g. partial DOM), so unguarded msg.textContent writes would throw.
      if (msg) msg.textContent = "";
      try {
        const r = await fetch("/api/agents/suggest", { method: "POST" });
        const data = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(data.error || ("HTTP " + r.status));
        const n = data.count || 0;
        if (msg) {
          msg.textContent = n > 0
            ? `${n} new suggestion${n === 1 ? "" : "s"}`
            : (data.note || "no suggestions");
        }
        await loadAgentProposals();
      } catch (e) {
        if (msg) msg.textContent = "failed: " + e.message;
        setMsg("#agent-suggest-msg", "err", "Suggest failed: " + e.message);
      } finally {
        btn.disabled = false;
        btn.textContent = "Suggest agents";
      }
    }

    async function openAgentProposalModal(id) {
      _currentAgentProposalId = id;
      const modal    = $("#agent-proposal-modal");
      const titleEl  = $("#agent-proposal-title");
      const metaEl   = $("#agent-proposal-meta");
      const bodyEl   = $("#agent-proposal-body");
      const acceptBtn = $("#agent-proposal-accept");
      const rejectBtn = $("#agent-proposal-reject");
      const msgEl    = $("#agent-proposal-msg");
      modal.hidden = false;
      titleEl.textContent = id;
      metaEl.innerHTML = "";
      bodyEl.innerHTML = `<span class="spinner"></span> loading…`;
      msgEl.textContent = "";
      acceptBtn.disabled = true;
      rejectBtn.disabled = true;
      try {
        const r = await fetch("/api/agents/proposals/" + encodeURIComponent(id), { cache: "no-store" });
        if (!r.ok) {
          const errJson = await r.json().catch(() => ({}));
          throw new Error(errJson.error || ("HTTP " + r.status));
        }
        const p = await r.json();
        titleEl.textContent = "Suggested agent · " + (p.name || p.slug || id);
        const targetShort = p.installed_path
          ? shortPath(p.installed_path)
          : (p.target_path ? shortPath(p.target_path) : "");
        const meta = [
          `slug: ${escape(p.slug || "—")}`,
          `status: ${escape(p.status || "pending")}`,
          p.confidence != null && p.confidence !== "" ? `confidence: ${escape(String(p.confidence))}` : "",
          p.tools ? `tools: ${escape(p.tools)}` : "",
          targetShort ? `target: ${escape(targetShort)}` : "",
          p.ts ? `ts: ${escape(p.ts)}` : "",
        ].filter(Boolean).map((s) => `<span>${s}</span>`).join("");
        const rationale = p.description
          ? `<div style="margin-top:6px;color:var(--text-2)">${escape(p.description)}</div>`
          : "";
        const triggers = (p.trigger_phrasings || []).length
          ? `<div style="margin-top:6px"><strong style="color:var(--fg-dim);font-size:11px;letter-spacing:0.12em;text-transform:uppercase">Triggers: </strong>${
              p.trigger_phrasings.slice(0, 6)
                .map((t) => `<span class="metric-pill">${escape(t)}</span>`).join(" ")
            }</div>`
          : "";
        metaEl.innerHTML = meta + rationale + triggers;
        const body = p.body || "";
        try { bodyEl.innerHTML = DOMPurify.sanitize(marked.parse(body)); }
        catch (_) { bodyEl.textContent = body; }
        const isFinal = ["accepted", "applied", "installed", "rejected"].includes(p.status);
        acceptBtn.disabled = isFinal;
        rejectBtn.disabled = isFinal;
        msgEl.textContent = isFinal ? `already ${p.status}` : "";
      } catch (e) {
        bodyEl.innerHTML = `<div class="err">Failed to load proposal: ${escape(e.message)}</div>`;
        msgEl.textContent = "load failed";
      }
    }

    function closeAgentProposalModal() {
      $("#agent-proposal-modal").hidden = true;
      _currentAgentProposalId = null;
    }

    async function decideAgentProposal(decision) {
      // Snapshot at entry so async work can detect if the user navigated
      // to a different proposal mid-flight. Without this, the modal that
      // is now showing proposal B could get UI mutations (button enable,
      // close, etc.) intended for the original proposal A.
      var propId = _currentAgentProposalId;
      if (!propId) return;
      const acceptBtn = $("#agent-proposal-accept");
      const rejectBtn = $("#agent-proposal-reject");
      const msgEl = $("#agent-proposal-msg");
      acceptBtn.disabled = true;
      rejectBtn.disabled = true;
      msgEl.textContent = decision + "ing…";
      try {
        const r = await fetch(
          `/api/agents/proposals/${encodeURIComponent(propId)}/${decision}`,
          { method: "POST" }
        );
        const data = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(data.error || ("HTTP " + r.status));
        if (propId !== _currentAgentProposalId) return;  // user navigated away
        msgEl.textContent = decision === "accept"
          ? `installed at ${shortPath(data.target_path || data.installed_path || ".claude/agents/")}`
          : "rejected";
        setMsg("#agent-proposal-msg", "ok", `Proposal ${decision}ed`, 4000);
        await loadAgentProposals();
        if (decision === "accept") {
          // The new agent should appear in the catalog now.
          await loadAgents();
        }
        if (propId !== _currentAgentProposalId) return;  // re-check after extra awaits
        setTimeout(closeAgentProposalModal, 700);
      } catch (e) {
        if (propId !== _currentAgentProposalId) return;  // same guard on error path
        msgEl.textContent = "failed: " + e.message;
        setMsg("#agent-proposal-msg", "err", `Proposal ${decision} failed: ${e.message}`);
        acceptBtn.disabled = false;
        rejectBtn.disabled = false;
      }
    }

    function wireAgentSuggestionsOnce() {
      const wireOnce = (sel, handler) => {
        const el = $(sel);
        if (el && !el.dataset.wired) {
          el.addEventListener("click", handler);
          el.dataset.wired = "1";
        }
      };
      wireOnce("#btn-suggest-agents",    suggestAgents);
      wireOnce("#agent-proposal-close",  closeAgentProposalModal);
      wireOnce("#agent-proposal-accept", () => decideAgentProposal("accept"));
      wireOnce("#agent-proposal-reject", () => decideAgentProposal("reject"));
    }

    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", wireAgentSuggestionsOnce);
    } else {
      wireAgentSuggestionsOnce();
    }
