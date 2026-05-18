/* Auto-select rankings view — fetches /api/auto-select and renders per-group
 * candidate tables (top 3). Powered by .ai/metrics.jsonl (PR 3).
 */
(function () {
  "use strict";

  function $(sel) {
    return document.querySelector(sel);
  }

  function escape(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;",
    })[c]);
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
    if (!Number.isFinite(t)) return iso;
    var diffMin = Math.round((Date.now() - t) / 60_000);
    var ago;
    if (diffMin < 1) ago = "just now";
    else if (diffMin < 60) ago = "~" + diffMin + "m ago";
    else if (diffMin < 60 * 24) ago = "~" + Math.round(diffMin / 60) + "h ago";
    else ago = "~" + Math.round(diffMin / (60 * 24)) + "d ago";
    return iso + " (" + ago + ")";
  }

  function renderGroup(group) {
    const k = group.key;
    const header = [
      escape(k.phase),
      k.size ? escape(k.size) : "any-size",
      k.risk ? escape(k.risk) : "any-risk",
      k.budget ? escape(k.budget) : "any-budget",
    ].join(" / ");
    const rows = (group.candidates || [])
      .map((c, idx) => {
        const eff = c.reasoning_effort == null ? "—" : escape(c.reasoning_effort);
        const sr = (c.success_rate * 100).toFixed(0) + "%";
        const sc = c.score.toFixed(3);
        return (
          `<tr>` +
          `<td>#${idx + 1}</td>` +
          `<td>${escape(c.tool)}</td>` +
          `<td>${escape(c.model)}</td>` +
          `<td>${eff}</td>` +
          `<td>${c.samples}</td>` +
          `<td>${sr}</td>` +
          `<td>${formatMs(c.mean_duration_ms)}</td>` +
          `<td>${sc}</td>` +
          `</tr>`
        );
      })
      .join("");
    return (
      `<div class="block" style="margin-top:12px">` +
      `<h3 style="margin:0 0 6px 0;font-size:13px;color:var(--fg-dim)">${header}</h3>` +
      `<table class="as-table" style="width:100%;font-size:12px;border-collapse:collapse">` +
      `<thead><tr>` +
      `<th style="text-align:left;padding:4px 6px">rank</th>` +
      `<th style="text-align:left;padding:4px 6px">tool</th>` +
      `<th style="text-align:left;padding:4px 6px">model</th>` +
      `<th style="text-align:left;padding:4px 6px">effort</th>` +
      `<th style="text-align:right;padding:4px 6px">samples</th>` +
      `<th style="text-align:right;padding:4px 6px">success</th>` +
      `<th style="text-align:right;padding:4px 6px">mean</th>` +
      `<th style="text-align:right;padding:4px 6px">score</th>` +
      `</tr></thead>` +
      `<tbody>${rows}</tbody>` +
      `</table>` +
      `</div>`
    );
  }

  async function loadAutoSelect() {
    const meta = $("#auto-select-meta");
    const root = $("#auto-select-rankings");
    if (!root) return;
    const threshold = setThreshold(getThreshold());
    try {
      const r = await fetch("/api/auto-select?min_samples=" + threshold, { cache: "no-store" });
      if (!r.ok) throw new Error("HTTP " + r.status);
      const data = await r.json();
      const groups = data.groups || [];
      const samples = data.samples || 0;
      const dropped = data.dropped_candidates || 0;
      const effective = data.min_samples != null ? data.min_samples : threshold;
      const lastRecord = formatLastRecord(data.last_record_ts);
      const cnt = $("#count-auto-select");
      if (cnt) cnt.textContent = groups.length;
      const label = $("#auto-select-threshold-label");
      if (label) label.textContent = String(effective);
      const parts = [
        `${samples} sample(s)`,
        `${groups.length} group(s)`,
        `${dropped} dropped (<${effective})`,
      ];
      if (lastRecord) parts.push(`last record ${lastRecord}`);
      meta.textContent = parts.join(" · ");
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
      meta.textContent = "load failed";
      root.innerHTML =
        `<div class="tl-empty">Failed to load: ${escape(String(err))}.</div>`;
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
