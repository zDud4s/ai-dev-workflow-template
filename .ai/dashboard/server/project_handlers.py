"""Request handlers for project-state endpoints.

Covers the TODO ledger (/api/todos*, /api/list), memory/decisions appends
(/api/memory, /api/decisions) and the events ledger (/api/events*). Extracted
from serve.py as the ``ProjectStateRoutes`` mixin; ``Handler`` inherits it, so
routing and ``serve.Handler._handle_memory`` resolve via MRO. Helpers the
methods close over are imported from their owning modules (importing them from
serve would be circular). ``todos_parser`` lives in the sibling ``scripts/``
folder, which serve and conftest put on ``sys.path`` before importing this.
"""
from __future__ import annotations

import datetime as _dt
import re

import todos_parser as _todos_parser
from server.paths import EVENTS_FILE, ROOT
from server.storage import _load_jsonl_cached, _write_text_lf


class ProjectStateRoutes:
    """TODO / memory / decisions / events endpoints, mixed into ``Handler``."""

    def _todos_latest(self) -> dict:
        rows = _todos_parser._load_jsonl(_todos_parser._todos_path(ROOT))
        return _todos_parser._fold_latest(rows)

    def _todos_banner(self) -> str | None:
        path = ROOT / ".ai" / "todos-banner.txt"
        try:
            if path.is_file():
                return path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return "TODO banner unavailable"
        return None

    def _clean_todo_tags(self, raw) -> list[str]:
        if not isinstance(raw, list):
            return []
        out: list[str] = []
        seen: set[str] = set()
        for value in raw:
            tag = re.sub(r"[^a-z0-9_-]+", "", str(value).strip().lower())[:30]
            if tag and tag not in seen:
                seen.add(tag)
                out.append(tag)
        return out

    def _handle_todos_list(self, qs: dict[str, list[str]]) -> None:
        latest = list(self._todos_latest().values())
        counts = {"open": 0, "resolved-suggested": 0, "resolved": 0, "archived": 0}
        for todo in latest:
            status = todo.get("status")
            if status in counts:
                counts[status] += 1

        status_filter = (qs.get("status") or [""])[0]
        tag_filter = (qs.get("tag") or [""])[0]

        def matches(todo: dict) -> bool:
            if status_filter and todo.get("status") != status_filter:
                return False
            if tag_filter:
                tags = todo.get("tags") or []
                if not isinstance(tags, list) or tag_filter not in {str(t) for t in tags}:
                    return False
            return True

        todos = sorted((dict(todo) for todo in latest if matches(todo)), key=lambda row: row.get("id", ""))
        self._json(200, {"todos": todos, "counts": counts, "banner": self._todos_banner()})

    def _handle_list(self, qs: dict[str, list[str]]) -> None:
        rel = (qs.get("path", [""])[0] or "").lstrip("/").replace("\\", "/")
        target = (ROOT / rel).resolve()
        # Compare against ``ROOT.resolve()``: a bare ``ROOT`` is unresolved, so
        # a symlink/junction *inside* the repo pointing outside would slip past
        # because ``target`` is followed through the symlink while ``ROOT`` is
        # not. The other path-checking sites in this file already use the
        # resolved form (see ``_handle_file_read``); this is the last holdout.
        try:
            target.relative_to(ROOT.resolve())
        except ValueError:
            self._json(403, {"error": "path outside repo root"})
            return
        if not target.is_dir():
            self._json(404, {"error": "not a directory", "path": rel})
            return
        entries = sorted(p.name for p in target.iterdir() if not p.name.startswith("."))
        self._json(200, {"path": rel, "entries": entries})

    # ----- POST handlers -----
    def _handle_todo_create(self, body: dict) -> None:
        if not isinstance(body, dict):
            self._json(400, {"error": "invalid request body"})
            return
        title = " ".join(str(body.get("title") or "").split())
        if not title or len(title) > 280:
            self._json(400, {"error": "title must be 1-280 characters"})
            return

        # Optional free-form detail. Collapse trailing whitespace but preserve
        # internal newlines so multi-line notes survive the round trip; the
        # frontend renders it as plain text (textContent), so no markup escaping
        # is needed here.
        description = str(body.get("description") or "").strip()
        if len(description) > 2000:
            self._json(400, {"error": "description must be 2000 characters or fewer"})
            return

        now = _todos_parser._utc_now()
        latest = self._todos_latest()
        source_ref = " ".join(str(body.get("source_ref") or "manual").split()) or "manual"
        todo = {
            "id": _todos_parser._allocate_id(latest, now),
            "title": title,
            "description": description,
            "tags": self._clean_todo_tags(body.get("tags") or []),
            "source": source_ref,
            "source_ref": source_ref,
            "status": "open",
            "created_at": now,
            "updated_at": now,
            "captured_by": "manual",
            "dedup_hash": _todos_parser._dedup_hash(source_ref, title),
            "resolution": None,
            "rejected_hashes": [],
        }
        _todos_parser._append_jsonl(_todos_parser._todos_path(ROOT), todo)
        regen = _todos_parser.regen_markdown(ROOT)
        payload = {"id": todo["id"], "todo": todo}
        if not regen.get("ok", False):
            payload["banner"] = regen.get("banner", "TODO.md export stale")
        self._json(201, payload)

    def _handle_todo_status(self, path: str, body: dict) -> None:
        if not isinstance(body, dict):
            self._json(400, {"error": "invalid request body"})
            return
        todo_id = path[len("/api/todos/"):-len("/status")].strip("/")
        if not re.fullmatch(r"td_\d{4}-\d{2}-\d{2}_\d{3}", todo_id):
            self._json(400, {"error": "invalid todo id"})
            return
        action = body.get("action")
        if action not in {"done", "archive", "reopen", "accept-suggest", "reject-suggest"}:
            self._json(400, {"error": "invalid action"})
            return

        current = self._todos_latest().get(todo_id)
        if current is None:
            self._json(404, {"error": "todo not found"})
            return

        now = _todos_parser._utc_now()
        todo = dict(current)
        todo["updated_at"] = now
        if action == "done":
            todo["status"] = "resolved"
            todo["resolution"] = {"by": "manual", "at": now}
        elif action == "archive":
            todo["status"] = "archived"
        elif action == "reopen":
            todo["status"] = "open"
            todo["resolution"] = None
        elif action == "accept-suggest":
            evidence = (current.get("resolution") or {}).get("evidence")
            todo["status"] = "resolved"
            todo["resolution"] = {"by": "manual-accept", "at": now}
            if evidence:
                todo["resolution"]["evidence"] = evidence
        elif action == "reject-suggest":
            evidence = (current.get("resolution") or {}).get("evidence")
            rejected = list(current.get("rejected_hashes") or [])
            if evidence and evidence not in rejected:
                rejected.append(evidence)
            todo["status"] = "open"
            todo["resolution"] = None
            todo["rejected_hashes"] = rejected

        _todos_parser._append_jsonl(_todos_parser._todos_path(ROOT), todo)
        regen = _todos_parser.regen_markdown(ROOT)
        payload = {"todo": todo}
        if not regen.get("ok", False):
            payload["banner"] = regen.get("banner", "TODO.md export stale")
        self._json(200, payload)

    def _handle_todos_scan(self) -> None:
        scan = _todos_parser.scan_and_append(ROOT, captured_by="scan-now")
        resolved = _todos_parser.auto_resolve(ROOT)
        self._json(200, {
            "added": int(scan.get("added", 0)),
            "suggested": int(resolved.get("suggested", 0)),
        })

    def _handle_memory(self, body: dict) -> None:
        topic = (body.get("topic") or "").strip()
        fact = (body.get("fact") or "").strip()
        if not topic or not fact:
            self._json(400, {"error": "topic and fact are required"})
            return
        if not re.fullmatch(r"[a-z0-9_-]{1,32}", topic):
            self._json(400, {"error": "topic must be lowercase letters, digits, '-' or '_' (max 32)"})
            return
        if len(fact) > 500:
            self._json(400, {"error": "fact must be 500 chars or fewer"})
            return
        # Single-line fact: collapse whitespace
        fact_single = " ".join(fact.split())
        date = _dt.date.today().strftime("%Y-%m-%d")
        line = f"- {date} [{topic}] {fact_single}\n"
        path = ROOT / ".ai" / "memory.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            # ``errors="replace"`` so a memory.md that picked up non-UTF-8
            # bytes (hand-edited in a non-UTF-8 editor, e.g.) doesn't 500
            # the append endpoint. The replacement char is benign in
            # markdown and the next manual edit will normalise it.
            existing = path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            existing = ""
        if existing and not existing.endswith("\n"):
            existing += "\n"
        _write_text_lf(path, existing + line)
        self._json(200, {"ok": True, "line": line.rstrip()})

    def _handle_decisions(self, body: dict) -> None:
        date = (body.get("date") or _dt.date.today().strftime("%Y-%m-%d")).strip()
        decision = (body.get("decision") or "").strip()
        why = (body.get("why") or "").strip()
        consequence = (body.get("consequence") or "").strip()
        revisit = (body.get("revisit") or "").strip()
        if not decision or not why:
            self._json(400, {"error": "decision and why are required"})
            return
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
            self._json(400, {"error": "date must be YYYY-MM-DD"})
            return
        for label, val in [("decision", decision), ("why", why), ("consequence", consequence), ("revisit", revisit)]:
            if len(val) > 1000:
                self._json(400, {"error": f"{label} must be 1000 chars or fewer"})
                return
        entry = (
            f"\n## {date} — {decision.splitlines()[0]}\n"
            f"- Date: {date}\n"
            f"- Decision: {decision}\n"
            f"- Why: {why}\n"
            f"- Consequence: {consequence or '—'}\n"
            f"- Revisit conditions: {revisit or '—'}\n"
        )
        path = ROOT / ".ai" / "decisions.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            # ``errors="replace"`` so a decisions.md that picked up non-UTF-8
            # bytes (hand-edited in a non-UTF-8 editor, e.g.) doesn't 500
            # the append endpoint. Matches the memory.md path above.
            existing = path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            existing = ""
        if existing and not existing.endswith("\n"):
            existing += "\n"
        _write_text_lf(path, existing + entry)
        self._json(200, {"ok": True, "entry": entry})

    def _handle_events_list(self, qs: dict[str, list[str]]) -> None:
        """GET /api/events?tail=N — parsed events.jsonl with optional tail.

        Replaces the previous client-side approach of fetching the raw
        .ai/ledgers/events.jsonl static file and re-parsing every line each poll.
        With ``tail=N`` (default 2000, max 5000) only the most recent N
        rows are returned, so a 100k-event ledger no longer triggers a
        multi-second freeze on every 5s refresh.
        """
        try:
            tail = int((qs.get("tail") or ["2000"])[0])
        except (TypeError, ValueError):
            tail = 2000
        tail = max(1, min(5000, tail))
        rows = _load_jsonl_cached(EVENTS_FILE)
        total = len(rows)
        truncated = total > tail
        if truncated:
            rows = rows[-tail:]
        self._json(200, {
            "events": rows,
            "total": total,
            "returned": len(rows),
            "truncated": truncated,
        })

    def _handle_events_clear(self) -> None:
        path = EVENTS_FILE
        # Audit-log the truncation BEFORE doing it. /api/events/clear is a
        # CSRF-gated POST but it's still an audit-erasing primitive — record
        # who/when so a future investigator can see when the ledger was wiped.
        try:
            size = path.stat().st_size if path.exists() else 0
        except OSError:
            size = -1
        print(
            f"[serve] AUDIT: events.jsonl cleared "
            f"(prior_size={size} bytes, client={self.client_address[0]})",
            flush=True,
        )
        try:
            if path.exists():
                path.unlink()
        except OSError as e:
            print(f"[serve] events.jsonl clear failed: {e}", flush=True)
            self._json(500, {"error": "could not clear events"})
            return
        self._json(200, {"ok": True})
