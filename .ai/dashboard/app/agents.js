// .ai/dashboard/app/agents.js -- catalog view for .claude/agents/*.md across
// project + user (editable) and plugin marketplaces + cache (read-only).
// Pattern mirrors skills.js but is intentionally simpler: no proposals,
// no telemetry, no auto-improver hooks. Agents are catalog + detail only.

    var _agentsState = { all: [], sources: {}, filter: "all", query: "" };

    async function loadAgents() {
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
      } catch (e) {
        $("#count-agents").textContent = "!";
        const grid = $("#agents-grid");
        if (grid) grid.innerHTML = `<div class="err">${escape(e.message)}</div>`;
      }
    }

    function renderAgentsSummary() {
      const total = _agentsState.all.length;
      const src = _agentsState.sources;
      const card = (label, s) => {
        const status = s.exists ? "" : `<div class="val" style="color:var(--warn);font-size:11px">no files yet</div>`;
        const editPill = s.editable
          ? `<span class="pill" style="color:var(--ok)">editable</span>`
          : `<span class="pill" style="color:var(--fg-dim)">read-only</span>`;
        return `<div class="card">
          <h3>${escape(label)} ${editPill}</h3>
          <div class="val big">${s.count}</div>
          <div class="path">${escape(s.path || "")}</div>
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
      $("#agents-summary").innerHTML = html;
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
        `<button class="refresh agents-filter${o.id === active ? " active" : ""}" data-source="${escape(o.id)}">${escape(o.label)} <span class="count">${o.count}</span></button>`
      ).join("");
      wrap.querySelectorAll(".agents-filter").forEach((b) => {
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
      if (!filtered.length) {
        if (!_agentsState.all.length) {
          grid.innerHTML = `<div class="empty">
            No agents installed yet. Use the <code>agent-creator</code> skill to add the first one to
            <code>.claude/agents/</code>, or browse plugin agents above by switching the filter.
          </div>`;
        } else {
          grid.innerHTML = `<div class="empty">No agents match the current filter.</div>`;
        }
        return;
      }
      grid.innerHTML = filtered.map((a) => {
        const sourcePill = `<span class="pill" title="${escape(a.path)}">${escape(a.source_label || a.source)}</span>`;
        const modelPill = a.model ? `<span class="metric-pill">${escape(a.model)}</span>` : "";
        const dupPill = a.duplicate ? `<span class="metric-pill warn" title="another agent shares this name in a different scope">duplicate name</span>` : "";
        const toolsRow = a.tools ? `<div class="path" style="color:var(--fg-dim);font-size:11px">tools: ${escape(a.tools)}</div>` : "";
        return `<div class="card skill-card" data-source="${escape(a.source)}" data-name="${escape(a.name)}" data-path="${escape(a.path)}" title="Click for details">
          <h3>${escape(a.name)} ${modelPill} ${dupPill}</h3>
          <div class="desc">${escape(a.description || "—")}</div>
          ${toolsRow}
          <div class="path">${escape(a.path)}</div>
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
      $("#agent-detail-title").textContent = name + (cached ? ` · ${cached.source_label}` : "");
      $("#agent-detail-content").textContent = "loading…";
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
        const r = await fetch("/" + path, { cache: "no-store" });
        if (!r.ok) throw new Error("HTTP " + r.status);
        const text = await r.text();
        const el = $("#agent-detail-content");
        try { el.innerHTML = marked.parse(text); }
        catch (_) { el.textContent = text; }
      } catch (e) {
        $("#agent-detail-content").innerHTML =
          `<div class="err">Failed to load agent file: ${escape(e.message)}</div>`;
      }
    }

    function closeAgentDetail() {
      $("#agent-detail-modal").hidden = true;
    }
