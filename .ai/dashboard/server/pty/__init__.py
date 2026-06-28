"""PTY (pseudo-terminal) session lifecycle for the dashboard server.

Extracted from serve.py. Real shell terminals run over WebSocket: each session
spawns a child via the ``pty_session`` scripts helper, a background reader
broadcasts its bytes to WS subscribers (buffering a tail in a per-session ring
for late attaches), and the registry is bounded by evicting the oldest *ended*
sessions. The module owns the ``PTYS`` registry and its lock plus the ring/cap
constants. serve.py re-exports every name here, so ``serve._pty_spawn`` /
``serve.PTYS`` and the ws/HTTP handlers and ``main()`` atexit/idle-loop wiring
that reference them keep working unchanged. The ``WebSocket`` class, the
``_ws_*`` helpers, and the ``_handle_pty_*`` request handlers stay in serve.py —
this module is only the session layer beneath them.
"""
from __future__ import annotations

import datetime as _dt
import queue as _stdqueue
import secrets
import threading
import time
import uuid

from server.pty import session as _pty_session
from server.paths import ROOT

PTYS: dict[str, dict] = {}
PTYS_LOCK = threading.Lock()
PTYS_MAX = 20

# Cap on the per-session ring buffer used for catch-up when a client
# (re)attaches to a long-running PTY. 256 KB keeps a full screen of
# scrollback for any reasonable terminal size while bounding memory.
PTY_RING_BYTES = 256 * 1024


def _pty_spawn(shell: str | None, cwd: str | None, cols: int, rows: int) -> dict:
    """Create a new PTY session and start its reader thread.

    Returns the session dict (registered in PTYS). Raises ImportError if
    the platform PTY backend is unavailable (e.g. pywinpty missing on
    Windows) so the HTTP layer can map it to a 503."""
    argv = _pty_session.resolve_shell(shell)
    target_cwd = cwd or str(ROOT)
    pty = _pty_session.Pty.spawn(argv, cwd=target_cwd, cols=cols, rows=rows)
    pty_id = str(uuid.uuid4())
    now = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    # Per-PTY access token: required on WS attach so an arbitrary
    # dashboard origin client can't enumerate PTYs and slurp their
    # 256 KiB scrollback (which often contains API keys / PATs).
    # Cryptographically random (URL-safe base64) so it's unguessable.
    entry = {
        "id": pty_id,
        "kind": "terminal",
        "shell": shell or "auto",
        "argv": argv,
        "cwd": target_cwd,
        "cols": cols,
        "rows": rows,
        "pid": pty.pid,
        "created_at": now,
        "status": "running",
        "exit_code": None,
        "_pty": pty,
        "_ring": bytearray(),     # accumulated output bytes, capped
        "_subscribers": [],       # list of queue.Queue[bytes|None]
        "_lock": threading.Lock(),
        "_token": secrets.token_urlsafe(32),
    }
    with PTYS_LOCK:
        PTYS[pty_id] = entry
    t = threading.Thread(target=_pty_reader_loop, args=(pty_id,), daemon=True)
    t.start()
    _evict_old_ptys()
    return entry


def _pty_reader_loop(pty_id: str) -> None:
    """Background reader: pulls bytes off the PTY master and broadcasts
    them to every WS subscriber. Buffers a tail in the ring so a late
    attach can catch up. Exits when the child closes its end."""
    with PTYS_LOCK:
        entry = PTYS.get(pty_id)
    if not entry:
        return
    pty = entry["_pty"]
    while True:
        chunk = pty.read(4096)
        if not chunk:
            # EOF — child closed its end of the master. Mark the session
            # done so the UI can show "ended" and prevent further writes.
            with entry["_lock"]:
                entry["status"] = "ended"
                # Push the EOF sentinel to every subscriber so each WS
                # loop can wake up and close.
                for q in entry["_subscribers"]:
                    try:
                        q.put_nowait(None)
                    except _stdqueue.Full:
                        pass
            return
        with entry["_lock"]:
            buf = entry["_ring"]
            buf += chunk
            if len(buf) > PTY_RING_BYTES:
                # Drop the oldest bytes once we exceed the cap. Anchored at
                # the cap so we don't memcpy on every read.
                del buf[: len(buf) - PTY_RING_BYTES]
            entry["_ring"] = buf
            subs = list(entry["_subscribers"])
        for q in subs:
            try:
                q.put_nowait(chunk)
            except _stdqueue.Full:
                # Slow consumer: drop the chunk for that subscriber. Their
                # ring catch-up on reconnect handles missed bytes.
                pass


