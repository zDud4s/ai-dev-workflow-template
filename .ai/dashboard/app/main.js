// .ai/dashboard/app/main.js -- extracted from app.js (was lines 3066..3121)
// NOTE: top-level const/let were converted to var so identifiers cross <script> boundaries.

    // ----- main loader -----
    // Pre-render skeleton cards for the Overview grid so the page doesn't
    // snap from empty to populated when YAML / counts finish loading.
    // Matches the structured skeleton pattern used by Agents / Skills.
    function renderOverviewSkeletons() {
      const cards = $("#overview-cards");
      if (!cards || cards.dataset.skeletoned) return;
      // 9 cards mirrors the real grid: stack, pkg managers, dispatch mode,
      // memory, plans/specs, claude tokens, codex tokens, limits, recent.
      cards.innerHTML = Array.from({ length: 9 }).map(() => `
        <div class="card skeleton-overview-card">
          <span class="skeleton skeleton-title"></span>
          <span class="skeleton skeleton-big"></span>
          <span class="skeleton skeleton-line"></span>
        </div>
      `).join("");
      cards.dataset.skeletoned = "1";
      const activity = $("#overview-activity");
      if (activity && !activity.dataset.skeletoned) {
        activity.innerHTML = `<div class="skeleton-doc-block">
          <span class="skeleton skeleton-doc-p1"></span>
          <span class="skeleton skeleton-doc-p2"></span>
          <span class="skeleton skeleton-doc-p3"></span>
        </div>`;
        activity.dataset.skeletoned = "1";
      }
    }

    async function loadAll() {
      // Null-guard every #meta dereference — if index.html ever drops the
      // status element (or this loader is mounted against a stripped shell),
      // an unguarded `.innerHTML =` aborts the whole boot before
      // renderOverviewSkeletons() even runs.
      const metaEl = $("#meta");
      if (metaEl) metaEl.innerHTML = `<span class="spinner"></span> loading…`;
      renderOverviewSkeletons();
      // Seed list skeletons for plans/specs/packets so the lists render
      // a couple of shimmer rows until listDir() resolves.
      if (typeof renderListSkeletons === "function") {
        renderListSkeletons("#plans-list", 6);
        renderListSkeletons("#specs-list", 6);
        renderListSkeletons("#packets-list", 6);
        renderListSkeletons("#jobs-list", 6);
      }
      try {
        const [projectRaw, modelsRaw, memoryText, decisionsText, plans, specs, packets] = await Promise.all([
          getText(".ai/project.yaml"),
          getText(".ai/models.yaml"),
          getText(".ai/memory.md").catch(() => ""),
          getText(".ai/decisions.md").catch(() => ""),
          listDir(".ai/plans").then((xs) => xs.filter((x) => x.endsWith(".md")).sort().reverse()),
          listDir(".ai/specs").then((xs) => xs.filter((x) => x.endsWith(".md")).sort().reverse()),
          listDir(".ai/packets").then((xs) => xs.filter((x) => x.endsWith(".md")).sort()),
        ]);

        // jsyaml.load throws on malformed YAML. The previous `|| {}` only
        // handled the empty-document case; a parse exception aborts the whole
        // boot. Surface the failure via toast so operators can spot a bad
        // .ai/project.yaml or .ai/models.yaml without diffing the console.
        let project = {};
        try {
          project = jsyaml.load(projectRaw) || {};
        } catch (e) {
          console.error("[dashboard] failed to parse .ai/project.yaml:", e);
          setMsg("#dashboard-load", "err", "project.yaml parse failed: " + (e && e.message ? e.message : e));
        }
        let models = {};
        try {
          models = jsyaml.load(modelsRaw) || {};
        } catch (e) {
          console.error("[dashboard] failed to parse .ai/models.yaml:", e);
          setMsg("#dashboard-load", "err", "models.yaml parse failed: " + (e && e.message ? e.message : e));
        }

        // Null-guard each count target — if any element is missing from a
        // stripped markup shell, the unguarded `.textContent = …` previously
        // aborted the rest of the success chain (renderOverview, loadTokenUsage,
        // renderActivity, etc. would never run).
        const projectNameEl = $("#project-name");
        if (projectNameEl) projectNameEl.textContent = project.project_name || "unknown";
        const memoryCount = countMemoryEntries(memoryText);
        const countMemoryEl = $("#count-memory");
        if (countMemoryEl) countMemoryEl.textContent = memoryCount;
        const countPlansEl = $("#count-plans");
        if (countPlansEl) countPlansEl.textContent = plans.length;
        const countSpecsEl = $("#count-specs");
        if (countSpecsEl) countSpecsEl.textContent = specs.length;

        renderOverview(project, models, memoryCount, plans.length, specs.length);
        loadTokenUsage();
        renderActivity(plans, specs);
        renderModels(models);
        renderProject(project, projectRaw);
        renderMarkdown($("#memory-doc"), memoryText || "_(empty)_");
        renderMarkdown($("#decisions-doc"), decisionsText || "_(empty)_");

        buildList("#plans-list", plans, async (name) => {
          renderMarkdown($("#plans-doc"), await getText(".ai/plans/" + name));
        });
        buildList("#specs-list", specs, async (name) => {
          renderMarkdown($("#specs-doc"), await getText(".ai/specs/" + name));
        });
        buildList("#packets-list", packets, async (name) => {
          renderMarkdown($("#packets-doc"), await getText(".ai/packets/" + name));
        });

        await Promise.all([
          loadEvents(),
          loadJobs(),
          loadSessions(),
          loadSkills(),
          loadAgents(),
        ]);

        if (metaEl) metaEl.textContent = `updated ${new Date().toLocaleTimeString()}`;
      } catch (err) {
        if (metaEl) metaEl.textContent = "error";
        const cards = $("#overview-cards");
        if (cards) {
          cards.innerHTML = `<div class="err">${escape(err.message)}</div>`;
          delete cards.dataset.skeletoned;
        }
        console.error(err);
        setMsg("#dashboard-load", "err", "Dashboard load failed: " + err.message);
      }
    }

    loadAll();

    // ----- Workflow update notification -----
    // On dashboard load, ask the backend if a newer template version exists
    // upstream. If so, surface a persistent banner that links into Settings →
    // Workflow updates. Cached for 6h in localStorage so a refresh storm doesn't
    // re-clone the template on every page load; dismissals are pinned to the
    // upstream sha so the banner re-appears only when a newer version lands.
    async function checkWorkflowUpdateOnStartup() {
      // Cache key is versioned so a server-side fix to /api/workflow/check
      // (e.g. now suppressing has_updates when serving from the template repo
      // itself, or when no baseline .version is recorded) invalidates stale
      // client caches that still report has_updates=true.
      const CACHE_KEY = "dash.updateCheck.v2";
      const DISMISS_KEY = "dash.updateDismissedSha";
      const THROTTLE_MS = 6 * 60 * 60 * 1000;
      // Best-effort cleanup of the previous cache key so it doesn't linger.
      try { localStorage.removeItem("dash.updateCheck"); } catch (_) { /* ignore */ }

      const now = Date.now();
      let cached = null;
      try { cached = JSON.parse(localStorage.getItem(CACHE_KEY) || "null"); } catch (_) { /* ignore */ }

      let data = null;
      if (cached && cached.data && (now - (cached.ts || 0)) < THROTTLE_MS) {
        data = cached.data;
      } else {
        try {
          const r = await fetch("/api/workflow/check", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: "{}",
          });
          if (!r.ok) return;
          data = await r.json();
          try { localStorage.setItem(CACHE_KEY, JSON.stringify({ ts: now, data })); } catch (_) { /* ignore */ }
        } catch (_) {
          return;
        }
      }

      if (!data || !data.success || !data.has_updates) return;
      // Defence in depth: even if a future backend returns has_updates=true,
      // never show the banner when (a) there's no recorded baseline (we have
      // nothing comparable, "no .version" is not "behind"), or (b) the dashboard
      // is being served from a checkout of the template repo itself.
      if (!data.current_sha) return;
      if (data.is_template_repo) return;

      let dismissed = null;
      try { dismissed = localStorage.getItem(DISMISS_KEY); } catch (_) { /* ignore */ }
      if (dismissed && dismissed === data.upstream_sha) return;

      showUpdateBanner(data);
    }

    function showUpdateBanner(data) {
      const existing = document.getElementById("update-banner");
      if (existing) existing.remove();

      const shortUp = String(data.upstream_sha || "").substring(0, 7) || "?";
      const shortCur = data.current_sha ? String(data.current_sha).substring(0, 7) : "none";

      const el = document.createElement("div");
      el.id = "update-banner";
      el.className = "update-banner";
      el.setAttribute("role", "status");
      el.innerHTML = ''
        + '<span class="update-banner-ico" aria-hidden="true">'
        +   '<svg viewBox="0 0 20 20" width="18" height="18">'
        +     '<path d="M10 3 L10 13" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/>'
        +     '<path d="M5.5 7.5 L10 3 L14.5 7.5" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/>'
        +     '<line x1="4" y1="16.5" x2="16" y2="16.5" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/>'
        +   '</svg>'
        + '</span>'
        + '<div class="update-banner-body">'
        +   '<strong class="update-banner-title">New workflow version available</strong>'
        +   '<span class="update-banner-meta">upstream ' + shortUp + ' · installed ' + shortCur + '</span>'
        + '</div>'
        + '<button type="button" class="update-banner-action">View update</button>'
        + '<button type="button" class="update-banner-close" aria-label="Dismiss until next version" title="Dismiss until next version">×</button>';

      document.body.appendChild(el);
      requestAnimationFrame(() => requestAnimationFrame(() => el.classList.add("in")));

      el.querySelector(".update-banner-action").addEventListener("click", () => {
        const navBtn = document.querySelector('nav button[data-view="settings"]');
        if (navBtn) navBtn.click();
        setTimeout(() => {
          const target = document.querySelector("#workflow-status");
          if (target) target.scrollIntoView({ behavior: "smooth", block: "center" });
        }, 80);
        dismissUpdateBanner(el);
      });
      el.querySelector(".update-banner-close").addEventListener("click", () => {
        try { localStorage.setItem("dash.updateDismissedSha", data.upstream_sha || ""); } catch (_) { /* ignore */ }
        dismissUpdateBanner(el);
      });
    }

    function dismissUpdateBanner(el) {
      el.classList.remove("in");
      el.classList.add("out");
      setTimeout(() => { if (el.parentNode) el.parentNode.removeChild(el); }, 220);
    }

    checkWorkflowUpdateOnStartup();
