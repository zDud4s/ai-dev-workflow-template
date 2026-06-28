"""Request handlers for the session status list, SSE stream and controls.

Covers /api/sessions (list), /api/sessions/<id>/stream (SSE) and the
input/release/interrupt/branch controls. Extracted from serve.py as the
``SessionRoutes`` mixin; ``Handler`` inherits it so routing and
``serve.Handler._handle_session_stream`` resolve via MRO. Helpers each method
closes over are imported from their owning ``server.*`` modules (importing them
from serve would be circular). The shared SSE writers and the ``_UUID_RE``
class attribute remain on Handler and are reached via ``self``.
"""
from __future__ import annotations

import datetime as _dt
import json
import re
import select
import time
import uuid
from pathlib import Path

from server.config import _read_yaml_field
from server.http_base import MAX_SSE_SESSION_S, MAX_TRANSCRIPT_CATCHUP_BYTES
from server.jobs import SESSION_REGISTRY, _copy_transcript_with_new_sid
from server.jobs_state import JOBS, JOBS_LOCK
from server.paths import ROOT
from server.runtime import _browser_cross_origin_blocked
from server.session_events import _jsonl_line_to_session_events
from server.storage import _bound_path_cache
from server.transcript_paths import _transcripts_dir_for_cwd
from server.transcripts import (
    _TRANSCRIPT_ACTIVITY_CACHE,
    _TRANSCRIPT_ACTIVITY_LOCK,
    _TRANSCRIPT_MODEL_CACHE,
    _TRANSCRIPT_MODEL_LOCK,
    _TRANSCRIPT_PREVIEW_CACHE,
    _TRANSCRIPT_PREVIEW_LOCK,
    _codex_rollout_path,
    _lookup_codex_activity,
    _lookup_session_activity,
    _lookup_session_model,
    _lookup_session_task,
    _lookup_session_title,
)