def _pty_kill(pty_id: str) -> bool:
    with PTYS_LOCK:
        entry = PTYS.get(pty_id)
    if not entry:
        return False
    pty = entry.get("_pty")
    try:
        pty.kill()
    except Exception as e:  # noqa: BLE001 — PTY backend may raise anything
        # Best-effort: the session is being torn down anyway, but log so an
        # operator can grep for stuck shells that wouldn't kill.
        print(f"[serve] pty kill({pty_id}) failed: {e}", flush=True)
    with PTYS_LOCK:
        PTYS.pop(pty_id, None)
    with entry["_lock"]:
        entry["status"] = "ended"
        for q in entry["_subscribers"]:
            try:
                q.put_nowait(None)
            except _stdqueue.Full:
                pass
    return True


def _pty_summary(entry: dict, *, include_token: bool = False) -> dict:
    out = {
        "id": entry["id"],
        "kind": entry["kind"],
        "shell": entry["shell"],
        "argv": list(entry.get("argv") or []),
        "cwd": entry.get("cwd"),
        "cols": entry.get("cols"),
        "rows": entry.get("rows"),
        "pid": entry.get("pid"),
        "status": entry.get("status"),
        "created_at": entry.get("created_at"),
    }
    # Token only flows out on the create response so the spawner can
    # save it locally and use it for subsequent WS attach. List + get
    # endpoints intentionally omit it so it never crosses the wire
    # except in the response to the request that created the PTY.
    if include_token:
        out["token"] = entry.get("_token")
    return out


def _evict_old_ptys() -> None:
    """Bound the PTY registry by killing the oldest *ended* sessions when
    we cross PTYS_MAX. Running sessions are never evicted."""
    with PTYS_LOCK:
        if len(PTYS) <= PTYS_MAX:
            return
        ended = [
            (entry["created_at"], pid)
            for pid, entry in PTYS.items()
            if entry.get("status") != "running"
        ]
        ended.sort()
        to_drop = len(PTYS) - PTYS_MAX
        victims = ended[:to_drop]
    for _, pid in victims:
        with PTYS_LOCK:
            entry = PTYS.pop(pid, None)
        if entry:
            try:
                entry["_pty"].kill()
            except Exception as e:  # noqa: BLE001 - PTY backend may raise anything
                print(f"[serve] evict-kill error pid={pid}: {e}", flush=True)


def _shutdown_all_ptys() -> None:
    try:
        reg = _pty_session.Pty._registry
        lock = getattr(_pty_session.Pty, "_registry_lock", None)
        if lock is not None:
            with lock:
                entries = list(reg.values())
        else:
            entries = list(reg.values())
    except Exception as e:  # noqa: BLE001 - shutdown is best-effort
        print(f"[serve] shutdown snapshot error: {e}")
        return
    for p in entries:
        try:
            p.kill()
        except Exception as e:  # noqa: BLE001 - shutdown is best-effort
            print(f"[serve] shutdown-kill error: {e}")


def _pty_idle_loop() -> None:
    while True:
        try:
            time.sleep(60)
            _pty_session.Pty.cleanup_idle()
        except Exception as e:  # noqa: BLE001 - timer must keep running
            print(f"[serve] pty-idle-loop error: {e}")
