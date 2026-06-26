/* Council view — ask one question to N seats (models or agent personas),
   stream the 3-stage karpathy flow (individual → anonymized peer-review →
   chairman), and browse run history.

   Talks to the /api/council/* endpoints (see serve.py). Model output is always
   rendered through marked.parse + DOMPurify.sanitize — never raw innerHTML. */
(function () {
  "use strict";

  function $(sel, root) { return (root || document).querySelector(sel); }
  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }
  function md(text) {
    // marked + DOMPurify are loaded globally in index.html.
    var html = (window.marked && window.marked.parse) ? window.marked.parse(text || "") : (text || "");
    return window.DOMPurify ? window.DOMPurify.sanitize(html) : "";
  }
  function setHTML(node, text) { node.innerHTML = DOMPurify.sanitize(md(text)); }
  function emptyNode(text) { return el("div", "empty", text); }
  function showEmpty(node, text) { node.replaceChildren(emptyNode(text)); }

  // --- state ---------------------------------------------------------------
  var config = null;         // { default:{chairman,members}, catalog:{claude,codex}, agents:[] }
  var seats = [];            // working copy of seats for the next run
  var chairman = null;       // working chairman seat
  var current = null;        // { id, es } for the active run
  var lastAnonMap = null;    // {label: seat_idx} for "reveal identities"

  function seatLabel(seat) {
    return seat.label || seat.ref;
  }
  function isCodexModel(modelId) {
    return !!(config && config.catalog && (config.catalog.codex || []).indexOf(modelId) !== -1);
  }

  // --- config + seat editor ------------------------------------------------
  function loadConfig() {
    return fetch("/api/council/config").then(function (r) { return r.json(); }).then(function (cfg) {
      config = cfg;
      seats = (cfg.default && cfg.default.members || []).map(function (s) { return Object.assign({}, s); });
      chairman = cfg.default && cfg.default.chairman ? Object.assign({}, cfg.default.chairman) : null;
      renderSeats();
      renderChairman();
    }).catch(function (err) {
      var msg = $("#council-msg");
      if (msg) msg.textContent = "Failed to load council config: " + err.message;
    });
  }

  function modelOptions() {
    if (!config || !config.catalog) return [];
    return (config.catalog.claude || []).concat(config.catalog.codex || []);
  }

  function renderSeats() {
    var wrap = $("#council-seats");
    if (!wrap) return;
    wrap.replaceChildren();
    seats.forEach(function (seat, idx) {
      var chip = el("div", "council-seat" + (chairman && sameSeat(seat, chairman) ? " is-chairman" : ""));
      chip.appendChild(el("span", "council-seat-kind", seat.type));

      // type toggle (model | agent)
      var typeSel = el("select");
      ["model", "agent"].forEach(function (t) {
        var o = el("option", null, t); o.value = t; if (t === seat.type) o.selected = true;
        typeSel.appendChild(o);
      });
      typeSel.addEventListener("change", function () {
        seat.type = typeSel.value;
        if (seat.type === "agent") {
          seat.ref = (config.agents && config.agents[0]) || "";
          // agents only run on claude models
          seat.model = firstClaudeModel();
        } else {
          seat.ref = firstClaudeModel();
          delete seat.model;
        }
        renderSeats();
      });
      chip.appendChild(typeSel);

      // ref picker — models from catalog, or agent slugs
      var refSel = el("select");
      var refs = seat.type === "agent" ? (config.agents || []) : modelOptions();
      refs.forEach(function (r) {
        var o = el("option", null, r); o.value = r; if (r === seat.ref) o.selected = true;
        refSel.appendChild(o);
      });
      refSel.addEventListener("change", function () { seat.ref = refSel.value; });
      chip.appendChild(refSel);

      // agent seats need a model pin — claude models only (codex has no --agent)
      if (seat.type === "agent") {
        var modelSel = el("select");
        (config.catalog.claude || []).forEach(function (m) {
          var o = el("option", null, m); o.value = m; if (m === seat.model) o.selected = true;
          modelSel.appendChild(o);
        });
        modelSel.addEventListener("change", function () { seat.model = modelSel.value; });
        chip.appendChild(modelSel);
      }

      var star = el("button", "council-seat-star", "★");
      star.title = "Make chairman";
      star.addEventListener("click", function () { chairman = Object.assign({}, seat); renderSeats(); renderChairman(); });
      chip.appendChild(star);

      var rm = el("button", "council-seat-remove", "✕");
      rm.title = "Remove seat";
      rm.addEventListener("click", function () { seats.splice(idx, 1); renderSeats(); });
      chip.appendChild(rm);

      wrap.appendChild(chip);
    });
  }

  function renderChairman() {
    var sel = $("#council-chairman");
    if (!sel) return;
    sel.replaceChildren();
    seats.forEach(function (seat, idx) {
      var o = el("option", null, seatLabel(seat) + " (" + seat.ref + ")");
      o.value = String(idx);
      if (chairman && sameSeat(seat, chairman)) o.selected = true;
      sel.appendChild(o);
    });
    sel.onchange = function () { chairman = Object.assign({}, seats[Number(sel.value)] || seats[0]); renderSeats(); };
  }

  function sameSeat(a, b) { return a && b && a.type === b.type && a.ref === b.ref && a.model === b.model; }
  function firstClaudeModel() { return (config.catalog.claude || [])[0]; }
  function addSeat() {
    seats.push({ type: "model", ref: firstClaudeModel() });
    renderSeats();
    renderChairman();
  }

  // --- running -------------------------------------------------------------
  function runCouncil() {
    var q = ($("#council-question") && $("#council-question").value || "").trim();
    var msg = $("#council-msg");
    if (!q) { if (msg) msg.textContent = "Question is required."; return; }
    if (!seats.length) { if (msg) msg.textContent = "Add at least one seat."; return; }
    if (!chairman) chairman = Object.assign({}, seats[0]);
    if (msg) msg.textContent = "Starting…";
    resetStages();

    fetch("/api/council/runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question: q, seats: seats, chairman: chairman })
    }).then(function (r) {
      if (!r.ok) return r.json().then(function (e) { throw new Error(e.error || ("HTTP " + r.status)); });
      return r.json();
    }).then(function (res) {
      if (msg) msg.textContent = "Running (" + res.id + ")…";
      openStream(res.id);
      toggleCancel(true);
      loadHistory();
    }).catch(function (err) { if (msg) msg.textContent = "Failed: " + err.message; });
  }

  function openStream(id) {
    closeStream();
    var es = new EventSource("/api/council/runs/" + encodeURIComponent(id) + "/stream");
    current = { id: id, es: es };
    es.onmessage = function (ev) {
      var data; try { data = JSON.parse(ev.data); } catch (e) { return; }
      handleEvent(data);
    };
    es.onerror = function () { /* server closes the stream on terminal event */ closeStream(); };
  }
  function closeStream() { if (current && current.es) { current.es.close(); } current = null; }

  function handleEvent(ev) {
    if (ev.stage === "run") {
      var msg = $("#council-msg");
      if (msg) msg.textContent = "Run " + ev.status + ".";
      toggleCancel(false);
      closeStream();
      // pull the final record for the leaderboard + anon map
      if (current === null && ev.status) refreshRun(lastRunId());
      return;
    }
    if (ev.stage === 1) { renderStage1Event(ev); }
    else if (ev.stage === 2) { renderStage2Event(ev); }
    else if (ev.stage === 3) { renderStage3Event(ev); }
  }

  // --- stage rendering -----------------------------------------------------
  function resetStages() {
    $("#council-stage1").replaceChildren();
    $("#council-stage2").replaceChildren();
    showEmpty($("#council-stage3"), "Synthesizing…");
    var reveal = $("#council-reveal"); if (reveal) reveal.hidden = true;
    lastAnonMap = null;
    seats.forEach(function (seat, idx) {
      var col = el("div", "council-seat-col");
      col.id = "council-seat-col-" + idx;
      var head = el("div", "council-seat-col-head");
      head.appendChild(el("span", null, seatLabel(seat)));
      head.appendChild(el("span", "council-seat-col-status", "…"));
      col.appendChild(head);
      col.appendChild(el("div", "council-seat-col-body"));
      $("#council-stage1").appendChild(col);
    });
  }

  function renderStage1Event(ev) {
    var col = $("#council-seat-col-" + ev.seat_idx);
    if (!col) return;
    if (ev.status === "error") { col.classList.add("is-error"); col.querySelector(".council-seat-col-status").textContent = "error"; return; }
    if (ev.field === "response") {
      col.querySelector(".council-seat-col-status").textContent = "done";
      setHTML(col.querySelector(".council-seat-col-body"), ev.value);
    }
  }

  function renderStage2Event(ev) {
    // Stage 2 events carry per-seat rankings; we render the aggregate from the
    // final record (refreshRun). Here we just note progress.
    var s2 = $("#council-stage2");
    if (ev.status === "started" && !s2.querySelector(".council-stage2-progress")) {
      s2.appendChild(el("div", "council-stage2-progress", "Peer review in progress…"));
    }
  }

  function renderStage3Event(ev) {
    if (ev.field === "response") { setHTML($("#council-stage3"), ev.value); }
    else if (ev.status === "error") { showEmpty($("#council-stage3"), "Chairman failed."); }
  }

  function refreshRun(id) {
    if (!id) return;
    fetch("/api/council/runs/" + encodeURIComponent(id)).then(function (r) { return r.json(); }).then(renderRecord);
  }

  function renderRecord(rec) {
    if (!rec) return;
    lastRunId(rec.id);
    // Stage 3
    if (rec.stage3 && rec.stage3.response) setHTML($("#council-stage3"), rec.stage3.response);
    // Stage 2 leaderboard
    lastAnonMap = rec.anon_map || null;
    renderLeaderboard(rec);
    var reveal = $("#council-reveal");
    if (reveal) reveal.hidden = !lastAnonMap;
  }

  function renderLeaderboard(rec) {
    var s2 = $("#council-stage2");
    s2.replaceChildren();
    var board = computeBoard(rec);
    if (!board.length) { s2.appendChild(el("div", "empty", "Peer review skipped (need ≥2 responses).")); return; }
    var table = el("table", "council-leaderboard");
    var thead = el("tr");
    ["#", "Seat", "Avg rank", "n"].forEach(function (h) { thead.appendChild(el("th", null, h)); });
    table.appendChild(thead);
    board.forEach(function (row, i) {
      var tr = el("tr");
      tr.appendChild(el("td", null, String(i + 1)));
      var seat = (rec.seats || [])[row.seat_idx] || {};
      var name = revealing() ? (seat.label || seat.ref || ("seat " + row.seat_idx)) : ("Response " + anonOf(row.seat_idx));
      tr.appendChild(el("td", null, name));
      tr.appendChild(el("td", null, row.avg_rank != null ? row.avg_rank.toFixed(2) : "—"));
      tr.appendChild(el("td", null, String(row.n != null ? row.n : "")));
      table.appendChild(tr);
    });
    s2.appendChild(table);
  }

  function computeBoard(rec) {
    // Prefer a server-provided board; else derive from stage2 rankings.
    if (rec.leaderboard) return rec.leaderboard;
    return [];
  }
  function anonOf(seatIdx) {
    if (!lastAnonMap) return "?";
    var found = "?";
    Object.keys(lastAnonMap).forEach(function (lbl) { if (lastAnonMap[lbl] === seatIdx) found = lbl; });
    return found;
  }
  var _revealing = false;
  function revealing() { return _revealing; }

  function toggleCancel(on) {
    var c = $("#council-cancel"); if (c) c.hidden = !on;
    var r = $("#council-run"); if (r) r.disabled = !!on;
  }
  function cancelRun() {
    var id = lastRunId();
    if (!id) return;
    fetch("/api/council/runs/" + encodeURIComponent(id) + "/cancel", { method: "POST" })
      .then(function () { toggleCancel(false); closeStream(); });
  }

  var _lastRunId = null;
  function lastRunId(set) { if (set !== undefined) _lastRunId = set; return _lastRunId; }

  // --- history -------------------------------------------------------------
  function loadHistory() {
    return fetch("/api/council/runs").then(function (r) { return r.json(); }).then(function (rows) {
      var wrap = $("#council-history");
      var count = $("#count-council-history");
      if (count) count.textContent = String((rows || []).length);
      if (!wrap) return;
      wrap.replaceChildren();
      if (!rows || !rows.length) { wrap.appendChild(el("div", "empty", "No past councils.")); return; }
      rows.forEach(function (row) {
        var item = el("div", "council-history-item");
        item.appendChild(el("span", "council-history-q", (row.question || "").slice(0, 80)));
        item.appendChild(el("span", "council-history-meta", row.status + " · " + (row.created || "")));
        item.addEventListener("click", function () { lastRunId(row.id); resetStages(); refreshRun(row.id); });
        wrap.appendChild(item);
      });
    }).catch(function () {});
  }

  // --- init ----------------------------------------------------------------
  function loadCouncil() {
    if (!config) { loadConfig().then(loadHistory); } else { loadHistory(); }
  }

  function initCouncil() {
    var view = $("#view-council");
    if (!view || view.dataset.wired === "1") return;
    view.dataset.wired = "1";

    var nav = document.querySelector('nav button[data-view="council"]');
    if (nav) nav.addEventListener("click", function () { view.hidden = false; loadCouncil(); });

    var add = $("#council-add-seat"); if (add) add.addEventListener("click", addSeat);
    var run = $("#council-run"); if (run) run.addEventListener("click", runCouncil);
    var cancel = $("#council-cancel"); if (cancel) cancel.addEventListener("click", cancelRun);
    var reveal = $("#council-reveal");
    if (reveal) reveal.addEventListener("click", function () {
      _revealing = !_revealing;
      reveal.textContent = _revealing ? "hide identities" : "reveal identities";
      refreshRun(lastRunId());
    });
  }

  window.loadCouncil = loadCouncil;
  window.initCouncil = initCouncil;
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initCouncil);
  } else {
    initCouncil();
  }
})();
