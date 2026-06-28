"""Request handlers for the job lifecycle + streaming endpoints.

Covers /api/jobs (create/list), /api/jobs/<id> (get/interrupt/cancel/input) and
the /api/jobs/<id>/stream SSE feed. Extracted from serve.py as the ``JobRoutes``
mixin; ``Handler`` inherits it so routing and ``serve.Handler._handle_job_stream``
resolve via MRO. The job-engine helpers each method calls are imported from their
owning ``server.*`` modules (importing them from serve would be circular). The
shared SSE writers (_write_sse_frame / _write_sse_event / _sse_client_gone) and
_is_blocked_path remain on Handler and are reached via ``self``.
"""
from __future__ import annotations

import datetime as _dt
import json
import re
import time
import uuid
from collections import deque
from pathlib import Path

from server.http_base import MAX_SSE_SESSION_S
from server.jobs import (
    _DROP_COUNTS,
    _DROP_COUNTS_LOCK,
    _VALID_PERMISSION_MODES,
    _cancel_job,
    _interrupt_chat_turn,
    _send_chat_blocks,
    _send_to_stdin,
    _spawn_job,
    _tail_chat_catchup,
)
from server.jobs.persistence import _extract_cost_from_log, _persist_job
from server.jobs.reaper import _evict_old_jobs, _reconcile_running_pids
from server.jobs.state import JOB_KINDS, JOBS, JOBS_LOCK, _TERMINAL_JOB_STATUSES
from server.paths import ROOT
from server.runtime import _browser_cross_origin_blocked
from server.validation import _safe_which


