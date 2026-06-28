"""Background reaper for the shared job registry: PID-liveness probing,
reconciliation of dead subprocesses, and dict-size bounding.

Runs both on a fixed-cadence daemon loop and request-driven from
``GET /api/jobs``. Builds on ``server.jobs.state`` (the shared ``JOBS`` dict)
and persists flipped jobs through ``server.jobs.persistence``.
"""
from __future__ import annotations

import datetime as _dt
import os
import re
import subprocess
import time

from server.jobs.state import JOBS, JOBS_LOCK, JOBS_MAX
from server.jobs.persistence import _persist_job

# Matches a Windows ``tasklist /NH /FO CSV`` row, capturing the PID (2nd field).
_RE_TASKLIST_PID = re.compile(r'"[^"]*","(\d+)"')

_PID_ALIVE_CACHE: dict[int, tuple[float, bool]] = {}
_PID_ALIVE_TTL_SECONDS = 2.0


def _pid_is_alive(pid: int) -> bool:
    """Cross-platform PID liveness check. Returns False only when we have
    high confidence the PID is gone; for uncertain cases (permission
    errors, OS quirks) we return True so we don't spuriously fail jobs.

    Results are cached for ``_PID_ALIVE_TTL_SECONDS`` because callers like
    ``_reconcile_running_pids`` run on every ``GET /api/jobs`` and on
    Windows each miss spawns a ``tasklist`` subprocess. The TTL is small
    enough that a freshly-dead PID is still detected within ~2s."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    now = time.monotonic()
    cached = _PID_ALIVE_CACHE.get(pid)
    if cached is not None and (now - cached[0]) < _PID_ALIVE_TTL_SECONDS:
        return cached[1]
    if os.name == "nt":
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
                capture_output=True, text=True, timeout=2,
            )
        except (OSError, subprocess.TimeoutExpired):
            # Tasklist failure is ambiguous — don't cache, don't fail jobs.
            return True
        alive = f'"{pid}"' in (out.stdout or "")
    else:
        try:
            os.kill(pid, 0)
            alive = True
        except ProcessLookupError:
            alive = False
        except (PermissionError, OSError):
            return True  # ambiguous — don't cache
    _PID_ALIVE_CACHE[pid] = (now, alive)
    # Bound cache size; on a tiny dashboard this rarely matters but keep it
    # from growing unbounded if many distinct PIDs are queried.
    if len(_PID_ALIVE_CACHE) > 256:
        for stale_pid in [k for k, (ts, _) in _PID_ALIVE_CACHE.items() if (now - ts) >= _PID_ALIVE_TTL_SECONDS]:
            _PID_ALIVE_CACHE.pop(stale_pid, None)
    return alive


def _batch_prime_pid_cache_windows(pids: set[int]) -> None:
    """Prime ``_PID_ALIVE_CACHE`` for ``pids`` in a single ``tasklist`` call.

    The per-PID ``tasklist /FI "PID eq X"`` calls in ``_pid_is_alive`` cost
    ~100-300 ms each on Windows. Issuing one ``tasklist /NH /FO CSV`` and
    matching the requested PIDs against the full process snapshot turns N
    sequential subprocess spawns into a single one. Any tasklist failure
    (timeout, OS error, non-zero exit) leaves the cache untouched so
    ``_pid_is_alive`` falls back to its per-PID query — the worst case is
    the pre-batch behaviour."""
    if not pids:
        return
    try:
        out = subprocess.run(
            ["tasklist", "/NH", "/FO", "CSV"],
            capture_output=True, text=True, timeout=4,
        )
    except (OSError, subprocess.TimeoutExpired):
        return
    if out.returncode != 0:
        return
    live: set[int] = set()
    for line in (out.stdout or "").splitlines():
        m = _RE_TASKLIST_PID.match(line)
        if not m:
            continue
        try:
            live.add(int(m.group(1)))
        except ValueError:
            pass
    now = time.monotonic()
    for pid in pids:
        _PID_ALIVE_CACHE[pid] = (now, pid in live)


def _reconcile_running_pids() -> int:
    """Flip jobs marked ``running`` / ``queued`` / ``cancelling`` whose
    tracked PID is no longer alive into ``failed``. Jobs whose ``proc``
    handle is still ours and still reports no exit are left alone — the
    runner thread will close them out. Returns the number of jobs
    reconciled so the caller can log it."""
    flipped: list[str] = []
    with JOBS_LOCK:
        # Windows: prime the PID-alive cache with a single tasklist call so
        # the per-job _pid_is_alive() checks below all hit the cache rather
        # than spawning one subprocess per running job. With N jobs this
        # collapses ~N tasklist spawns into 1, saving ~(N-1)*150ms per
        # GET /api/jobs that triggers reconciliation.
        if os.name == "nt":
            now_pre = time.monotonic()
            to_query: set[int] = set()
            for j in JOBS.values():
                if j.get("status") not in {"running", "queued", "cancelling"}:
                    continue
                pid_raw = j.get("pid")
                if not pid_raw:
                    continue
                try:
                    pid_i = int(pid_raw)
                except (TypeError, ValueError):
                    continue
                if pid_i <= 0:
                    continue
                cached = _PID_ALIVE_CACHE.get(pid_i)
                if cached is None or (now_pre - cached[0]) >= _PID_ALIVE_TTL_SECONDS:
                    to_query.add(pid_i)
            _batch_prime_pid_cache_windows(to_query)
        for jid, j in list(JOBS.items()):
            if j.get("status") not in {"running", "queued", "cancelling"}:
                continue
            pid = j.get("pid")
            if not pid:
                continue
            proc = j.get("proc")
            if proc is not None:
                try:
                    rc = proc.poll()
                except OSError as e:
                    # poll() can raise on closed handles / interrupted
                    # syscalls on some platforms — fall through to the
                    # PID-alive probe below, but record the anomaly.
                    print(f"[serve] reaper poll() failed for job {jid}: {e}", flush=True)
                    rc = None
                if rc is None:
                    continue
            if _pid_is_alive(int(pid)):
                continue
            j["status"] = "failed"
            j["error"] = j.get("error") or "subprocess exited (dead PID detected)"
            j["ended_at"] = j.get("ended_at") or _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
            flipped.append(jid)
    for jid in flipped:
        _persist_job(jid)
    return len(flipped)


def _evict_old_jobs() -> None:
    """Keep JOBS dict bounded; remove oldest finished entries when over cap."""
    with JOBS_LOCK:
        if len(JOBS) <= JOBS_MAX:
            return
        finished = [
            (jid, j) for jid, j in JOBS.items()
            if j["status"] in {"done", "failed", "cancelled"}
        ]
        finished.sort(key=lambda x: x[1].get("ended_at") or "")
        for jid, _j in finished[: len(JOBS) - JOBS_MAX]:
            JOBS.pop(jid, None)


# How often the background reaper reconciles dead PIDs and bounds the JOBS
# dict. Reconciliation also runs request-driven on GET /api/jobs, but that
# only fires when a browser is polling. With no client attached, a job whose
# subprocess died (or was killed out-of-band) would otherwise sit in
# ``running`` forever — pinning its proc handle + subscriber queues — and
# finished entries would never be evicted, so the dict could grow unbounded
# between the rare HTTP hits. 30s keeps the leak window small without adding
# meaningful load (one tasklist batch per tick on Windows).
JOB_REAP_INTERVAL_S = 30.0


def _job_reaper_tick() -> int:
    """One reaper pass: flip dead-PID jobs to failed and bound the dict.

    Split out from :func:`_job_reaper_loop` so it can be exercised in tests
    without spinning the ``while True`` loop. Returns the number of jobs
    reconciled this pass."""
    flipped = _reconcile_running_pids()
    _evict_old_jobs()
    return flipped


def _job_reaper_loop() -> None:
    """Background daemon: run :func:`_job_reaper_tick` on a fixed cadence so
    dead job subprocesses are reaped and the JOBS dict stays bounded even when
    no browser is polling ``/api/jobs``. One bad tick must not kill the loop."""
    while True:
        time.sleep(JOB_REAP_INTERVAL_S)
        try:
            n = _job_reaper_tick()
            if n:
                print(f"[serve] job reaper: reconciled {n} dead job(s)", flush=True)
        except Exception as e:  # noqa: BLE001 — log and continue so the loop survives
            print(f"[serve] job reaper tick failed: {e}", flush=True)
