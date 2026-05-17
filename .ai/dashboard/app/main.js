// .ai/dashboard/app/main.js -- extracted from app.js (was lines 3066..3121)
// NOTE: top-level const/let were converted to var so identifiers cross <script> boundaries.

    // ----- main loader -----
    async function loadAll() {
      $("#meta").innerHTML = `<span class="spinner"></span> loading…`;
      $("#overview-cards").innerHTML = `<div class="card-skeleton"><span class="spinner lg"></span> loading overview…</div>`;
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

        $("#meta").textContent = `updated ${new Date().toLocaleTimeString()}`;
      } catch (err) {
        $("#meta").textContent = "error";
        $("#overview-cards").innerHTML = `<div class="err">${escape(err.message)}</div>`;
        console.error(err);
      }
    }

    loadAll();
