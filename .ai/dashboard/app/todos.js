(function () {
  "use strict";

  var _todosCache = [];
  var _todosLoadInFlight = false;
  var _todosLoadedOnce = false;
  var _todosOpenCount = null;

  function $todo(sel) {
    return document.querySelector(sel);
  }

  function todoText(v) {
    return String(v == null ? "" : v);
  }

  function todoId(todo, index) {
    return todoText(todo.id || todo.key || todo.path || todo.source || ("todo-" + index));
  }

  function todoTitle(todo) {
    return todoText(todo.title || todo.text || todo.summary || "(untitled)");
  }

  function todoSource(todo) {
    return todoText(todo.source || todo.file || todo.path || "");
  }

  function todoStatus(todo) {
    return todoText(todo.status || "open").toLowerCase();
  }

  function todoTags(todo) {
    var raw = todo.tags || todo.labels || [];
    if (typeof raw === "string") {
      raw = raw.split(",").map(function (x) { return x.trim(); });
    }
    if (!Array.isArray(raw)) return [];
    return raw.map(function (x) { return todoText(x).trim(); }).filter(Boolean);
  }

  function isOpenTodo(todo) {
    return todoStatus(todo) === "open";
  }

  function csrfToken() {
    return (document.body && document.body.dataset && document.body.dataset.csrf)
      || window._csrfToken
      || "";
  }

  function showTodosBanner(text) {
    var banner = $todo("#todos-banner");
    if (!banner) return;
    if (!text) {
      banner.hidden = true;
      banner.textContent = "";
      return;
    }
    banner.textContent = text;
    banner.hidden = false;
  }

  function setTodosCount(items) {
    var el = $todo("#count-todos");
    if (!el) return;
    el.textContent = _todosOpenCount != null ? _todosOpenCount : items.filter(isOpenTodo).length;
  }

  function normalizeTodosPayload(data) {
    if (Array.isArray(data)) return { todos: data, banner: "" };
    data = data || {};
    return {
      todos: Array.isArray(data.todos) ? data.todos : (Array.isArray(data.items) ? data.items : []),
      banner: todoText(data.banner || ""),
      open_count: data.open_count,
    };
  }

  function renderTitle(el, title) {
    if (!el) return;
    if (typeof DOMPurify !== "undefined" && typeof marked !== "undefined") {
      el.innerHTML = DOMPurify.sanitize(marked.parse(title || ""));
    } else {
      el.textContent = title || "(untitled)";
    }
  }

  function makeTextEl(tag, className, text) {
    var el = document.createElement(tag);
    if (className) el.className = className;
    el.textContent = text;
    return el;
  }

  function renderEmpty(message) {
    var list = $todo("#todos-list");
    if (!list) return;
    delete list.dataset.skeletoned;
    var empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = message || "No todos match the current filter.";
    list.replaceChildren(empty);
  }

  function syncTagFilter(items) {
    var select = $todo("#todos-filter-tag");
    if (!select) return;
    var current = select.value;
    var tags = [];
    items.forEach(function (todo) {
      todoTags(todo).forEach(function (tag) {
        if (!tags.includes(tag)) tags.push(tag);
      });
    });
    tags.sort(function (a, b) { return a.localeCompare(b); });
    select.replaceChildren(new Option("all tags", ""));
    tags.forEach(function (tag) {
      select.appendChild(new Option(tag, tag));
    });
    if (current && tags.includes(current)) select.value = current;
  }

  var ACTIVE_STATUSES = ["open", "resolved-suggested"];

  function filteredTodos() {
    var tag = ($todo("#todos-filter-tag") && $todo("#todos-filter-tag").value) || "";
    var status = ($todo("#todos-filter-status") && $todo("#todos-filter-status").value) || "";
    var query = (($todo("#todos-search") && $todo("#todos-search").value) || "").trim().toLowerCase();
    return _todosCache.filter(function (todo) {
      if (tag && !todoTags(todo).includes(tag)) return false;
      if (status === "active") {
        if (ACTIVE_STATUSES.indexOf(todoStatus(todo)) === -1) return false;
      } else if (status && todoStatus(todo) !== status) {
        return false;
      }
      if (query) {
        var hay = (todoTitle(todo) + " " + todoSource(todo)).toLowerCase();
        if (!hay.includes(query)) return false;
      }
      return true;
    });
  }

  function buildRowActions(status) {
    var actions = document.createElement("div");
    actions.className = "todo-row-actions";
    var pairs = [];
    if (status === "open") {
      pairs = [["done", "Done"], ["archive", "Archive"]];
    } else if (status === "resolved-suggested") {
      pairs = [["accept-suggest", "Accept"], ["reject-suggest", "Reject"]];
    } else if (status === "resolved" || status === "archived") {
      pairs = [["reopen", "Reopen"]];
    }
    pairs.forEach(function (pair) {
      var b = document.createElement("button");
      b.type = "button";
      b.className = "todo-action";
      b.dataset.action = pair[0];
      b.textContent = pair[1];
      actions.appendChild(b);
    });
    return actions;
  }

  function renderTodoRow(row, todo, index) {
    var status = todoStatus(todo);
    row.className = "todo-row" + (status === "resolved-suggested" ? " suggested" : "");
    row.dataset.id = todoId(todo, index);
    row.replaceChildren();

    var head = document.createElement("div");
    head.className = "todo-row-head";
    var title = document.createElement("div");
    title.className = "todo-title";
    renderTitle(title, todoTitle(todo));
    head.appendChild(title);
    head.appendChild(makeTextEl("span", "pill", status || "open"));
    row.appendChild(head);

    var metaParts = [];
    var source = todoSource(todo);
    if (source) metaParts.push(source);
    var tags = todoTags(todo);
    if (tags.length) metaParts.push(tags.map(function (tag) { return "#" + tag; }).join(" "));
    if (metaParts.length) row.appendChild(makeTextEl("div", "sub", metaParts.join(" · ")));

    row.appendChild(buildRowActions(status));
  }

  function renderTodos() {
    var list = $todo("#todos-list");
    if (!list) return;
    delete list.dataset.skeletoned;
    syncTagFilter(_todosCache);
    setTodosCount(_todosCache);

    var rows = filteredTodos();
    if (!rows.length) {
      renderEmpty(_todosCache.length ? "No todos match the current filters." : "No todos match the current filter.");
      return;
    }

    var existing = new Map();
    Array.from(list.querySelectorAll(".todo-row")).forEach(function (row) {
      if (row.dataset.id) existing.set(row.dataset.id, row);
    });
    var next = rows.map(function (todo, index) {
      var id = todoId(todo, index);
      var row = existing.get(id) || document.createElement("div");
      renderTodoRow(row, todo, index);
      return row;
    });
    list.replaceChildren.apply(list, next);
  }

  async function loadTodos() {
    var view = $todo("#view-todos");
    if (view) view.hidden = false;
    if (_todosLoadInFlight) return;
    _todosLoadInFlight = true;
    try {
      var r = await fetch("/api/todos", { credentials: "same-origin", cache: "no-store" });
      if (!r.ok) {
        _todosCache = [];
        _todosOpenCount = null;
        setTodosCount(_todosCache);
        showTodosBanner("");
        renderEmpty("No todos match the current filter.");
        return;
      }
      var data = normalizeTodosPayload(await r.json());
      _todosCache = data.todos;
      _todosOpenCount = typeof data.open_count === "number" ? data.open_count : null;
      showTodosBanner(data.banner === "TODO.md export stale" ? data.banner : "");
      renderTodos();
      _todosLoadedOnce = true;
    } catch (err) {
      _todosCache = [];
      _todosOpenCount = null;
      setTodosCount(_todosCache);
      showTodosBanner("");
      renderEmpty("No todos match the current filter.");
    } finally {
      _todosLoadInFlight = false;
    }
  }

  async function postTodos(path, body, channel) {
    var token = csrfToken();
    var headers = { "Content-Type": "application/json" };
    if (token) headers["X-CSRF-Token"] = token;
    try {
      var r = await fetch(path, {
        method: "POST",
        credentials: "same-origin",
        headers: headers,
        body: JSON.stringify(body || {}),
      });
      if (r.status === 404) {
        showTodosBanner("Endpoint not found: " + path + " (restart dashboard server?)");
        return;
      }
      if (!r.ok) throw new Error("HTTP " + r.status);
      await loadTodos();
    } catch (err) {
      if (typeof setMsg === "function") setMsg(channel || "#todos-banner", "err", "Todos action failed: " + err.message);
      else showTodosBanner("Todos action failed: " + err.message);
    }
  }

  function initTodos() {
    var view = $todo("#view-todos");
    if (!view || view.dataset.wired === "1") return;
    view.dataset.wired = "1";

    var nav = document.querySelector('nav button[data-view="todos"]');
    if (nav) {
      nav.addEventListener("click", function () {
        view.hidden = false;
        loadTodos();
      });
    }

    var toolbar = view.querySelector(".todos-toolbar");
    var addModal = $todo("#todos-add-modal");
    var addForm = $todo("#todos-add-form");
    var addTitle = $todo("#todos-add-title");
    var addTags = $todo("#todos-add-tags");
    var addMsg = $todo("#todos-add-msg");
    var previewModal = $todo("#todos-preview-modal");
    var previewDoc = $todo("#todos-preview-doc");
    var autoToggle = $todo("#todos-auto-toggle");

    function openModal(modal, focusEl) {
      if (!modal) return;
      modal.hidden = false;
      document.body.classList.add("modal-open");
      var target = focusEl || modal.querySelector(".proposal-modal-pane");
      if (target && target.focus) target.focus();
    }
    function closeModal(modal) {
      if (!modal) return;
      modal.hidden = true;
      if (!document.querySelector(".proposal-modal:not([hidden])")) {
        document.body.classList.remove("modal-open");
      }
    }

    function openAddModal() {
      if (addTitle) addTitle.value = "";
      if (addTags) addTags.value = "";
      if (addMsg) addMsg.textContent = "";
      openModal(addModal, addTitle);
    }
    function submitAdd(e) {
      if (e) e.preventDefault();
      var title = (addTitle && addTitle.value || "").trim();
      if (!title) {
        if (addMsg) addMsg.textContent = "Title is required.";
        if (addTitle) addTitle.focus();
        return;
      }
      var tags = (addTags && addTags.value || "").split(",").map(function (x) { return x.trim(); }).filter(Boolean);
      closeModal(addModal);
      postTodos("/api/todos", { title: title, tags: tags }, "#todos-banner");
    }

    function openPreviewModal() {
      openModal(previewModal);
      if (!previewDoc) return;
      previewDoc.replaceChildren();
      var empty = document.createElement("div");
      empty.className = "empty";
      empty.textContent = "Loading…";
      previewDoc.appendChild(empty);
      fetch("/.ai/TODO.md", { credentials: "same-origin", cache: "no-store" })
        .then(function (r) { return r.ok ? r.text() : ""; })
        .then(function (text) {
          if (!text) {
            previewDoc.replaceChildren();
            var none = document.createElement("div");
            none.className = "empty";
            none.textContent = "TODO.md not generated yet — run Scan now.";
            previewDoc.appendChild(none);
            return;
          }
          if (typeof renderMarkdown === "function") {
            renderMarkdown(previewDoc, text);
          } else if (typeof DOMPurify !== "undefined" && typeof marked !== "undefined") {
            previewDoc.innerHTML = DOMPurify.sanitize(marked.parse(text));
          } else {
            previewDoc.textContent = text;
          }
        })
        .catch(function () {
          previewDoc.replaceChildren();
          var err = document.createElement("div");
          err.className = "empty";
          err.textContent = "Failed to load TODO.md.";
          previewDoc.appendChild(err);
        });
    }

    if (toolbar) {
      toolbar.addEventListener("click", function (e) {
        var action = e.target && e.target.closest ? e.target.closest("[data-action]") : null;
        if (!action || !toolbar.contains(action)) return;
        var name = action.dataset.action;
        if (!["todo-add", "todo-scan", "todo-preview"].includes(name)) return;
        e.preventDefault();
        e.stopPropagation();
        if (name === "todo-add") openAddModal();
        if (name === "todo-scan") postTodos("/api/todos/scan", {}, "#todos-banner");
        if (name === "todo-preview") openPreviewModal();
      });
    }
    if (addForm) {
      addForm.addEventListener("submit", submitAdd);
      addForm.addEventListener("click", function (e) {
        var btn = e.target && e.target.closest ? e.target.closest("[data-action]") : null;
        if (!btn) return;
        if (btn.dataset.action === "todo-add-submit") submitAdd(e);
        if (btn.dataset.action === "todo-add-cancel") { e.preventDefault(); closeModal(addModal); }
      });
    }
    [addModal, previewModal].forEach(function (modal) {
      if (!modal) return;
      modal.addEventListener("click", function (e) {
        if (e.target === modal) closeModal(modal);
      });
      var closeBtn = modal.querySelector("header .refresh");
      if (closeBtn) closeBtn.addEventListener("click", function () { closeModal(modal); });
    });
    document.addEventListener("keydown", function (e) {
      if (e.key !== "Escape") return;
      if (addModal && !addModal.hidden) { closeModal(addModal); return; }
      if (previewModal && !previewModal.hidden) { closeModal(previewModal); return; }
    });

    if (autoToggle) {
      fetch("/api/todos/config", { credentials: "same-origin", cache: "no-store" })
        .then(function (r) { return r.ok ? r.json() : { auto_enabled: true }; })
        .then(function (cfg) { autoToggle.checked = cfg.auto_enabled !== false; })
        .catch(function () { autoToggle.checked = true; });
      autoToggle.addEventListener("change", function () {
        postTodos("/api/todos/config", { auto_enabled: autoToggle.checked }, "#todos-banner");
      });
    }

    var list = $todo("#todos-list");
    if (list) {
      list.addEventListener("click", function (e) {
        var btn = e.target && e.target.closest ? e.target.closest(".todo-action[data-action]") : null;
        if (!btn || !list.contains(btn)) return;
        var row = btn.closest(".todo-row");
        if (!row || !row.dataset.id) return;
        e.preventDefault();
        e.stopPropagation();
        postTodos(
          "/api/todos/" + encodeURIComponent(row.dataset.id) + "/status",
          { action: btn.dataset.action },
          "#todos-banner"
        );
      });
    }

    ["#todos-filter-tag", "#todos-filter-status", "#todos-search"].forEach(function (sel) {
      var el = $todo(sel);
      if (el) el.addEventListener(sel === "#todos-search" ? "input" : "change", renderTodos);
    });

    if (view.classList.contains("active") && !_todosLoadedOnce) loadTodos();
  }

  window.loadTodos = loadTodos;
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initTodos);
  } else {
    initTodos();
  }
})();
