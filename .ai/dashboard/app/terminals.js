// .ai/dashboard/app/terminals.js -- extracted from app.js (was lines 1471..3065)
// NOTE: top-level const/let were converted to var so identifiers cross <script> boundaries.

    // ----- terminals (multi-pane real-time view) -----
    // Each entry: { jobId, source, pane, body, input, sendBtn, status, task }
    var TERMS = new Map();

    async function termRefreshPicker(jobs) {
      const sel = $("#term-picker");
      if (!sel) return;
      const prev = sel.value;
      const openKeys = new Set(TERMS.keys());

      // Jobs spawned by the dashboard (chat / orchestrate / plan / codex).
      const jobChoices = (jobs || []).filter((j) => !openKeys.has(j.id));

      // IDE transcripts that we can mirror live.
      let transcripts = [];
      try {
        const r = await fetch("/api/transcripts", { cache: "no-store" });
        if (r.ok) {
          const data = await r.json();
          transcripts = (data.transcripts || []).filter((t) => !openKeys.has("ide:" + t.session_id));
        }
      } catch (_) { /* ignore — picker still works for jobs */ }

      if (!jobChoices.length && !transcripts.length) {
        sel.innerHTML = `<option value="">— nothing to open —</option>`;
        sel.disabled = true;
        $("#term-open").disabled = true;
        return;
      }
      const parts = [];
      if (jobChoices.length) {
        parts.push(`<optgroup label="Dashboard jobs">` + jobChoices.map((j) => {
          const preview = (j.task || "").replace(/\s+/g, " ").slice(0, 60);
          return `<option value="job:${escape(j.id)}">[${escape(j.status)}] ${escape(j.kind)} — ${escape(preview)}</option>`;
        }).join("") + `</optgroup>`);
      }
      if (transcripts.length) {
        parts.push(`<optgroup label="IDE chats (live read-only)">` + transcripts.map((t) => {
          const sid = t.session_id;
          const kb = Math.round(t.size_bytes / 1024);
          const when = (t.modified || "").slice(11, 16);
          return `<option value="ide:${escape(sid)}">[${escape(when)}] ${escape(sid.slice(0, 8))}… (${kb} KB)</option>`;
        }).join("") + `</optgroup>`);
      }
      sel.innerHTML = parts.join("");
      sel.disabled = false;
      $("#term-open").disabled = false;
      if (sel.querySelector(`option[value="${prev}"]`)) sel.value = prev;
    }

    // Back-compat shim so existing call sites that only refresh the
    // transcripts side end up reusing the unified refresh.
    async function termRefreshTranscriptPicker() {
      // Replay loadJobs's tail using whatever was returned last time so we
      // don't double-fetch. If no cached jobs, just refresh empty.
      try {
        const r = await fetch("/api/jobs", { cache: "no-store" });
        const data = await r.json();
        await termRefreshPicker(data.jobs || []);
      } catch (_) {
        await termRefreshPicker([]);
      }
    }

    function termRenderEmptyState() {
      const grid = $("#terms-grid");
      if (TERMS.size === 0) {
        grid.innerHTML = `<div class="term-empty">No terminal panes open. Pick a job above, or start one in <em>Run</em>.</div>`;
      } else {
        // Drop the empty placeholder if it's still there.
        const empty = grid.querySelector(".term-empty");
        if (empty) empty.remove();
      }
      $("#count-terminals").textContent = TERMS.size || "·";
    }

    function termAppendChunk(t, chunk) {
      if (!chunk) return;
      // Cap pane buffer at ~200 KB to keep DOM responsive.
      const MAX = 200_000;
      const node = document.createTextNode(chunk);
      t.body.appendChild(node);
      if (t.body.textContent.length > MAX) {
        t.body.textContent = t.body.textContent.slice(-MAX);
      }
      termAutoScroll(t);
    }

    // Classic chat-pane scroll behaviour: stick to bottom unless the user has
    // scrolled up manually. Reset to "follow" when they scroll back near the
    // bottom. The FIRST scroll after a pane opens uses smooth behaviour so
    // big catch-up dumps slide down rather than snapping.
    // ----- In-pane search (Ctrl+F) -----
    function termToggleSearch(t, open) {
      const bar = t.pane.querySelector(".term-search");
      const wantOpen = open === undefined ? !bar.classList.contains("open") : open;
      bar.classList.toggle("open", wantOpen);
      if (wantOpen) {
        bar.querySelector("input").focus();
        termRunSearch(t);
      } else {
        termClearSearchHighlights(t);
      }
    }

    function termClearSearchHighlights(t) {
      t.body.querySelectorAll("mark.term-hit").forEach((m) => {
        const txt = document.createTextNode(m.textContent);
        m.parentNode.replaceChild(txt, m);
      });
      t.body.normalize();
      t._searchHits = [];
      t._searchIdx = 0;
      const m = t.pane.querySelector(".term-search .matches");
      if (m) m.textContent = "0 / 0";
    }

    function termRunSearch(t) {
      termClearSearchHighlights(t);
      const q = t.pane.querySelector(".term-search input").value;
      if (!q) return;
      const lower = q.toLowerCase();
      const walker = document.createTreeWalker(t.body, NodeFilter.SHOW_TEXT, null);
      const targets = [];
      while (walker.nextNode()) {
        const n = walker.currentNode;
        if (!n.nodeValue) continue;
        if (n.parentElement.closest(".term-search, mark.term-hit")) continue;
        if (n.nodeValue.toLowerCase().includes(lower)) targets.push(n);
      }
      const hits = [];
      for (const n of targets) {
        const text = n.nodeValue;
        const parent = n.parentNode;
        let cursor = 0;
        const frag = document.createDocumentFragment();
        let i;
        while ((i = text.toLowerCase().indexOf(lower, cursor)) !== -1) {
          if (i > cursor) frag.appendChild(document.createTextNode(text.slice(cursor, i)));
          const mark = document.createElement("mark");
          mark.className = "term-hit";
          mark.textContent = text.slice(i, i + lower.length);
          frag.appendChild(mark);
          hits.push(mark);
          cursor = i + lower.length;
        }
        if (cursor < text.length) frag.appendChild(document.createTextNode(text.slice(cursor)));
        parent.replaceChild(frag, n);
      }
      t._searchHits = hits;
      t._searchIdx = 0;
      const matches = t.pane.querySelector(".term-search .matches");
      if (matches) matches.textContent = hits.length ? "1 / " + hits.length : "0 / 0";
      if (hits.length) {
        hits[0].classList.add("current");
        try { hits[0].scrollIntoView({block: "center", behavior: "smooth"}); } catch (_) {}
      }
    }

    function termSearchStep(t, delta) {
      const hits = t._searchHits || [];
      if (!hits.length) return;
      hits[t._searchIdx]?.classList.remove("current");
      t._searchIdx = (t._searchIdx + delta + hits.length) % hits.length;
      const next = hits[t._searchIdx];
      next.classList.add("current");
      try { next.scrollIntoView({block: "center", behavior: "smooth"}); } catch (_) {}
      const m = t.pane.querySelector(".term-search .matches");
      if (m) m.textContent = (t._searchIdx + 1) + " / " + hits.length;
    }

    // ----- Export pane as markdown -----
    function termExportMarkdown(t) {
      const lines = [];
      lines.push("# " + (t.task || "Chat") + "\n");
      lines.push("> session " + (t.jobId || "") + "  ·  " + new Date().toISOString());
      lines.push("");
      const messages = t.body.querySelectorAll(".msg");
      for (const m of messages) {
        const role = m.classList.contains("assistant") ? "assistant"
                   : m.classList.contains("user") ? "user"
                   : m.classList.contains("system") ? "system"
                   : m.classList.contains("result") ? "result" : "note";
        const text = m.querySelector(".text")?.innerText || m.innerText;
        if (!text || !text.trim()) continue;
        lines.push(`## ${role}\n`);
        lines.push(text.trim());
        lines.push("");
      }
      const blob = new Blob([lines.join("\n")], { type: "text/markdown" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `chat-${(t.jobId || "session").slice(0, 8)}-${Date.now()}.md`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    }

    function termInitAutoFollow(t) {
      t.autoFollowBottom = true;
      t.firstScroll = true;
      // Use rAF to detect user-initiated scroll (vs our programmatic
      // scrollTo which also fires the event).
      let programmatic = false;
      t._markProgrammaticScroll = () => {
        programmatic = true;
        requestAnimationFrame(() => requestAnimationFrame(() => { programmatic = false; }));
      };
      t.body.addEventListener("scroll", () => {
        if (programmatic) return;
        const fromBottom = t.body.scrollHeight - t.body.scrollTop - t.body.clientHeight;
        t.autoFollowBottom = fromBottom < 40;
      });
    }

    function termSetDead(t, label) {
      // If the subprocess died mid-turn, the placeholder has no streaming
      // event coming to replace it — clear it explicitly.
      termClearThinkingPlaceholder(t);
      t.pane.classList.add("dead");
      const status = t.pane.querySelector(".status-pill");
      if (status && label) status.outerHTML = `<span class="pill ${label === "done" ? "done" : "bad"} status-pill">${escape(label)}</span>`;

      // For chat panes whose claude subprocess has exited but where the
      // session_id is known, repurpose the composer as a "resume" entry
      // point: the next message spawns a fresh job with --resume <sid>,
      // and the new pane opens alongside (the dead pane stays as history).
      // Without this, the operator has to manually go back to the Run tab,
      // copy the session id, and create a new job. Annoying.
      if (t.kind === "chat" && t.sessionId) {
        t.input.disabled = false;
        t.sendBtn.disabled = false;
        t.input.placeholder = "session ended — next message resumes in a fresh job";
        t.sendBtn.textContent = "resume →";
        // Replace the original send handler with the resume handler.
        const resume = async () => {
          const text = t.input.value.trim();
          if (!text) return;
          t.sendBtn.disabled = true;
          try {
            const res = await postJson("/api/jobs", {
              kind: "chat",
              task: text,
              resume_session_id: t.sessionId,
            });
            t.input.value = "";
            // Open the resumed pane alongside this dead one.
            termOpen(res.id, res);
            await loadJobs();
          } catch (e) {
            const err = document.createElement("div");
            err.className = "msg system";
            err.style.color = "var(--bad)";
            err.textContent = `[resume failed: ${e.message}]`;
            t.body.appendChild(err);
          } finally {
            t.sendBtn.disabled = false;
          }
        };
        // Replace the node to drop the previous "send" listener cleanly.
        const newBtn = t.sendBtn.cloneNode(true);
        t.sendBtn.parentNode.replaceChild(newBtn, t.sendBtn);
        t.sendBtn = newBtn;
        newBtn.addEventListener("click", resume);
        // Same for Enter-to-send on the input.
        const newInput = t.input.cloneNode(true);
        t.input.parentNode.replaceChild(newInput, t.input);
        newInput.value = "";
        newInput.placeholder = "session ended — next message resumes in a fresh job";
        t.input = newInput;
        newInput.addEventListener("keydown", (e) => {
          if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); resume(); }
        });
      } else {
        t.input.disabled = true;
        t.sendBtn.disabled = true;
      }
    }

    function termClose(jobId) {
      const t = TERMS.get(jobId);
      if (!t) return;
      try { t.source.close(); } catch (_) {}
      t.pane.remove();
      TERMS.delete(jobId);
      termRenderEmptyState();
      loadJobs();
    }

    async function termSend(jobId) {
      const t = TERMS.get(jobId);
      if (!t) return;
      const text = t.input.value;
      const attached = t.attached || { images: [], files: [] };
      if (!text.trim() && !attached.images.length && !attached.files.length) return;
      t.sendBtn.disabled = true;
      try {
        const payload = { text };
        if (attached.images.length) payload.images = attached.images;
        if (attached.files.length) payload.files = attached.files;
        await postJson(`/api/jobs/${jobId}/input`, payload);
        if (t.kind === "chat") {
          termRenderUserMessage(t, text);
          // Show the "thinking" bubble immediately — it's the user's only
          // signal that the model has the turn until the first stream
          // event arrives, which can take several seconds for cold caches.
          termShowThinkingPlaceholder(t);
          // Echo attached files/images locally too so the operator sees
          // what they sent.
          for (const f of attached.files) {
            const tag = document.createElement("div");
            tag.className = "tool-pill";
            tag.textContent = "file · " + f;
            t.body.appendChild(tag);
          }
          for (const img of attached.images) {
            const el = document.createElement("img");
            el.src = "data:" + (img.media_type || "image/png") + ";base64," + img.data;
            el.style.maxWidth = "240px";
            el.style.maxHeight = "180px";
            el.style.border = "1px solid var(--border-soft)";
            el.style.borderRadius = "var(--r-sm)";
            el.style.margin = "4px 0";
            t.body.appendChild(el);
          }
        } else {
          const echo = document.createElement("span");
          echo.className = "stdin-echo";
          echo.textContent = `\n> ${text}\n`;
          t.body.appendChild(echo);
        }
        t.body.scrollTop = t.body.scrollHeight;
        t.input.value = "";
        // Reset textarea auto-grown height so the next prompt starts at one row.
        if (t.input.tagName === "TEXTAREA") t.input.style.height = "";
        t.attached = { images: [], files: [] };
        termRenderAttachments(t);
      } catch (e) {
        const err = document.createElement("span");
        err.style.color = "var(--bad)";
        err.textContent = `\n[input failed: ${e.message}]\n`;
        t.body.appendChild(err);
        if (/not running|409/i.test(e.message)) termSetDead(t, "ended");
      } finally {
        t.sendBtn.disabled = false;
        t.input.focus();
      }
    }

    // ----- composer: image paste/drop + @/  autocomplete -----

    function termRenderAttachments(t) {
      const tray = t.pane.querySelector(".attach-tray");
      if (!tray) return;
      const a = t.attached || { images: [], files: [] };
      if (!a.images.length && !a.files.length) {
        tray.style.display = "none";
        tray.innerHTML = "";
        return;
      }
      tray.style.display = "flex";
      tray.innerHTML = "";
      a.files.forEach((f, i) => {
        const chip = document.createElement("span");
        chip.className = "attach-chip";
        chip.textContent = "@ " + f + "  ×";
        chip.addEventListener("click", () => { a.files.splice(i, 1); termRenderAttachments(t); });
        tray.appendChild(chip);
      });
      a.images.forEach((img, i) => {
        const chip = document.createElement("span");
        chip.className = "attach-chip";
        const src = "data:" + (img.media_type || "image/png") + ";base64," + img.data;
        chip.innerHTML = `<img src="${src}" style="height:18px;vertical-align:middle;border-radius:2px;margin-right:6px"/>image  ×`;
        chip.addEventListener("click", () => { a.images.splice(i, 1); termRenderAttachments(t); });
        tray.appendChild(chip);
      });
    }

    function termPasteImage(t, file) {
      const reader = new FileReader();
      reader.onload = () => {
        const r = reader.result || "";
        const comma = r.indexOf(",");
        if (comma < 0) return;
        const data = r.slice(comma + 1);
        const mt = (r.slice(5, comma).split(";")[0]) || "image/png";
        t.attached = t.attached || { images: [], files: [] };
        t.attached.images.push({ data, media_type: mt });
        termRenderAttachments(t);
      };
      reader.readAsDataURL(file);
    }

    function termCloseAutocomplete(t) {
      const pop = t.pane.querySelector(".composer-pop");
      if (pop) { pop.remove(); t._popOpen = false; }
    }

    function termOpenAutocomplete(t, items, onPick) {
      termCloseAutocomplete(t);
      if (!items.length) return;
      const pop = document.createElement("div");
      pop.className = "composer-pop";
      items.slice(0, 20).forEach((it, idx) => {
        const row = document.createElement("div");
        row.className = "composer-pop-row" + (idx === 0 ? " active" : "");
        row.innerHTML = `<span class="pop-name">${escape(it.label)}</span>` +
          (it.detail ? `<span class="pop-detail">${escape(it.detail)}</span>` : "");
        row.addEventListener("mousedown", (e) => { e.preventDefault(); onPick(it); termCloseAutocomplete(t); });
        pop.appendChild(row);
      });
      t.pane.querySelector(".term-foot").appendChild(pop);
      t._popOpen = true;
    }

    async function termHandleComposerInput(t) {
      const input = t.input;
      const val = input.value;
      const caret = input.selectionStart || val.length;
      // Token under caret starting with @ or /.
      const before = val.slice(0, caret);
      const m = before.match(/([@/])([^\s]*)$/);
      if (!m) { termCloseAutocomplete(t); return; }
      const trigger = m[1];
      const prefix = m[2];
      if (trigger === "/") {
        try {
          const r = await fetch("/api/skills", { cache: "no-store" });
          if (!r.ok) return;
          const data = await r.json();
          const items = (data.skills || [])
            .filter((s) => s.name.toLowerCase().includes(prefix.toLowerCase()))
            .map((s) => ({ label: "/" + s.name, detail: s.description || "", pick: "/" + s.name }));
          termOpenAutocomplete(t, items, (it) => {
            const newVal = val.slice(0, caret - prefix.length - 1) + it.pick + val.slice(caret);
            input.value = newVal;
            input.focus();
            const pos = caret - prefix.length - 1 + it.pick.length;
            input.setSelectionRange(pos, pos);
          });
        } catch (_) { /* ignore */ }
      } else {
        try {
          const r = await fetch("/api/files/list?prefix=" + encodeURIComponent(prefix), { cache: "no-store" });
          if (!r.ok) return;
          const data = await r.json();
          const items = (data.files || []).map((f) => ({ label: "@" + f, detail: "", pick: f }));
          termOpenAutocomplete(t, items, (it) => {
            // Attach the file (don't paste path into the text). Remove the @prefix from the input.
            t.attached = t.attached || { images: [], files: [] };
            t.attached.files.push(it.pick);
            const newVal = val.slice(0, caret - prefix.length - 1) + val.slice(caret);
            input.value = newVal;
            input.focus();
            const pos = caret - prefix.length - 1;
            input.setSelectionRange(pos, pos);
            termRenderAttachments(t);
          });
        } catch (_) { /* ignore */ }
      }
    }

    // ----- chat rendering (stream-json -> structured DOM) -----

    // Compact form for the header chip: just the dollar amount, rounded
    // to 2 decimal places once the cost crosses $0.01 (anything smaller
    // would round to "$0.00", which reads as broken — keep the 4-decimal
    // long form there so the user sees something).
    function termFormatCostCompact(c) {
      if (!c || c.cost_usd == null) return "";
      const v = Number(c.cost_usd);
      return "$" + v.toFixed(v >= 0.01 ? 2 : 4);
    }
    // Verbose form for tooltips and the legacy header layout: dollars + turns + duration.
    function termFormatCost(c) {
      if (!c) return "";
      const parts = [];
      if (c.cost_usd != null) parts.push("$" + Number(c.cost_usd).toFixed(4));
      if (c.turns != null) parts.push(c.turns + " turn" + (c.turns === 1 ? "" : "s"));
      if (c.duration_ms != null && c.duration_ms > 0) parts.push((c.duration_ms / 1000).toFixed(1) + "s");
      return parts.join(" · ");
    }

    async function termRefreshCost(t) {
      if (!t || !t.pane.isConnected) return;
      if (t.kind !== "chat" && t.kind !== "chat-codex") return;
      try {
        const r = await fetch(`/api/jobs/${t.jobId}?tail=1`, { cache: "no-store" });
        if (!r.ok) return;
        const data = await r.json();
        const pill = t.pane.querySelector(".cost-pill");
        if (!pill) return;
        pill.textContent = termFormatCostCompact(data.cost);
        const verbose = termFormatCost(data.cost);
        pill.title = verbose
          ? verbose + "  ·  job " + (t.jobId || "").slice(0, 8)
          : "aggregated cost / turns / time for this session";
      } catch (_) { /* ignore */ }
    }

    function termAutoScroll(t) {
      // Honour the "follow bottom" flag set by user scroll behaviour.
      // First call after open uses smooth scroll so the catch-up content
      // slides into view; subsequent calls snap (lower latency for live
      // streaming text).
      if (!t.autoFollowBottom) return;
      if (t._markProgrammaticScroll) t._markProgrammaticScroll();
      if (t.firstScroll) {
        t.firstScroll = false;
        // Defer to next frame so the freshly-appended DOM has been laid out.
        requestAnimationFrame(() => {
          try {
            t.body.scrollTo({ top: t.body.scrollHeight, behavior: "smooth" });
          } catch (_) {
            t.body.scrollTop = t.body.scrollHeight;
          }
        });
        return;
      }
      t.body.scrollTop = t.body.scrollHeight;
    }

    function termHandleChatChunk(t, chunk) {
      t.jsonBuf += chunk;
      let nl;
      while ((nl = t.jsonBuf.indexOf("\n")) !== -1) {
        const line = t.jsonBuf.slice(0, nl);
        t.jsonBuf = t.jsonBuf.slice(nl + 1);
        const trimmed = line.trim();
        if (!trimmed) continue;
        let obj;
        try { obj = JSON.parse(trimmed); }
        catch (_) { termRenderRaw(t, line); continue; }
        termRenderJsonObject(t, obj);
      }
      termAutoScroll(t);
    }

    // Patterns that are pure noise from the operator's POV — Node deprecation
    // warnings printed to stderr, the `[unhandled rate_limit_event]` line that
    // claude prints when it hits a rate-limit telemetry frame, blank lines.
    // Adding patterns here is preferred over surfacing them as "msg system"
    // blocks that drown the actual conversation.
    var RAW_NOISE_PATTERNS = [
      /^\s*$/,                                              // blank
      /^\(node:\d+\)\s/,                                    // node warnings
      /^\[unhandled (rate_limit_event|.*)\]\s*$/,           // unhandled telemetry
      /^DeprecationWarning:/,                               // node deprecation
      /^\(Use `node --trace-deprecation/,                   // node trace hint
      /^# job [0-9a-f-]+ kind=/,                            // pump-injected header
      /^# task:/,                                           // pump-injected task line
    ];
    function termRenderRaw(t, line) {
      // Non-JSON line (rare: e.g. CLI noise). Silence known-noise patterns
      // entirely; everything else surfaces as a dim system block so we
      // notice genuinely-unexpected output rather than hiding it.
      for (const pat of RAW_NOISE_PATTERNS) {
        if (pat.test(line)) return;
      }
      const div = document.createElement("div");
      div.className = "msg system";
      div.textContent = line;
      t.body.appendChild(div);
    }

    function termRenderUserMessage(t, text) {
      const msg = document.createElement("div");
      msg.className = "msg user";
      msg.innerHTML = `<div class="role">user</div><div class="text"></div>`;
      msg.querySelector(".text").textContent = text;
      t.body.appendChild(msg);
      // After a user turn, prepare for a fresh assistant block on the next
      // assistant event.
      t.currentAssistant = null;
    }

    // Convert a model id into a human-friendly label for the role chip:
    //   claude-sonnet-4-6              -> CLAUDE SONNET 4.6
    //   claude-opus-4-7                -> CLAUDE OPUS 4.7
    //   claude-haiku-4-5-20251001      -> CLAUDE HAIKU 4.5  (drops YYYYMMDD)
    //   o4-mini                        -> O4 MINI
    //   gpt-5                          -> GPT 5
    // The tooltip on the role chip carries the unmodified id so power users
    // can still read the exact version.
    function termFormatModel(model) {
      if (!model) return "";
      return String(model)
        .replace(/-\d{8}$/, "")
        .replace(/-(\d+)-(\d+)(?=$|-)/, " $1.$2")
        .replace(/-/g, " ")
        .toUpperCase();
    }

    // Resolve the best label for an assistant role chip, given what we know
    // about the pane: explicit model wins; otherwise fall back to the tool
    // identity ("claude" / "codex") implied by the job kind; otherwise the
    // generic "assistant".
    function termAssistantRoleLabel(t) {
      if (t.model) return termFormatModel(t.model);
      if (t.kind === "chat") return "claude";
      if (t.kind === "chat-codex") return "codex";
      return "assistant";
    }

    // Record a model id for this pane and retro-update any assistant role
    // chips that were created before the model was known (chat-mode panes
    // create the block on the first text_delta, but stream-json's `init`
    // frame arrives just before that — they race).
    function termSetPaneModel(t, model) {
      if (!model || t.model === model) return;
      t.model = model;
      const label = termFormatModel(model);
      const title = "model: " + model;
      t.body.querySelectorAll(".msg.assistant:not(.thinking-placeholder) .role")
        .forEach((r) => {
          // Skip chips that were locked by another caller (e.g. the dispatch
          // tracker pane renames its role to "dispatch result").
          if (r.dataset.roleLocked === "1") return;
          r.textContent = label;
          r.title = title;
        });
    }

    // Show an animated "thinking" bubble while we wait for the first
    // assistant event. Replaced in-place as soon as text/tool_use starts
    // streaming (see termAssistantBlock and termRenderResult below).
    function termShowThinkingPlaceholder(t) {
      if (!t || !t.body) return;
      if (t.kind !== "chat") return;
      termClearThinkingPlaceholder(t);  // de-dupe
      const msg = document.createElement("div");
      msg.className = "msg assistant thinking-placeholder";
      msg.innerHTML = `<div class="role">thinking</div>`
        + `<div class="thinking-dots" aria-label="generating response">`
        + `<span class="dot"></span><span class="dot"></span><span class="dot"></span>`
        + `</div>`;
      t.body.appendChild(msg);
      termAutoScroll(t);
    }

    function termClearThinkingPlaceholder(t) {
      if (!t || !t.body) return;
      t.body.querySelectorAll(".thinking-placeholder").forEach((el) => el.remove());
    }

    function termAssistantBlock(t) {
      if (t.currentAssistant && t.currentAssistant.isConnected) return t.currentAssistant;
      // First real content for this turn — drop the thinking placeholder.
      termClearThinkingPlaceholder(t);
      const msg = document.createElement("div");
      msg.className = "msg assistant";
      const label = termAssistantRoleLabel(t);
      const titleAttr = t.model ? ` title="model: ${escape(t.model)}"` : "";
      msg.innerHTML = `<div class="role"${titleAttr}>${escape(label)}</div><div class="text"></div>`;
      t.body.appendChild(msg);
      t.currentAssistant = msg;
      return msg;
    }

    function termAppendAssistantText(t, text) {
      if (!text) return;
      const block = termAssistantBlock(t);
      const textEl = block.querySelector(".text");
      // Accumulate raw text in a data attribute so we can re-render markdown
      // each time without losing earlier deltas.
      const acc = (textEl.dataset.raw || "") + text;
      textEl.dataset.raw = acc;
      // Use marked.parse for full markdown rendering with code fences.
      try { textEl.innerHTML = marked.parse(acc); }
      catch (_) { textEl.textContent = acc; }
    }

    function termAddToolPill(t, toolUseId, name, input) {
      // Some tools deserve inline rich rendering instead of a collapsed pill.
      if (name === "TodoWrite") return termRenderTodoWrite(t, toolUseId, input);

      const block = termAssistantBlock(t);
      const textEl = block.querySelector(".text");
      const wrap = document.createElement("div");
      const pill = document.createElement("span");
      pill.className = "tool-pill";
      const argSummary = termSummariseToolInput(input);
      pill.textContent = name + (argSummary ? "  " + argSummary : "");

      // Pick a tool-specific inline renderer so file edits look like a
      // proper diff view, not a JSON dump.
      let detail;
      if (name === "Edit" && typeof input?.old_string === "string" && typeof input?.new_string === "string") {
        detail = renderEditDiff(input.file_path, input.old_string, input.new_string);
      } else if (name === "Write" && typeof input?.content === "string") {
        detail = renderNewFile(input.file_path, input.content);
      } else if (name === "Read" && input?.file_path) {
        detail = renderReadIntent(input.file_path, input.offset, input.limit);
      } else if (name === "Bash" && typeof input?.command === "string") {
        detail = renderBashCommand(input.command, input.description);
      } else if (name === "Grep" && typeof input?.pattern === "string") {
        detail = renderGrep(input);
      } else if (name === "Glob" && typeof input?.pattern === "string") {
        detail = renderGlob(input);
      } else if ((name === "WebFetch" || name === "WebSearch") && (input?.url || input?.query)) {
        detail = renderWebTool(name, input);
      } else {
        detail = document.createElement("pre");
        detail.className = "tool-detail";
        detail.textContent = JSON.stringify(input ?? {}, null, 2);
      }

      pill.addEventListener("click", () => detail.classList.toggle("open"));
      wrap.appendChild(pill);
      wrap.appendChild(detail);
      textEl.appendChild(wrap);
      t.toolUseEls.set(toolUseId, { pill, detail });

      // If this is a Bash invocation that boots ANOTHER LLM (codex exec /
      // claude -p / claude --print), the dispatched subprocess is what the
      // operator usually wants to watch live. Open it as a tracking pane
      // automatically (unless they disabled auto-open).
      if (name === "Bash" && termIsLLMDispatchCommand(input?.command)) {
        termOpenDispatchTracker(t, toolUseId, input);
      }
    }

    // Heuristic: does this Bash command spawn a Claude or Codex agent?
    function termIsLLMDispatchCommand(cmd) {
      if (!cmd || typeof cmd !== "string") return false;
      // Codex CLI dispatch.
      if (/\bcodex\s+exec(\s|$)/.test(cmd)) return true;
      // Claude CLI dispatch in non-interactive mode.
      if (/\bclaude(\.[a-z]+)?\s+(-p\b|--print\b)/i.test(cmd)) return true;
      if (/\bclaude(\.[a-z]+)?\s+.*--input-format\s+stream-json/.test(cmd)) return true;
      return false;
    }

    // Map<dispatch tool_use_id, dispatch pane state> so termMarkToolResult
    // can hand the result over to the right tracker pane.
    var DISPATCH_TRACKERS = new Map();

    function termOpenDispatchTracker(parentTerm, toolUseId, input) {
      if (!termAutoOpenEnabled()) return;
      const paneKey = "dispatch:" + toolUseId;
      if (TERMS.has(paneKey)) return;
      const grid = $("#terms-grid");
      if (!grid) return;
      const cmd = input?.command || "";
      const isCodex = /\bcodex\s+exec/.test(cmd);
      const label = (isCodex ? "Codex" : "Claude") + " dispatch (" + toolUseId.slice(0, 6) + ")";
      const pane = document.createElement("div");
      pane.className = "term-pane focus";
      pane.dataset.jobId = paneKey;
      pane.innerHTML = `
        <div class="term-head">
          <span class="pill ${isCodex ? "codex" : "claude"} status-pill">dispatch</span>
          <span class="task" title="${escape(cmd)}">${escape(label)}</span>
          <span class="id">${escape(toolUseId.slice(0, 8))}</span>
          <span class="actions">
            <button class="close-btn" title="Close this pane">close</button>
          </span>
        </div>
        <div class="term-body chat" tabindex="0"></div>
        <div class="term-foot">
          <textarea class="stdin-input" rows="1" disabled placeholder="read-only — dispatch is owned by the parent orchestrate session"></textarea>
          <button class="send-btn" disabled>send</button>
        </div>
      `;
      grid.appendChild(pane);
      const body = pane.querySelector(".term-body");
      const t = {
        jobId: paneKey,
        pane, body,
        input: pane.querySelector(".stdin-input"),
        sendBtn: pane.querySelector(".send-btn"),
        source: null,
        task: cmd,
        kind: "dispatch",
        toolUseEls: new Map(),
        currentAssistant: null,
        parentTermId: parentTerm.jobId,
        toolUseId,
      };
      TERMS.set(paneKey, t);
      termInitAutoFollow(t);
      DISPATCH_TRACKERS.set(toolUseId, t);
      pane.querySelector(".close-btn").addEventListener("click", () => {
        DISPATCH_TRACKERS.delete(toolUseId);
        termClose(paneKey);
      });
      // Render the prompt up-front so the operator sees what's being run.
      const header = document.createElement("div");
      header.className = "msg system";
      header.textContent = "$ " + cmd;
      body.appendChild(header);
      const waiting = document.createElement("div");
      waiting.className = "msg system";
      waiting.style.opacity = "0.7";
      waiting.textContent = "(waiting for output…)";
      body.appendChild(waiting);
      t._waitingMsg = waiting;
      termRenderEmptyState();
    }

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

    function renderBashCommand(command, description) {
      const wrap = document.createElement("div");
      wrap.className = "tool-detail bash-view";
      if (description) {
        const d = document.createElement("div");
        d.className = "diff-header";
        d.textContent = description;
        wrap.appendChild(d);
      }
      const c = document.createElement("pre");
      c.className = "bash-cmd";
      c.textContent = "$ " + command;
      wrap.appendChild(c);
      return wrap;
    }

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

    // Line-level diff using LCS backtrace. Falls back to "all removed +
    // all added" for huge edits to bound memory.
    function simpleLineDiff(oldStr, newStr) {
      const a = oldStr.split("\n");
      const b = newStr.split("\n");
      const n = a.length, m = b.length;
      if (n * m > 200000) {
        const out = [];
        for (const ln of a) out.push({ kind: "removed", text: ln });
        for (const ln of b) out.push({ kind: "added",   text: ln });
        return out;
      }
      const dp = new Array(n + 1);
      for (let i = 0; i <= n; i++) dp[i] = new Int32Array(m + 1);
      for (let i = n - 1; i >= 0; i--) {
        for (let j = m - 1; j >= 0; j--) {
          dp[i][j] = a[i] === b[j] ? dp[i+1][j+1] + 1 : Math.max(dp[i+1][j], dp[i][j+1]);
        }
      }
      const out = [];
      let i = 0, j = 0;
      while (i < n && j < m) {
        if (a[i] === b[j]) { out.push({ kind: "common",  text: a[i] }); i++; j++; }
        else if (dp[i+1][j] >= dp[i][j+1]) { out.push({ kind: "removed", text: a[i] }); i++; }
        else { out.push({ kind: "added",   text: b[j] }); j++; }
      }
      while (i < n) out.push({ kind: "removed", text: a[i++] });
      while (j < m) out.push({ kind: "added",   text: b[j++] });
      return out;
    }

    // Self-test on load: catch diff algorithm regressions without a JS
    // test framework. Failures appear in the browser console.
    window.addEventListener("DOMContentLoaded", () => {
      try {
        const kinds = simpleLineDiff("a\nb\nc", "a\nB\nc").map((x) => x.kind).join(",");
        const ok = kinds === "common,removed,added,common" || kinds === "common,added,removed,common";
        if (!ok) console.error("[dashboard] simpleLineDiff self-test FAILED:", kinds);
        else console.log("[dashboard] simpleLineDiff self-test OK");
      } catch (e) { console.error("[dashboard] simpleLineDiff threw:", e); }
    });

    function termRenderTodoWrite(t, toolUseId, input) {
      const block = termAssistantBlock(t);
      const textEl = block.querySelector(".text");
      const todos = Array.isArray(input?.todos) ? input.todos : [];
      const done = todos.filter((x) => x?.status === "completed").length;
      const wrap = document.createElement("div");
      wrap.className = "todo-widget";
      const header = document.createElement("div");
      header.className = "todo-header";
      header.innerHTML = `<span>TodoWrite</span><span class="meta">${done}/${todos.length} done</span>`;
      wrap.appendChild(header);
      const ul = document.createElement("ul");
      ul.className = "todo-list";
      for (const todo of todos) {
        const li = document.createElement("li");
        const status = todo?.status || "pending";
        li.className = "todo-item " + status;
        // While a task is in progress show its activeForm; otherwise show
        // the imperative content. Falls back gracefully on either field.
        const label = (status === "in_progress" && todo?.activeForm) ? todo.activeForm : (todo?.content ?? todo?.activeForm ?? "(unnamed)");
        // Wrap the label in its own span so that line-through on
        // completed items only crosses the text — not the status icon.
        const labelEl = document.createElement("span");
        labelEl.className = "todo-label";
        labelEl.textContent = label;
        li.appendChild(labelEl);
        ul.appendChild(li);
      }
      wrap.appendChild(ul);
      textEl.appendChild(wrap);
      // Still register so a tool_result event can mark it succeeded/failed.
      t.toolUseEls.set(toolUseId, { pill: wrap, detail: null });
    }

    function termSummariseToolInput(input) {
      if (!input || typeof input !== "object") return "";
      const keys = Object.keys(input);
      if (!keys.length) return "";
      // Prefer a recognised summary key.
      const candidate = ["command", "file_path", "path", "pattern", "url", "query"]
        .find((k) => typeof input[k] === "string" && input[k]);
      if (candidate) {
        const v = String(input[candidate]);
        return v.length > 60 ? v.slice(0, 57) + "…" : v;
      }
      return "(" + keys.slice(0, 3).join(", ") + (keys.length > 3 ? "…" : "") + ")";
    }

    function termMarkToolResult(t, toolUseId, isError, content) {
      const entry = t.toolUseEls.get(toolUseId);
      if (entry) {
        entry.pill.classList.add(isError ? "error" : "result");
        // Inline-rendered tools (like TodoWrite) don't have a detail panel
        // to dump raw JSON into - the rich widget already shows the state.
        if (entry.detail) {
          const result = "\n--- result ---\n" + (typeof content === "string" ? content : JSON.stringify(content, null, 2));
          entry.detail.textContent += result;
        }
      }
      // If this tool_use_id has a dispatch tracker pane open, forward the
      // result into it so the operator sees the dispatched LLM's output as
      // if it were its own terminal.
      const tracker = DISPATCH_TRACKERS.get(toolUseId);
      if (tracker) {
        if (tracker._waitingMsg) { tracker._waitingMsg.remove(); tracker._waitingMsg = null; }
        const block = document.createElement("div");
        block.className = "msg " + (isError ? "system" : "assistant");
        if (isError) block.style.color = "var(--bad)";
        const role = document.createElement("div");
        role.className = "role";
        role.textContent = isError ? "dispatch failed" : "dispatch result";
        role.dataset.roleLocked = "1";  // protect from termSetPaneModel retro-rename
        block.appendChild(role);
        const text = document.createElement("div");
        text.className = "text";
        // Render the result; for chat-style content arrays surface each
        // element, otherwise dump the JSON / string verbatim.
        const raw = typeof content === "string"
          ? content
          : Array.isArray(content)
            ? content.map((b) => typeof b === "string" ? b : (b?.text ?? JSON.stringify(b))).join("\n")
            : JSON.stringify(content, null, 2);
        try { text.innerHTML = marked.parse(raw); }
        catch (_) { text.textContent = raw; }
        block.appendChild(text);
        tracker.body.appendChild(block);
        // Header pill goes from "dispatch" to "done" / "failed".
        const status = tracker.pane.querySelector(".status-pill");
        if (status) { status.textContent = isError ? "failed" : "done"; status.classList.toggle("done", !isError); }
        termAutoScroll(tracker);
      }
    }

    function termRenderSystem(t, obj) {
      const sub = obj.subtype || obj.type;
      const div = document.createElement("div");
      div.className = "msg system";
      div.textContent = `[${obj.type}${sub && sub !== obj.type ? ":" + sub : ""}]`;
      // Don't show every system frame — only init / shutdown / errors.
      if (sub === "init" || sub === "shutdown" || /error/i.test(String(sub))) {
        t.body.appendChild(div);
      }
    }

    function termRenderResult(t, obj) {
      // Result frames close out a turn — drop any lingering thinking
      // placeholder (e.g. when the turn finishes without any assistant
      // text, the placeholder would otherwise persist forever).
      termClearThinkingPlaceholder(t);
      const div = document.createElement("div");
      div.className = "msg result";
      const usd = (obj.cost_usd ?? obj.total_cost_usd);
      const dur = (obj.duration_ms != null) ? `${(obj.duration_ms / 1000).toFixed(1)}s` : "";
      const turns = (obj.num_turns != null) ? `${obj.num_turns}t` : "";
      const cost = (usd != null) ? `$${Number(usd).toFixed(4)}` : "";
      const meta = [dur, turns, cost].filter(Boolean).join(" · ");
      div.textContent = `[done${meta ? "  " + meta : ""}]`;
      t.body.appendChild(div);
      t.currentAssistant = null;  // next assistant goes in a fresh block
      termRefreshCost(t);          // bring the header pill up to date
      // Notify the operator if the tab is in the background.
      termNotifyTurnComplete(t, meta);
    }

    // Browser-notification on turn complete, when this tab isn't focused.
    // We ask permission lazily on the first notification opportunity per
    // session - never pop up a permission dialog out of nowhere.
    var _notifyPermAsked = false;
    function termNotifyTurnComplete(t, metaStr) {
      if (typeof Notification === "undefined") return;
      if (document.visibilityState === "visible" && document.hasFocus()) return;
      const fire = () => {
        try {
          const title = (t.task || "Chat").slice(0, 80);
          const body = "Turn finished" + (metaStr ? "  ·  " + metaStr : "");
          const n = new Notification(title, { body, tag: "term-" + t.jobId, silent: false });
          n.onclick = () => { window.focus(); try { t.pane.scrollIntoView({behavior:"smooth"}); } catch (_) {} n.close(); };
          setTimeout(() => { try { n.close(); } catch (_) {} }, 8000);
        } catch (_) { /* notifications can throw in some browsers */ }
      };
      if (Notification.permission === "granted") return fire();
      if (Notification.permission === "denied") return;
      if (_notifyPermAsked) return;
      _notifyPermAsked = true;
      Notification.requestPermission().then((p) => { if (p === "granted") fire(); }).catch(() => {});
    }

    // Transcript-format meta records that the IDE writes for plumbing
    // (hooks, queue, file backups). They are noise from the operator's POV.
    var TRANSCRIPT_META_NOISE = new Set([
      "attachment",
      "queue-operation",
      "file-history-snapshot",
      "summary",
      "compaction",
      "last-prompt",   // duplicate of the latest user message
    ]);

    function termRenderJsonObject(t, obj) {
      if (!obj || typeof obj !== "object") return;
      const type = obj.type;

      // Silence transcript-format meta noise (hooks, queue ops, snapshots).
      if (TRANSCRIPT_META_NOISE.has(type)) return;

      // Capture the model identifier early so the assistant role chip can
      // render with the real model name (e.g. "CLAUDE SONNET 4.6") instead
      // of the generic "assistant". stream-json carries it on the `init`
      // frame and on every `assistant` message; transcripts only on the
      // assistant record. First one wins, but later updates retro-apply.
      const declaredModel = obj.model || (obj.message && obj.message.model);
      if (declaredModel) termSetPaneModel(t, declaredModel);

      // Transcript-format ai-title: rename the pane.
      if (type === "ai-title" && typeof obj.aiTitle === "string") {
        const head = t.pane.querySelector(".term-head .task");
        if (head) head.textContent = obj.aiTitle;
        return;
      }

      if (type === "system") return termRenderSystem(t, obj);
      if (type === "result") return termRenderResult(t, obj);

      if (type === "assistant" && obj.message) {
        const content = obj.message.content;
        if (Array.isArray(content)) {
          // The final assistant message arrives AFTER the stream_event deltas
          // that already painted the same text/tool_use into the current
          // block. Re-appending duplicates the answer ("Hi!Hi!" syndrome) and
          // re-renders the same tool pills twice. Dedupe by checking what we
          // already have in the live block.
          for (const blk of content) {
            if (blk.type === "text" && typeof blk.text === "string") {
              const cur = t.currentAssistant;
              const accSoFar = cur ? (cur.querySelector(".text").dataset.raw || "") : "";
              // If deltas already streamed (any) text into this block, the
              // final text is a copy — skip. If the block is empty, this IS
              // the first text we've seen (e.g. transcript replay where no
              // deltas exist) — append normally.
              if (!accSoFar) termAppendAssistantText(t, blk.text);
            } else if (blk.type === "tool_use") {
              // stream_event/content_block_start may have already created the
              // pill; don't duplicate it here.
              if (!t.toolUseEls.has(blk.id)) {
                termAddToolPill(t, blk.id, blk.name, blk.input);
              }
            } else if (blk.type === "thinking" && typeof blk.thinking === "string") {
              const block = termAssistantBlock(t);
              const t2 = block.querySelector(".text");
              // Render thinking as a collapsed <details> so long internal
              // monologues don't drown the actual answer. Click summary to
              // expand. The char-count gives a sense of how much thinking
              // happened without forcing the user to read all of it.
              const det = document.createElement("details");
              det.className = "thinking-block";
              const sum = document.createElement("summary");
              sum.textContent = `thinking · ${blk.thinking.length} chars`;
              const pre = document.createElement("pre");
              pre.textContent = blk.thinking;
              det.appendChild(sum);
              det.appendChild(pre);
              t2.appendChild(det);
            }
          }
        } else if (typeof content === "string") {
          // Transcript shape: assistant message as a plain string.
          t.currentAssistant = null;
          termAppendAssistantText(t, content);
        }
        return;
      }

      if (type === "user" && obj.message) {
        const content = obj.message.content;
        if (typeof content === "string") {
          // Skip system-reminder-only frames (cluttering, not user-typed).
          const stripped = content.replace(/<system-reminder>[\s\S]*?<\/system-reminder>/g, "").trim();
          if (!stripped) return;
          termRenderUserMessage(t, stripped);
        } else if (Array.isArray(content)) {
          for (const blk of content) {
            if (blk.type === "tool_result") {
              termMarkToolResult(t, blk.tool_use_id, !!blk.is_error, blk.content);
            } else if (blk.type === "text" && typeof blk.text === "string") {
              termRenderUserMessage(t, blk.text);
            }
          }
        }
        return;
      }

      if (type === "stream_event") {
        // Partial deltas - extract text and append to the current assistant block.
        const ev = obj.event || {};
        if (ev.type === "content_block_delta" && ev.delta && ev.delta.type === "text_delta") {
          termAppendAssistantText(t, ev.delta.text || "");
        } else if (ev.type === "content_block_start" && ev.content_block) {
          const cb = ev.content_block;
          if (cb.type === "tool_use") {
            termAddToolPill(t, cb.id, cb.name, cb.input || {});
          }
        }
        return;
      }

      // Genuinely unknown — dump as a small dim line so we notice it but
      // it doesn't dominate the pane.
      const pre = document.createElement("pre");
      pre.style.color = "var(--text-faint)";
      pre.style.fontSize = "11px";
      pre.style.margin = "4px 0";
      pre.textContent = "[unhandled " + (type || "?") + "]";
      t.body.appendChild(pre);
    }

    function termOpen(jobId, meta) {
      if (TERMS.has(jobId)) return;
      const grid = $("#terms-grid");
      const taskPreview = (meta?.task || "").replace(/\s+/g, " ").slice(0, 120);
      const pane = document.createElement("div");
      pane.className = "term-pane";
      pane.dataset.jobId = jobId;
      pane.innerHTML = `
        <div class="term-head">
          <span class="pill running status-pill" title="job ${escape(jobId)}">connecting</span>
          <span class="task" title="${escape(meta?.task || "")}">${escape(taskPreview || jobId)}</span>
          <span class="cost-pill" title="aggregated cost / turns / time for this session"></span>
          <span class="id">${escape(jobId.slice(0, 8))}</span>
          <span class="actions">
            <button class="stop-btn" title="Interrupt the current generation (keep session alive)">stop</button>
            <button class="search-btn" title="Search in this pane (Ctrl+F)">find</button>
            <button class="pin-btn" title="Maximise / restore this pane">pin</button>
            <button class="export-btn" title="Export as markdown">export</button>
            <button class="cancel-btn danger" title="Cancel the running subprocess">cancel</button>
            <button class="close-btn" title="Close this pane">close</button>
          </span>
        </div>
        <div class="term-search">
          <input type="text" placeholder="search in this pane (Esc to close)" />
          <span class="matches">0 / 0</span>
          <button class="search-prev">↑</button>
          <button class="search-next">↓</button>
          <button class="search-close">×</button>
        </div>
        <div class="term-body" tabindex="0"></div>
        <div class="attach-tray" style="display:none"></div>
        <div class="term-foot">
          <textarea class="stdin-input" rows="1" autocomplete="off" placeholder="type, /skill, @file, paste/drop images, Enter sends · Shift+Enter newline"></textarea>
          <button class="send-btn">send</button>
        </div>
      `;
      grid.appendChild(pane);

      const body = pane.querySelector(".term-body");
      const input = pane.querySelector(".stdin-input");
      const sendBtn = pane.querySelector(".send-btn");
      // Auto-grow the textarea up to a sensible max so long prompts don't
      // get clipped to one line but also don't eat the entire pane.
      const autosize = () => {
        input.style.height = "auto";
        const next = Math.min(input.scrollHeight, 220);
        input.style.height = next + "px";
      };
      input.addEventListener("input", autosize);
      const kind = meta?.kind || "orchestrate";
      if (kind === "chat") body.classList.add("chat");
      const t = {
        jobId, pane, body, input, sendBtn,
        source: null,
        task: meta?.task || "",
        kind,
        jsonBuf: "",
        currentAssistant: null,   // element for the in-progress assistant message
        toolUseEls: new Map(),    // tool_use_id -> {pill, detail}
        attached: { images: [], files: [] },
        sessionId: meta?.session_id || "",  // enables resume on dead-pane
        model: meta?.model || "",  // seed from /api/jobs; replaced on first init/assistant frame
      };
      TERMS.set(jobId, t);

      // Composer wiring (only meaningful for chat panes; harmless otherwise).
      input.addEventListener("input", () => termHandleComposerInput(t));
      input.addEventListener("keydown", (e) => {
        if (e.key === "Escape") { termCloseAutocomplete(t); return; }
        if (t._popOpen && e.key === "Enter") {
          // Let the popup row's mousedown handle picks; fall back to send.
          const first = t.pane.querySelector(".composer-pop-row.active");
          if (first) { first.dispatchEvent(new MouseEvent("mousedown")); e.preventDefault(); return; }
        }
      });
      input.addEventListener("paste", (e) => {
        const items = e.clipboardData?.items || [];
        for (const it of items) {
          if (it.kind === "file" && it.type.startsWith("image/")) {
            const f = it.getAsFile();
            if (f) { termPasteImage(t, f); e.preventDefault(); }
          }
        }
      });
      pane.addEventListener("dragover", (e) => { e.preventDefault(); pane.classList.add("dragover"); });
      pane.addEventListener("dragleave", () => pane.classList.remove("dragover"));
      pane.addEventListener("drop", (e) => {
        e.preventDefault();
        pane.classList.remove("dragover");
        for (const f of e.dataTransfer.files || []) {
          if (f.type.startsWith("image/")) termPasteImage(t, f);
        }
      });
      termInitAutoFollow(t);

      pane.addEventListener("click", () => {
        document.querySelectorAll(".term-pane.focus").forEach((p) => p.classList.remove("focus"));
        pane.classList.add("focus");
      });
      pane.querySelector(".close-btn").addEventListener("click", (e) => {
        e.stopPropagation();
        termClose(jobId);
      });
      pane.querySelector(".cancel-btn").addEventListener("click", async (e) => {
        e.stopPropagation();
        try {
          await postJson(`/api/jobs/${jobId}/cancel`, {});
        } catch (err) {
          /* ignore */
        }
      });
      pane.querySelector(".stop-btn")?.addEventListener("click", async (e) => {
        e.stopPropagation();
        try {
          await postJson(`/api/jobs/${jobId}/interrupt`, {});
        } catch (err) {
          const note = document.createElement("div");
          note.className = "msg system";
          note.style.color = "var(--bad)";
          note.textContent = "[stop failed: " + err.message + "]";
          t.body.appendChild(note);
        }
      });
      pane.querySelector(".pin-btn")?.addEventListener("click", (e) => {
        e.stopPropagation();
        pane.classList.toggle("pinned");
        const btn = pane.querySelector(".pin-btn");
        btn.classList.toggle("active", pane.classList.contains("pinned"));
        btn.textContent = pane.classList.contains("pinned") ? "unpin" : "pin";
      });
      pane.querySelector(".export-btn")?.addEventListener("click", (e) => {
        e.stopPropagation();
        termExportMarkdown(t);
      });
      pane.querySelector(".search-btn")?.addEventListener("click", (e) => {
        e.stopPropagation();
        termToggleSearch(t);
      });
      // In-pane search wiring.
      const searchBar = pane.querySelector(".term-search");
      const searchInput = searchBar.querySelector("input");
      searchInput.addEventListener("input", () => termRunSearch(t));
      searchInput.addEventListener("keydown", (e) => {
        if (e.key === "Escape") { termToggleSearch(t, false); return; }
        if (e.key === "Enter") { e.preventDefault(); termSearchStep(t, e.shiftKey ? -1 : +1); }
      });
      searchBar.querySelector(".search-next").addEventListener("click", () => termSearchStep(t, +1));
      searchBar.querySelector(".search-prev").addEventListener("click", () => termSearchStep(t, -1));
      searchBar.querySelector(".search-close").addEventListener("click", () => termToggleSearch(t, false));
      // Ctrl+F / Cmd+F inside the body opens the search bar.
      pane.addEventListener("keydown", (e) => {
        if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "f") {
          e.preventDefault();
          termToggleSearch(t, true);
        }
      });
      sendBtn.addEventListener("click", () => termSend(jobId));
      input.addEventListener("keydown", (e) => {
        if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); termSend(jobId); }
      });

      // Wire SSE
      const es = new EventSource(`/api/jobs/${jobId}/stream`);
      t.source = es;
      const statusPill = pane.querySelector(".status-pill");
      es.onopen = () => { statusPill.classList.remove("queued"); statusPill.textContent = "live"; };
      // For chat jobs the SSE stream is one stream-json object per line;
      // buffer partial lines, parse each, and render structured messages.
      // For other kinds the chunk is plain text and goes straight in.
      //
      // IMPORTANT: do NOT append "\n" here. The server's pump reads stdout
      // in 1024-byte chunks, so a single long JSON record (e.g. the 8KB
      // SessionStart hook context) gets split across multiple SSE events.
      // ``ev.data`` already preserves the original chunk's newline boundaries
      // (an internal trailing newline becomes a final empty data: line);
      // forcing an extra "\n" would prematurely terminate a partial line and
      // hand a corrupt half-record to JSON.parse, which then falls through to
      // termRenderRaw and dumps it as a raw "msg system" block.
      es.onmessage = (ev) => {
        if (t.kind === "chat") termHandleChatChunk(t, ev.data);
        else termAppendChunk(t, ev.data);
      };
      es.addEventListener("end", () => {
        termSetDead(t, "done");
        try { es.close(); } catch (_) {}
        loadJobs();
      });
      es.onerror = () => {
        // The stream ends with `end` event; an error here usually means the
        // subprocess finished AND the server closed the connection. Mark dead
        // but don't spam the body.
        if (!t.pane.classList.contains("dead")) {
          termSetDead(t, "ended");
        }
        try { es.close(); } catch (_) {}
      };
      // Initial cost fetch (also handles resumed sessions that already
      // have prior turns accumulated on disk).
      termRefreshCost(t);

      termRenderEmptyState();
    }

    // ----- IDE transcript mirror panes -----

    async function termRefreshTranscriptPicker() {
      const sel = $("#term-transcript-picker");
      if (!sel) return;
      try {
        const r = await fetch("/api/transcripts", { cache: "no-store" });
        if (!r.ok) { sel.innerHTML = `<option value="">— unavailable —</option>`; return; }
        const data = await r.json();
        const items = data.transcripts || [];
        if (!items.length) {
          sel.innerHTML = `<option value="">— no IDE transcripts found —</option>`;
          $("#term-transcript-open").disabled = true;
          $("#term-transcript-newest").disabled = true;
          return;
        }
        const open = new Set([...TERMS.keys()].filter((k) => k.startsWith("ide:")));
        sel.innerHTML = items.map((t) => {
          const sid = t.session_id;
          const stale = open.has("ide:" + sid) ? " (open)" : "";
          const kb = Math.round(t.size_bytes / 1024);
          const when = (t.modified || "").slice(11, 16);
          return `<option value="${escape(sid)}">[${escape(when)}] ${escape(sid.slice(0,8))}… (${kb} KB)${stale}</option>`;
        }).join("");
        $("#term-transcript-open").disabled = false;
        $("#term-transcript-newest").disabled = false;
      } catch (e) {
        sel.innerHTML = `<option value="">— error: ${escape(e.message)} —</option>`;
      }
    }

    function termOpenTranscript(sessionId) {
      const paneKey = "ide:" + sessionId;
      if (TERMS.has(paneKey)) return;
      const grid = $("#terms-grid");
      const pane = document.createElement("div");
      pane.className = "term-pane focus";
      pane.dataset.jobId = paneKey;
      pane.innerHTML = `
        <div class="term-head">
          <span class="pill claude status-pill">IDE mirror</span>
          <span class="task" title="mirror of Claude Code session ${escape(sessionId)}">IDE chat ${escape(sessionId.slice(0, 8))}…</span>
          <span class="id">${escape(sessionId.slice(0, 8))}</span>
          <span class="actions">
            <button class="close-btn" title="Close this pane">close</button>
          </span>
        </div>
        <div class="term-body chat" tabindex="0"></div>
        <div class="term-foot">
          <textarea class="stdin-input" rows="1" placeholder="type to fork this IDE session — Enter forks &amp; sends · Shift+Enter newline"></textarea>
          <button class="send-btn">fork &amp; send</button>
        </div>
      `;
      grid.appendChild(pane);
      const body = pane.querySelector(".term-body");
      const t = {
        jobId: paneKey,
        pane, body,
        input: pane.querySelector(".stdin-input"),
        sendBtn: pane.querySelector(".send-btn"),
        source: null,
        task: "IDE session " + sessionId,
        kind: "transcript",
        jsonBuf: "",
        currentAssistant: null,
        toolUseEls: new Map(),
      };
      TERMS.set(paneKey, t);
      termInitAutoFollow(t);
      pane.querySelector(".close-btn").addEventListener("click", (e) => {
        e.stopPropagation();
        termClose(paneKey);
      });
      pane.addEventListener("click", () => {
        document.querySelectorAll(".term-pane.focus").forEach((p) => p.classList.remove("focus"));
        pane.classList.add("focus");
      });

      // First send forks the IDE session into a writable dashboard chat
      // (claude --resume <sid>). The mirror pane is KEPT OPEN alongside
      // the fork so the operator can compare the original IDE branch
      // (still owned by the IDE writer) to the new dashboard branch
      // side-by-side. Mirror's composer is disabled after the first fork
      // — additional forks should come from the IDE-side itself.
      const forkAndSend = async () => {
        const text = t.input.value.trim();
        if (!text) return;
        t.sendBtn.disabled = true;
        try {
          const res = await postJson("/api/jobs", {
            kind: "chat",
            task: text,
            resume_session_id: sessionId,
          });
          // Banner inside the mirror documenting what just happened.
          const banner = document.createElement("div");
          banner.className = "msg system";
          banner.style.color = "var(--warn)";
          banner.textContent = `[forked into dashboard chat ${res.id.slice(0,8)} — new pane opened to the right]`;
          t.body.appendChild(banner);
          // Lock down the mirror's composer; this branch is now history.
          t.input.value = "";
          t.input.disabled = true;
          t.input.placeholder = "mirror pane is read-only — continue in the fork pane";
          t.sendBtn.disabled = true;
          t.sendBtn.textContent = "forked";
          const sp = t.pane.querySelector(".status-pill");
          if (sp) { sp.textContent = "forked"; sp.classList.add("warn"); }
          // Open the writable chat pane next to this one.
          termOpen(res.id, res);
          await loadJobs();
        } catch (e) {
          const err = document.createElement("div");
          err.className = "msg system";
          err.style.color = "var(--bad)";
          err.textContent = `[fork failed: ${e.message}]`;
          t.body.appendChild(err);
          t.sendBtn.disabled = false;
        }
      };
      t.sendBtn.addEventListener("click", forkAndSend);
      t.input.addEventListener("keydown", (e) => {
        if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); forkAndSend(); }
      });

      const es = new EventSource(`/api/transcripts/${sessionId}/stream`);
      t.source = es;
      const statusPill = pane.querySelector(".status-pill");
      es.onopen = () => { statusPill.textContent = "IDE live"; };
      es.onmessage = (ev) => termHandleTranscriptChunk(t, ev.data + "\n");
      es.addEventListener("end", () => {
        statusPill.textContent = "IDE ended";
        statusPill.classList.add("done");
        try { es.close(); } catch (_) {}
      });
      es.onerror = () => {
        if (!t.pane.classList.contains("dead")) {
          statusPill.textContent = "disconnected";
          statusPill.classList.add("warn");
        }
      };
      termRenderEmptyState();
      termRefreshTranscriptPicker();
    }

    function termHandleTranscriptChunk(t, chunk) {
      t.jsonBuf += chunk;
      let nl;
      while ((nl = t.jsonBuf.indexOf("\n")) !== -1) {
        const line = t.jsonBuf.slice(0, nl);
        t.jsonBuf = t.jsonBuf.slice(nl + 1);
        const trimmed = line.trim();
        if (!trimmed) continue;
        let obj;
        try { obj = JSON.parse(trimmed); } catch (_) { continue; }
        termRenderTranscriptRecord(t, obj);
      }
      termAutoScroll(t);
    }

    // Strip IDE/system wrapper blocks from a user message. If nothing
    // meaningful is left after stripping (i.e. the message was ONLY
    // wrappers), return null so the caller can skip rendering entirely.
    function termCleanUserPrompt(text) {
      if (!text) return null;
      let s = String(text);
      s = s.replace(/<system-reminder>[\s\S]*?<\/system-reminder>/g, "");
      s = s.replace(/<ide_opened_file>[\s\S]*?<\/ide_opened_file>/g, "");
      s = s.replace(/<ide_selection>[\s\S]*?<\/ide_selection>/g, "");
      s = s.replace(/<task-notification>[\s\S]*?<\/task-notification>/g, "");
      s = s.trim();
      return s || null;
    }

    function termRenderTranscriptRecord(t, obj) {
      // Claude Code JSONL records have shapes like:
      //   {type:"user",      message:{role:"user", content:"..." | [...]}}
      //   {type:"assistant", message:{role:"assistant", content:[{type:"text",text}, {type:"tool_use",...}]}}
      //   {type:"tool_use_result", ...}
      //   {type:"attachment", ... }   // hooks/observations
      //   {type:"last-prompt", lastPrompt:"..."}
      //   {type:"ai-title", aiTitle:"..."}
      // We map them to the same UI primitives as the chat panes.
      const type = obj.type;
      // Mirror the chat-mode behaviour: capture the model id so subsequent
      // assistant blocks render with the real model name in the role chip.
      const declaredModel = obj.model || (obj.message && obj.message.model);
      if (declaredModel) termSetPaneModel(t, declaredModel);
      if (type === "user" && obj.message) {
        const content = obj.message.content;
        if (typeof content === "string") {
          const cleaned = termCleanUserPrompt(content);
          if (cleaned) termRenderUserMessage(t, cleaned);
        } else if (Array.isArray(content)) {
          // Could be tool_result blocks too.
          for (const blk of content) {
            if (blk.type === "tool_result") {
              termMarkToolResult(t, blk.tool_use_id, !!blk.is_error, blk.content);
            } else if (blk.type === "text" && typeof blk.text === "string") {
              const cleaned = termCleanUserPrompt(blk.text);
              if (cleaned) termRenderUserMessage(t, cleaned);
            }
          }
        }
        return;
      }
      if (type === "assistant" && obj.message && Array.isArray(obj.message.content)) {
        for (const blk of obj.message.content) {
          if (blk.type === "text" && typeof blk.text === "string") {
            // Each transcript "assistant" entry is a discrete message - start fresh.
            t.currentAssistant = null;
            termAppendAssistantText(t, blk.text);
          } else if (blk.type === "tool_use") {
            termAddToolPill(t, blk.id, blk.name, blk.input);
          }
        }
        return;
      }
      if (type === "tool_use_result" || type === "tool_result") {
        const id = obj.tool_use_id || obj.id;
        if (id) termMarkToolResult(t, id, !!obj.is_error, obj.content ?? obj.output ?? "");
        return;
      }
      if (type === "last-prompt") {
        // Meta event — duplicates the corresponding `type:"user"` record.
        // Skip to avoid showing the user's message twice.
        return;
      }
      if (type === "ai-title" && typeof obj.aiTitle === "string") {
        // Surface the session title once at the top.
        const head = t.pane.querySelector(".term-head .task");
        if (head) head.textContent = obj.aiTitle;
        return;
      }
      // Attachments and other meta frames: ignore quietly.
    }

    // Track which job ids we've already auto-opened in this browser tab.
    // We never re-open an id once the operator has closed it.
    var AUTO_OPENED_ONCE = new Set();
    // User preference: if disabled, auto-open does nothing.
    function termAutoOpenEnabled() {
      return localStorage.getItem("dash.autoOpenChats") !== "0";
    }
    function termSetAutoOpen(enabled) {
      localStorage.setItem("dash.autoOpenChats", enabled ? "1" : "0");
      const btn = $("#term-autoopen-toggle");
      if (btn) btn.textContent = enabled ? "auto-open: on" : "auto-open: off";
    }
    function termAutoOpenActive(jobs) {
      if (!termAutoOpenEnabled()) return;
      // Only auto-open when the operator is actually on the Terminals view,
      // so we don't yank focus while they're reading Memory or Decisions.
      if (!$("#view-terminals").classList.contains("active")) return;
      for (const j of (jobs || [])) {
        if (j.kind !== "chat" && j.kind !== "chat-codex") continue;
        if (j.status !== "running" && j.status !== "queued") continue;
        if (TERMS.has(j.id)) continue;
        if (AUTO_OPENED_ONCE.has(j.id)) continue;
        AUTO_OPENED_ONCE.add(j.id);
        termOpen(j.id, j);
      }
      // Also mirror live IDE Claude Code sessions running outside the
      // dashboard (any transcript file written-to in the last 5 minutes).
      termAutoOpenActiveTranscripts();
    }

    // IDE transcript files touched within this window count as "live".
    // Claude Code writes to the JSONL on every user/assistant turn, so a
    // few minutes of silence is a safe "abandoned" threshold.
    var TRANSCRIPT_ACTIVE_WINDOW_MS = 5 * 60 * 1000;
    async function termAutoOpenActiveTranscripts() {
      if (!termAutoOpenEnabled()) return;
      if (!$("#view-terminals").classList.contains("active")) return;
      try {
        const r = await fetch("/api/transcripts", { cache: "no-store" });
        if (!r.ok) return;
        const data = await r.json();
        const now = Date.now();
        for (const t of (data.transcripts || [])) {
          const sid = t.session_id;
          if (!sid) continue;
          const key = "ide:" + sid;
          if (TERMS.has(key)) continue;
          if (AUTO_OPENED_ONCE.has(key)) continue;
          const mtime = t.modified ? Date.parse(t.modified) : 0;
          if (!Number.isFinite(mtime) || mtime <= 0) continue;
          if ((now - mtime) > TRANSCRIPT_ACTIVE_WINDOW_MS) continue;
          AUTO_OPENED_ONCE.add(key);
          termOpenTranscript(sid);
        }
      } catch (_) { /* ignore - we'll retry next poll */ }
    }

    async function termOpenAllRunning() {
      // Combines two sources of "active chat":
      //   1. Dashboard chat jobs (running / queued / cancelling).
      //   2. IDE Claude Code transcripts whose JSONL was written-to in the
      //      last 5 minutes — those are live sessions running in your IDE.
      let opened = 0, already = 0, scanned = 0;
      try {
        const r = await fetch("/api/jobs", { cache: "no-store" });
        if (r.ok) {
          const data = await r.json();
          const all = data.jobs || [];
          scanned += all.length;
          const candidates = all.filter((j) =>
            (j.kind === "chat" || j.kind === "chat-codex") &&
            ["running", "queued", "cancelling"].includes(j.status)
          );
          for (const j of candidates) {
            if (TERMS.has(j.id)) { already++; continue; }
            termOpen(j.id, j);
            AUTO_OPENED_ONCE.add(j.id);
            opened++;
          }
        }
      } catch (e) {
        setMsg("#term-msg", "err", "jobs: " + e.message, 4000);
      }
      try {
        const r = await fetch("/api/transcripts", { cache: "no-store" });
        if (r.ok) {
          const data = await r.json();
          const tx = data.transcripts || [];
          scanned += tx.length;
          const now = Date.now();
          for (const t of tx) {
            const sid = t.session_id;
            if (!sid) continue;
            const key = "ide:" + sid;
            const mtime = t.modified ? Date.parse(t.modified) : 0;
            if (!Number.isFinite(mtime) || mtime <= 0) continue;
            if ((now - mtime) > TRANSCRIPT_ACTIVE_WINDOW_MS) continue;
            if (TERMS.has(key)) { already++; continue; }
            AUTO_OPENED_ONCE.add(key);
            termOpenTranscript(sid);
            opened++;
          }
        }
      } catch (e) {
        setMsg("#term-msg", "err", "transcripts: " + e.message, 4000);
      }
      if (!opened && !already) {
        setMsg("#term-msg", "warn", `nothing active (scanned ${scanned} job/transcript entr(ies))`, 4000);
        return;
      }
      const msg = opened
        ? `opened ${opened}${already ? `, ${already} already open` : ""}`
        : `${already} already open — nothing to do`;
      setMsg("#term-msg", opened ? "ok" : "warn", msg, 4000);
    }

    function termCloseAllFinished() {
      for (const [jobId, t] of TERMS.entries()) {
        if (t.pane.classList.contains("dead")) termClose(jobId);
      }
    }

    document.addEventListener("DOMContentLoaded", () => {
      $("#term-open")?.addEventListener("click", async () => {
        const raw = $("#term-picker").value;
        if (!raw) return;
        const sep = raw.indexOf(":");
        const source = raw.slice(0, sep);
        const id = raw.slice(sep + 1);
        if (source === "ide") {
          termOpenTranscript(id);
          return;
        }
        // Default: dashboard-spawned job.
        try {
          const r = await fetch("/api/jobs", { cache: "no-store" });
          const data = await r.json();
          const meta = (data.jobs || []).find((j) => j.id === id);
          termOpen(id, meta || { task: "" });
          await loadJobs();
        } catch (e) {
          setMsg("#term-msg", "err", e.message, 4000);
        }
      });
      $("#term-open-all")?.addEventListener("click", termOpenAllRunning);
      // Restore the auto-open preference and wire its toggle.
      termSetAutoOpen(termAutoOpenEnabled());
      $("#term-autoopen-toggle")?.addEventListener("click", () => {
        termSetAutoOpen(!termAutoOpenEnabled());
      });
      $("#term-close-all")?.addEventListener("click", termCloseAllFinished);
      // Initial picker fill (single unified source).
      termRefreshTranscriptPicker();
    });

