# .ai/dashboard/scripts/session_lock.py
"""Cross-process file lock with heartbeat and stale-reclaim for dashboard sessions.

Two dashboard processes (e.g. after a restart) must not both run an engine on
the same session simultaneously. SessionLock coordinates them via a JSON lock
file per session id. The IDE does not read these files; they are purely an
inter-dashboard contract.

Lock file location: <lock_dir>/<sid>.lock
Lock file schema  : {"owner": str, "pid": int, "heartbeat_ts": float, "state": str}
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import time

logger = logging.getLogger(__name__)

# A lock whose heartbeat_ts is older than this many seconds is considered stale
# and may be reclaimed by another process.
LOCK_STALE_S: float = 30

# How often the owner should call heartbeat() to keep its lock alive.
HEARTBEAT_INTERVAL_S: float = 5


class SessionLock:
    """File-based per-session lock with heartbeat and stale-reclaim.

    The clock is injectable so tests can advance time without real sleeps.
    Use time.time (wall clock, not monotonic) because the lock must be
    comparable across different processes on the same host.
    """

    def __init__(self, lock_dir: pathlib.Path, clock=time.time) -> None:
        """Initialize the lock manager.

        Args:
            lock_dir: Directory where <sid>.lock files are stored.
            clock: Zero-argument callable returning wall-clock seconds.
                   Defaults to time.time. Must NOT be time.monotonic — lock
                   staleness is measured across process boundaries.
        """
        self._lock_dir = lock_dir
        self._clock = clock

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _lock_path(self, sid: str) -> pathlib.Path:
        return self._lock_dir / f"{sid}.lock"

    def _read_lock(self, sid: str) -> dict | None:
        """Return the parsed lock dict, or None if absent or corrupt."""
        path = self._lock_path(sid)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            # Ensure the fields we care about are present and typed correctly.
            float(data["heartbeat_ts"])
            str(data["owner"])
            return data
        except FileNotFoundError:
            return None
        except Exception:
            # Corrupt or unreadable lock — treat as reclaimable.
            logger.warning("session lock file for %r is corrupt; treating as reclaimable", sid)
            return None

    def _write_lock(self, sid: str, owner: str) -> None:
        """Write (or overwrite) the lock file with current pid and timestamp."""
        path = self._lock_path(sid)
        payload = {
            "owner": owner,
            "pid": os.getpid(),
            "heartbeat_ts": self._clock(),
            "state": "engine",
        }
        path.write_text(json.dumps(payload), encoding="utf-8")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def try_acquire(self, sid: str, owner: str) -> bool:
        """Attempt to acquire the lock for *sid* on behalf of *owner*.

        Returns True if the lock was acquired (or re-acquired by the same owner).
        Returns False if a fresh lock held by a different owner already exists.

        Reclaimable conditions: no lock file, stale lock, or same owner.
        Corrupt/unreadable lock files are always reclaimable.
        """
        # Create the lock directory if it does not exist yet.
        self._lock_dir.mkdir(parents=True, exist_ok=True)

        existing = self._read_lock(sid)

        if existing is None:
            # No lock (or corrupt): acquire freely.
            self._write_lock(sid, owner)
            return True

        # Same owner: re-acquire (idempotent).
        if existing.get("owner") == owner:
            self._write_lock(sid, owner)
            return True

        # Different owner: check staleness.
        if self._clock() - existing["heartbeat_ts"] > LOCK_STALE_S:
            # Lock has expired; reclaim it.
            logger.info(
                "reclaiming stale session lock for %r from owner %r",
                sid,
                existing.get("owner"),
            )
            self._write_lock(sid, owner)
            return True

        # Fresh lock held by someone else.
        return False

    def heartbeat(self, sid: str) -> None:
        """Refresh the heartbeat timestamp so the lock stays alive.

        Rewrites the lock file preserving owner/pid/state. No-op if the
        lock file is absent (e.g. was released by the owning process).
        """
        path = self._lock_path(sid)
        existing = self._read_lock(sid)
        if existing is None:
            # Lock was already released; nothing to refresh.
            return

        # Preserve all fields except heartbeat_ts.
        existing["heartbeat_ts"] = self._clock()
        path.write_text(json.dumps(existing), encoding="utf-8")

    def release(self, sid: str) -> None:
        """Delete the lock file for *sid*.

        Safe to call even if the lock file does not exist (idempotent).
        Logs a debug message rather than raising on a missing file.
        """
        path = self._lock_path(sid)
        try:
            path.unlink()
            logger.debug("released session lock for %r", sid)
        except FileNotFoundError:
            logger.debug("session lock for %r was already absent on release", sid)

    def is_stale(self, sid: str) -> bool:
        """Return True iff the lock file exists AND its heartbeat has expired.

        Returns False when no lock file exists (absence is not staleness).
        """
        existing = self._read_lock(sid)
        if existing is None:
            return False
        return self._clock() - existing["heartbeat_ts"] > LOCK_STALE_S
