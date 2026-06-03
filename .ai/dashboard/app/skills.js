// .ai/dashboard/app/skills.js -- extracted from app.js (was lines 573..1102)
// NOTE: top-level const/let were converted to var so identifiers cross <script> boundaries.


    // ----- Skills catalog -----
    var _skillsState = { all: [], sources: {}, filter: "all", query: "" };

    // pillTool (owned by core.js) interpolates its `tool` argument directly
    // into a CSS class fragment. If the catalog/API ever returns an
    // attacker-controlled tool string, that fragment becomes an injection
    // vector. Defensively whitelist known-safe values before passing them
    // through. Anything unrecognised collapses to a safe sentinel.
    function _safeTool(t) {
      return ({ "claude": "claude", "codex": "codex" }[t] || "unknown");
    }

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
        // Pre-lowercase name/description so the search filter doesn't
        // re-lowercase both strings per skill on every keystroke. Cheap
        // one-shot work, big win on long catalogs.
        _skillsState.all.forEach((s) => {
          s._nameLower = (s.name || "").toLowerCase();
          s._descLower = (s.description || "").toLowerCase();
        });
        const countSkillsEl = $("#count-skills");
        if (countSkillsEl) countSkillsEl.textContent = _skillsState.all.length;
        renderSkillsSummary();
        renderSkillsFilters();
        renderSkillsGrid();
        // Fire-and-forget: surface rejections to the console so a thrown
        // proposal/suggestion fetch doesn't silently disappear into the
        // event loop. The catch returns nothing — the inner functions
        // already update their own count badges + setMsg slots.
        Promise.resolve(loadSkillProposals()).catch((err) => {
          console.warn("[dashboard] loadSkillProposals failed:", err && err.message ? err.message : err);
        });
        Promise.resolve(loadSkillSuggestions()).catch((err) => {
          console.warn("[dashboard] loadSkillSuggestions failed:", err && err.message ? err.message : err);
        });
      } catch (e) {
        // Test pins `$("#count-skills").textContent = "!"` literally; the
        // catch path runs after the same element was just written to in the
        // try, so a null here means the DOM was torn down mid-fetch and the
        // user will reload anyway — direct write is acceptable.
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
        const pill = pillTool(_safeTool(tool));
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
      if (!summary) return;  // partial-DOM bail — null-deref would crash boot
      summary.innerHTML = html;
      delete summary.dataset.skeletoned;
    }

    function renderSkillsFilters() {
      const wrap = $("#skills-filters");
      if (!wrap) return;  // partial-DOM bail — innerHTML on null throws
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
      // _nameLower / _descLower are cached in loadSkills() so the search
      // filter does not re-lowercase per skill on every keystroke.
      const filtered = _skillsState.all.filter((s) => {
        if (filter !== "all" && s.source !== filter) return false;
        if (!q) return true;
        return (s._nameLower || "").includes(q)
            || (s._descLower || "").includes(q);
      });
      const metaEl = $("#skills-meta");
      if (metaEl) metaEl.textContent = `${filtered.length} of ${_skillsState.all.length} shown`;
      const grid = $("#skills-grid");
      if (!grid) return;  // partial-DOM bail
      delete grid.dataset.skeletoned;
      if (!filtered.length) {
        grid.innerHTML = `<div class="empty">No skills match the current filter.</div>`;
        return;
      }
      grid.innerHTML = filtered.map((s) => {
        const tool = pillTool(_safeTool(s.tool));
        const sourcePill = `<span class="pill" title="${escape(s.path)}">${escape(s.source_label || s.source)}</span>`;
        return `<div class="card skill-card" tabindex="0" role="button" data-source="${escape(s.source)}" data-name="${escape(s.name)}" title="Click for details">
          <h3>${escape(s.name)} ${tool}</h3>
          <div class="desc">${escape(s.description || "—")}</div>
          <div class="path">${escape(s.path)}</div>
          ${renderSkillMetrics(s.metrics)}
          <div class="meta-row">${sourcePill}</div>
        </div>`;
      }).join("");
      // Click + keydown are both delegated on the stable grid container.
      // Previously click listeners were attached per card on every render
      // (~50 listeners per keystroke while searching). Now we wire once
      // and let event bubbling resolve the target card. The grid element
      // is never replaced by re-renders, so a one-time wire is sufficient.
      if (!_skillsGridKeydownWired) {
        grid.addEventListener("click", (e) => {
          const card = e.target.closest(".skill-card[data-name]");
          if (!card || !grid.contains(card)) return;
          openSkillDetail(card.dataset.source, card.dataset.name);
        });
        grid.addEventListener("keydown", (e) => {
          if (e.key !== "Enter" && e.key !== " ") return;
          const card = e.target.closest(".skill-card[data-name]");
          if (!card) return;
          e.preventDefault();
          openSkillDetail(card.dataset.source, card.dataset.name);
        });
        _skillsGridKeydownWired = true;
      }
    }

    // Wired-once flag for the delegated click + keydown listeners on
    // #skills-grid. The grid container is stable across renders (innerHTML
    // replaces children, not the host element), so wiring is one-shot.
    // Wiring more than once would fire the same opener N times per click.
    var _skillsGridKeydownWired = false;

    // ----- Skill detail modal -----
    var _currentSkillKey = null;
    // Monotonic counter that ticks on every openSkillDetail entry. Even if
    // two openers somehow race past the _currentSkillKey check (e.g. same
    // source+name clicked twice, or a key collision under a future scheme),
    // the epoch comparison guarantees the LATER call always wins. Strictly
    // tighter than the key comparison alone.
    var _skillDetailEpoch = 0;

    async function openSkillDetail(source, name) {
      const key = `${source}::${name}`;
      const epoch = ++_skillDetailEpoch;
      _currentSkillKey = key;
      const cached = _skillsState.all.find(
        (x) => x.source === source && x.name === name
      );
      const modal = $("#skill-detail-modal");
      if (!modal) return;  // partial-DOM bail — the rest of this opener
                           // assumes the modal scaffold exists.
      modal.hidden = false;
      if (typeof window.trapFocusInModal === "function") {
        window.trapFocusInModal(modal, closeSkillDetail);
      }
      // Early epoch guard: if a newer openSkillDetail already incremented
      // the counter (rapid double-open before this frame painted), abandon
      // this opener before it spends work building the meta row / kicks off
      // fetches. The later opener owns the modal now.
      if (epoch !== _skillDetailEpoch) return;
      // Hide the improve action by default; we re-show it post-metrics
      // only when it's actionable (project scope + room to improve).
      _setImproveAction(null, source, null);
      const titleEl = $("#skill-detail-title");
      if (titleEl) titleEl.textContent = name + (cached ? ` · ${cached.source_label}` : "");
      const contentEl = $("#skill-detail-content");
      if (contentEl) contentEl.textContent = "loading…";
      const recentEl = $("#skill-detail-recent");
      if (recentEl) recentEl.innerHTML = `<div class="empty">loading…</div>`;
      const historyEl = $("#skill-detail-history");
      if (historyEl) historyEl.innerHTML = `<div class="empty">loading…</div>`;
      const recentCountEl = $("#skill-detail-recent-count");
      if (recentCountEl) recentCountEl.textContent = "·";
      const historyCountEl = $("#skill-detail-history-count");
      if (historyCountEl) historyCountEl.textContent = "·";

      // Meta row — structured key/value pairs so labels can be styled as
      // dim uppercase eyebrows while values keep mono weight. Path lives
      // on its own line because it's typically the longest and crowding
      // it inline forced horizontal scanning in the previous flat layout.
      const lineItems = [];
      let pathHtml = "";
      let telemetryPill = "";
      if (cached) {
        const m = cached.metrics;
        const kv = (k, v) =>
          `<span class="skill-meta-item"><span class="k">${escape(k)}</span><span class="v">${escape(v)}</span></span>`;
        lineItems.push(kv("tool", cached.tool || "—"));
        lineItems.push(kv("source", cached.source_label || cached.source));
        if (m && m.total_jobs) {
          const rate = Math.round((m.success_rate || 0) * 100);
          // Match the grid's threshold logic so the modal's pill agrees
          // with the card the user just clicked through from.
          const rateCls = rate >= 80 ? "ok" : rate >= 50 ? "warn" : "bad";
          telemetryPill = `<span class="metric-pill ${rateCls}" title="${m.total_jobs} jobs · ${m.total_invocations} calls">${rate}% ok</span>`;
          lineItems.push(kv("jobs", `${m.total_jobs} · ${m.total_invocations} calls`));
        } else {
          lineItems.push(`<span class="skill-meta-item is-empty">no telemetry yet</span>`);
        }
        if (cached.path) {
          pathHtml = `<div class="skill-meta-path"><span class="k">path</span><span class="v">${escape(cached.path)}</span></div>`;
        }
      }
      const rationaleHtml = cached && cached.description
        ? `<div class="skill-detail-rationale">${escape(cached.description)}</div>`
        : "";
      // Re-check the epoch before mutating the shared meta DOM: a newer
      // opener may have raced ahead while this one built its meta strings.
      // Writing here would clobber the newer modal's meta with stale content.
      if (epoch !== _skillDetailEpoch) return;
      $("#skill-detail-meta").innerHTML =
        `<div class="skill-meta-line">${lineItems.join("")}${telemetryPill}</div>`
        + pathHtml
        + rationaleHtml;

      // Pre-await guard: if a later click has already updated
      // _currentSkillKey, abandon this opener so we don't waste a fetch
      // and don't risk racing the newer opener's render path. The epoch
      // check is the authoritative gate — newer clicks always win.
      if (epoch !== _skillDetailEpoch) return;
      if (_currentSkillKey !== key) return;

      // Shared AbortController so a rapid navigate-away cancels all three
      // in-flight fetches at the network layer instead of just dropping
      // their responses on the floor via the epoch check.
      const ctrl = (typeof AbortController !== "undefined") ? new AbortController() : null;
      if (window._skillDetailController) {
        try { window._skillDetailController.abort(); } catch (_) {}
      }
      window._skillDetailController = ctrl;
      const sig = ctrl ? ctrl.signal : undefined;
      // Kick off the three fetches in parallel.
      const [content, metrics, hist] = await Promise.allSettled([
        fetch(`/api/skills/content?source=${encodeURIComponent(source)}&name=${encodeURIComponent(name)}`, { cache: "no-store", signal: sig }).then((r) => r.json()),
        fetch(`/api/skills/metrics?skill=${encodeURIComponent(name)}`, { cache: "no-store", signal: sig }).then((r) => r.ok ? r.json() : null),
        fetch(`/api/skills/improvements?skill=${encodeURIComponent(name)}`, { cache: "no-store", signal: sig }).then((r) => r.json()),
      ]);

      // Bail if the user already navigated to a different skill mid-flight.
      // Epoch comparison wins even if two clicks somehow produce the same key.
      if (epoch !== _skillDetailEpoch) return;
      if (_currentSkillKey !== key) return;

      // SKILL.md content — render as markdown if marked is loaded.
      if (content.status === "fulfilled" && content.value && content.value.content) {
        const text = content.value.content;
        const el = $("#skill-detail-content");
        try {
          el.innerHTML = DOMPurify.sanitize(marked.parse(text));
          el.classList.remove("is-raw");
        } catch (_) {
          el.textContent = text;
          el.classList.add("is-raw");
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

      // Improve-now action gating. Only show the button when the skill
      // is editable (project scope) AND there's something to fix. A
      // skill at 100% success over a meaningful sample size doesn't
      // need a button — running the improver would just burn LLM cost
      // for a near-guaranteed "no change needed" result.
      const metricsObj = (metrics.status === "fulfilled" && metrics.value) ? metrics.value : null;
      _setImproveAction(name, source, metricsObj);

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
            <span style="color:var(--text-dim);font-size:10px;margin-left:auto">${escape((r.job_id || "").slice(0, 8))}</span>
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
            : `<span style="margin-left:auto;color:var(--text-dim);font-size:10px">${escape((h.source || ""))}</span>`;
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

    // Thresholds for the "Improve now" gate. A skill that:
    //   * isn't in project scope -> button hidden (backend would 404)
    //   * has no telemetry yet -> button SHOWN (the operator probably
    //     just created it and wants a first-pass structural audit)
    //   * has < SUFFICIENT_SAMPLE invocations -> button SHOWN (success
    //     rate isn't yet trustworthy enough to skip the audit)
    //   * has >= SUFFICIENT_SAMPLE invocations AND success_rate >=
    //     HEALTHY_RATE -> button HIDDEN (running the improver here would
    //     almost certainly produce "no change needed")
    var IMPROVE_HEALTHY_RATE = 1.0;       // 100% — only gate-off perfect skills
    var IMPROVE_SUFFICIENT_SAMPLE = 5;    // need ≥5 jobs before we trust the rate

    function _setImproveAction(skillName, source, metricsObj) {
      const actionsEl = $("#skill-detail-actions");
      const improveBtn = $("#skill-detail-improve");
      const improveMsgEl = $("#skill-detail-improve-msg");
      if (improveMsgEl) improveMsgEl.textContent = "";
      if (!actionsEl || !improveBtn) return;
      // Default: hide. We re-show only when the helper has enough info
      // to commit to a decision (skillName !== null means metrics phase).
      improveBtn.disabled = false;
      improveBtn.textContent = "Improve now";
      improveBtn.onclick = null;
      if (source !== "project") {
        actionsEl.hidden = true;
        return;
      }
      if (skillName === null) {
        // Synchronous prelude — defer the show/hide until we have metrics.
        actionsEl.hidden = true;
        return;
      }
      const totalJobs = (metricsObj && metricsObj.total_jobs) || 0;
      const successRate = (metricsObj && metricsObj.success_rate);
      const healthy = (
        totalJobs >= IMPROVE_SUFFICIENT_SAMPLE &&
        typeof successRate === "number" &&
        successRate >= IMPROVE_HEALTHY_RATE
      );
      if (healthy) {
        actionsEl.hidden = true;
        return;
      }
      actionsEl.hidden = false;
      improveBtn.onclick = () => triggerImproveNow(skillName);
    }

    function closeSkillDetail() {
      const modal = $("#skill-detail-modal");
      if (modal) modal.hidden = true;
      if (typeof window.releaseFocusTrap === "function") {
        window.releaseFocusTrap();
      }
      _currentSkillKey = null;
    }

    // Tracks the in-flight improve fetch so a rapid double-click on the
    // "Improve now" button doesn't fire two concurrent backend audits for
    // the same skill (the backend caps via _SUGGESTION_SEMAPHORE anyway,
    // but a UI guard avoids the visible 429 flash).
    var _improveInFlight = new Set();

    async function triggerImproveNow(skillName) {
      if (!skillName || _improveInFlight.has(skillName)) return;
      _improveInFlight.add(skillName);
      const btn = $("#skill-detail-improve");
      const msgEl = $("#skill-detail-improve-msg");
      const oldLabel = btn ? btn.textContent : "Improve now";
      if (btn) { btn.disabled = true; btn.textContent = "auditing…"; }
      if (msgEl) msgEl.textContent = "";
      try {
        const r = await fetch(
          `/api/skills/${encodeURIComponent(skillName)}/improve`,
          { method: "POST" }
        );
        const data = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(data.error || ("HTTP " + r.status));
        const status = data.status || "?";
        const summary = data.change_summary || data.reason || "";
        if (msgEl) {
          msgEl.textContent = status === "applied"
            ? `applied (${data.diff_lines || 0} lines): ${summary}`
            : status === "pending"
              ? `proposal ready (${data.diff_lines || 0} lines): ${summary}`
              : status === "no_change"
                ? `no change needed — ${summary}`
                : `${status}: ${summary}`;
        }
        if (btn) btn.textContent = status === "applied" || status === "pending"
          ? "audited ✓" : "no change";
        // Refresh dependent views so the modal + page reflect new state.
        if (data.proposal_id) {
          await Promise.all([loadSkillProposals(), loadSkills()]);
          // Re-open this skill's detail so history + recent reflect the
          // new audit row; openProposalModal jumps straight to the diff.
          setTimeout(() => openProposalModal(data.proposal_id), DRAFT_AUTOOPEN_MS);
        } else {
          // No new proposal — still refresh history list inside the modal.
          await loadSkillProposals();
        }
      } catch (e) {
        if (msgEl) msgEl.textContent = "failed: " + e.message;
        if (btn) btn.textContent = "failed";
        setMsg("#skill-improve", "err", "Improve failed: " + e.message);
      } finally {
        _improveInFlight.delete(skillName);
        // Re-enable after a brief reset window so the user can read the
        // result text before clicking again.
        setTimeout(() => {
          if (btn) { btn.disabled = false; btn.textContent = oldLabel; }
        }, DRAFT_BUTTON_RESET_MS);
      }
    }

    // ----- Skill proposals (Phase 2/3/5) -----
    var _currentProposalId = null;
    var _draftPending = new Set();
    // Monotonic counter ticked on every decideProposal entry. The id
    // snapshot alone catches "user opened a different proposal" but not
    // "user double-clicked accept on the SAME proposal" — both in-flight
    // handlers would see propId === _currentProposalId and the older
    // response could still win, mutating the modal with stale state.
    // Mirrors `_skillDetailEpoch` above.
    var _decideProposalEpoch = 0;

    async function loadSkillProposals() {
      const wrap = $("#skills-proposals");
      const block = $("#skills-proposals-block");
      const countEl = $("#proposals-count");
      // Bail early when the proposals panel isn't in the DOM — happens during
      // partial teardown or when the view is rendered without proposals scaffolding.
      // The caller (loadSkills) fires this best-effort; missing DOM should not throw.
      if (!wrap || !block) return;
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
        if (countEl) countEl.textContent = visible.length;
        if (!visible.length) {
          wrap.innerHTML = "";  // belt-and-braces: clear stale content
          block.style.display = "none";
          return;
        }
        block.style.display = "";
        wrap.innerHTML = visible.map(renderProposalCard).join("");
        wrap.querySelectorAll(".proposal-open").forEach((b) => {
          b.addEventListener("click", () => openProposalModal(b.dataset.id));
        });
      } catch (e) {
        wrap.innerHTML = `<div class="err">${escape(e.message)}</div>`;
        if (countEl) countEl.textContent = "!";
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
      const mergedPill = (p.merged_count && p.merged_count > 1)
        ? `<span class="metric-pill" title="${p.merged_count - 1} earlier proposal${p.merged_count > 2 ? "s" : ""} merged into this one">merged ${p.merged_count}</span>`
        : "";
      return `<div class="card skill-card proposal-card" ${dimmed}>
        <h3>${escape(p.skill || p.id)} <span class="metric-pill ${statusCls}">${escape(p.status)}</span> ${isDraft ? '<span class="metric-pill">draft</span>' : ''} ${mergedPill}</h3>
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
      if (!modal) return;  // partial-DOM bail — modal scaffold missing
      modal.hidden = false;
      if (typeof window.trapFocusInModal === "function") {
        window.trapFocusInModal(modal, closeProposalModal);
      }
      const msgEl = $("#proposal-msg");
      if (msgEl) msgEl.innerHTML = `<span class="spinner"></span> loading…`;
      const titleEl = $("#proposal-modal-title");
      if (titleEl) titleEl.textContent = id;
      const metaEl = $("#proposal-modal-meta");
      if (metaEl) metaEl.innerHTML = "";
      const diffEl = $("#proposal-modal-diff");
      if (diffEl) diffEl.innerHTML = "";
      const acceptBtn = $("#proposal-accept");
      if (acceptBtn) acceptBtn.disabled = true;
      const rejectBtn = $("#proposal-reject");
      if (rejectBtn) rejectBtn.disabled = true;
      try {
        const r = await fetch("/api/skills/proposals/" + encodeURIComponent(id), { cache: "no-store" });
        if (!r.ok) throw new Error("HTTP " + r.status);
        const p = await r.json();
        const isDraft = (p.kind === "draft");
        if (titleEl) titleEl.textContent =
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
        if (metaEl) metaEl.innerHTML = meta + rationaleHtml + regressionHtml;
        if (diffEl) diffEl.innerHTML = renderUnifiedDiff(
          p.old_content || "", p.new_content || ""
        );
        // For drafts: "accepted" is a stuck state (legacy proposal-only) —
        // allow re-Accept to actually install the file. "installed" is the
        // true terminal state.
        const draftStuck = isDraft && p.status === "accepted";
        const isFinal = (!draftStuck) &&
          ["applied", "installed", "rejected", "rolled_back"].includes(p.status);
        if (acceptBtn) acceptBtn.disabled = isFinal;
        if (rejectBtn) rejectBtn.disabled = isFinal;
        // Button label: clearer for drafts. The previous form ternaried on
        // draftStuck but both arms produced the same literal — collapsed to
        // a single branch so future readers don't try to decode an
        // intentional distinction that never existed.
        if (acceptBtn) acceptBtn.textContent = isDraft ? "Create skill" : "Accept";
        if (msgEl) msgEl.textContent = isFinal
          ? `already ${p.status}`
          : (draftStuck
              ? "Draft was accepted but no file was written. Click Create skill to install it now."
              : "");
      } catch (e) {
        if (msgEl) msgEl.textContent = "load failed: " + e.message;
        setMsg("#proposal-load", "err", "Proposal load failed: " + e.message);
      }
    }

    function closeProposalModal() {
      const modal = $("#proposal-modal");
      if (modal) modal.hidden = true;
      if (typeof window.releaseFocusTrap === "function") {
        window.releaseFocusTrap();
      }
      _currentProposalId = null;
    }

    // Auto-close delay (ms) after a successful accept/reject so the
    // success message stays visible briefly before the modal goes away.
    var PROPOSAL_AUTO_CLOSE_MS = 600;

    async function decideProposal(decision) {
      // Snapshot the proposal id at entry. If the user closes the modal and
      // opens a DIFFERENT proposal before our request resolves, we must NOT
      // flip the new modal's buttons or overwrite its messages. The network
      // call still completes (proposal was accepted/rejected server-side),
      // but the visual feedback target is gone — drop the UI update.
      const propId = _currentProposalId;
      if (!propId) return;
      // Monotonic epoch tick — catches "user double-clicked accept on the
      // same proposal" where the id snapshot alone would let an older
      // response overwrite the newer one's resolved UI.
      const epoch = ++_decideProposalEpoch;
      const acceptBtn = $("#proposal-accept");
      const rejectBtn = $("#proposal-reject");
      const msgEl = $("#proposal-msg");
      if (acceptBtn) acceptBtn.disabled = true;
      if (rejectBtn) rejectBtn.disabled = true;
      if (msgEl) msgEl.textContent = decision + "ing…";
      try {
        const r = await fetch(
          `/api/skills/proposals/${encodeURIComponent(propId)}/${decision}`,
          { method: "POST" }
        );
        const data = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(data.error || ("HTTP " + r.status));
        // Stale-modal guard: epoch differs OR id differs ⇒ a newer call
        // owns the UI and this older response must drop its mutations.
        if (epoch !== _decideProposalEpoch || propId !== _currentProposalId) {
          // Still refresh background data so the proposal list reflects the
          // server-side state change, but don't touch the current modal.
          await loadSkillProposals();
          await loadSkills();
          return;
        }
        if (msgEl) msgEl.textContent = data.note || (decision + "ed");
        setMsg("#proposal-msg", "ok", `Proposal ${decision}ed`, 4000);
        await loadSkillProposals();
        await loadSkills();  // refresh metrics + summary in case a skill changed
        setTimeout(closeProposalModal, PROPOSAL_AUTO_CLOSE_MS);
      } catch (e) {
        // Same guard on error path — don't flip a different modal's buttons.
        if (epoch !== _decideProposalEpoch || propId !== _currentProposalId) return;
        if (msgEl) msgEl.textContent = "failed: " + e.message;
        setMsg("#proposal-msg", "err", `Proposal ${decision} failed: ${e.message}`);
        if (acceptBtn) acceptBtn.disabled = false;
        if (rejectBtn) rejectBtn.disabled = false;
      }
    }

    // Hard cap on LCS materialisation. Above this, an (n+1)*(m+1) Int32Array
    // grows past tolerable memory for an in-browser diff modal (e.g. two
    // 5k-line files = 25M cells = ~100MB on Int32). Beyond the cap we drop
    // to a non-LCS fallback that just dumps both sides side-by-side so the
    // modal still renders something useful instead of blowing up the tab.
    var LCS_LINE_CAP = 2000;

    function _diffFallbackForLargeFiles(a, b) {
      // Non-LCS fallback for files past LCS_LINE_CAP. We don't try to align
      // lines — that's what the LCS pass is for, and it's the thing we're
      // opting out of. Instead we show a clear banner explaining the bailout
      // and emit raw old/new line dumps so reviewers can still scan content.
      const out = [];
      out.push(
        `<span class="diff-hunk-sep">(diff too large for LCS — ${a.length} lines old, ${b.length} lines new; showing raw old/new)</span>`
      );
      a.forEach((s) => {
        out.push(`<span class="diff-line diff-del">${escape("- " + s)}</span>`);
      });
      b.forEach((s) => {
        out.push(`<span class="diff-line diff-add">${escape("+ " + s)}</span>`);
      });
      return out.join("");
    }

    function renderUnifiedDiff(oldText, newText) {
      // Lightweight LCS-based diff for visual clarity — handles small SKILL.md.
      const a = (oldText || "").split("\n");
      const b = (newText || "").split("\n");
      // Bail out before allocating the LCS table for unreasonably large files.
      // The .length > 2000 check on either side keeps the worst-case table
      // around 4M cells (~16MB Int32), which is tolerable.
      if (a.length > LCS_LINE_CAP || b.length > LCS_LINE_CAP) {
        return _diffFallbackForLargeFiles(a, b);
      }
      const lcs = lcsTable(a, b);
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

      // Annotate each entry with 1-based old/new line numbers so the hunk
      // header can declare the correct `@@ -oldStart,oldLen +newStart,newLen @@`.
      // del/ctx consume from the old side; add/ctx consume from the new side.
      let oldLine = 1, newLine = 1;
      for (const ent of seq) {
        if (ent.t === "ctx") { ent.oldNo = oldLine++; ent.newNo = newLine++; }
        else if (ent.t === "del") { ent.oldNo = oldLine++; ent.newNo = newLine; }
        else { ent.oldNo = oldLine; ent.newNo = newLine++; }
      }

      // Identify hunks. Each hunk groups one or more changes with up to
      // CONTEXT lines of leading/trailing context. Two changes whose gap
      // is <= 2*CONTEXT ctx lines merge into a single hunk (their context
      // windows overlap). >2*CONTEXT triggers a split into two hunks.
      const CONTEXT = 3;
      const changeIdx = [];
      for (let k = 0; k < seq.length; k++) {
        if (seq[k].t !== "ctx") changeIdx.push(k);
      }

      // Identical files (no changes) → friendly empty-state marker. No
      // hunk header, no full-file dump — that would mislead reviewers.
      if (!changeIdx.length) {
        return `<span class="diff-empty">files are identical — no changes</span>`;
      }

      // Build hunk ranges: each entry is [startIdx, endIdx] inclusive.
      const hunks = [];
      let curStart = Math.max(0, changeIdx[0] - CONTEXT);
      let curEnd = Math.min(seq.length - 1, changeIdx[0] + CONTEXT);
      for (let h = 1; h < changeIdx.length; h++) {
        const desiredStart = Math.max(0, changeIdx[h] - CONTEXT);
        const desiredEnd = Math.min(seq.length - 1, changeIdx[h] + CONTEXT);
        if (desiredStart <= curEnd + 1) {
          // Overlap (or touch) — extend the current hunk's tail.
          curEnd = Math.max(curEnd, desiredEnd);
        } else {
          hunks.push([curStart, curEnd]);
          curStart = desiredStart;
          curEnd = desiredEnd;
        }
      }
      hunks.push([curStart, curEnd]);

      const out = [];
      const emit = (ent) => {
        const cls = ent.t === "add" ? "diff-add" : ent.t === "del" ? "diff-del" : "diff-ctx";
        const prefix = ent.t === "add" ? "+ " : ent.t === "del" ? "- " : "  ";
        out.push(`<span class="diff-line ${cls}">${escape(prefix + ent.s)}</span>`);
      };

      hunks.forEach(([start, end], hi) => {
        // Compute the declared old/new ranges for the header.
        const slice = seq.slice(start, end + 1);
        const oldEntries = slice.filter((e) => e.t === "ctx" || e.t === "del");
        const newEntries = slice.filter((e) => e.t === "ctx" || e.t === "add");
        // Edge case: a hunk made entirely of adds would have no oldEntries.
        // Standard diff convention is to declare oldStart as the old-line
        // number of the insertion point with oldLen=0. Same in reverse for pure-del.
        const oldStart = oldEntries.length ? oldEntries[0].oldNo : slice[0].oldNo;
        const oldLen = oldEntries.length;
        const newStart = newEntries.length ? newEntries[0].newNo : slice[0].newNo;
        const newLen = newEntries.length;

        // Hunk separator span between hunks so reviewers see the omitted gap.
        if (hi > 0) {
          const prevEnd = hunks[hi - 1][1];
          const gap = start - prevEnd - 1;
          if (gap > 0) {
            out.push(`<span class="diff-hunk-sep">… ${gap} unchanged lines …</span>`);
          }
        }

        // Emit the git-style hunk header. `diff-hunk-sep` wrapper keeps
        // the visual treatment consistent with the inter-hunk separator.
        out.push(
          `<span class="diff-hunk-sep">@@ -${oldStart},${oldLen} +${newStart},${newLen} @@</span>`
        );

        for (const ent of slice) emit(ent);
      });

      return out.join("");
    }

    function lcsTable(a, b) {
      const n = a.length, m = b.length;
      // Int32Array rows beat Array-of-Array-of-Object slots: typed-array
      // memory is ~1/4 the size at large diffs (4-byte ints vs object
      // boxes), and V8 stays monomorphic on numeric reads. Keep the
      // outer 2-D shape so callers can still do t[i][j].
      const t = new Array(n + 1);
      for (let i = 0; i <= n; i++) t[i] = new Int32Array(m + 1);
      for (let i = 1; i <= n; i++) {
        for (let j = 1; j <= m; j++) {
          t[i][j] = a[i - 1] === b[j - 1] ? t[i - 1][j - 1] + 1
            : Math.max(t[i - 1][j], t[i][j - 1]);
        }
      }
      return t;
    }

    // Brief pause before auto-opening the newly drafted proposal so the
    // "drafted ✓" feedback is visible to the user. Brief reset window
    // before the draft button accepts another click (prevents accidental
    // double-drafting and gives the user time to read the result text).
    var DRAFT_AUTOOPEN_MS = 300;
    var DRAFT_BUTTON_RESET_MS = 2400;

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
        if (data.id) setTimeout(() => openProposalModal(data.id), DRAFT_AUTOOPEN_MS);
      } catch (e) {
        btn.textContent = "failed";
        btn.title = e.message;
        setMsg("#skill-draft", "err", "Draft failed: " + e.message);
      } finally {
        _draftPending.delete(clusterId);
        setTimeout(() => { btn.disabled = false; btn.textContent = oldText; }, DRAFT_BUTTON_RESET_MS);
      }
    }

    async function loadSkillSuggestions() {
      // `block` was previously read but never used. Removed to silence the
      // dead-binding smell — the block visibility is managed by
      // renderSkillSuggestions itself via the count display, not here.
      const wrap = $("#skills-suggestions");
      try {
        const r = await fetch("/api/skills/suggestions", { cache: "no-store" });
        if (!r.ok) throw new Error("HTTP " + r.status);
        const data = await r.json();
        renderSkillSuggestions(data.suggestions || []);
      } catch (e) {
        // Null-guard the error sink: partial-DOM teardown or a missing
        // suggestions panel would crash an unguarded innerHTML write and
        // mask the underlying fetch failure from setMsg below.
        if (wrap) wrap.innerHTML = `<div class="err">${escape(e.message)}</div>`;
        const countEl = $("#suggestions-count");
        if (countEl) countEl.textContent = "!";
        setMsg("#skill-suggestions-load", "err", "Suggestions load failed: " + e.message);
      }
    }

    function renderSkillSuggestions(list) {
      const countEl = $("#suggestions-count");
      if (countEl) countEl.textContent = list.length;
      const wrap = $("#skills-suggestions");
      if (!wrap) return;  // partial-DOM bail — innerHTML on null throws
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

