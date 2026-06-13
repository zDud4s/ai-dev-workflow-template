"""Tests for the background job reaper added to serve.py.

The reaper closes a leak: reconciliation of dead-PID jobs and eviction of
finished entries used to run ONLY request-driven (GET /api/jobs). With no
browser polling, a job whose subprocess died sat in ``running`` forever,
pinning its proc handle + subscriber queues, and the JOBS dict was never
bounded. ``_job_reaper_loop`` now runs ``_job_reaper_tick`` on a cadence.

These tests exercise ``_job_reaper_tick`` directly (no loop, no sleep, no
real subprocess) so they stay fast and deterministic.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / ".ai" / "dashboard"))
import serve  # noqa: E402 — sys.path tweak above is the import setup


def _reset_jobs() -> None:
    with serve.JOBS_LOCK:
        serve.JOBS.clear()


def test_reaper_thread_is_registered_in_main() -> None:
    """The reaper must be wired as a daemon in main(), else it never runs."""
    src = (pathlib.Path(serve.__file__)).read_text(encoding="utf-8")
    assert "target=_job_reaper_loop" in src
    assert 'name="job-reaper"' in src


def test_reaper_tick_flips_dead_pid_job_to_failed(monkeypatch) -> None:
    """A running job whose PID is no longer alive is reconciled to failed."""
    _reset_jobs()
    try:
        with serve.JOBS_LOCK:
            serve.JOBS["dead-1"] = {
                "id": "dead-1", "kind": "orchestrate", "status": "running",
                "pid": 424242, "proc": None, "ended_at": None,
            }
        # Force the alive-probe to report the PID as dead.
        monkeypatch.setattr(serve, "_pid_is_alive", lambda _pid: False)
        # _persist_job touches disk for flipped jobs — stub it out.
        monkeypatch.setattr(serve, "_persist_job", lambda _jid: None)

        flipped = serve._job_reaper_tick()

        assert flipped == 1
        assert serve.JOBS["dead-1"]["status"] == "failed"
        assert serve.JOBS["dead-1"]["error"]
    finally:
        _reset_jobs()


def test_reaper_tick_leaves_live_pid_job_running(monkeypatch) -> None:
    """A job whose PID is still alive must NOT be flipped."""
    _reset_jobs()
    try:
        with serve.JOBS_LOCK:
            serve.JOBS["live-1"] = {
                "id": "live-1", "kind": "orchestrate", "status": "running",
                "pid": 111, "proc": None, "ended_at": None,
            }
        monkeypatch.setattr(serve, "_pid_is_alive", lambda _pid: True)
        monkeypatch.setattr(serve, "_persist_job", lambda _jid: None)

        flipped = serve._job_reaper_tick()

        assert flipped == 0
        assert serve.JOBS["live-1"]["status"] == "running"
    finally:
        _reset_jobs()


def test_reaper_tick_bounds_jobs_dict(monkeypatch) -> None:
    """Finished entries over JOBS_MAX are evicted oldest-first each tick."""
    _reset_jobs()
    try:
        monkeypatch.setattr(serve, "_persist_job", lambda _jid: None)
        over = serve.JOBS_MAX + 10
        with serve.JOBS_LOCK:
            for i in range(over):
                serve.JOBS[f"done-{i:03d}"] = {
                    "id": f"done-{i:03d}", "kind": "orchestrate",
                    "status": "done", "pid": None, "proc": None,
                    # Lexicographically ordered timestamps so eviction is
                    # deterministic (oldest = smallest -> dropped first).
                    "ended_at": f"2026-06-10T00:{i:02d}:00+00:00",
                }
        serve._job_reaper_tick()

        with serve.JOBS_LOCK:
            assert len(serve.JOBS) == serve.JOBS_MAX
            # The oldest entries were the ones evicted.
            assert "done-000" not in serve.JOBS
            assert f"done-{over - 1:03d}" in serve.JOBS
    finally:
        _reset_jobs()
