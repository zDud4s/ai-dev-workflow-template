// .ai/dashboard/app/terminals-tooldetail.js -- extracted from terminals.js (was lines 2254-2356)
// Inline tool-detail renderers (pure DOM builders for Edit/Write/Read/Grep/Glob/Web tool
// calls). No top-level state or side-effects; loaded BEFORE terminals.js so these globals
// resolve when the chat renderer paints a tool pill. termAddToolPill/simpleLineDiff stay in
// terminals.js and call into these by global name (cross-script, resolved at call time).

    // ----- Inline tool-detail renderers -----

    function renderEditDiff(filePath, oldStr, newStr) {
      const wrap = document.createElement("div");
      wrap.className = "tool-detail diff-view";
      if (filePath) {
        const h = document.createElement("div");
        h.className = "diff-header";
        h.textContent = filePath;
        wrap.appendChild(h);
      }
      for (const part of simpleLineDiff(oldStr || "", newStr || "")) {
        const line = document.createElement("div");
        line.className = "diff-line " + part.kind;
        const prefix = part.kind === "removed" ? "- " : part.kind === "added" ? "+ " : "  ";
        line.textContent = prefix + part.text;
        wrap.appendChild(line);
      }
      return wrap;
    }

    function renderNewFile(filePath, content) {
      const wrap = document.createElement("div");
      wrap.className = "tool-detail diff-view";
      const h = document.createElement("div");
      h.className = "diff-header";
      h.textContent = (filePath || "(new file)") + "  · " + content.split("\n").length + " lines";
      wrap.appendChild(h);
      for (const ln of content.split("\n")) {
        const line = document.createElement("div");
        line.className = "diff-line added";
        line.textContent = "+ " + ln;
        wrap.appendChild(line);
      }
      return wrap;
    }

    function renderReadIntent(filePath, offset, limit) {
      const wrap = document.createElement("div");
      wrap.className = "tool-detail diff-view";
      const h = document.createElement("div");
      h.className = "diff-header";
      const range = (offset != null || limit != null)
        ? "  · lines " + (offset ?? 1) + "–" + ((offset ?? 1) + (limit ?? 2000) - 1)
        : "";
      h.textContent = filePath + range;
      wrap.appendChild(h);
      return wrap;
    }

    // renderBashCommand moved to pane-helpers.js (pure render leaf; loaded
    // before terminals.js, resolves as a global).

    function renderGrep(input) {
      const wrap = document.createElement("div");
      wrap.className = "tool-detail bash-view";
      const h = document.createElement("div");
      h.className = "diff-header";
      const where = input.path ? " in " + input.path : "";
      const glob = input.glob ? " (glob: " + input.glob + ")" : "";
      const type = input.type ? " (type: " + input.type + ")" : "";
      h.textContent = "Grep: /" + input.pattern + "/" + where + glob + type;
      wrap.appendChild(h);
      return wrap;
    }

    function renderGlob(input) {
      const wrap = document.createElement("div");
      wrap.className = "tool-detail bash-view";
      const h = document.createElement("div");
      h.className = "diff-header";
      const where = input.path ? " in " + input.path : "";
      h.textContent = "Glob: " + input.pattern + where;
      wrap.appendChild(h);
      return wrap;
    }

    function renderWebTool(name, input) {
      const wrap = document.createElement("div");
      wrap.className = "tool-detail bash-view";
      const h = document.createElement("div");
      h.className = "diff-header";
      h.textContent = name + ": " + (input.url || input.query || "");
      wrap.appendChild(h);
      if (input.prompt) {
        const p = document.createElement("pre");
        p.className = "bash-cmd";
        p.textContent = input.prompt;
        wrap.appendChild(p);
      }
      return wrap;
    }

    // Fallback for diffs above the LCS size cliff. Returns marker entries
    // the renderer can display without paying the O(n*m) DP cost.
    function _fallbackDiffStub(oldLines, newLines) {
      const n = oldLines.length, m = newLines.length;
      return [
        { kind: "common", text: "(diff too large to display inline; " + n + " old / " + m + " new lines)" },
        ...oldLines.map((ln) => ({ kind: "removed", text: ln })),
        ...newLines.map((ln) => ({ kind: "added",   text: ln })),
      ];
    }