class JobRoutes:
    """Job lifecycle + streaming endpoints, mixed into ``Handler``."""

    def _job_summary(self, j: dict) -> dict:
        out = {
            "id": j["id"],
            "kind": j["kind"],
            "task": j["task"][:120],
            "status": j["status"],
            "created_at": j.get("created_at"),
            "started_at": j.get("started_at"),
            "ended_at": j.get("ended_at"),
            "exit_code": j.get("exit_code"),
            "command": j.get("command"),
            "session_id": j.get("session_id"),
            "model": j.get("model"),
            "tags": list(j.get("tags") or []),
        }
        # Chat jobs surface aggregated cost so the UI can show running
        # totals. Prefer the live counter (updated by the pump in real
        # time); fall back to scanning the log file post-hoc for older
        # jobs whose live counter wasn't tracked. For terminal jobs the
        # scanned cost is cached back onto the job entry so subsequent
        # /api/jobs polls don't re-scan the log file every time.
        if j.get("kind") in {"chat", "chat-codex"}:
            live = j.get("cost")
            if isinstance(live, dict):
                out["cost"] = live
            elif j.get("log_path"):
                cost = _extract_cost_from_log(Path(j["log_path"]))
                if cost is not None:
                    out["cost"] = cost
                    if j.get("status") in _TERMINAL_JOB_STATUSES:
                        j["cost"] = cost
        return out

    def _handle_jobs_create(self, body: dict) -> None:
        kind = (body.get("kind") or "").strip()
        task = (body.get("task") or "").strip()
        resume_session_id = (body.get("resume_session_id") or "").strip() or None
        if kind not in JOB_KINDS:
            self._json(400, {"error": f"kind must be one of {sorted(JOB_KINDS)}"})
            return
        if not task:
            self._json(400, {"error": "task is required"})
            return
        if len(task) > 4000:
            self._json(400, {"error": "task must be 4000 chars or fewer"})
            return
        if resume_session_id and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", resume_session_id):
            self._json(400, {"error": "resume_session_id must match [A-Za-z0-9][A-Za-z0-9._-]{0,79}"})
            return
        # Optional fork: like resume but adds --fork-session so the new
        # turns land in a fresh session id instead of overwriting the
        # original transcript.
        fork_session_id = (body.get("fork_session_id") or "").strip() or None
        if fork_session_id and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", fork_session_id):
            self._json(400, {"error": "fork_session_id must match [A-Za-z0-9][A-Za-z0-9._-]{0,79}"})
            return
        if fork_session_id and resume_session_id:
            self._json(400, {"error": "set either fork_session_id or resume_session_id, not both"})
            return
        # Optional permission mode for chat jobs. Falls back to the legacy
        # ``--dangerously-skip-permissions`` flag when not provided so the
        # default behaviour is unchanged.
        permission_mode = (body.get("permission_mode") or "").strip() or None
        if permission_mode and permission_mode not in _VALID_PERMISSION_MODES:
            self._json(400, {"error": f"permission_mode must be one of {sorted(_VALID_PERMISSION_MODES)}"})
            return
        # Optional explicit model id (e.g. selected from the "New terminal"
        # picker on the Terminals page). When absent the spawner falls back
        # to ``session.model`` in models.yaml.
        model_override = (body.get("model") or "").strip() or None
        if model_override and (len(model_override) > 80 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", model_override)):
            self._json(400, {"error": "model must be a short id matching [A-Za-z0-9._-]{1,80}"})
            return
        # Optional tag list: lowercase short slugs only, max 8 tags per job,
        # so the persistence ledger stays small and the resume filter works.
        tags_raw = body.get("tags") or []
        if not isinstance(tags_raw, list):
            self._json(400, {"error": "tags must be a list of strings"})
            return
        if len(tags_raw) > 8:
            self._json(400, {"error": "max 8 tags per job"})
            return
        tags: list[str] = []
        for t in tags_raw:
            if not isinstance(t, str):
                self._json(400, {"error": "tags must be strings"})
                return
            t = t.strip()
            if not t:
                continue
            if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,23}", t):
                self._json(400, {"error": f"invalid tag {t!r} (lowercase a-z0-9_-, 1-24 chars)"})
                return
            if t not in tags:
                tags.append(t)
        # The specific CLI we need depends on the kind.
        required_bin = "codex" if kind == "chat-codex" else "claude"
        if not _safe_which(required_bin):
            self._json(503, {"error": f"`{required_bin}` CLI not found on PATH"})
            return
        job_id = str(uuid.uuid4())
        now = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
        with JOBS_LOCK:
            JOBS[job_id] = {
                "id": job_id,
                "kind": kind,
                "task": task,
                "status": "queued",
                "created_at": now,
                "pid": None,
                "log_path": None,
                "exit_code": None,
                "started_at": None,
                "ended_at": None,
                "tags": tags,
            }
        _spawn_job(
            job_id,
            kind,
            task,
            resume_session_id=resume_session_id,
            permission_mode=permission_mode,
            fork_session_id=fork_session_id,
            model_override=model_override,
        )
        _persist_job(job_id)  # capture the initial queued/running snapshot
        _evict_old_jobs()
        with JOBS_LOCK:
            self._json(201, self._job_summary(JOBS[job_id]))

    def _handle_jobs_list(self) -> None:
        # Reconcile dead PIDs first so the response never shows zombie
        # "running" rows whose subprocess was killed externally. Cheap on
        # POSIX (signal-0); a few-ms tasklist call on Windows.
        _reconcile_running_pids()
        with JOBS_LOCK:
            items = [self._job_summary(j) for j in JOBS.values()]
        items.sort(key=lambda x: x.get("created_at") or "", reverse=True)
        self._json(200, {"jobs": items})

    def _handle_job_get(self, job_id: str, qs: dict[str, list[str]]) -> None:
        try:
            tail = max(1, min(2000, int((qs.get("tail", ["200"])[0]))))
        except ValueError:
            tail = 200
        with JOBS_LOCK:
            j = JOBS.get(job_id)
            summary = self._job_summary(j) if j else None
            log_path = j.get("log_path") if j else None
        if not summary:
            self._json(404, {"error": "job not found"})
            return
        log_lines: list[str] = []
        if log_path and Path(log_path).exists():
            try:
                # Tail the last N lines without loading the whole file.
                with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                    log_lines = list(deque(f, maxlen=tail))
            except OSError:
                log_lines = []
        summary["log_tail"] = "".join(log_lines)
        # _job_summary already prefers the live cost counter; only fall
        # back to log-scanning here if neither was set.
        if summary.get("kind") in {"chat", "chat-codex"} and "cost" not in summary and log_path:
            cost = _extract_cost_from_log(Path(log_path))
            if cost is not None:
                summary["cost"] = cost
        self._json(200, summary)

    def _handle_job_interrupt(self, job_id: str) -> None:
        with JOBS_LOCK:
            exists = job_id in JOBS
        if not exists:
            self._json(404, {"error": "job not found"})
            return
        ok, err = _interrupt_chat_turn(job_id)
        if not ok:
            code = 404 if err == "not found" else 409
            self._json(code, {"error": err})
            return
        self._json(200, {"ok": True})

    def _handle_job_cancel(self, job_id: str) -> None:
        if _cancel_job(job_id):
            self._json(200, {"ok": True})
        else:
            self._json(409, {"error": "job not running or not found"})

    def _compose_multimodal_blocks(self, text: str, images: list, files: list):
        """Validate + assemble a stream-json content array from a composer
        payload. Returns the blocks list on success or a ``(code, payload)``
        tuple on validation error."""
        blocks: list[dict] = []
        if text:
            blocks.append({"type": "text", "text": text})
        # Inline file contents as fenced text blocks. The agent treats
        # each as part of the user turn (no IDE-style @-mention expansion
        # since stream-json doesn't run that pass).
        for rel in files:
            if not isinstance(rel, str) or not rel.strip():
                return 400, {"error": "files entries must be non-empty strings"}
            try:
                resolved = (ROOT / rel).resolve()
                resolved.relative_to(ROOT.resolve())
            except (ValueError, OSError):
                return 403, {"error": f"file path outside repo: {rel}"}
            if self._is_blocked_path(resolved):
                return 403, {"error": f"file is blocked: {rel}"}
            if not resolved.is_file():
                return 404, {"error": f"file not found: {rel}"}
            try:
                content = resolved.read_text(encoding="utf-8", errors="replace")
            except OSError as e:
                return 500, {"error": f"could not read {rel}: {e}"}
            cap = 64 * 1024
            if len(content) > cap:
                content = content[:cap] + "\n...[truncated]"
            blocks.append({
                "type": "text",
                "text": f"<file path=\"{rel}\">\n{content}\n</file>",
            })
        # Image blocks: each must have base64 ``data`` and ``media_type``.
        for img in images:
            if not isinstance(img, dict):
                return 400, {"error": "images entries must be objects"}
            data = img.get("data")
            mt = img.get("media_type") or "image/png"
            if not isinstance(data, str) or not data:
                return 400, {"error": "image data must be a base64 string"}
            blocks.append({
                "type": "image",
                "source": {"type": "base64", "media_type": mt, "data": data},
            })
        return blocks

    def _handle_job_input(self, job_id: str, body: dict) -> None:
        with JOBS_LOCK:
            j = JOBS.get(job_id)
            exists = j is not None
            kind = j.get("kind") if j else ""
        if not exists:
            self._json(404, {"error": "job not found"})
            return
        text = body.get("text") or ""
        images = body.get("images") or []
        files = body.get("files") or []
        if not isinstance(text, str):
            self._json(400, {"error": "text must be a string"})
            return
        if len(text) > 8000:
            self._json(400, {"error": "text must be 8000 chars or fewer"})
            return
        if not isinstance(images, list) or not isinstance(files, list):
            self._json(400, {"error": "images and files must be arrays"})
            return
        if not text and not images and not files:
            self._json(400, {"error": "text, images or files is required"})
            return

        # For chat jobs we can build a richer content array (text + image
        # + file blocks). For other kinds we fall back to plain-text stdin.
        if kind == "chat" and (images or files):
            blocks = self._compose_multimodal_blocks(text, images, files)
            if isinstance(blocks, tuple):  # error
                code, payload = blocks
                self._json(code, payload)
                return
            ok, err = _send_chat_blocks(job_id, blocks)
        else:
            if not text:
                self._json(400, {"error": "text is required for non-chat jobs"})
                return
            # Strip ASCII control chars (except \n / \t) before piping into
            # a non-chat subprocess's stdin. \r in particular can confuse a
            # stream-json reader that line-frames on \n — the bare \r would
            # be appended to the prior line as opaque data and the partial
            # framing breaks for the rest of the session.
            sanitized = "".join(
                ch for ch in text
                if ch in ("\n", "\t") or (32 <= ord(ch) < 127) or ord(ch) >= 128
            )
            ok, err = _send_to_stdin(job_id, sanitized)
        if not ok:
            code = 404 if err == "not found" else 409
            self._json(code, {"error": err})
            return
        self._json(200, {"ok": True})

    def _handle_job_stream(self, job_id: str) -> None:
        """Server-Sent Events stream of the subprocess output.

        Strategy:
          1. Take a snapshot of the existing log file and flush it as one
             `data:` frame (so reconnecting clients catch up).
          2. Register a queue as a subscriber; each chunk written by the
             runner thread gets forwarded as a `data:` frame.
          3. Terminate when the runner publishes the EOF sentinel (None).

        Hard upper-bound on a single SSE session: ``MAX_SSE_SESSION_S``
        seconds, regardless of idleness. A chatty job that emits one
        chunk every second forever would otherwise pin the response
        thread, the queue subscriber slot, and the TCP connection
        indefinitely. The client reconnects transparently, so the
        forced rotation is observationally invisible.
        """
        if _browser_cross_origin_blocked(self.headers):
            self._json(403, {"error": "origin not allowed"})
            return
        import queue as _queue

        session_start = time.monotonic()

        with JOBS_LOCK:
            j = JOBS.get(job_id)
            if not j:
                self._json(404, {"error": "job not found"})
                return
            log_path = j.get("log_path")
            subs = j.setdefault("subscribers", [])
            q: _queue.Queue = _queue.Queue(maxsize=1024)
            subs.append(q)

        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()

            # 1. Catch-up: flush what's already on disk. For chat jobs whose
            # log_path is the IDE transcript file (potentially MBs of history
            # — hooks, attachments, queue ops), we tail just the recent
            # conversation records so the browser doesn't choke parsing the
            # entire backlog. For non-chat jobs (orchestrate/plan/codex) the
            # log file is dashboard-owned and small; full dump stays.
            # Re-check status under lock after subscriber registration — closes the EOF-publish race that hangs terminal-status streams for MAX_SSE_SESSION_S.
            with JOBS_LOCK:
                j_now = JOBS.get(job_id)
                catchup_kind = (j_now or {}).get("kind")
                status = (j_now or {}).get("status")
            if log_path and Path(log_path).exists():
                try:
                    if catchup_kind == "chat":
                        existing = _tail_chat_catchup(Path(log_path))
                    else:
                        existing = Path(log_path).read_text(encoding="utf-8", errors="replace")
                except OSError:
                    existing = ""
                if existing:
                    self._write_sse_frame(existing)

            if status in _TERMINAL_JOB_STATUSES or j_now is None:
                # Job already finished (or entry evicted) — close immediately
                # after catch-up rather than entering the live-tail loop.
                self._write_sse_event("end", "{}")
                return

            # 2. Live tail until EOF sentinel arrives.
            while True:
                # Hard session cap — emit a final SSE event so the client
                # can distinguish "server rotated me" from a network drop.
                if time.monotonic() - session_start > MAX_SSE_SESSION_S:
                    self._write_sse_event("end", '{"reason":"max_session"}')
                    return
                # Catch client disconnect between chunks. A chatty job
                # whose chunks all fit in the kernel send buffer would
                # otherwise spin through ``_write_sse_frame`` indefinitely
                # after the browser has closed the EventSource — broken
                # pipe is only surfaced once the buffer fills, which on
                # Windows can take minutes.
                if self._sse_client_gone():
                    return
                try:
                    chunk = q.get(timeout=15)
                except _queue.Empty:
                    # Heartbeat keeps the connection alive through proxies.
                    try:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        return
                    continue
                if chunk is None:
                    self._write_sse_event("end", "{}")
                    return
                if isinstance(chunk, dict) and chunk.get("type") == "resync":
                    self._write_sse_event("resync", json.dumps(chunk))
                    continue
                if not self._write_sse_frame(chunk):
                    return
        finally:
            with JOBS_LOCK:
                try:
                    subs.remove(q)
                except ValueError:
                    pass
            with _DROP_COUNTS_LOCK:
                counts = _DROP_COUNTS.get(job_id)
                if counts:
                    counts.pop(id(q), None)
                    if not counts:
                        _DROP_COUNTS.pop(job_id, None)
