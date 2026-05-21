// .ai/dashboard/app/skills.js -- extracted from app.js (was lines 573..1102)
// NOTE: top-level const/let were converted to var so identifiers cross <script> boundaries.


    // ----- Skills catalog -----
    var _skillsState = { all: [], sources: {}, filter: "all", query: "" };

    // Pre-render skeleton placeholders so the page does not snap from
    // empty to fully-populated. The skeleton shapes match the real
    // card layout so there is no layout shift when data lands.
    // Mirrors renderAgentsSkeletons() in agents.js.
    function renderSkillsSkeletons() {
      const summary = $("#skills-summary");
      if (summary && !summary.dataset.skeletoned) {
        const labels = ["Total skills", "Project", "Claude global", "Codex global"];
        summary.innerHTML = labels.map(() => `
          <div class="card skeleton-summary-card">
            <span class="skeleton skeleton-title"></span>
            <span class="skeleton skeleton-big"></span>
            <span class="skeleton skeleton-sub"></span>
          </div>
        `).join("");
        summary.dataset.skeletoned = "1";
      }
      const grid = $("#skills-grid");
      if (grid && !grid.dataset.skeletoned) {
        grid.innerHTML = Array.from({ length: 12 }).map(() => `
          <div class="card skill-card skeleton-card">
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

    async function loadSkills() {
      renderSkillsSkeletons();
      try {
        const r = await fetch("/api/skills/all", { cache: "no-store" });
        if (!r.ok) throw new Error("HTTP " + r.status);
        const data = await r.json();
        _skillsState.all = data.skills || [];
        _skillsState.sources = data.sources || {};
        $("#count-skills").textContent = _skillsState.all.length;
        renderSkillsSummary();
        renderSkillsFilters();
        renderSkillsGrid();
        loadSkillProposals();
        loadSkillSuggestions();
      } catch (e) {
        $("#count-skills").textContent = "!";
        const grid = $("#skills-grid");
        if (grid) {
          grid.innerHTML = `<div class="err">${escape(e.message)}</div>`;
          delete grid.dataset.skeletoned;
        }
        setMsg("#skills-load", "err", "Skills load failed: " + e.message);
      }
    }

    function renderSkillsSummary() {
      const total = _skillsState.all.length;
      const src = _skillsState.sources;
      const card = (label, count, tool, path, exists) => {
        const pill = pillTool(tool);
        const status = exists ? "" : `<div class="val" style="color:var(--warn);font-size:11px">missing on disk</div>`;
        return `<div class="card">
          <h3>${escape(label)} ${pill}</h3>
          <div class="val big">${count}</div>
          <div class="path">${escape(path || "")}</div>
          ${status}
        </div>`;
      };
      const entries = [
        ["project",       src.project],
        ["claude_global", src.claude_global],
        ["codex_global",  src.codex_global],
      ];
      const html = [
        `<div class="card"><h3>Total skills</h3><div class="val big">${total}</div><div class="path">across both models</div></div>`,
        ...entries
          .filter(([, s]) => s)
          .map(([, s]) => card(s.label, s.count, s.tool, s.path, s.exists)),
      ].join("");
      const summary = $("#skills-summary");
      summary.innerHTML = html;
      delete summary.dataset.skeletoned;
    }

    function renderSkillsFilters() {
      const wrap = $("#skills-filters");
      const active = _skillsState.filter;
      const opts = [
        { id: "all", label: "All", count: _skillsState.all.length },
      ];
      const src = _skillsState.sources;
      ["project", "claude_global", "codex_global"].forEach((id) => {
        if (src[id]) opts.push({ id, label: src[id].label, count: src[id].count });
      });
      wrap.innerHTML = opts.map((o) =>
        `<button class="refresh skills-filter${o.id === active ? " active" : ""}" data-source="${escape(o.id)}">${escape(o.label)} <span class="count">${o.count}</span></button>`
      ).join("");
      wrap.querySelectorAll(".skills-filter").forEach((b) => {
        b.addEventListener("click", () => {
          _skillsState.filter = b.dataset.source;
          renderSkillsFilters();
          renderSkillsGrid();
        });
      });
    }

    function renderSkillsGrid() {
      const q = (_skillsState.query || "").trim().toLowerCase();
      const filter = _skillsState.filter;
      const filtered = _skillsState.all.filter((s) => {
        if (filter !== "all" && s.source !== filter) return false;
        if (!q) return true;
        return (s.name || "").toLowerCase().includes(q)
            || (s.description || "").toLowerCase().includes(q);
      });
      $("#skills-meta").textContent = `${filtered.length} of ${_skillsState.all.length} shown`;
      const grid = $("#skills-grid");
      delete grid.dataset.skeletoned;
      if (!filtered.length) {
        grid.innerHTML = `<div class="empty">No skills match the current filter.</div>`;
        return;
      }
      grid.innerHTML = filtered.map((s) => {
        const tool = pillTool(s.tool);
        const sourcePill = `<span class="pill" title="${escape(s.path)}">${escape(s.source_label || s.source)}</span>`;
        return `<div class="card skill-card" data-source="${escape(s.source)}" data-name="${escape(s.name)}" title="Click for details">
          <h3>${escape(s.name)} ${tool}</h3>
          <div class="desc">${escape(s.description || "—")}</div>
          <div class="path">${escape(s.path)}</div>
          ${renderSkillMetrics(s.metrics)}
          <div class="meta-row">${sourcePill}</div>
        </div>`;
      }).join("");
      grid.querySelectorAll(".skill-card[data-name]").forEach((card) => {
        card.addEventListener("click", () => {
          openSkillDetail(card.dataset.source, card.dataset.name);
        });
      });
    }

    // ----- Skill detail modal -----
    var _currentSkillKey = null;

    async function openSkillDetail(source, name) {
      const key = `${source}::${name}`;
      _currentSkillKey = key;
      const cached = _skillsState.all.find(
        (x) => x.source === source && x.name === name
      );
      const modal = $("#skill-detail-modal");
      modal.hidden = false;
      $("#skill-detail-title").textContent = name + (cached ? ` · ${cached.source_label}` : "");
      $("#skill-detail-content").textContent = "loading…";
      $("#skill-detail-recent").innerHTML = `<div class="empty">loading…</div>`;
      $("#skill-detail-history").innerHTML = `<div class="empty">loading…</div>`;
      $("#skill-detail-recent-count").textContent = "·";
      $("#skill-detail-history-count").textContent = "·";

      // Meta row
      const meta = [];
      if (cached) {
        const m = cached.metrics;
        meta.push(`tool: ${escape(cached.tool || "—")}`);
        meta.push(`source: ${escape(cached.source_label || cached.source)}`);
        meta.push(`path: ${escape(cached.path)}`);
        if (m && m.total_jobs) {
          const rate = Math.round((m.success_rate || 0) * 100);
          meta.push(`success: ${rate}%`);
          meta.push(`${m.total_jobs} jobs · ${m.total_invocations} calls`);
        } else {
          meta.push("no telemetry yet");
        }
      }
      const rationaleHtml = cached && cached.description
        ? `<div style="margin-top:6px;color:var(--text-2)">${escape(cached.description)}</div>`
        : "";
      $("#skill-detail-meta").innerHTML =
        meta.map((s) => `<span>${s}</span>`).join("") + rationaleHtml;

      // Kick off the three fetches in parallel.
      const [content, metrics, hist] = await Promise.allSettled([
        fetch(`/api/skills/content?source=${encodeURIComponent(source)}&name=${encodeURIComponent(name)}`, { cache: "no-store" }).then((r) => r.json()),
        fetch(`/api/skills/metrics?skill=${encodeURIComponent(name)}`, { cache: "no-store" }).then((r) => r.ok ? r.json() : null),
        fetch(`/api/skills/improvements?skill=${encodeURIComponent(name)}`, { cache: "no-store" }).then((r) => r.json()),
      ]);

      // Bail if the user already navigated to a different skill mid-flight.
      if (_currentSkillKey !== key) return;

      // SKILL.md content — render as markdown if marked is loaded.
      if (content.status === "fulfilled" && content.value && content.value.content) {
        const text = content.value.content;
        const el = $("#skill-detail-content");
        try {
          el.innerHTML = marked.parse(text);
        } catch (_) {
          el.textContent = text;
        }
        if (content.value.truncated) {
          el.insertAdjacentHTML(
            "beforeend",
            `<div style="margin-top:8px;color:var(--warn)">…content truncated at 256 KB</div>`
          );
        }
      } else {
        $("#skill-detail-content").innerHTML =
          `<div class="err">Failed to load SKILL.md: ${
            content.status === "fulfilled" ? escape(content.value?.error || "(no content)") : escape(String(content.reason))
          }</div>`;
      }

      // Recent invocations
      const recentList = (metrics.status === "fulfilled" && metrics.value && metrics.value.recent)
        ? metrics.value.recent : [];
      $("#skill-detail-recent-count").textContent = recentList.length;
      if (!recentList.length) {
        $("#skill-detail-recent").innerHTML = `<div class="empty">No telemetry yet for this skill.</div>`;
      } else {
        $("#skill-detail-recent").innerHTML = recentList.map((r) => {
          const when = r.ts ? new Date(r.ts).toLocaleString() : "—";
          const outcomeCls = r.outcome === "done" ? "ok" : "bad";
          const cost = (r.cost_usd != null) ? "$" + Number(r.cost_usd).toFixed(4) : "—";
          const dur = (r.duration_ms && r.duration_ms > 0) ? (r.duration_ms / 1000).toFixed(1) + "s" : "—";
          return `<div class="skill-detail-recent-row">
            <span class="ts">${escape(when)}</span>
            <span class="metric-pill ${outcomeCls}">${escape(r.outcome || "?")}</span>
            <span class="metric-pill">${escape(r.kind || "?")}</span>
            <span class="metric-pill">${escape(cost)}</span>
            <span class="metric-pill">${escape(dur)}</span>
            <span style="color:var(--text-faint);font-size:10px;margin-left:auto">${escape((r.job_id || "").slice(0, 8))}</span>
          </div>`;
        }).join("");
      }

      // Improvement history
      const histList = (hist.status === "fulfilled" && hist.value && hist.value.improvements)
        ? hist.value.improvements : [];
      $("#skill-detail-history-count").textContent = histList.length;
      if (!histList.length) {
        $("#skill-detail-history").innerHTML = `<div class="empty">No proposals have targeted this skill yet.</div>`;
      } else {
        $("#skill-detail-history").innerHTML = histList.map((h) => {
          const when = h.ts ? new Date(h.ts).toLocaleString() : "—";
          const statusCls = (h.status === "applied" || h.status === "installed") ? "ok"
            : (h.status === "rolled_back" || h.status === "revert_failed") ? "bad"
            : (h.status === "pending" ? "warn" : "");
          const propBtn = h.proposal_id
            ? `<button class="refresh" data-prop-id="${escape(h.proposal_id)}" style="margin-left:auto">Open proposal</button>`
            : `<span style="margin-left:auto;color:var(--text-faint);font-size:10px">${escape((h.source || ""))}</span>`;
          return `<div class="skill-detail-history-row">
            <span class="ts">${escape(when)}</span>
            <span class="metric-pill ${statusCls}">${escape(h.status || "?")}</span>
            <span class="metric-pill">${escape(String(h.diff_lines ?? 0))} lines</span>
            <span style="color:var(--text-2)">${escape((h.reason || "").slice(0, 100))}</span>
            ${propBtn}
          </div>`;
        }).join("");
        // Wire "Open proposal" buttons to the existing proposal modal.
        $("#skill-detail-history").querySelectorAll("button[data-prop-id]").forEach((b) => {
          b.addEventListener("click", (e) => {
            e.stopPropagation();
            closeSkillDetail();
            openProposalModal(b.dataset.propId);
          });
        });
      }
    }

    function closeSkillDetail() {
      $("#skill-detail-modal").hidden = true;
      _currentSkillKey = null;
    }

    // ----- Skill proposals (Phase 2/3/5) -----
    var _currentProposalId = null;
    var _draftPending = new Set();

    async function loadSkillProposals() {
      const wrap = $("#skills-proposals");
      const block = $("#skills-proposals-block");
      try {
        const r = await fetch("/api/skills/proposals", { cache: "no-store" });
        if (!r.ok) throw new Error("HTTP " + r.status);
        const data = await r.json();
        const all = data.proposals || [];
        // Show all non-rejected; pending first, then applied/accepted dimmed.
        // "Pending proposals" = anything that still needs the user's action.
        // Truly pending items + legacy stuck drafts (status="accepted" from
        // the old proposal-only behaviour, retro-installable via re-Accept).
        // Everything terminal (applied / installed / rejected / rolled_back)
        // drops out — those are history, not work-to-do.
        const visible = all.filter((p) =>
          p.status === "pending" ||
          (p.kind === "draft" && p.status === "accepted")
        );
        $("#proposals-count").textContent = visible.length;
        if (!visible.length) { block.style.display = "none"; return; }
        block.style.display = "";
        wrap.innerHTML = visible.map(renderProposalCard).join("");
        wrap.querySelectorAll(".proposal-open").forEach((b) => {
          b.addEventListener("click", () => openProposalModal(b.dataset.id));
        });
      } catch (e) {
        wrap.innerHTML = `<div class="err">${escape(e.message)}</div>`;
        $("#proposals-count").textContent = "!";
        block.style.display = "";
        setMsg("#skill-proposals-load", "err", "Proposals load failed: " + e.message);
      }
    }

    function renderProposalCard(p) {
      const isDraft = (p.kind === "draft");
      const statusCls = (p.status === "applied" || p.status === "installed")
        ? "ok"
        : (p.status === "pending" ? "warn"
          : (p.status === "rolled_back" ? "bad"
            : (p.status === "accepted" ? "warn" : "")));
      const summaryCls = isDraft ? "draft" : "";
      const summary = p.change_summary || (isDraft ? "(no description)" : "(no summary)");
      const when = p.ts ? new Date(p.ts).toLocaleString() : "—";
      // Applied/installed are dimmed historical; rolled_back stays prominent.
      // Accepted drafts are NOT dimmed — they're a stuck state (file not
      // yet on disk) and the user needs to re-Accept to actually install.
      const dimmed = (p.status === "applied" || p.status === "installed") ? "style=\"opacity:0.6\"" : "";
      return `<div class="card skill-card proposal-card" ${dimmed}>
        <h3>${escape(p.skill || p.id)} <span class="metric-pill ${statusCls}">${escape(p.status)}</span> ${isDraft ? '<span class="metric-pill">draft</span>' : ''}</h3>
        <div class="change-summary ${summaryCls}">${escape(summary)}</div>
        <div class="metrics-row">
          <span class="metric-pill">${p.diff_lines ?? "?"} lines</span>
          ${p.applied_via ? `<span class="metric-pill">${escape(p.applied_via)}</span>` : ""}
          <span class="metric-pill" title="${escape(when)}">${escape(when)}</span>
        </div>
        <div class="meta-row">
          <button class="refresh proposal-open" data-id="${escape(p.id)}">Open</button>
        </div>
      </div>`;
    }

    async function openProposalModal(id) {
      _currentProposalId = id;
      const modal = $("#proposal-modal");
      modal.hidden = false;
      $("#proposal-msg").innerHTML = `<span class="spinner"></span> loading…`;
      $("#proposal-modal-title").textContent = id;
      $("#proposal-modal-meta").innerHTML = "";
      $("#proposal-modal-diff").innerHTML = "";
      $("#proposal-accept").disabled = true;
      $("#proposal-reject").disabled = true;
      try {
        const r = await fetch("/api/skills/proposals/" + encodeURIComponent(id), { cache: "no-store" });
        if (!r.ok) throw new Error("HTTP " + r.status);
        const p = await r.json();
        const isDraft = (p.kind === "draft");
        $("#proposal-modal-title").textContent =
          (isDraft ? "Draft new skill · " : "Improve · ") + (p.skill || id);
        const meta = [
          `skill: ${escape(p.skill || "—")}`,
          `status: ${escape(p.status || "—")}`,
          `diff_lines: ${escape(String(p.diff_lines ?? "?"))}`,
          p.applied_via ? `applied_via: ${escape(p.applied_via)}` : "",
          p.installed_path ? `installed_path: ${escape(p.installed_path)}`
            : (p.target_path ? `target_path: ${escape(p.target_path)}` : ""),
          p.job_id ? `job: ${escape(p.job_id)}` : "",
          p.cluster_size ? `cluster_size: ${escape(String(p.cluster_size))}` : "",
        ].filter(Boolean).map((s) => `<span>${s}</span>`).join("");
        const rationaleHtml = p.rationale
          ? `<div style="margin-top:6px;color:var(--text-2)">${escape(p.rationale)}</div>` : "";
        // Regression banner when the safety net fired.
        let regressionHtml = "";
        if (p.regression && (p.status === "rolled_back")) {
          const pre = Math.round((p.regression.pre_rate || 0) * 100);
          const post = Math.round((p.regression.post_rate || 0) * 100);
          regressionHtml = `<div style="margin-top:8px;padding:6px 10px;border:1px solid var(--bad);color:var(--bad);background:var(--bad-bg)">
            Auto-reverted · success rate dropped from <strong>${pre}%</strong> (${escape(String(p.regression.n_pre))} jobs)
            to <strong>${post}%</strong> (${escape(String(p.regression.n_post))} jobs)
          </div>`;
        }
        $("#proposal-modal-meta").innerHTML = meta + rationaleHtml + regressionHtml;
        $("#proposal-modal-diff").innerHTML = renderUnifiedDiff(
          p.old_content || "", p.new_content || ""
        );
        // For drafts: "accepted" is a stuck state (legacy proposal-only) —
        // allow re-Accept to actually install the file. "installed" is the
        // true terminal state.
        const draftStuck = isDraft && p.status === "accepted";
        const isFinal = (!draftStuck) &&
          ["applied", "installed", "rejected", "rolled_back"].includes(p.status);
        $("#proposal-accept").disabled = isFinal;
        $("#proposal-reject").disabled = isFinal;
        // Button label: clearer for drafts.
        $("#proposal-accept").textContent = isDraft
          ? (draftStuck ? "Create skill" : "Create skill")
          : "Accept";
        $("#proposal-msg").textContent = isFinal
          ? `already ${p.status}`
          : (draftStuck
              ? "Draft was accepted but no file was written. Click Create skill to install it now."
              : "");
      } catch (e) {
        $("#proposal-msg").textContent = "load failed: " + e.message;
        setMsg("#proposal-load", "err", "Proposal load failed: " + e.message);
      }
    }

    function closeProposalModal() {
      $("#proposal-modal").hidden = true;
      _currentProposalId = null;
    }

    async function decideProposal(decision) {
      if (!_currentProposalId) return;
      $("#proposal-accept").disabled = true;
      $("#proposal-reject").disabled = true;
      $("#proposal-msg").textContent = decision + "ing…";
      const propId = _currentProposalId;
      try {
        const r = await fetch(
          `/api/skills/proposals/${encodeURIComponent(propId)}/${decision}`,
          { method: "POST" }
        );
        const data = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(data.error || ("HTTP " + r.status));
        $("#proposal-msg").textContent = data.note || (decision + "ed");
        setMsg("#proposal-msg", "ok", `Proposal ${decision}ed`, 4000);
        await loadSkillProposals();
        await loadSkills();  // refresh metrics + summary in case a skill changed
        setTimeout(closeProposalModal, 600);
      } catch (e) {
        $("#proposal-msg").textContent = "failed: " + e.message;
        setMsg("#proposal-msg", "err", `Proposal ${decision} failed: ${e.message}`);
        $("#proposal-accept").disabled = false;
        $("#proposal-reject").disabled = false;
      }
    }

    function renderUnifiedDiff(oldText, newText) {
      // Lightweight LCS-based diff for visual clarity — handles small SKILL.md.
      const a = (oldText || "").split("\n");
      const b = (newText || "").split("\n");
      const lcs = lcsTable(a, b);
      const lines = [];
      let i = a.length, j = b.length;
      const seq = [];
      while (i > 0 && j > 0) {
        if (a[i - 1] === b[j - 1]) { seq.push({ t: "ctx", s: a[i - 1] }); i--; j--; }
        else if (lcs[i - 1][j] >= lcs[i][j - 1]) { seq.push({ t: "del", s: a[i - 1] }); i--; }
        else { seq.push({ t: "add", s: b[j - 1] }); j--; }
      }
      while (i > 0) { seq.push({ t: "del", s: a[i - 1] }); i--; }
      while (j > 0) { seq.push({ t: "add", s: b[j - 1] }); j--; }
      seq.reverse();
      // Compact contexts longer than ~3 lines.
      const out = [];
      let ctxRun = 0;
      seq.forEach((ent, idx) => {
        if (ent.t === "ctx") {
          ctxRun++;
          if (ctxRun > 3 && idx < seq.length - 3) return;
        } else { ctxRun = 0; }
        const cls = ent.t === "add" ? "diff-add" : ent.t === "del" ? "diff-del" : "diff-ctx";
        const prefix = ent.t === "add" ? "+ " : ent.t === "del" ? "- " : "  ";
        out.push(`<span class="diff-line ${cls}">${escape(prefix + ent.s)}</span>`);
      });
      return out.join("");
    }

    function lcsTable(a, b) {
      const n = a.length, m = b.length;
      const t = Array.from({ length: n + 1 }, () => new Array(m + 1).fill(0));
      for (let i = 1; i <= n; i++) {
        for (let j = 1; j <= m; j++) {
          t[i][j] = a[i - 1] === b[j - 1] ? t[i - 1][j - 1] + 1
            : Math.max(t[i - 1][j], t[i][j - 1]);
        }
      }
      return t;
    }

    async function draftSkillFromCluster(clusterId, btn) {
      if (_draftPending.has(clusterId)) return;
      _draftPending.add(clusterId);
      const oldText = btn.textContent;
      btn.disabled = true; btn.textContent = "drafting…";
      try {
        const r = await fetch(
          `/api/skills/suggestions/${encodeURIComponent(clusterId)}/draft`,
          { method: "POST" }
        );
        const data = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(data.error || ("HTTP " + r.status));
        await loadSkillProposals();
        btn.textContent = "drafted ✓";
        setMsg("#skill-draft", "ok", "Draft created — review the proposal", 4000);
        if (data.id) setTimeout(() => openProposalModal(data.id), 300);
      } catch (e) {
        btn.textContent = "failed";
        btn.title = e.message;
        setMsg("#skill-draft", "err", "Draft failed: " + e.message);
      } finally {
        _draftPending.delete(clusterId);
        setTimeout(() => { btn.disabled = false; btn.textContent = oldText; }, 2400);
      }
    }

    async function loadSkillSuggestions() {
      const block = $("#skills-suggestions-block");
      const wrap = $("#skills-suggestions");
      try {
        const r = await fetch("/api/skills/suggestions", { cache: "no-store" });
        if (!r.ok) throw new Error("HTTP " + r.status);
        const data = await r.json();
        renderSkillSuggestions(data.suggestions || []);
      } catch (e) {
        wrap.innerHTML = `<div class="err">${escape(e.message)}</div>`;
        $("#suggestions-count").textContent = "!";
        setMsg("#skill-suggestions-load", "err", "Suggestions load failed: " + e.message);
      }
    }

    function renderSkillSuggestions(list) {
      $("#suggestions-count").textContent = list.length;
      const wrap = $("#skills-suggestions");
      if (!list.length) {
        wrap.innerHTML = `<div class="empty">No repeated patterns detected yet. Run a few similar jobs and they'll cluster here.</div>`;
        return;
      }
      wrap.innerHTML = list.map((c) => {
        const tokens = (c.top_tokens || []).slice(0, 4)
          .map((t) => `<span class="metric-pill">${escape(t)}</span>`).join("");
        const skills = (c.skills_invoked || []).slice(0, 4)
          .map((s) => `<span class="pill">${escape(s)}</span>`).join(" ");
        const samples = (c.sample_tasks || []).slice(0, 3)
          .map((s) => `<li>${escape(s)}</li>`).join("");
        const seen = c.last_seen ? new Date(c.last_seen).toLocaleString() : "—";
        return `<div class="card skill-card suggestion-card">
          <h3>${escape(c.suggested_name || "repeated-task")}
            <span class="metric-pill ok">${c.size} jobs</span>
          </h3>
          <div class="metrics-row">${tokens}</div>
          ${skills ? `<div class="metrics-row">${skills}</div>` : ""}
          <div class="desc"><ul style="margin:0;padding-left:18px">${samples}</ul></div>
          <div class="meta-row">
            <span title="last time this pattern was seen">last · ${escape(seen)}</span>
            <button class="refresh suggestion-draft" data-id="${escape(c.id)}" title="Generate a SKILL.md draft from this cluster (proposal only — never overwrites)">Draft SKILL.md</button>
          </div>
        </div>`;
      }).join("");
      wrap.querySelectorAll(".suggestion-draft").forEach((btn) => {
        btn.addEventListener("click", () => draftSkillFromCluster(btn.dataset.id, btn));
      });
    }

    function renderSkillMetrics(m) {
      if (!m || !m.total_jobs) {
        return `<div class="metrics-row metrics-empty" title="auto-improver populates this after jobs use the skill">no telemetry yet</div>`;
      }
      const rate = Math.round((m.success_rate || 0) * 100);
      const rateCls = rate >= 80 ? "ok" : rate >= 50 ? "warn" : "bad";
      const avgCost = (m.avg_cost_usd != null) ? "$" + Number(m.avg_cost_usd).toFixed(4) : "—";
      const avgDur = (m.avg_duration_ms && m.avg_duration_ms > 0)
        ? (m.avg_duration_ms / 1000).toFixed(1) + "s"
        : "—";
      const last = m.last_used ? new Date(m.last_used).toLocaleString() : "—";
      return `<div class="metrics-row" title="last used ${escape(last)}">
        <span class="metric-pill ${rateCls}">${rate}% ok</span>
        <span class="metric-pill">${m.total_jobs} jobs</span>
        <span class="metric-pill">${m.total_invocations} calls</span>
        <span class="metric-pill" title="avg cost per job">${escape(avgCost)}</span>
        <span class="metric-pill" title="avg duration per job">${escape(avgDur)}</span>
      </div>`;
    }

