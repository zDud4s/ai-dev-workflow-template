/* Council view — convene a roster of seats (raw models or agent personas),
   run the 3-stage flow (answer alone → blind critique → chair's verdict),
   stream it live, and browse the archive.

   Talks to /api/council/*. Tool identity drives colour: cyan=claude,
   magenta=codex, violet=agent. Model output is always rendered through
   marked.parse + DOMPurify.sanitize — never raw innerHTML. */
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
    var html = (window.marked && window.marked.parse) ? window.marked.parse(text || "") : (text || "");
    return window.DOMPurify ? window.DOMPurify.sanitize(html) : "";
  }
  function setHTML(node, text) { node.innerHTML = DOMPurify.sanitize(md(text)); }
  function showEmpty(node, text, cls) { node.replaceChildren(el("p", cls || "empty", text)); }

  // --- state ---------------------------------------------------------------
  var config = null;       // { default:{chairman,members}, catalog:{claude,codex}, agents:[] }
  var seats = [];          // working roster for the next sitting
  var chairman = null;     // working chair seat (a copy)
  var current = null;      // { id, es } for the active run
  var lastAnonMap = null;  // {label: seat_idx}
  var lastRecord = null;   // last full record (for re-render on reveal)
  var revealing = false;

  // Identity: which hue a seat carries. Agents are violet personas; raw models
  // take their tool's colour (claude=cyan, codex=magenta).
  function isCodex(modelId) {
    return !!(config && config.catalog && (config.catalog.codex || []).indexOf(modelId) !== -1);
  }
  function identOf(seat) {
    var kind = seat.type === "agent" ? "agent" : "model";
    var model = seat.type === "agent" ? seat.model : (seat.model || seat.ref);
    var tool = isCodex(model) ? "codex" : "claude";
    return { kind: kind, tool: tool };
  }
  function applyIdent(node, seat) {
    var id = identOf(seat);
    node.dataset.kind = id.kind;
    node.dataset.tool = id.tool;
  }
  function firstClaudeModel() { return (config.catalog.claude || [])[0]; }
  function modelOptions() {
    if (!config || !config.catalog) return [];
    return (config.catalog.claude || []).concat(config.catalog.codex || []);
  }
  function sameSeat(a, b) { return a && b && a.type === b.type && a.ref === b.ref && a.model === b.model; }
  function seatName(seat) { return seat.label || seat.ref; }

  // --- config + roster -----------------------------------------------------
  function loadConfig() {
    return fetch("/api/council/config").then(function (r) { return r.json(); }).then(function (cfg) {
      config = cfg;
      seats = (cfg.default && cfg.default.members || []).map(function (s) { return Object.assign({}, s); });
      chairman = cfg.default && cfg.default.chairman ? Object.assign({}, cfg.default.chairman) : (seats[0] && Object.assign({}, seats[0]));
      renderSeats();
    }).catch(function (err) {
      var msg = $("#council-msg");
      if (msg) msg.textContent = "Couldn't load the roster: " + err.message;
    });
  }

  function renderSeats() {
    var wrap = $("#council-seats");
    if (!wrap) return;
    wrap.replaceChildren();
    seats.forEach(function (seat, idx) { wrap.appendChild(seatTile(seat, idx)); });

    var add = el("button", "council-seat council-seat-add");
    add.type = "button";
    add.appendChild(el("span", "council-seat-dot"));
    add.appendChild(el("span", "council-seat-ref", "Add seat"));
    add.addEventListener("click", addSeat);
    wrap.appendChild(add);

    var hint = $("#council-roster-hint");
    if (hint) hint.textContent = seats.length + (seats.length === 1 ? " seat · chair " : " seats · chair ") + (chairman ? seatName(chairman) : "—");
  }

  function seatTile(seat, idx) {
    var tile = el("div", "council-seat");
    tile.setAttribute("role", "listitem");
    applyIdent(tile, seat);
    if (chairman && sameSeat(seat, chairman)) tile.classList.add("is-chair");

    tile.appendChild(el("span", "council-seat-dot"));

    // type selector (model | agent)
    var typeSel = el("select", "council-seat-field council-seat-kind");
    typeSel.setAttribute("aria-label", "Seat type");
    ["model", "agent"].forEach(function (t) {
      var o = el("option", null, t === "agent" ? "agent" : "model"); o.value = t;
      if (t === seat.type) o.selected = true; typeSel.appendChild(o);
    });
    typeSel.addEventListener("change", function () {
      seat.type = typeSel.value;
      if (seat.type === "agent") { seat.ref = (config.agents && config.agents[0]) || ""; seat.model = firstClaudeModel(); }
      else { seat.ref = firstClaudeModel(); delete seat.model; }
      renderSeats();
    });
    tile.appendChild(typeSel);

    // ref selector — agent slugs, or catalog models
    var refSel = el("select", "council-seat-field council-seat-ref");
    refSel.setAttribute("aria-label", "Seat " + (idx + 1) + " identity");
    var refs = seat.type === "agent" ? (config.agents || []) : modelOptions();
    refs.forEach(function (r) { var o = el("option", null, r); o.value = r; if (r === seat.ref) o.selected = true; refSel.appendChild(o); });
    refSel.addEventListener("change", function () { seat.ref = refSel.value; renderSeats(); });
    tile.appendChild(refSel);

    // agent seats pin a claude model (codex has no --agent)
    if (seat.type === "agent") {
      var on = el("span", "council-seat-model", "on");
      tile.appendChild(on);
      var modelSel = el("select", "council-seat-field council-seat-model");
      modelSel.setAttribute("aria-label", "Model running the persona");
      (config.catalog.claude || []).forEach(function (m) { var o = el("option", null, m); o.value = m; if (m === seat.model) o.selected = true; modelSel.appendChild(o); });
      modelSel.addEventListener("change", function () { seat.model = modelSel.value; renderSeats(); });
      tile.appendChild(modelSel);
    }

    if (chairman && sameSeat(seat, chairman)) {
      tile.appendChild(el("span", "council-seat-gavel", "chair"));
    } else {
      var chair = el("button", "council-seat-btn council-seat-chair", "⚑"); // pennant/flag
      chair.type = "button"; chair.title = "Make chair"; chair.setAttribute("aria-label", "Make " + seatName(seat) + " the chair");
      chair.addEventListener("click", function () { chairman = Object.assign({}, seat); renderSeats(); });
      tile.appendChild(chair);
    }

    var rm = el("button", "council-seat-btn council-seat-remove", "×");
    rm.type = "button"; rm.title = "Remove seat"; rm.setAttribute("aria-label", "Remove " + seatName(seat));
    rm.addEventListener("click", function () {
      var wasChair = chairman && sameSeat(seat, chairman);
      seats.splice(idx, 1);
      if (wasChair) chairman = seats[0] ? Object.assign({}, seats[0]) : null;
      renderSeats();
    });
    tile.appendChild(rm);

    return tile;
  }

  function addSeat() {
    seats.push({ type: "model", ref: firstClaudeModel() });
    if (!chairman) chairman = Object.assign({}, seats[0]);
    renderSeats();
  }

  // --- running -------------------------------------------------------------
  function runCouncil() {
    var q = ($("#council-question") && $("#council-question").value || "").trim();
    var msg = $("#council-msg");
    if (!q) { if (msg) msg.textContent = "Ask a question first."; return; }
    if (!seats.length) { if (msg) msg.textContent = "Add at least one seat."; return; }
    if (!chairman) chairman = Object.assign({}, seats[0]);
    if (msg) msg.textContent = "Convening…";
    resetStages();

    fetch("/api/council/runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question: q, seats: seats, chairman: chairman })
    }).then(function (r) {
      if (!r.ok) return r.json().then(function (e) { throw new Error(e.error || ("HTTP " + r.status)); });
      return r.json();
    }).then(function (res) {
      _lastRunId = res.id;
      if (msg) msg.textContent = "In session · " + res.id;
      openStream(res.id);
      toggleCancel(true);
      loadHistory();
    }).catch(function (err) { if (msg) msg.textContent = "Couldn't convene: " + err.message; });
  }

  function openStream(id) {
    closeStream();
    var es = new EventSource("/api/council/runs/" + encodeURIComponent(id) + "/stream");
    current = { id: id, es: es };
    es.onmessage = function (ev) { var d; try { d = JSON.parse(ev.data); } catch (e) { return; } handleEvent(d); };
    es.onerror = function () { closeStream(); };
  }
  function closeStream() { if (current && current.es) current.es.close(); current = null; }

  function handleEvent(ev) {
    if (ev.stage === "run") {
      var msg = $("#council-msg");
      if (msg) msg.textContent = "Council " + (ev.status === "done" ? "adjourned." : ev.status + ".");
      toggleCancel(false);
      closeStream();
      refreshRun(_lastRunId);
      loadHistory();
      return;
    }
    if (ev.stage === 1) renderStage1Event(ev);
    else if (ev.stage === 2) markCritiqueLive();
    else if (ev.stage === 3) renderVerdictEvent(ev);
  }

  // --- stage rendering -----------------------------------------------------
  function resetStages() {
    var s1 = $("#council-stage1"); if (s1) s1.replaceChildren();
    var s2 = $("#council-stage2"); if (s2) s2.replaceChildren();
    var reveal = $("#council-reveal"); if (reveal) reveal.hidden = true;
    lastAnonMap = null; lastRecord = null; revealing = false;

    var verdict = $(".council-verdict");
    if (verdict) { verdict.classList.remove("is-error"); verdict.classList.add("is-live"); }
    showEmpty($("#council-stage3"), "The chair is waiting for the table…", "council-idle");

    seats.forEach(function (seat, idx) {
      var col = el("div", "council-seat-col is-live");
      col.id = "council-seat-col-" + idx;
      col.setAttribute("role", "listitem");
      col.style.animationDelay = (idx * 60) + "ms";
      applyIdent(col, seat);
      var head = el("div", "council-seat-col-head");
      head.appendChild(el("span", "council-seat-dot"));
      head.appendChild(el("span", "council-seat-col-name", seatName(seat)));
      head.appendChild(el("span", "council-seat-col-status", "thinking"));
      col.appendChild(head);
      col.appendChild(el("div", "council-prose council-seat-col-body"));
      $("#council-stage1").appendChild(col);
    });
  }

  function renderStage1Event(ev) {
    var col = $("#council-seat-col-" + ev.seat_idx);
    if (!col) return;
    var status = col.querySelector(".council-seat-col-status");
    if (ev.status === "started") return;
    col.classList.remove("is-live");
    if (ev.status === "ok") {
      col.classList.add("is-done");
      if (status) status.textContent = "answered";
      if (ev.field === "response") setHTML(col.querySelector(".council-seat-col-body"), ev.value);
    } else {
      col.classList.add("is-error");
      if (status) status.textContent = ev.status === "timeout" ? "timed out" : "no answer";
    }
  }

  function markCritiqueLive() {
    var s2 = $("#council-stage2");
    if (s2 && !s2.dataset.live) { s2.dataset.live = "1"; showEmpty(s2, "Seats are ranking each other…"); }
  }

  function renderVerdictEvent(ev) {
    var verdict = $(".council-verdict");
    if (ev.status === "ok" && ev.field === "response") {
      if (verdict) verdict.classList.remove("is-live");
      setHTML($("#council-stage3"), ev.value);
    } else if (ev.status === "error") {
      if (verdict) { verdict.classList.remove("is-live"); verdict.classList.add("is-error"); }
      showEmpty($("#council-stage3"), "The chair could not reach a verdict.", "council-idle");
    }
  }

  function refreshRun(id) {
    if (!id) return;
    fetch("/api/council/runs/" + encodeURIComponent(id)).then(function (r) { return r.json(); }).then(renderRecord).catch(function () {});
  }

  function renderRecord(rec) {
    if (!rec) return;
    lastRecord = rec;
    _lastRunId = rec.id;
    lastAnonMap = rec.anon_map || null;

    var verdict = $(".council-verdict");
    if (verdict) { verdict.classList.remove("is-live"); verdict.classList.toggle("is-error", rec.status === "error"); }
    if (rec.stage3 && rec.stage3.response) setHTML($("#council-stage3"), rec.stage3.response);
    else if (rec.status !== "running") showEmpty($("#council-stage3"), "No verdict was recorded.", "council-idle");

    renderResponses(rec);
    renderLeaderboard(rec);
    var reveal = $("#council-reveal");
    if (reveal) reveal.hidden = !(lastAnonMap && Object.keys(lastAnonMap).length);
  }

  // Re-render stage 1 from a persisted record (history replay).
  function renderResponses(rec) {
    var s1 = $("#council-stage1");
    if (!s1 || !rec.stage1) return;
    s1.replaceChildren();
    (rec.seats || []).forEach(function (seat, idx) {
      var entry = rec.stage1.filter(function (e) { return e.seat_idx === idx; })[0];
      var col = el("div", "council-seat-col");
      col.setAttribute("role", "listitem");
      applyIdent(col, seat);
      var head = el("div", "council-seat-col-head");
      head.appendChild(el("span", "council-seat-dot"));
      head.appendChild(el("span", "council-seat-col-name", seatName(seat)));
      var st = el("span", "council-seat-col-status");
      var ok = entry && entry.status === "ok";
      col.classList.add(ok ? "is-done" : "is-error");
      st.textContent = ok ? "answered" : (entry && entry.status === "timeout" ? "timed out" : "no answer");
      head.appendChild(st);
      col.appendChild(head);
      var body = el("div", "council-prose council-seat-col-body");
      if (ok && entry.response) body.innerHTML = DOMPurify.sanitize(md(entry.response));
      col.appendChild(body);
      s1.appendChild(col);
    });
  }

  function renderLeaderboard(rec) {
    var s2 = $("#council-stage2");
    if (!s2) return;
    delete s2.dataset.live;
    var board = (rec.leaderboard || []).slice();
    if (!board.length) { showEmpty(s2, "Blind critique needs at least two answers — skipped this sitting."); return; }
    s2.replaceChildren();
    var worst = board.reduce(function (m, r) { return Math.max(m, r.avg_rank || 1); }, 1);
    var best = board.reduce(function (m, r) { return Math.min(m, r.avg_rank || 1); }, worst);
    var span = Math.max(0.0001, worst - best);
    board.forEach(function (row, i) {
      var seat = (rec.seats || [])[row.seat_idx] || {};
      var r = el("div", "council-rank" + (i === 0 ? " is-top" : ""));
      // Keep critique blind: only colour by tool identity once names are revealed.
      if (revealing) applyIdent(r, seat);
      r.appendChild(el("span", "council-rank-num", String(i + 1)));

      var main = el("div", "council-rank-main");
      var name = revealing ? seatName(seat) : ("Response " + anonOf(row.seat_idx));
      main.appendChild(el("span", "council-rank-name", name));
      var bar = el("div", "council-rank-bar");
      var fill = el("span", "council-rank-bar-fill");
      // Fuller bar = better (lower) average rank.
      var pct = Math.round((1 - ((row.avg_rank - best) / span)) * 88) + 12;
      fill.style.setProperty("--bar", pct + "%");
      bar.appendChild(fill);
      main.appendChild(bar);
      r.appendChild(main);

      var meta = el("span", "council-rank-meta");
      meta.appendChild(el("b", null, (row.avg_rank != null ? row.avg_rank.toFixed(2) : "—")));
      meta.appendChild(document.createTextNode(" avg · " + (row.n || 0) + " votes"));
      r.appendChild(meta);
      s2.appendChild(r);
    });
  }

  function anonOf(seatIdx) {
    if (!lastAnonMap) return "?";
    var found = "?";
    Object.keys(lastAnonMap).forEach(function (lbl) { if (lastAnonMap[lbl] === seatIdx) found = lbl; });
    return found;
  }

  function toggleCancel(on) {
    var c = $("#council-cancel"); if (c) c.hidden = !on;
    var r = $("#council-run"); if (r) { r.disabled = !!on; r.textContent = on ? "In session…" : "Convene"; }
  }
  function cancelRun() {
    if (!_lastRunId) return;
    fetch("/api/council/runs/" + encodeURIComponent(_lastRunId) + "/cancel", { method: "POST" })
      .then(function () { toggleCancel(false); closeStream(); var m = $("#council-msg"); if (m) m.textContent = "Council dismissed."; });
  }

  var _lastRunId = null;

  // --- archive -------------------------------------------------------------
  function loadHistory() {
    return fetch("/api/council/runs").then(function (r) { return r.json(); }).then(function (data) {
      var rows = (data && data.runs) || (Array.isArray(data) ? data : []);
      var wrap = $("#council-history");
      var count = $("#count-council-history");
      if (count) count.textContent = String(rows.length);
      if (!wrap) return;
      wrap.replaceChildren();
      if (!rows || !rows.length) { showEmpty(wrap, "No councils have sat yet."); return; }
      rows.forEach(function (row) {
        var item = el("div", "council-history-item");
        item.setAttribute("role", "listitem"); item.tabIndex = 0;
        item.appendChild(el("span", "council-history-q", (row.question || "(no question)")));
        var meta = el("span", "council-history-meta", (row.status || "?"));
        meta.dataset.status = row.status || "";
        item.appendChild(meta);
        function open() { _lastRunId = row.id; refreshRun(row.id); var fl = $("#council-floor"); if (fl && fl.scrollIntoView) fl.scrollIntoView({ behavior: "smooth", block: "start" }); }
        item.addEventListener("click", open);
        item.addEventListener("keydown", function (e) { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); } });
        wrap.appendChild(item);
      });
    }).catch(function () {});
  }

  // --- init ----------------------------------------------------------------
  function loadCouncil() {
    if (!config) loadConfig().then(loadHistory); else loadHistory();
  }

  function initCouncil() {
    var view = $("#view-council");
    if (!view || view.dataset.wired === "1") return;
    view.dataset.wired = "1";

    var nav = document.querySelector('nav button[data-view="council"]');
    if (nav) nav.addEventListener("click", function () { view.hidden = false; loadCouncil(); });

    var run = $("#council-run"); if (run) run.addEventListener("click", runCouncil);
    var cancel = $("#council-cancel"); if (cancel) cancel.addEventListener("click", cancelRun);
    var q = $("#council-question");
    if (q) q.addEventListener("keydown", function (e) { if ((e.metaKey || e.ctrlKey) && e.key === "Enter") runCouncil(); });
    var reveal = $("#council-reveal");
    if (reveal) reveal.addEventListener("click", function () {
      revealing = !revealing;
      reveal.textContent = revealing ? "Hide names" : "Reveal names";
      reveal.setAttribute("aria-pressed", revealing ? "true" : "false");
      if (lastRecord) renderLeaderboard(lastRecord);
    });
  }

  window.loadCouncil = loadCouncil;
  window.initCouncil = initCouncil;
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", initCouncil);
  else initCouncil();
})();
