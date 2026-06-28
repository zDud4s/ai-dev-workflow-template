"""Request handlers for the IDE transcript list + live SSE stream.

Covers /api/transcripts (list) and /api/transcripts/<id>/stream (SSE tail).
Extracted from serve.py as the ``TranscriptRoutes`` mixin; ``Handler`` inherits
it so routing and ``serve.Handler._handle_transcript_stream`` resolve via MRO.
Helpers each method closes over are imported from their owning ``server.*``
modules (importing them from serve would be circular). The shared SSE writers
remain on Handler and are reached via ``self``.
"""
from __future__ import annotations

import datetime as _dt
import select
import time

from server.http_base import MAX_SSE_SESSION_S, MAX_TRANSCRIPT_CATCHUP_BYTES
from server.paths import ROOT
from server.runtime import _browser_cross_origin_blocked
from server.storage import _bound_path_cache
from server.transcripts.paths import _transcripts_dir_for_cwd
from server.transcripts import (
    _TRANSCRIPT_PREVIEW_CACHE,
    _TRANSCRIPT_PREVIEW_LOCK,
    _lookup_session_task,
    _lookup_session_title,
)


class TranscriptRoutes:
    """IDE transcript list + stream endpoints, mixed into ``Handler``."""

    def _handle_transcripts_list(self) -> None:
        """List the IDE transcript files for the current repo - the JSONL
        files Claude Code (the VSCode/Cursor extension) writes for every
        session in ``~/.claude/projects/<slug>/<session_id>.jsonl``.

        Each entry carries a ``task`` preview (first real user message) so
        the Resume-session picker can show a meaningful label, not just the
        session UUID."""
        tdir = _transcripts_dir_for_cwd(ROOT)
        if tdir is None:
            self._json(200, {"transcripts": [], "note": "no ~/.claude/projects directory for this repo"})
            return
        files = sorted(tdir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        # Cap task-preview reads so very large project histories don't slow
        # down the picker. Older entries still appear in the list, just
        # without a task preview.
        TASK_PREVIEW_LIMIT = 60
        items = []
        for idx, p in enumerate(files):
            try:
                st = p.stat()
            except OSError:
                continue
            session_id = p.stem
            task = None
            title = None
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
            items.append({
                "session_id": session_id,
                "size_bytes": st.st_size,
                "modified": _dt.datetime.fromtimestamp(st.st_mtime, _dt.timezone.utc).isoformat(timespec="seconds"),
                "path": str(p.relative_to(tdir.parent)),
                "task": task,
                "title": title,
            })
        self._json(200, {"transcripts": items, "dir": str(tdir)})

    def _handle_transcript_stream(self, session_id: str) -> None:
        """SSE: tail an IDE transcript JSONL file. Emits any existing
        content first (catch-up), then continues forwarding bytes as the
        file grows (live mirror of the ongoing IDE session)."""
        if _browser_cross_origin_blocked(self.headers):
            self._json(403, {"error": "origin not allowed"})
            return
        tdir = _transcripts_dir_for_cwd(ROOT)
        path = (tdir / f"{session_id}.jsonl") if tdir else None
        if not path or not path.is_file():
            self._json(404, {"error": "transcript not found", "session_id": session_id})
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
            # Client disconnected before the headers flushed — close fh so we
            # don't leak the descriptor (and, on Windows, a lock on the file).
            try:
                fh.close()
            except OSError:
                pass
            return

        # Catch-up: flush existing content first, capped so a multi-MB
        # transcript doesn't pin the whole file in memory per subscriber.
        # When the file is over cap, seek to ``size - cap`` and discard up to
        # the first newline so the catch-up never emits a partial JSONL line.
        try:
            fh.seek(0, 2)  # SEEK_END
            size = fh.tell()
            truncated = size > MAX_TRANSCRIPT_CATCHUP_BYTES
            if truncated:
                fh.seek(size - MAX_TRANSCRIPT_CATCHUP_BYTES)
                # Drop the partial line at the head of the window.
                fh.readline()
            else:
                fh.seek(0)
            existing = fh.read()
            pos = fh.tell()
        except OSError:
            try:
                fh.close()
            except Exception as e:
                print(f"[serve] transcript stream close failed: {e}", flush=True)
            return
        if existing:
            text = existing.decode("utf-8", "replace").replace("\r\n", "\n")
            if not self._write_sse_frame(text):
                try:
                    fh.close()
                except Exception as e:
                    print(f"[serve] transcript stream close failed: {e}", flush=True)
                return

        # Live tail: poll for appended bytes. Exit when client disconnects,
        # after a long idle period (defensive cap), OR when the hard
        # wall-clock cap (``MAX_SSE_SESSION_S``) trips — a chatty transcript
        # that emits one record per second forever would otherwise pin the
        # request thread + TCP connection indefinitely. The idle cap and the
        # wall-clock cap are complementary: idle catches "stalled stream",
        # wall-clock catches "infinite chatter".
        session_start = time.monotonic()
        last_size = pos
        idle_ticks = 0
        max_idle_ticks = 240  # ~ 4 minutes at 1s; client will reconnect
        try:
            while idle_ticks < max_idle_ticks:
                if time.monotonic() - session_start > MAX_SSE_SESSION_S:
                    self._write_sse_event("end", '{"reason":"max_session"}')
                    return
                # Wait up to 1s for either the file to grow OR the client to
                # disconnect. Replaces the previous unconditional sleep(1.0):
                # the cadence for file-stat polling is unchanged, but disconnect
                # is now detected within milliseconds via FIN on the socket
                # read-side instead of waiting for a future wfile.write to
                # surface a broken pipe — which on Windows can be delayed
                # for many minutes while small chunks still fit in the kernel
                # send buffer, accumulating phantom request threads.
                try:
                    readable, _, _ = select.select([self.connection], [], [], 1.0)
                except (OSError, ValueError):
                    return
                if readable and self._sse_client_gone():
                    return
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
                    if not self._write_sse_frame(chunk.decode("utf-8", "replace").replace("\r\n", "\n")):
                        return
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
                print(f"[serve] transcript stream close failed: {e}", flush=True)
