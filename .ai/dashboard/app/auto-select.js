/* Auto-select rankings view — fetches /api/auto-select and renders per-group
 * candidate tables (top 3). Powered by .ai/metrics.jsonl (PR 3).
 */
(function () {
  "use strict";

  function $(sel) {
    return document.querySelector(sel);
  }

  const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
  function escape(s) {
    return (s == null ? "" : String(s)).replace(/[&<>"']/g, (c) => ESC[c]);
  }

  function formatMs(ms) {
    if (typeof ms !== "number" || ms < 0) return "—";
    if (ms < 1000) return ms + " ms";
    if (ms < 60_000) return (ms / 1000).toFixed(1) + " s";
    return Math.round(ms / 1000) + " s";
  }

  var THRESHOLD_KEY = "auto-select.min_samples";
  var THRESHOLD_DEFAULT = 3;

  function clampThreshold(v) {
    var n = parseInt(v, 10);
    if (!Number.isFinite(n)) return THRESHOLD_DEFAULT;
    return Math.max(1, Math.min(50, n));
  }

  function getThreshold() {
    var input = $("#auto-select-min-samples");
    if (input && input.value !== "") return clampThreshold(input.value);
    var stored = null;
    try { stored = localStorage.getItem(THRESHOLD_KEY); } catch (_) { /* ignore */ }
    return clampThreshold(stored != null ? stored : THRESHOLD_DEFAULT);
  }

  function setThreshold(v) {
    var n = clampThreshold(v);
    try { localStorage.setItem(THRESHOLD_KEY, String(n)); } catch (_) { /* ignore */ }
    var input = $("#auto-select-min-samples");
    if (input) input.value = String(n);
    return n;
  }

  function formatLastRecord(iso) {
    if (!iso || typeof iso !== "string") return null;
    var t = Date.parse(iso);
    if (!Number.isFinite(t)) return { label: iso, stale: false };
    var diffMin = Math.round((Date.now() - t) / 60_000);
    var ago;
    if (diffMin < 1) ago = "just now";
    else if (diffMin < 60) ago = "~" + diffMin + "m ago";
    else if (diffMin < 60 * 24) ago = "~" + Math.round(diffMin / 60) + "h ago";
    else ago = "~" + Math.round(diffMin / (60 * 24)) + "d ago";
    return { label: iso + " (" + ago + ")", stale: diffMin >= 60 * 24 };
  }

  function srClass(rate) {
    if (!Number.isFinite(rate)) return "";
    if (rate >= 0.9) return "as-sr-good";
    if (rate >= 0.7) return "as-sr-warn";
    return "as-sr-bad";
  }

  function scoreBar(score) {
    var pct = Math.max(0, Math.min(1, Number(score) || 0)) * 100;
    return `<span class="as-score-bar" aria-hidden="true"><span style="width:${pct.toFixed(1)}%"></span></span>`;
  }

  function renderGroup(group) {
    const k = group.key;
    const header =
      `<span class="as-phase">${escape(k.phase)}</span> / ` +
      `${escape(k.size || "any-size")} / ` +
      `${escape(k.risk || "any-risk")} / ` +
      `${escape(k.budget || "any-budget")}`;
    const rows = (group.candidates || [])
      .map((c, idx) => {
        const eff = c.reasoning_effort == null ? "—" : escape(c.reasoning_effort);
        const sr = (c.success_rate * 100).toFixed(0) + "%";
        const sc = c.score.toFixed(3);
        const rowClass = idx === 0 ? ' class="as-top"' : "";
        return (
          `<tr${rowClass}>` +
          `<td><span class="as-rank">#${idx + 1}</span></td>` +
          `<td class="as-tool">${escape(c.tool)}</td>` +
          `<td class="as-model">${escape(c.model)}</td>` +
          `<td class="as-effort">${eff}</td>` +
          `<td class="as-num">${c.samples}</td>` +
          `<td class="as-num ${srClass(c.success_rate)}">${sr}</td>` +
          `<td class="as-num">${formatMs(c.mean_duration_ms)}</td>` +
          `<td class="as-num"><span class="as-score">${scoreBar(c.score)}<span>${sc}</span></span></td>` +
          `</tr>`
        );
      })
      .join("");
    return (
      `<div class="as-group">` +
      `<h3 class="as-group-key">${header}</h3>` +
      `<table class="as-table">` +
      `<thead><tr>` +
      `<th>rank</th>` +
      `<th>tool</th>` +
      `<th>model</th>` +
      `<th>effort</th>` +
      `<th class="as-num">samples</th>` +
      `<th class="as-num">success</th>` +
      `<th class="as-num">mean</th>` +
      `<th class="as-num">score</th>` +
      `</tr></thead>` +
      `<tbody>${rows}</tbody>` +
      `</table>` +
      `</div>`
    );
  }

  function renderAutoSelectSkeletons() {
    const root = $("#auto-select-rankings");
    if (!root || root.dataset.skeletoned) return;
    const lines = '<span class="skeleton skeleton-as-line"></span>'.repeat(4);
    const group = `<div class="skeleton-as-group"><span class="skeleton skeleton-as-head"></span>${lines}</div>`;
    root.innerHTML = group.repeat(3);
    root.dataset.skeletoned = "1";
  }

  // Monotonic sequence so a slow response from threshold=3 can't paint
  // over a freshly-issued threshold=5 (typing in the input fires
  // loadAutoSelect every debounced keystroke).
  var _autoSelectSeq = 0;
  async function loadAutoSelect() {
    const meta = $("#auto-select-meta");
    const root = $("#auto-select-rankings");
    if (!root) return;
    renderAutoSelectSkeletons();
    const threshold = setThreshold(getThreshold());
    const mySeq = ++_autoSelectSeq;
    try {
      const r = await fetch("/api/auto-select?min_samples=" + threshold, { cache: "no-store" });
      if (mySeq !== _autoSelectSeq) return;
      if (!r.ok) throw new Error("HTTP " + r.status);
      const data = await r.json();
      if (mySeq !== _autoSelectSeq) return;
      const groups = data.groups || [];
      // `??` (not `||`) for numeric counters so a legitimate 0 from the API
      // is preserved rather than tripping the falsy fallback. For samples /
      // dropped the fallback is itself 0 so the user-visible result matches,
      // but using `??` keeps the intent (only substitute for null/undefined)
      // explicit and matches the style of `effective` below.
      const samples = data.samples ?? 0;
      const dropped = data.dropped_candidates ?? 0;
      const effective = data.min_samples ?? threshold;
      const lastRecord = formatLastRecord(data.last_record_ts);
      const label = $("#auto-select-threshold-label");
      if (label) label.textContent = String(effective);
      const parts = [
        `${samples} sample(s)`,
        `${groups.length} group(s)`,
        `${dropped} dropped (<${effective})`,
      ];
      let metaHtml = parts.map(escape).join(" · ");
      if (lastRecord) {
        const cls = lastRecord.stale ? "as-meta-fresh is-stale" : "as-meta-fresh";
        metaHtml += ` · last record <span class="${cls}">${escape(lastRecord.label)}</span>`;
      }
      if (meta) meta.innerHTML = metaHtml;
      delete root.dataset.skeletoned;
      if (groups.length === 0) {
        root.innerHTML =
          `<div class="tl-empty">No ranked groups yet. The planner needs at least ${effective} record(s) per ` +
          `<code>(tool, model, effort)</code> for a <code>(phase, size, risk, budget)</code> tuple ` +
          `before adaptive scoring kicks in. Lower the threshold above, or run more tasks with ` +
          `<code>auto_select.enabled: true</code> to populate <code>.ai/metrics.jsonl</code>.</div>`;
        return;
      }
      root.innerHTML = groups.map(renderGroup).join("");
    } catch (err) {
      if (meta) meta.textContent = "load failed";
      delete root.dataset.skeletoned;
      // Mirror settings.js: keep the prior render visible so a transient
      // network blip doesn't wipe the operator's last-known auto-select
      // state. Only surface the failure in the meta strip + toast.
      if (typeof window.setMsg === "function") {
        window.setMsg("#auto-select-load", "err", "Auto-select load failed: " + err);
      }
      // If the panel was empty (first paint failed), still show the error
      // marker so the operator sees something rather than a blank pane.
      if (!root.innerHTML || root.dataset.firstPaint !== "1") {
        root.innerHTML =
          `<div class="tl-empty">Failed to load: ${escape(err && err.message ? err.message : String(err))}.</div>`;
      }
    } finally {
      if (root) root.dataset.firstPaint = "1";
    }
  }

  function wireThresholdInput() {
    const input = $("#auto-select-min-samples");
    if (!input || input.dataset.wired === "1") return;
    input.dataset.wired = "1";
    input.value = String(getThreshold());
    let debounce = null;
    input.addEventListener("input", () => {
      if (debounce) clearTimeout(debounce);
      debounce = setTimeout(() => {
        setThreshold(input.value);
        loadAutoSelect();
      }, 250);
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", wireThresholdInput);
  } else {
    wireThresholdInput();
  }

  window.loadAutoSelect = loadAutoSelect;
})();
