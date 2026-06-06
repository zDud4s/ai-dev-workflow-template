"""Tests for the session_lock module (TDD — written before the implementation).

Covers: acquire on clean dir, block on fresh lock, idempotent own-reacquire,
heartbeat update, staleness detection, stale-lock reclaim, and release.
"""

from __future__ import annotations

import json
import time

import pytest

# Import target module (will fail until implemented — that is intentional).
from session_lock import SessionLock, LOCK_STALE_S


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_clock(initial: float = 1_000_000.0):
    """Return a mutable-cell clock: a list[float] and a zero-arg callable."""
    cell = [initial]

    def clock() -> float:
        return cell[0]

    return cell, clock


def _read_lock(lock_dir, sid: str) -> dict:
    path = lock_dir / f"{sid}.lock"
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_acquire_on_clean_dir_returns_true(tmp_path):
    """try_acquire on a directory with no lock file must succeed and create it."""
    cell, clock = _make_clock()
    sl = SessionLock(tmp_path, clock=clock)

    result = sl.try_acquire("sess1", "owner-a")

    assert result is True
    data = _read_lock(tmp_path, "sess1")
    assert data["owner"] == "owner-a"
    assert data["state"] == "engine"
    assert "pid" in data
    assert "heartbeat_ts" in data


def test_acquire_blocked_by_fresh_lock_different_owner(tmp_path):
    """A second try_acquire with a different owner while the lock is fresh returns False."""
    cell, clock = _make_clock()
    sl = SessionLock(tmp_path, clock=clock)

    sl.try_acquire("sess1", "owner-a")

    # Same time — lock is fresh.
    result = sl.try_acquire("sess1", "owner-b")
    assert result is False


def test_acquire_idempotent_same_owner(tmp_path):
    """try_acquire for the same owner while fresh returns True (own-reacquire)."""
    cell, clock = _make_clock()
    sl = SessionLock(tmp_path, clock=clock)

    sl.try_acquire("sess1", "owner-a")
    result = sl.try_acquire("sess1", "owner-a")

    assert result is True


def test_heartbeat_updates_timestamp(tmp_path):
    """heartbeat must rewrite the lock file with a newer heartbeat_ts."""
    cell, clock = _make_clock(initial=1_000_000.0)
    sl = SessionLock(tmp_path, clock=clock)

    sl.try_acquire("sess1", "owner-a")
    before = _read_lock(tmp_path, "sess1")["heartbeat_ts"]

    # Advance time.
    cell[0] = 1_000_010.0
    sl.heartbeat("sess1")

    after = _read_lock(tmp_path, "sess1")["heartbeat_ts"]
    assert after > before
    assert after == pytest.approx(1_000_010.0)

    # Owner and state must be preserved.
    data = _read_lock(tmp_path, "sess1")
    assert data["owner"] == "owner-a"
    assert data["state"] == "engine"


def test_is_stale_false_immediately_after_acquire(tmp_path):
    """is_stale is False immediately after acquisition (heartbeat is current)."""
    cell, clock = _make_clock()
    sl = SessionLock(tmp_path, clock=clock)
    sl.try_acquire("sess1", "owner-a")

    assert sl.is_stale("sess1") is False


def test_is_stale_true_after_clock_advances(tmp_path):
    """is_stale is True once the clock has advanced beyond LOCK_STALE_S."""
    cell, clock = _make_clock(initial=1_000_000.0)
    sl = SessionLock(tmp_path, clock=clock)
    sl.try_acquire("sess1", "owner-a")

    cell[0] = 1_000_000.0 + LOCK_STALE_S + 1
    assert sl.is_stale("sess1") is True


def test_stale_lock_is_reclaimable(tmp_path):
    """After the lock becomes stale, a different owner can acquire it."""
    cell, clock = _make_clock(initial=1_000_000.0)
    sl = SessionLock(tmp_path, clock=clock)
    sl.try_acquire("sess1", "owner-a")

    cell[0] = 1_000_000.0 + LOCK_STALE_S + 1
    result = sl.try_acquire("sess1", "owner-b")

    assert result is True
    assert _read_lock(tmp_path, "sess1")["owner"] == "owner-b"


def test_release_removes_lock_file(tmp_path):
    """release must delete the lock file if it exists."""
    cell, clock = _make_clock()
    sl = SessionLock(tmp_path, clock=clock)
    sl.try_acquire("sess1", "owner-a")

    sl.release("sess1")

    assert not (tmp_path / "sess1.lock").exists()


def test_release_is_idempotent(tmp_path):
    """release must not raise if the lock file is already gone."""
    cell, clock = _make_clock()
    sl = SessionLock(tmp_path, clock=clock)
    sl.try_acquire("sess1", "owner-a")
    sl.release("sess1")

    # Should not raise.
    sl.release("sess1")


def test_is_stale_false_when_no_lock_file(tmp_path):
    """is_stale returns False if no lock file exists."""
    cell, clock = _make_clock()
    sl = SessionLock(tmp_path, clock=clock)
    assert sl.is_stale("sess1") is False


def test_try_acquire_after_release_succeeds(tmp_path):
    """After release, a fresh try_acquire (any owner) must succeed."""
    cell, clock = _make_clock()
    sl = SessionLock(tmp_path, clock=clock)
    sl.try_acquire("sess1", "owner-a")
    sl.release("sess1")

    result = sl.try_acquire("sess1", "owner-b")
    assert result is True


def test_corrupt_lock_file_is_reclaimable(tmp_path):
    """A corrupt (unreadable JSON) lock file is treated as reclaimable."""
    lock_path = tmp_path / "sess1.lock"
    lock_path.write_text("not json at all", encoding="utf-8")

    cell, clock = _make_clock()
    sl = SessionLock(tmp_path, clock=clock)

    result = sl.try_acquire("sess1", "owner-a")
    assert result is True


def test_heartbeat_is_noop_when_no_lock(tmp_path):
    """heartbeat must be a no-op when the lock file is absent (no exception)."""
    cell, clock = _make_clock()
    sl = SessionLock(tmp_path, clock=clock)

    # Must not raise.
    sl.heartbeat("sess1")


def test_lock_dir_created_if_missing(tmp_path):
    """try_acquire must create lock_dir if it does not exist yet."""
    lock_dir = tmp_path / "locks" / "sub"
    cell, clock = _make_clock()
    sl = SessionLock(lock_dir, clock=clock)

    result = sl.try_acquire("sess1", "owner-a")

    assert result is True
    assert lock_dir.is_dir()
