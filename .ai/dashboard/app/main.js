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
      $("#meta").innerHTML = `<span class="spinner"></span> loading…`;
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

        const project = jsyaml.load(projectRaw) || {};
        const models = jsyaml.load(modelsRaw) || {};

        $("#project-name").textContent = project.project_name || "unknown";
        const memoryCount = countMemoryEntries(memoryText);
        $("#count-memory").textContent = memoryCount;
        $("#count-plans").textContent = plans.length;
        $("#count-specs").textContent = specs.length;

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

        await loadEvents();
        await loadJobs();
        await loadSessions();
        await loadSkills();
        await loadAgents();

        $("#meta").textContent = `updated ${new Date().toLocaleTimeString()}`;
      } catch (err) {
        $("#meta").textContent = "error";
        const cards = $("#overview-cards");
        cards.innerHTML = `<div class="err">${escape(err.message)}</div>`;
        delete cards.dataset.skeletoned;
        console.error(err);
        setMsg("#dashboard-load", "err", "Dashboard load failed: " + err.message);
      }
    }

    loadAll();
