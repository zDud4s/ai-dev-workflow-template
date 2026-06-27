"""Durable job persistence: the jobs.jsonl ledger + per-job cost extraction.

Replays/snapshots the shared ``JOBS`` registry to disk (byte-range locked so
concurrent writers don't interleave), aggregates ``type=result`` cost events
from chat logs, and prunes old log files. Builds on ``server.jobs_state`` (the
shared registry) and ``server.storage`` (the JSONL cache).
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

from server.jobs_state import JOBS, JOBS_LOCK, _JOB_RUNTIME_FIELDS
from server.paths import JOBS_PERSIST_FILE
from server.storage import _JSONL_CACHE, _JSONL_CACHE_LOCK, _bound_path_cache

_JOBS_PERSIST_LOCK = threading.Lock()

# Captured at import so the pytest write-guard in _persist_job can tell "the
# test redirected JOBS_PERSIST_FILE to a tmp path" from "still the real ledger".
_DEFAULT_JOBS_PERSIST_FILE = JOBS_PERSIST_FILE

_COST_EXTRACT_CACHE: dict[str, tuple[int, dict | None]] = {}
_COST_EXTRACT_LOCK = threading.Lock()


def _persist_job(job_id: str) -> None:
    """Append the current snapshot of ``JOBS[job_id]`` to the persistence
    ledger. Idempotent across calls — restoring on boot just replays the
    last snapshot per id."""
    # Defensive guard: under pytest, refuse to write the real ledger unless
    # the test explicitly monkeypatched JOBS_PERSIST_FILE to a tmp path.
    # Without this, tests that import serve and trigger _persist_job
    # transitively (without per-test monkeypatch) silently pollute the
    # developer's working .ai/ledgers/jobs.jsonl with hundreds of fake
    # entries per pytest run.
    if os.environ.get("PYTEST_CURRENT_TEST") and JOBS_PERSIST_FILE == _DEFAULT_JOBS_PERSIST_FILE:
        return
    with JOBS_LOCK:
        j = JOBS.get(job_id)
        if not j:
            return
        snapshot = {k: v for k, v in j.items() if k not in _JOB_RUNTIME_FIELDS}
    try:
        JOBS_PERSIST_FILE.parent.mkdir(parents=True, exist_ok=True)
        with _JOBS_PERSIST_LOCK:
            with JOBS_PERSIST_FILE.open("a", encoding="utf-8") as f:
                line = json.dumps(snapshot, default=str) + "\n"
                if sys.platform == "win32":
                    try:
                        import msvcrt
                        msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
                        try:
                            f.write(line)
                            f.flush()
                        finally:
                            try:
                                f.seek(0)
                                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                            except OSError as e:
                                # Unlock failed; the OS releases the byte-range
                                # lock on handle close anyway, but a recurring
                                # trace here points at a flaky fs/handle.
                                print(f"[serve] file unlock failed: {e}", flush=True)
                    except (ImportError, OSError):
                        # Lock acquisition failed (rare) - fall back to a plain
                        # write rather than dropping the event entirely.
                        f.write(line)
                else:
                    try:
                        import fcntl
                        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                        try:
                            f.write(line)
                        finally:
                            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                    except (ImportError, OSError):
                        f.write(line)
    except OSError as e:
        # Persistence is best-effort; never break the live pipeline. Log
        # so an operator who notices restarts losing job history has a
        # trail to follow (disk full, permissions, file locked, etc.).
        print(f"[serve] persist_job failed for {job_id}: {e}", flush=True)


def _update_job_cost(job_id: str, result_obj: dict) -> None:
    """Accumulate cost / duration / turns from a single ``type=result``
    event onto the live ``JOBS[job_id]["cost"]`` summary."""
    usd_raw = result_obj.get("total_cost_usd")
    if usd_raw is None:
        usd_raw = result_obj.get("cost_usd")
    try:
        usd = float(usd_raw) if usd_raw is not None else 0.0
    except (TypeError, ValueError):
        usd = 0.0
    try:
        dur = int(result_obj.get("duration_ms") or 0)
    except (TypeError, ValueError):
        dur = 0
    with JOBS_LOCK:
        j = JOBS.get(job_id)
        if not j:
            return
        cost = j.get("cost")
        if not isinstance(cost, dict):
            cost = {"turns": 0, "cost_usd": 0.0, "duration_ms": 0}
            j["cost"] = cost
        cost["turns"] = int(cost.get("turns", 0)) + 1
        cost["cost_usd"] = round(float(cost.get("cost_usd", 0.0)) + usd, 6)
        cost["duration_ms"] = int(cost.get("duration_ms", 0)) + dur


def _prune_old_logs(jobs_dir: Path, max_age_days: int = 14, keep_newest: int = 200) -> int:
    """Remove ``.log`` files in ``jobs_dir`` that are older than
    ``max_age_days`` OR beyond the ``keep_newest`` cap. Returns the
    number of files deleted. Best-effort: tolerates missing dir and
    individual unlink failures."""
    try:
        if not jobs_dir.is_dir():
            return 0
        entries = []
        for p in jobs_dir.glob("*.log"):
            try:
                entries.append((p.stat().st_mtime, p))
            except OSError:
                continue
    except OSError:
        return 0

    cutoff = time.time() - (max_age_days * 86400)
    deleted = 0
    # Sort newest first so the "keep newest N" rule is easy to apply.
    entries.sort(key=lambda x: x[0], reverse=True)
    for idx, (mtime, p) in enumerate(entries):
        too_old = mtime < cutoff
        over_cap = idx >= keep_newest
        if too_old or over_cap:
            try:
                p.unlink()
                deleted += 1
            except OSError as e:
                # File may be locked (Windows) or already gone (race
                # with another sweep). Log so a chronic leak is
                # discoverable rather than silent.
                print(f"[serve] log sweep unlink failed for {p}: {e}", flush=True)
    return deleted


def _extract_cost_from_log(log_path: Path) -> dict | None:
    """Scan a chat-mode log for ``{"type":"result", ...}`` events and
    aggregate cost / duration / turn count. Returns None if the file does
    not exist; an empty summary (turns=0) for files with no result events.
    """
    cache_key = str(log_path)
    try:
        path = Path(log_path)
        st = path.stat()
        if not path.is_file():
            return None
    except OSError:
        return None
    mtime_ns = st.st_mtime_ns
    with _COST_EXTRACT_LOCK:
        cached = _COST_EXTRACT_CACHE.get(cache_key)
        if cached is not None and cached[0] == mtime_ns:
            return cached[1]

    cost = 0.0
    duration = 0
    turns = 0
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    obj = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if obj.get("type") != "result":
                    continue
                usd = obj.get("total_cost_usd")
                if usd is None:
                    usd = obj.get("cost_usd")
                if usd is not None:
                    try:
                        cost += float(usd)
                    except (TypeError, ValueError):
                        pass
                dur = obj.get("duration_ms")
                if dur is not None:
                    try:
                        duration += int(dur)
                    except (TypeError, ValueError):
                        pass
                turns += 1
    except OSError:
        return None
    if turns == 0 and cost == 0.0 and duration == 0:
        result = {"turns": 0, "cost_usd": 0.0, "duration_ms": 0}
    else:
        result = {"turns": turns, "cost_usd": round(cost, 6), "duration_ms": duration}
    with _COST_EXTRACT_LOCK:
        _COST_EXTRACT_CACHE[cache_key] = (mtime_ns, result)
        _bound_path_cache(_COST_EXTRACT_CACHE)
    return result


def _load_persisted_jobs() -> None:
    """Replay the persistence ledger at server startup and seed ``JOBS``.

    Jobs serialised in a non-terminal state (queued/running/cancelling)
    are flagged as ``interrupted`` since their subprocess is dead — we
    cannot honestly call them running after a restart.
    """
    seen: dict[str, dict] = {}
    rows: list[dict] = []
    try:
        with JOBS_PERSIST_FILE.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if isinstance(obj, dict):
                    rows.append(obj)
    except FileNotFoundError:
        rows = []
    except OSError:
        rows = []
    row_count = len(rows)
    for obj in rows:
        jid = obj.get("id")
        if jid:
            # Copy the cached row: we hand the object straight to ``JOBS`` and
            # the loop below mutates ``status`` / ``error`` in place. Without a
            # copy those mutations would leak back into the JSONL cache and
            # poison every subsequent reader.
            seen[jid] = dict(obj)  # last snapshot per id wins

    if len(seen) < row_count:
        try:
            tmp = JOBS_PERSIST_FILE.with_suffix(".jsonl.tmp")
            with _JOBS_PERSIST_LOCK:
                with tmp.open("w", encoding="utf-8") as f:
                    for snap in seen.values():
                        f.write(json.dumps(snap, default=str) + "\n")
                os.replace(tmp, JOBS_PERSIST_FILE)
            with _JSONL_CACHE_LOCK:
                _JSONL_CACHE.pop(str(JOBS_PERSIST_FILE), None)
            print(f"[serve] compacted jobs.jsonl: {row_count} -> {len(seen)} rows", flush=True)
        except Exception as e:
            print(f"[serve] jobs.jsonl compaction failed: {e}", flush=True)

    with JOBS_LOCK:
        for obj in seen.values():
            if obj.get("status") in {"queued", "running", "cancelling"}:
                obj["status"] = "interrupted"
                obj.setdefault("error", "server restart")
            JOBS[obj["id"]] = obj