class SessionRoutes:
    """Session status + SSE + control endpoints, mixed into ``Handler``."""

    def _handle_session_stream(self, session_id: str) -> None:
        """SSE: unified SessionEvent stream for a session.

        Emits a leading state_change frame with the session's current registry
        state, then tails the session's .jsonl normalizing each line via
        _jsonl_line_to_session_events (one line can expand to several events).

        For Phase 1 both mirror/acquiring and engine states fall through to the
        same .jsonl tail path — the engine writes to the same file, so the
        stream stays consistent. The engine path is validated manually (Task 10).
        """
        if _browser_cross_origin_blocked(self.headers):
            self._json(403, {"error": "origin not allowed"})
            return

        # Validate that session_id is a UUID.
        try:
            uuid.UUID(session_id)
        except ValueError:
            self._json(404, {"error": "session not found", "session_id": session_id})
            return

        # Discover the .jsonl file the same way _handle_transcript_stream does.
        tdir = _transcripts_dir_for_cwd(ROOT)
        path = (tdir / f"{session_id}.jsonl") if tdir else None

        # Also accept a session that is only in the registry (engine-started, no
        # file yet) — fall back to a 404 only when neither source is available.
        with SESSION_REGISTRY._lock:
            reg_session = SESSION_REGISTRY._sessions.get(session_id)
            reg_state = reg_session.state.value if reg_session is not None else None

        if (path is None or not path.is_file()) and reg_state is None:
            self._json(404, {"error": "session not found", "session_id": session_id})
            return

        # Use registry state when available; default to "mirror".
        state_label = reg_state if reg_state is not None else "mirror"

        # If we have a registry entry but no file yet, try again via registry.
        if path is None or not path.is_file():
            if reg_session is not None and reg_session.jsonl_path:
                path = Path(reg_session.jsonl_path)
            if path is None or not path.is_file():
                self._json(404, {"error": "session not found", "session_id": session_id})
                return

        try:
            fh = path.open("rb")
        except OSError as e:
            self._json(500, {"error": str(e)})
            return

        def _open_stream_file():
            return path.open("rb")

        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
        except OSError:
            try:
                fh.close()
            except OSError as exc:
                print("[serve] session stream: file close on header flush error: %r" % (exc,), flush=True)
            return

        # Leading state_change frame — always emitted first.
        # Include the pending flag so the chip can show "em fila" immediately.
        _leading_pending = (reg_session.pending_turn is not None) if reg_session is not None else False
        state_event = json.dumps({
            "seq": 0,
            "kind": "state_change",
            "role": None,
            "text": None,
            "partial": False,
            "state": state_label,
            "pending": _leading_pending,
        })
        if not self._write_sse_frame(state_event):
            try:
                fh.close()
            except OSError as exc:
                print("[serve] session stream: file close on state frame write error: %r" % (exc,), flush=True)
            return

        # Catch-up: flush existing content, capped to avoid large memory use.
        try:
            fh.seek(0, 2)  # SEEK_END
            size = fh.tell()
            truncated = size > MAX_TRANSCRIPT_CATCHUP_BYTES
            if truncated:
                fh.seek(size - MAX_TRANSCRIPT_CATCHUP_BYTES)
                fh.readline()  # discard partial first line
            else:
                fh.seek(0)
            existing = fh.read()
            pos = fh.tell()
        except OSError:
            try:
                fh.close()
            except Exception as e:
                print(f"[serve] session stream close failed: {e}", flush=True)
            return

        seq = 1
        if existing:
            for raw_line in existing.decode("utf-8", "replace").replace("\r\n", "\n").split("\n"):
                for evt in _jsonl_line_to_session_events(raw_line):
                    evt["seq"] = seq
                    if not self._write_sse_frame(json.dumps(evt)):
                        try:
                            fh.close()
                        except Exception as e:
                            print(f"[serve] session stream close failed: {e}", flush=True)
                        return
                    seq += 1

        # Live tail: poll for appended bytes, normalize each new line.
        session_start = time.monotonic()
        last_size = pos
        last_emitted_state = state_label
        last_emitted_pending = _leading_pending  # track pending alongside state
        # Per-stream cursor into the session's append-only warnings list. Seed
        # at 0 so a freshly connected stream replays every warning raised so far
        # (incl. ones recorded before connect), and — because we never clear the
        # shared list — concurrent streams on the same session each get them all.
        warn_seen = 0
        idle_ticks = 0
        max_idle_ticks = 240  # ~4 minutes at 1 s; client will reconnect
        try:
            while idle_ticks < max_idle_ticks:
                if time.monotonic() - session_start > MAX_SSE_SESSION_S:
                    self._write_sse_event("end", '{"reason":"max_session"}')
                    return
                try:
                    readable, _, _ = select.select([self.connection], [], [], 1.0)
                except (OSError, ValueError):
                    return
                if readable and self._sse_client_gone():
                    return
                # Surface registry state transitions (mirror -> acquiring ->
                # engine) so the pane chip updates live. The leading frame only
                # captured the state at connect time; without this an open
                # stream would never see the session go live.
                # Also surface the pending flag and drain any conflict warnings.
                with SESSION_REGISTRY._lock:
                    _rs = SESSION_REGISTRY._sessions.get(session_id)
                    _cur_state = _rs.state.value if _rs is not None else None
                    _cur_pending = (_rs.pending_turn is not None) if _rs is not None else False
                    # Emit only the warnings appended since this stream last
                    # looked, WITHOUT clearing the shared list — so multiple
                    # concurrent streams on the same session each receive every
                    # warning (no first-reader-wins drop). The list is
                    # append-only and bounded by the rare anomaly count.
                    if _rs is not None:
                        _ws = _rs.warnings[warn_seen:]
                        warn_seen = len(_rs.warnings)
                    else:
                        _ws = []
                if _cur_state and (_cur_state != last_emitted_state or _cur_pending != last_emitted_pending):
                    last_emitted_state = _cur_state
                    last_emitted_pending = _cur_pending
                    _sframe = json.dumps({
                        "seq": seq, "kind": "state_change", "role": None,
                        "text": None, "partial": False, "state": _cur_state,
                        "pending": _cur_pending,
                    })
                    if not self._write_sse_frame(_sframe):
                        return
                    seq += 1
                # Emit each drained warning as its own SSE frame.
                for _wmsg in _ws:
                    _wframe = json.dumps({
                        "seq": seq, "kind": "warning", "role": None,
                        "text": _wmsg, "partial": False, "state": None,
                    })
                    if not self._write_sse_frame(_wframe):
                        return
                    seq += 1
                try:
                    st = path.stat()
                except OSError:
                    break
                if st.st_size < last_size:
                    try:
                        fh.close()
                        fh = _open_stream_file()
                    except OSError:
                        break
                    last_size = 0
                if st.st_size > last_size:
                    idle_ticks = 0
                    try:
                        fh.seek(last_size)
                        chunk = fh.read(st.st_size - last_size)
                    except OSError:
                        break
                    text = chunk.decode("utf-8", "replace").replace("\r\n", "\n")
                    for raw_line in text.split("\n"):
                        for evt in _jsonl_line_to_session_events(raw_line):
                            evt["seq"] = seq
                            if not self._write_sse_frame(json.dumps(evt)):
                                return
                            seq += 1
                    last_size = st.st_size
                else:
                    idle_ticks += 1
                    try:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        return
            self._write_sse_event("end", "{}")
        finally:
            try:
                fh.close()
            except Exception as e:
                print(f"[serve] session stream close failed: {e}", flush=True)

    def _handle_sessions_list(self) -> None:
        """Return a unified list merging IDE transcript sessions and in-memory
        dashboard chat sessions.

        (a) IDE sessions: discovered via the same transcript directory used by
            _handle_transcripts_list — one entry per .jsonl file in
            ~/.claude/projects/<slug>/.
        (b) Dashboard sessions: in-memory chat / chat-codex JOBS that carry a
            session_id (the existing behavior).

        Items are de-duplicated by sid (session_id). When a sid appears in both
        sources the dashboard-job record is treated as authoritative for
        kind/model/status/timing fields, while transcript fields (title,
        modified, size) fill any gaps.

        Every item includes back-compat keys expected by existing tests
        (session_id, task, model) plus the new additions (sid, state,
        has_engine, title, modified, size).
        """
        # -- (b) Collect dashboard JOBS sessions ----------------------------
        by_sid: dict[str, dict] = {}
        with JOBS_LOCK:
            for j in JOBS.values():
                if j.get("kind") not in {"chat", "chat-codex"}:
                    continue
                sid = j.get("session_id")
                if not sid:
                    continue
                by_sid[sid] = {
                    # Back-compat keys — must remain unchanged.
                    "session_id": sid,
                    "kind": j.get("kind"),
                    "task": (j.get("task") or "")[:120],
                    "model": j.get("model"),
                    "started_at": j.get("started_at"),
                    "ended_at": j.get("ended_at"),
                    "status": j.get("status"),
                    "last_job_id": j.get("id"),
                    # Running totals so the status list can show duration /
                    # cost / turns without a second /api/jobs round-trip.
                    "cost": j.get("cost") if isinstance(j.get("cost"), dict) else None,
                    "exit_code": j.get("exit_code"),
                    "activity": None,
                    # New unified keys.
                    "sid": sid,
                    "title": None,
                    "modified": None,
                    "size": None,
                    "source": "dashboard",
                }

        # -- (a) Collect IDE transcript sessions ----------------------------
        tdir = _transcripts_dir_for_cwd(ROOT)
        if tdir is not None:
            try:
                files = sorted(tdir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
            except OSError:
                files = []
            TASK_PREVIEW_LIMIT = 60
            for idx, p in enumerate(files):
                try:
                    st = p.stat()
                except OSError:
                    continue
                session_id = p.stem
                task = None
                title = None
                model = None
                activity = None
                if idx < TASK_PREVIEW_LIMIT:
                    mtime_ns = st.st_mtime_ns
                    with _TRANSCRIPT_PREVIEW_LOCK:
                        cached = _TRANSCRIPT_PREVIEW_CACHE.get(session_id)
                    if cached is not None and cached[0] == mtime_ns:
                        _, task, title = cached
                    else:
                        task = _lookup_session_task(session_id)
                        title = _lookup_session_title(session_id)
                        with _TRANSCRIPT_PREVIEW_LOCK:
                            _TRANSCRIPT_PREVIEW_CACHE[session_id] = (mtime_ns, task, title)
                            _bound_path_cache(_TRANSCRIPT_PREVIEW_CACHE)
                    with _TRANSCRIPT_MODEL_LOCK:
                        mcached = _TRANSCRIPT_MODEL_CACHE.get(session_id)
                    if mcached is not None and mcached[0] == mtime_ns:
                        model = mcached[1]
                    else:
                        model = _lookup_session_model(session_id)
                        with _TRANSCRIPT_MODEL_LOCK:
                            _TRANSCRIPT_MODEL_CACHE[session_id] = (mtime_ns, model)
                            _bound_path_cache(_TRANSCRIPT_MODEL_CACHE)
                    with _TRANSCRIPT_ACTIVITY_LOCK:
                        acached = _TRANSCRIPT_ACTIVITY_CACHE.get(session_id)
                    if acached is not None and acached[0] == mtime_ns:
                        activity = acached[1]
                    else:
                        activity = _lookup_session_activity(session_id)
                        with _TRANSCRIPT_ACTIVITY_LOCK:
                            _TRANSCRIPT_ACTIVITY_CACHE[session_id] = (mtime_ns, activity)
                            _bound_path_cache(_TRANSCRIPT_ACTIVITY_CACHE)
                modified = _dt.datetime.fromtimestamp(
                    st.st_mtime, _dt.timezone.utc
                ).isoformat(timespec="seconds")
                if session_id in by_sid:
                    # Enrich the existing dashboard-job record with transcript info.
                    entry = by_sid[session_id]
                    if entry.get("title") is None:
                        entry["title"] = title
                    if entry.get("modified") is None:
                        entry["modified"] = modified
                    if entry.get("size") is None:
                        entry["size"] = st.st_size
                    if not entry.get("task"):
                        entry["task"] = (task or "")[:120]
                    if not entry.get("model") and model:
                        entry["model"] = model
                    entry["activity"] = activity
                else:
                    # New IDE-only entry.
                    by_sid[session_id] = {
                        # Back-compat keys.
                        "session_id": session_id,
                        "kind": "ide",
                        "task": (task or "")[:120],
                        "model": model,
                        "started_at": None,
                        "ended_at": None,
                        "status": None,
                        "last_job_id": None,
                        "cost": None,
                        "exit_code": None,
                        # New unified keys.
                        "sid": session_id,
                        "title": title,
                        "modified": modified,
                        "size": st.st_size,
                        "source": "ide",
                        "activity": activity,
                    }

        # -- (c) Codex sessions: live activity from the rollout tail ---------
        # chat-codex JOBS aren't Claude transcripts, so the IDE loop above
        # never touched them. Resolve each one's rollout and derive the same
        # tool/thinking activity, cached by the rollout's mtime.
        for sid, entry in by_sid.items():
            if entry.get("kind") != "chat-codex":
                continue
            rp = _codex_rollout_path(sid)
            if rp is None:
                continue
            try:
                cm = rp.stat().st_mtime_ns
            except OSError:
                continue
            with _TRANSCRIPT_ACTIVITY_LOCK:
                ac = _TRANSCRIPT_ACTIVITY_CACHE.get(sid)
            if ac is not None and ac[0] == cm:
                entry["activity"] = ac[1]
            else:
                a = _lookup_codex_activity(sid)
                with _TRANSCRIPT_ACTIVITY_LOCK:
                    _TRANSCRIPT_ACTIVITY_CACHE[sid] = (cm, a)
                    _bound_path_cache(_TRANSCRIPT_ACTIVITY_CACHE)
                entry["activity"] = a

        # -- Annotate each entry with registry state / has_engine -----------
        with SESSION_REGISTRY._lock:
            registry_snapshot = {
                sid: (s.state.value, s.engine is not None)
                for sid, s in SESSION_REGISTRY._sessions.items()
            }
        for sid, entry in by_sid.items():
            reg = registry_snapshot.get(sid)
            entry["state"] = reg[0] if reg is not None else "mirror"
            entry["has_engine"] = reg[1] if reg is not None else False

        # A session minted via POST /input (create-on-first-turn) lives in the
        # registry for a couple of seconds before claude writes its transcript —
        # and it has no JOBS entry. Without this it would be invisible in the
        # list during that window, so a just-launched terminal "disappears"
        # until the .jsonl lands. Surface registry-known sessions immediately.
        _now_iso = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
        for sid, (state, has_engine) in registry_snapshot.items():
            if sid in by_sid:
                continue
            by_sid[sid] = {
                "session_id": sid, "kind": "ide", "task": "", "model": None,
                # A live registry session is "now" — stamp modified so it sorts
                # to the top of the active list instead of the bottom.
                "started_at": _now_iso, "ended_at": None, "status": None,
                "last_job_id": None, "sid": sid, "title": None,
                "cost": None, "exit_code": None, "activity": None,
                "modified": _now_iso, "size": None, "source": "registry",
                "state": state, "has_engine": has_engine,
            }

        sessions = list(by_sid.values())
        # Sort: prefer modified timestamp (IDE), fall back to started_at (dashboard).
        sessions.sort(
            key=lambda s: s.get("modified") or s.get("started_at") or "",
            reverse=True,
        )
        self._json(200, {"sessions": sessions})

    def _handle_session_input(self, sid: str, body: dict) -> None:
        """POST /api/sessions/<sid>/input {text, model?, owner?}

        Validates that ``sid`` is a UUID and ``text`` is non-empty, then calls
        SESSION_REGISTRY.get_or_create + submit_turn.  Maps the registry result
        to an HTTP status:
          "accepted" -> 200 {"status": "accepted"}
          "queued"   -> 202 {"status": "queued"}
          "rejected" -> 409 {"status": "already_queued"}

        An optional ``owner`` field (client/tab id) is validated against the
        same short-id pattern used elsewhere and forwarded to submit_turn for
        multi-tab ownership tracking.  Defaults to None when absent.
        """
        # Validate sid is a canonical UUID before doing anything else.
        if not self._UUID_RE.match(sid):
            self._json(400, {"error": "sid must be a UUID"})
            return
        # Validate text is present and non-empty.
        text = body.get("text") or ""
        if not isinstance(text, str) or not text.strip():
            self._json(400, {"error": "text is required and must be non-empty"})
            return
        # Validate optional owner field: must match [A-Za-z0-9._-], max 64 chars.
        owner = body.get("owner") or None
        if owner is not None:
            if not isinstance(owner, str) or len(owner) > 64 or not re.fullmatch(r"[A-Za-z0-9._-]+", owner):
                self._json(400, {"error": "owner must be a short id matching [A-Za-z0-9._-]{1,64}"})
                return
        # Resolve the session transcript path (may be None if no .claude/projects dir).
        tdir = _transcripts_dir_for_cwd(ROOT)
        jsonl_path = str(tdir / f"{sid}.jsonl") if tdir else f"{sid}.jsonl"
        # Pick the model: body wins, otherwise fall back to session.model in
        # models.yaml, otherwise the hard-coded fallback used by job creation.
        model_override = (body.get("model") or "").strip() or None
        # Validate the body-provided model id against the same regex used by
        # _handle_jobs_create. The trusted models.yaml default skips this check.
        if model_override and (
            len(model_override) > 80
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", model_override)
        ):
            self._json(400, {"error": "model must be a short id matching [A-Za-z0-9._-]{1,80}"})
            return
        if not model_override:
            session_cfg = _read_yaml_field(ROOT / ".ai" / "models.yaml", "session")
            model_override = (session_cfg.get("model") if isinstance(session_cfg, dict) else None) or "claude-sonnet-4-6"
        SESSION_REGISTRY.get_or_create(sid, jsonl_path=jsonl_path)
        result = SESSION_REGISTRY.submit_turn(sid, {"text": text}, model_override, owner=owner)
        # Map the registry result to the appropriate HTTP status code.
        if result == "accepted":
            self._json(200, {"status": "accepted"})
        elif result == "queued":
            self._json(202, {"status": "queued"})
        else:
            # "rejected" means the pending slot was already occupied.
            self._json(409, {"status": "already_queued"})

    def _handle_session_release(self, sid: str) -> None:
        """POST /api/sessions/<sid>/release

        Validates that ``sid`` is a UUID, then releases the session back to
        MIRROR state via SESSION_REGISTRY.release.  Responds 200 {"status":
        "released"}.
        """
        # Validate sid is a canonical UUID.
        if not self._UUID_RE.match(sid):
            self._json(400, {"error": "sid must be a UUID"})
            return
        # Ensure the session exists in the registry (create in MIRROR if not).
        tdir = _transcripts_dir_for_cwd(ROOT)
        jsonl_path = str(tdir / f"{sid}.jsonl") if tdir else f"{sid}.jsonl"
        SESSION_REGISTRY.get_or_create(sid, jsonl_path=jsonl_path)
        SESSION_REGISTRY.release(sid)
        self._json(200, {"status": "released"})

    def _handle_session_interrupt(self, sid: str) -> None:
        """POST /api/sessions/<sid>/interrupt

        Signals the running engine to stop mid-turn and reconciles registry
        state so writing_ours() returns False immediately.

        Design choice: responds 200 even when the sid is not in the registry
        (idempotent — interrupting a gone or never-started session is a no-op).
        """
        # Validate sid is a canonical UUID.
        if not self._UUID_RE.match(sid):
            self._json(400, {"error": "sid must be a UUID"})
            return
        with SESSION_REGISTRY._lock:
            session = SESSION_REGISTRY._sessions.get(sid)
            if session is not None and session.engine is not None:
                # Signal the subprocess to stop generating output.
                session.engine.interrupt()
        # State reconcile: clears turn_in_flight and last_rendered_offset offset.
        # Safe to call even when the session is absent — KeyError is swallowed below.
        if sid in SESSION_REGISTRY._sessions:
            try:
                SESSION_REGISTRY.interrupt(sid)
            except Exception as exc:
                print(f"[serve] session interrupt reconcile failed for {sid}: {exc}", flush=True)
        self._json(200, {"status": "interrupted"})

    def _handle_session_branch(self, sid: str) -> None:
        """POST /api/sessions/<sid>/branch

        Branch ``sid`` into a fresh session by copying its transcript on disk:
        mint a new session id, copy ``<sid>.jsonl`` record-by-record (rewriting
        each record's ``sessionId`` to the new id), and return ``{"sid": <new>}``.
        The caller opens a fresh session pane on the new sid, which resumes the
        copied transcript on first input (the engine factory sees the file and
        uses ``--resume``). No subprocess, poll, or force-kill is involved, so
        the branch can neither time out nor truncate the new transcript.
        """
        if not self._UUID_RE.match(sid):
            self._json(400, {"error": "sid must be a UUID"})
            return
        tdir = _transcripts_dir_for_cwd(ROOT)
        if tdir is None:
            self._json(404, {"error": "no transcripts directory for this project"})
            return
        src_path = tdir / f"{sid}.jsonl"
        if not src_path.is_file():
            self._json(404, {"error": "no transcript to branch from"})
            return
        new_sid = str(uuid.uuid4())
        dst_path = tdir / f"{new_sid}.jsonl"
        try:
            n = _copy_transcript_with_new_sid(src_path, dst_path, new_sid)
        except OSError as exc:
            print(f"[serve] branch: copy {sid} -> {new_sid} failed: {exc}", flush=True)
            self._json(500, {"error": "branch copy failed"})
            return
        print(f"[serve] branch: {sid} -> {new_sid} ({n} records copied)", flush=True)
        self._json(200, {"sid": new_sid})
