"""Local dashboard server for the AI workflow.

Run from repo root:
    python .ai/dashboard/serve.py

Then open http://localhost:8765/.ai/dashboard/

Serves the whole repo as static files (read-only) plus a small JSON API:

    GET  /api/list?path=.ai/plans              ->  {"entries": [...]}
    POST /api/memory       {topic, fact}        ->  appends to .ai/memory.md
    POST /api/decisions    {date, decision,
                            why, consequence,
                            revisit}            ->  appends to .ai/decisions.md
    POST /api/events/clear                      ->  truncates .ai/events.jsonl
    POST /api/models/dispatch_mode {mode}       ->  flips dispatch_mode in
                                                    .ai/models.yaml

    POST /api/jobs            {kind, task}      ->  spawn an orchestrate /
                                                    planner subprocess
    GET  /api/jobs                              ->  list recent jobs
    GET  /api/jobs/<id>?tail=N                  ->  job details + last N log lines
    GET  /api/jobs/<id>/stream                  ->  SSE: streams new log bytes
                                                    as the subprocess writes them
    POST /api/jobs/<id>/input  {text}           ->  write a line to the
                                                    subprocess stdin
    POST /api/jobs/<id>/cancel                  ->  terminate the subprocess

    POST /api/ptys           {shell, cwd?,      ->  spawn a real shell session
                              cols?, rows?}         in a PTY (cross-platform)
    GET  /api/ptys                              ->  list active PTY sessions
    GET  /api/ptys/<id>                         ->  PTY session metadata
    GET  /api/ptys/<id>/io                      ->  WebSocket: bidirectional
                                                    byte stream + resize control
    POST /api/ptys/<id>/kill                    ->  terminate the shell

Optional dependency (Windows only): ``pywinpty>=2.0`` for real PTY support
via ConPTY. POSIX uses the stdlib ``pty`` module. /api/ptys returns 503
on Windows when pywinpty isn't installed.
"""
from __future__ import annotations

import base64
import datetime as _dt
import hashlib
import http.server
import json
import os
import queue as _stdqueue
import re
import shutil
import socket
import socketserver
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import uuid
from collections import deque
from pathlib import Path

# PTY helper (cross-platform). The Terminals page can spawn real shell
# sessions in addition to the existing chat-claude / chat-codex panes;
# this module wraps POSIX `pty.fork` and Windows `pywinpty.PtyProcess`
# behind one interface.
import pty_session as _pty_session  # noqa: E402 — sibling module

PORT = int(os.environ.get("DASHBOARD_PORT", "8765"))
# The actually-bound port. Diverges from PORT when main()'s dynamic-port
# fallback picks a different candidate (e.g. another project already holds
# PORT). CSRF allowlist and /api/system/info read this so a second
# concurrent dashboard validates Origins against its real port instead of
# the stale configured one.
BOUND_PORT = PORT
ROOT = Path(__file__).resolve().parents[2]  # repo root
_SERVER_STARTED_AT = time.time()
# Source of truth for the workflow template. /api/workflow/check and
# /api/workflow/update clone this fresh on each call so a one-click update from
# the dashboard always reflects the latest upstream version. Override via
# AI_WORKFLOW_TEMPLATE_URL (useful for forks or for testing against a local
# bare repo via file:// URL).
WORKFLOW_TEMPLATE_URL = os.environ.get(
    "AI_WORKFLOW_TEMPLATE_URL",
    "https://github.com/zDud4s/ai-dev-workflow-template.git",
)
# One-line file with the upstream sha that produced the currently-installed
# workflow files. Written by /api/workflow/update after a successful run; read
# by /api/workflow/check to compute "ahead/behind in commits". Absent on
# projects that haven't been updated through the dashboard yet.
WORKFLOW_VERSION_FILE = ROOT / ".ai" / "workflow" / ".version"
JOBS_DIR = ROOT / ".ai" / "dashboard" / "jobs"
# Append-only ledger of job snapshots — every status transition adds one
# JSON line so the dashboard can rebuild the JOBS dict after a server
# restart. Last snapshot per ``id`` wins. Tests override this with a tmp
# path via monkeypatch.
JOBS_PERSIST_FILE = ROOT / ".ai" / "dashboard" / "jobs.jsonl"
# Append-only telemetry stream written by .ai/dashboard/log_event.py (a
# PostToolUse hook). The /api/timeline endpoint aggregates phase_dispatch
# events from this file. Tests override it via monkeypatch.
EVENTS_FILE = ROOT / ".ai" / "events.jsonl"
# Append-only metrics stream written by the orchestrate skill, one line per
# dispatched phase. Powers the /api/auto-select ranking. See the orchestrate
# skill "## Metrics logging" section for the schema.
METRICS_FILE = ROOT / ".ai" / "metrics.jsonl"
# Append-only ledger of per-(skill, job) invocations. The auto skill-improver
# (Phase 2+) reads this to decide which skills need adapting. One line per
# unique skill invoked in a job; the entry-skill of orchestrate/plan jobs is
# always credited even when the log isn't stream-json.
SKILL_METRICS_FILE = ROOT / ".ai" / "dashboard" / "skill_metrics.jsonl"
# Auto-improver storage. Proposals are dropped here as JSON + .old.md + .new.md
# triples so the dashboard can render a diff and the user can Accept / Reject.
# Backups of overwritten SKILL.md content go to SKILL_BACKUPS_DIR; every
# decision (auto-apply, manual-apply, reject, skip) is appended to the
# ledger for forensic readability.
SKILL_PROPOSALS_DIR  = ROOT / ".ai" / "dashboard" / "skill_proposals"
SKILL_BACKUPS_DIR    = ROOT / ".ai" / "dashboard" / "skill_backups"
IMPROVEMENTS_LEDGER  = ROOT / ".ai" / "dashboard" / "improvements.jsonl"
_JOBS_PERSIST_LOCK = threading.Lock()
_IMPROVEMENTS_LEDGER_LOCK = threading.Lock()
_SKILL_METRICS_LOCK = threading.Lock()
# Serialises /api/workflow/update so two concurrent clients can't both spawn
# update-workflow.sh against the same tree at the same time (interleaved file
# writes corrupt the workflow core). Non-blocking acquire — second caller gets
# 409.
_WORKFLOW_UPDATE_LOCK = threading.Lock()
# Caps concurrent /api/suggestions/<id>/draft + /api/agents/suggest requests.
# Both endpoints spawn long-running `claude -p` / `codex` subprocesses
# (timeout_seconds, default 120s) on the request thread; without a cap a
# handful of concurrent clients can exhaust the server thread pool. Shared
# between both endpoints because they consume the same LLM CLI binary.
_SUGGESTION_SEMAPHORE = threading.Semaphore(2)
# Hard cap on the request-thread wall-clock for /api/suggestions/<id>/draft
# and /api/agents/suggest. ``cfg["timeout_seconds"]`` can be set as high as
# 3600s (see _IMPROVER_TIMEOUT_BOUNDS); even with the semaphore cap above,
# a 1-hour subprocess pinning a request thread + browser tab connection is
# a trivial DoS vector. 60s is well above any healthy LLM response time
# yet bounded so a misbehaving CLI can't park the dashboard.
_SUGGESTION_HTTP_TIMEOUT_MAX = 60
# Agent suggestions storage. Mirrors the skill-proposal layout but for the
# agent-improver "Suggest-new-agents" mode: one .json payload + one .body.md
# per proposal. Accept writes a real file at .claude/agents/<slug>.md;
# reject just marks status="rejected" and leaves the proposal on disk.
AGENT_PROPOSALS_DIR  = ROOT / ".ai" / "dashboard" / "agent_proposals"
# Defaults used when models.yaml has no `improver:` block. The improver
# only edits skills under PROJECT (.claude/skills/) — global skills are
# never modified.
_IMPROVER_DEFAULTS = {
    "enabled": True,
    "tool": "claude",
    "model": "claude-haiku-4-5",
    "small_change_max_lines": 6,    # auto-apply threshold (added+removed lines)
    "min_interval_seconds": 300,    # per-skill throttle
    "timeout_seconds": 120,         # subprocess wall-clock cap
    # Auto-revert safety net: if a skill that received an `applied` proposal
    # later shows success-rate regression by >= ``revert_margin`` over the
    # next ``revert_after_n_uses`` invocations, restore the .bak silently.
    "revert_after_n_uses": 5,
    "revert_margin": 0.2,
}

# Maximum size of a JSON request body. Anything larger gets a 413 before we
# even allocate a buffer — a single multi-MB POST against an endpoint that
# expects ``{"mode": "..."}`` is a trivial DoS otherwise. 1 MiB is well above
# any legitimate payload the dashboard sends (the largest is the chat
# composer with inlined files, which is capped client-side at ~256 KB).
MAX_JSON_BODY = 1024 * 1024  # 1 MiB

# Hard upper bound on a single Server-Sent Events session, regardless of
# whether the subscriber is idle or not. ``_handle_job_stream`` already
# bails on a 4-minute idle window, but a chatty job could keep a single
# connection open indefinitely otherwise — and the SSE response holds a
# request thread, a queue subscriber slot, and a TCP connection for the
# whole lifetime. Clients reconnect transparently, so a forced rotation
# is observationally invisible.
MAX_SSE_SESSION_S = 1800  # 30 minutes

# Directories the fallback ``ROOT.rglob("*")`` walk in ``_handle_files_list``
# must not descend into. Without this, the autocomplete endpoint walks the
# entire repo on every keystroke when ``git ls-files`` is unavailable —
# slow on large repos and leaks dotfile paths (``.git/objects/*``,
# ``node_modules/**``, ``.venv/**``) into the suggestion list.
SKIP_DIRS = frozenset({
    ".git", "node_modules", "__pycache__", ".pytest_cache",
    ".venv", "venv", ".tox", ".mypy_cache", "tmp",
})

# Fields that exist only at runtime inside the JOBS dict and must NOT be
# serialised to disk (they are either not JSON-encodable or meaningless
# after the subprocess dies).
_JOB_RUNTIME_FIELDS = frozenset({"proc", "subscribers", "stdin_lock"})

# Terminal job statuses — used to know when scanned log-file cost can be
# memoised back onto the job entry (cost can't change once the subprocess
# is dead).
_TERMINAL_JOB_STATUSES = frozenset({"done", "failed", "cancelled", "interrupted"})


# Generic mtime-invalidated cache for append-only JSONL ledgers (jobs,
# improvements, skill metrics, ...). Every list/aggregate endpoint used to
# re-read and re-parse its whole ledger on every call — at ~100MB the
# dashboard became unresponsive. The cache returns the same parsed ``list``
# object until the file's mtime changes, so a cache hit is a single
# ``stat()`` + dict lookup. The cache lock guards only the dict; the actual
# read happens between two lock acquisitions on purpose so a slow disk
# can't block other readers. Two concurrent first-callers may parse the
# same payload twice; the second write just replaces the first with an
# identical value, which is harmless.
#
# Write-side locks on the ledgers (``_JOBS_PERSIST_LOCK``,
# ``_IMPROVEMENTS_LEDGER_LOCK``, ``_SKILL_METRICS_LOCK``) are independent —
# we never hold the cache lock while opening the file, so there is no
# deadlock path.
_JSONL_CACHE: dict[str, tuple[int, list[dict]]] = {}
_JSONL_CACHE_LOCK = threading.Lock()


def _load_jsonl_cached(path: Path) -> list[dict]:
    """Return parsed rows from a JSONL file, cached until ``mtime`` changes.

    Behaviour matches the prior hand-rolled readers: blank lines are skipped,
    decode errors fall back to the unicode replacement character, and
    ``json.JSONDecodeError`` on individual lines is silently swallowed so a
    single corrupt entry can't poison the whole endpoint. Returns ``[]`` when
    the file does not exist (callers used to special-case this themselves).
    """
    try:
        st = path.stat()
    except FileNotFoundError:
        return []
    except OSError:
        # Permission errors etc. — behave as if empty so a transient FS hiccup
        # doesn't surface as a 500. Endpoints that need to know the difference
        # already wrap their own file ops in try/except above this layer.
        return []
    key = str(path)
    with _JSONL_CACHE_LOCK:
        cached = _JSONL_CACHE.get(key)
        if cached is not None and cached[0] == st.st_mtime_ns:
            return cached[1]
    # Read outside the lock — slow I/O must not block other cache readers.
    # Two concurrent first-callers will parse twice; both writes produce the
    # same list so the race is benign.
    rows: list[dict] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except (json.JSONDecodeError, ValueError):
                    # Mirror the prior hand-rolled behaviour: skip malformed
                    # rows silently rather than failing the whole endpoint.
                    continue
    except OSError:
        # If the file vanished or became unreadable between ``stat()`` and
        # ``open()`` (a rare race during rotation), treat as empty. Don't
        # cache the empty result — the next call will retry the stat.
        return []
    with _JSONL_CACHE_LOCK:
        _JSONL_CACHE[key] = (st.st_mtime_ns, rows)
    return rows


def _write_text_lf(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` with LF line endings, regardless of platform.

    Python's ``Path.write_text`` defaults to ``newline=None`` which translates
    ``\\n`` to the OS line terminator (``\\r\\n`` on Windows). The repo's
    ``.gitattributes`` pins ``*.yaml`` / ``*.md`` to ``eol=lf``, so writing
    those files through the dashboard previously produced spurious
    ``"CRLF will be replaced by LF"`` git warnings on Windows."""
    path.write_text(text, encoding="utf-8", newline="\n")


def _persist_job(job_id: str) -> None:
    """Append the current snapshot of ``JOBS[job_id]`` to the persistence
    ledger. Idempotent across calls — restoring on boot just replays the
    last snapshot per id."""
    with JOBS_LOCK:
        j = JOBS.get(job_id)
        if not j:
            return
        snapshot = {k: v for k, v in j.items() if k not in _JOB_RUNTIME_FIELDS}
    try:
        JOBS_PERSIST_FILE.parent.mkdir(parents=True, exist_ok=True)
        with _JOBS_PERSIST_LOCK:
            with JOBS_PERSIST_FILE.open("a", encoding="utf-8") as f:
                f.write(json.dumps(snapshot, default=str) + "\n")
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
    try:
        if not Path(log_path).is_file():
            return None
    except OSError:
        return None
    cost = 0.0
    duration = 0
    turns = 0
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
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
        return {"turns": 0, "cost_usd": 0.0, "duration_ms": 0}
    return {"turns": turns, "cost_usd": round(cost, 6), "duration_ms": duration}


def _load_persisted_jobs() -> None:
    """Replay the persistence ledger at server startup and seed ``JOBS``.

    Jobs serialised in a non-terminal state (queued/running/cancelling)
    are flagged as ``interrupted`` since their subprocess is dead — we
    cannot honestly call them running after a restart.
    """
    seen: dict[str, dict] = {}
    for obj in _load_jsonl_cached(JOBS_PERSIST_FILE):
        jid = obj.get("id")
        if jid:
            # Copy the cached row: we hand the object straight to ``JOBS`` and
            # the loop below mutates ``status`` / ``error`` in place. Without a
            # copy those mutations would leak back into the JSONL cache and
            # poison every subsequent reader.
            seen[jid] = dict(obj)  # last snapshot per id wins

    with JOBS_LOCK:
        for obj in seen.values():
            if obj.get("status") in {"queued", "running", "cancelling"}:
                obj["status"] = "interrupted"
                obj.setdefault("error", "server restart")
            JOBS[obj["id"]] = obj

# IDE transcript mirror: Claude Code (the VSCode/Cursor extension) writes a
# JSONL transcript of every session to ~/.claude/projects/<encoded-cwd>/<sid>.jsonl
# so the dashboard can tail those files and surface ANY ongoing IDE chat as
# a read-only terminal pane. Tests override this with a tmp tree.
_CLAUDE_PROJECTS_ROOT_OVERRIDE: Path | None = None


def _claude_projects_root() -> Path | None:
    if _CLAUDE_PROJECTS_ROOT_OVERRIDE is not None:
        return _CLAUDE_PROJECTS_ROOT_OVERRIDE
    home = Path.home()
    candidate = home / ".claude" / "projects"
    return candidate if candidate.is_dir() else None


def _transcripts_dir_for_cwd(cwd: Path) -> Path | None:
    """Pick the ``~/.claude/projects/<slug>`` directory matching ``cwd``.

    Claude Code's slug rule (observed): replace ``:``, ``/``, ``\\`` and
    spaces with ``-``. We try a few common variants because case-folding
    of the drive letter has been seen both ways across machines."""
    root = _claude_projects_root()
    if root is None:
        return None
    s = str(cwd)
    slug_lower = (s[0].lower() + s[1:]).replace(":", "-").replace("\\", "-").replace("/", "-").replace(" ", "-")
    slug_upper = (s[0].upper() + s[1:]).replace(":", "-").replace("\\", "-").replace("/", "-").replace(" ", "-")
    for slug in (slug_lower, slug_upper, slug_lower.lower()):
        p = root / slug
        if p.is_dir():
            return p
    # Last-ditch: scan all subdirs and check if any transcript records this cwd.
    target = str(cwd).lower()
    for sub in root.iterdir():
        if not sub.is_dir():
            continue
        # Peek the first non-empty line of any jsonl file looking for a cwd match.
        for f in sub.glob("*.jsonl"):
            try:
                with f.open("r", encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            obj = json.loads(line)
                        except (json.JSONDecodeError, ValueError):
                            continue
                        if str(obj.get("cwd") or "").lower() == target:
                            return sub
                        break  # only peek first record per file
            except OSError:
                continue
    return None


# Codex stores per-session rollouts here. Tests override via
# ``_CODEX_SESSIONS_ROOT_OVERRIDE``.
_CODEX_SESSIONS_ROOT_OVERRIDE: Path | None = None


def _codex_sessions_root() -> Path | None:
    if _CODEX_SESSIONS_ROOT_OVERRIDE is not None:
        return _CODEX_SESSIONS_ROOT_OVERRIDE
    p = Path.home() / ".codex" / "sessions"
    return p if p.is_dir() else None


def _parse_iso_ts(s):
    """Return a timezone-aware UTC datetime, or None on failure."""
    if not isinstance(s, str) or not s:
        return None
    raw = s[:-1] + "+00:00" if s.endswith("Z") else s
    try:
        dt = _dt.datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    return dt.astimezone(_dt.timezone.utc)


def _normalise_path_for_match(s: str) -> str:
    return (s or "").lower().replace("\\", "/").rstrip("/")


# Cached aggregator result (re-read after _USAGE_TTL_SECONDS) so the overview
# can refresh without re-scanning ~/.codex/sessions on every reload.
_USAGE_CACHE: dict = {"at": 0.0, "data": None}
_USAGE_TTL_SECONDS = 30.0


def _aggregate_claude_usage(repo_root: Path, now: _dt.datetime) -> dict:
    """Per-model + windowed token totals from this repo's Claude transcripts.

    Reads ``message.usage`` plus ``message.model`` and ``timestamp`` from each
    line in ``~/.claude/projects/<slug>/*.jsonl`` and accumulates totals by
    model for three windows: ``all``, last 5 hours, last 7 days. Returns
    zeros if there is no transcripts directory for this repo."""
    cutoff_5h = now - _dt.timedelta(hours=5)
    cutoff_7d = now - _dt.timedelta(days=7)

    def empty():
        return {
            "total": 0, "input": 0, "output": 0,
            "cache_creation": 0, "cache_read": 0,
            "messages": 0, "by_model": {},
        }

    out = {"all": empty(), "5h": empty(), "7d": empty(), "transcripts": 0}

    def add(win, model, usage):
        inp = int(usage.get("input_tokens") or 0)
        outp = int(usage.get("output_tokens") or 0)
        cc = int(usage.get("cache_creation_input_tokens") or 0)
        cr = int(usage.get("cache_read_input_tokens") or 0)
        tot = inp + outp + cc + cr
        win["total"] += tot
        win["input"] += inp
        win["output"] += outp
        win["cache_creation"] += cc
        win["cache_read"] += cr
        win["messages"] += 1
        bm = win["by_model"].setdefault(model, {"total": 0, "messages": 0})
        bm["total"] += tot
        bm["messages"] += 1

    tdir = _transcripts_dir_for_cwd(repo_root)
    if tdir is None:
        return out
    try:
        files = list(tdir.glob("*.jsonl"))
    except OSError:
        return out
    for p in files:
        out["transcripts"] += 1
        try:
            with p.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line.startswith("{"):
                        continue
                    try:
                        obj = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    msg = obj.get("message")
                    if not isinstance(msg, dict):
                        continue
                    usage = msg.get("usage")
                    if not isinstance(usage, dict):
                        continue
                    model = msg.get("model") or "unknown"
                    ts = _parse_iso_ts(obj.get("timestamp"))
                    try:
                        add(out["all"], model, usage)
                        if ts is not None and ts >= cutoff_5h:
                            add(out["5h"], model, usage)
                        if ts is not None and ts >= cutoff_7d:
                            add(out["7d"], model, usage)
                    except (TypeError, ValueError):
                        continue
        except OSError:
            continue
    _fill_percent_shares(out)
    return out


def _aggregate_codex_usage(repo_root: Path, now: _dt.datetime) -> dict:
    """Per-model + windowed token totals from Codex session rollouts for this
    repo, plus the most recent account-wide ``rate_limits`` block.

    Codex rollouts at ``~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`` carry
    ``turn_context`` events (which expose ``payload.model`` for each turn) and
    ``event_msg`` events with ``payload.type == "token_count"`` that bundle
    both ``info.last_token_usage`` (per-turn) and ``rate_limits`` (5h primary
    + weekly secondary, account-wide). Per-turn totals are filtered by
    matching ``session_meta.payload.cwd`` to ``repo_root`` so the overview
    only counts tokens spent on this project. Rate-limit percentages are
    account-wide (Codex does not expose per-project quotas)."""
    cutoff_5h = now - _dt.timedelta(hours=5)
    cutoff_7d = now - _dt.timedelta(days=7)

    def empty():
        return {
            "total": 0, "input": 0, "cached_input": 0,
            "output": 0, "reasoning_output": 0,
            "turns": 0, "by_model": {},
        }

    out = {
        "all": empty(), "5h": empty(), "7d": empty(),
        "sessions": 0, "rate_limits": None, "matched_sessions": 0,
    }

    root = _codex_sessions_root()
    if root is None:
        return out

    target = _normalise_path_for_match(str(repo_root))
    latest_rl: tuple[_dt.datetime, dict] | None = None  # account-wide

    try:
        files = list(root.rglob("rollout-*.jsonl"))
    except OSError:
        return out

    # Cap traversal at the N most recently modified rollouts. ``rglob`` walks
    # ALL Codex sessions on the machine (across every repo); the unfiltered
    # set can be ~150MB and hundreds of files. Older sessions don't contribute
    # meaningfully to "recent usage", and the 30s TTL cache means the first
    # call after expiry would otherwise stall the overview. Sort by mtime
    # descending and keep the most recent slice.
    _CODEX_SESSION_FILE_CAP = 100
    if len(files) > _CODEX_SESSION_FILE_CAP:
        try:
            files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            # If stat() fails on some entries, fall back to the unsorted
            # truncation; better than a 500 on the overview endpoint.
            pass
        files = files[:_CODEX_SESSION_FILE_CAP]

    def add(win, model, last):
        inp = int(last.get("input_tokens") or 0)
        cinp = int(last.get("cached_input_tokens") or 0)
        outp = int(last.get("output_tokens") or 0)
        rout = int(last.get("reasoning_output_tokens") or 0)
        tot = int(last.get("total_tokens") or 0) or (inp + outp + rout)
        win["total"] += tot
        win["input"] += inp
        win["cached_input"] += cinp
        win["output"] += outp
        win["reasoning_output"] += rout
        win["turns"] += 1
        bm = win["by_model"].setdefault(model, {"total": 0, "turns": 0})
        bm["total"] += tot
        bm["turns"] += 1

    for p in files:
        out["sessions"] += 1
        cwd_matches = False
        current_model = "unknown"
        try:
            with p.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line.startswith("{"):
                        continue
                    try:
                        obj = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    t = obj.get("type")
                    payload = obj.get("payload") or {}
                    if t == "session_meta":
                        cwd = _normalise_path_for_match(payload.get("cwd") or "")
                        if cwd == target:
                            cwd_matches = True
                    elif t == "turn_context":
                        m = payload.get("model")
                        if isinstance(m, str) and m:
                            current_model = m
                        cwd = _normalise_path_for_match(payload.get("cwd") or "")
                        if cwd and cwd == target:
                            cwd_matches = True
                    elif t == "event_msg" and payload.get("type") == "token_count":
                        ts = _parse_iso_ts(obj.get("timestamp"))
                        rl = payload.get("rate_limits")
                        if isinstance(rl, dict) and ts is not None:
                            # Once the 5h quota is exhausted Codex switches to an
                            # empty rate_limits payload (limit_id="premium",
                            # primary/secondary both null). Skip those so they
                            # don't clobber the last healthy snapshot — otherwise
                            # the UI sticks on "—" until the next real event.
                            has_payload = isinstance(rl.get("primary"), dict) or isinstance(rl.get("secondary"), dict)
                            if has_payload and (latest_rl is None or ts > latest_rl[0]):
                                latest_rl = (ts, rl)
                        if not cwd_matches:
                            continue
                        info = payload.get("info") or {}
                        last = info.get("last_token_usage")
                        if not isinstance(last, dict):
                            continue
                        try:
                            add(out["all"], current_model, last)
                            if ts is not None and ts >= cutoff_5h:
                                add(out["5h"], current_model, last)
                            if ts is not None and ts >= cutoff_7d:
                                add(out["7d"], current_model, last)
                        except (TypeError, ValueError):
                            continue
        except OSError:
            continue
        if cwd_matches:
            out["matched_sessions"] += 1

    if latest_rl is not None:
        ts, rl = latest_rl
        # Each window carries its own resets_at (epoch seconds). If that
        # moment is in the past, the window has rolled over since the
        # snapshot was recorded and the percent is no longer current —
        # mark it stale so the UI can show that rather than pretending the
        # old number is live (Codex's IDE pulls live API data; we can't
        # from local rollouts).
        now_ts = now.timestamp()
        def _annotate_stale(win):
            if not isinstance(win, dict):
                return win
            ra = win.get("resets_at")
            if isinstance(ra, (int, float)) and ra < now_ts:
                w = dict(win)
                w["stale"] = True
                return w
            return win
        out["rate_limits"] = {
            "primary": _annotate_stale(rl.get("primary")),
            "secondary": _annotate_stale(rl.get("secondary")),
            "plan_type": rl.get("plan_type"),
            "last_event_at": ts.isoformat(),
        }
    _fill_percent_shares(out)
    return out


def _fill_percent_shares(out: dict) -> None:
    """Annotate each ``by_model[m]`` entry with a ``percent`` share of the
    window total (rounded to one decimal). Mutates in place."""
    for w in ("all", "5h", "7d"):
        win = out.get(w)
        if not isinstance(win, dict):
            continue
        total = win.get("total") or 0
        bm = win.get("by_model") or {}
        if total > 0:
            for info in bm.values():
                info["percent"] = round(100.0 * info["total"] / total, 1)
        else:
            for info in bm.values():
                info["percent"] = 0.0


# Claude Code stores its OAuth token here when the user is signed into a
# subscription plan. Tests override via ``_CLAUDE_CREDENTIALS_PATH_OVERRIDE``.
_CLAUDE_CREDENTIALS_PATH_OVERRIDE: Path | None = None
_CLAUDE_USAGE_CACHE: dict = {"at": 0.0, "data": None}
_CLAUDE_USAGE_TTL_SECONDS = 60.0
_CLAUDE_OAUTH_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"


def _read_claude_oauth_token() -> tuple[str | None, str | None]:
    """Read the local Claude Code OAuth access token plus subscription tier.

    Returns ``(token, tier)`` or ``(None, None)`` when the credentials file
    is missing, malformed, or the token has expired. ``tier`` is the
    ``rateLimitTier`` (e.g. ``default_claude_max_5x``); useful for UI hints
    but not required for the API call itself."""
    path = _CLAUDE_CREDENTIALS_PATH_OVERRIDE or (Path.home() / ".claude" / ".credentials.json")
    try:
        creds = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None, None
    oauth = creds.get("claudeAiOauth")
    if not isinstance(oauth, dict):
        return None, None
    tok = oauth.get("accessToken")
    if not isinstance(tok, str) or not tok:
        return None, None
    exp = oauth.get("expiresAt")
    # expiresAt is milliseconds since epoch in this file.
    if isinstance(exp, (int, float)) and exp <= time.time() * 1000:
        return None, oauth.get("rateLimitTier")
    return tok, oauth.get("rateLimitTier")


def _fetch_claude_oauth_usage() -> dict:
    """Fetch real Claude session/weekly utilization from the OAuth usage
    endpoint Claude Code itself uses for ``/usage``. Caches the response for
    ``_CLAUDE_USAGE_TTL_SECONDS`` so a busy overview reload doesn't hammer
    the API. The token never leaves this process.

    Returns ``{"available": bool, "data"?: {...}, "error"?: str, "tier"?: str}``.
    Network errors are swallowed and surfaced as ``available=False``."""
    import urllib.error
    import urllib.request

    now_mono = time.monotonic()
    cached = _CLAUDE_USAGE_CACHE["data"]
    if cached is not None and (now_mono - _CLAUDE_USAGE_CACHE["at"]) < _CLAUDE_USAGE_TTL_SECONDS:
        return cached

    tok, tier = _read_claude_oauth_token()
    if not tok:
        result = {
            "available": False,
            "error": "no claude oauth token (~/.claude/.credentials.json missing or expired)",
            "tier": tier,
        }
    else:
        req = urllib.request.Request(
            _CLAUDE_OAUTH_USAGE_URL,
            headers={
                "Authorization": f"Bearer {tok}",
                "User-Agent": "ai-workflow-dashboard/0.1 (claude-code-compatible)",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=8) as r:  # nosec: B310 - constant trusted URL
                body = r.read().decode("utf-8")
                data = json.loads(body)
            result = {"available": True, "data": data, "tier": tier}
        except urllib.error.HTTPError as e:
            result = {"available": False, "error": f"http {e.code}", "tier": tier}
        except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError) as e:
            result = {"available": False, "error": str(e)[:160], "tier": tier}

    _CLAUDE_USAGE_CACHE["data"] = result
    _CLAUDE_USAGE_CACHE["at"] = now_mono
    return result


def _aggregate_project_token_usage() -> dict:
    """Combined Claude + Codex token usage for this repo, with per-model and
    per-window breakdowns. Cached for ``_USAGE_TTL_SECONDS`` to keep the
    overview reload cheap (Codex scans can read ~150MB across all rollouts).
    Claude's real session-vs-quota utilization comes from the OAuth usage
    endpoint and is cached separately (see ``_fetch_claude_oauth_usage``)."""
    now_mono = time.monotonic()
    if _USAGE_CACHE["data"] is not None and (now_mono - _USAGE_CACHE["at"]) < _USAGE_TTL_SECONDS:
        cached = dict(_USAGE_CACHE["data"])
        # Always re-evaluate Claude OAuth usage on its own (shorter) TTL.
        cached["claude"] = dict(cached["claude"], rate_limits=_fetch_claude_oauth_usage())
        return cached
    now = _dt.datetime.now(_dt.timezone.utc)
    claude = _aggregate_claude_usage(ROOT, now)
    codex = _aggregate_codex_usage(ROOT, now)
    claude["rate_limits"] = _fetch_claude_oauth_usage()
    combined_all = (
        claude["all"]["total"] + codex["all"]["total"]
    )
    data = {
        "generated_at": now.isoformat(timespec="seconds"),
        "windows": {
            "5h": {"hours": 5},
            "7d": {"days": 7},
        },
        "claude": claude,
        "codex": codex,
        "combined": {"total": combined_all},
        # Backwards-compat with the first iteration of the API.
        "input": claude["all"]["input"],
        "output": claude["all"]["output"],
        "cache_creation": claude["all"]["cache_creation"],
        "cache_read": claude["all"]["cache_read"],
        "messages": claude["all"]["messages"],
        "transcripts": claude["transcripts"],
        "total": claude["all"]["total"],
    }
    _USAGE_CACHE["data"] = data
    _USAGE_CACHE["at"] = now_mono
    return data


# Allowed job kinds.
#   orchestrate / plan : one-shot `claude -p <skill prompt>` runs.
#   chat               : long-lived interactive `claude` session driven by
#                        JSON messages on stdin and JSON events on stdout
#                        (--input-format stream-json / --output-format stream-json).
#   chat-codex         : one-turn `codex exec --json` run per user message.
#                        Resumed via `codex exec resume <session_id>`.
JOB_KINDS = {
    "orchestrate": "orchestrate",
    "plan": "planner",
    "chat": None,
    "chat-codex": None,
}

# In-memory job registry. State is lost on server restart by design;
# log files survive on disk for forensic reading.
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()
JOBS_MAX = 50  # cap memory; oldest finished entries get evicted


# ----- PTY sessions (real shell terminals via WebSocket) ----------------
#
# Separate from JOBS because the lifecycle, transport, and rendering are
# completely different: PTY sessions move raw bytes over WS rather than
# stream-json events over SSE. Each entry mirrors the JOBS shape just
# enough for the dashboard UI to list / open / close them.
PTYS: dict[str, dict] = {}
PTYS_LOCK = threading.Lock()
PTYS_MAX = 20

# Cap on the per-session ring buffer used for catch-up when a client
# (re)attaches to a long-running PTY. 256 KB keeps a full screen of
# scrollback for any reasonable terminal size while bounding memory.
PTY_RING_BYTES = 256 * 1024

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _ws_accept_key(client_key: str) -> str:
    raw = (client_key + WS_GUID).encode("ascii")
    return base64.b64encode(hashlib.sha1(raw).digest()).decode("ascii")


class _WsClosed(Exception):
    """Raised by WebSocket recv/send when the peer has disconnected."""


def _origin_allowed(headers) -> bool:
    """Origin allowlist for state-changing requests. Returns True iff:
      - the Origin header is present, AND
      - it exactly matches a loopback dashboard origin for the bound port.
    'null' Origin (sandboxed iframes / file://) is rejected. No trailing-
    slash tolerance -- Origin per RFC6454 has no path. Validates against
    BOUND_PORT (the port the server is actually listening on) rather than
    the configured PORT, so the dynamic-port-fallback in main() doesn't
    break CSRF for the second concurrent dashboard.
    """
    origin = headers.get("Origin")
    if origin is None:
        return False
    allowed = {
        f"http://127.0.0.1:{BOUND_PORT}",
        f"http://localhost:{BOUND_PORT}",
        f"http://[::1]:{BOUND_PORT}",
    }
    return origin in allowed


class WebSocket:
    """Minimal RFC6455 server endpoint.

    Frames are sent unfragmented (FIN=1 always), payload up to 2^63
    bytes. Receives single-frame messages and pings; replies to pings
    automatically. Control flow:

        ws = WebSocket.accept(handler, expected_path)
        try:
            while True:
                opcode, data = ws.recv()
                # opcode 0x1 = text, 0x2 = binary, 0x8 = close
                ...
        except _WsClosed:
            ...
        finally:
            ws.close()
    """

    OPCODE_CONT  = 0x0
    OPCODE_TEXT  = 0x1
    OPCODE_BIN   = 0x2
    OPCODE_CLOSE = 0x8
    OPCODE_PING  = 0x9
    OPCODE_PONG  = 0xA

    def __init__(self, handler):
        self._handler = handler
        self._rfile = handler.rfile
        self._wfile = handler.wfile
        # ``self.connection`` is the raw socket; we don't read from it
        # directly but tracking it lets us shutdown on close.
        self._sock = getattr(handler, "connection", None)
        self._write_lock = threading.Lock()
        self.closed = False

    @classmethod
    def accept(cls, handler) -> "WebSocket | None":
        """Complete the RFC6455 handshake. Returns a WebSocket on success,
        or sends an HTTP error and returns ``None`` on failure."""
        h = handler.headers
        if h.get("Upgrade", "").lower() != "websocket":
            handler.send_error(400, "Expected WebSocket upgrade")
            return None
        if "upgrade" not in h.get("Connection", "").lower():
            handler.send_error(400, "Expected Connection: Upgrade")
            return None
        if not _origin_allowed(h):
            handler.send_error(403, "Origin not allowed")
            return None
        key = h.get("Sec-WebSocket-Key", "").strip()
        if not key:
            handler.send_error(400, "Missing Sec-WebSocket-Key")
            return None
        accept = _ws_accept_key(key)
        # Write the 101 response manually so we don't pick up "Server"
        # / "Date" headers from the base handler.
        try:
            handler.wfile.write(
                b"HTTP/1.1 101 Switching Protocols\r\n"
                b"Upgrade: websocket\r\n"
                b"Connection: Upgrade\r\n"
                b"Sec-WebSocket-Accept: " + accept.encode("ascii") + b"\r\n"
                b"\r\n"
            )
            handler.wfile.flush()
        except OSError:
            return None
        return cls(handler)

    def recv(self) -> tuple[int, bytes]:
        b0 = self._rfile.read(1)
        if not b0:
            raise _WsClosed()
        b0 = b0[0]
        opcode = b0 & 0x0F
        b1 = self._rfile.read(1)
        if not b1:
            raise _WsClosed()
        b1 = b1[0]
        masked = bool(b1 & 0x80)
        length = b1 & 0x7F
        if length == 126:
            ext = self._rfile.read(2)
            if len(ext) < 2:
                raise _WsClosed()
            length = struct.unpack(">H", ext)[0]
        elif length == 127:
            ext = self._rfile.read(8)
            if len(ext) < 8:
                raise _WsClosed()
            length = struct.unpack(">Q", ext)[0]
        mask = self._rfile.read(4) if masked else None
        if masked and (mask is None or len(mask) < 4):
            raise _WsClosed()
        payload = b""
        if length:
            payload = self._rfile.read(length)
            if len(payload) < length:
                raise _WsClosed()
        if mask:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        # Auto-handle pings (reply with pong) and close frames so callers
        # only deal with text/binary data.
        if opcode == self.OPCODE_PING:
            self._send_frame(self.OPCODE_PONG, payload)
            return self.recv()
        if opcode == self.OPCODE_CLOSE:
            self.closed = True
            raise _WsClosed()
        return opcode, payload

    def send_binary(self, data: bytes) -> None:
        self._send_frame(self.OPCODE_BIN, data)

    def send_text(self, text: str) -> None:
        self._send_frame(self.OPCODE_TEXT, text.encode("utf-8", errors="replace"))

    def _send_frame(self, opcode: int, data: bytes) -> None:
        if self.closed:
            raise _WsClosed()
        header = bytearray([0x80 | (opcode & 0x0F)])
        length = len(data)
        if length < 126:
            header.append(length)
        elif length < 65536:
            header.append(126)
            header += struct.pack(">H", length)
        else:
            header.append(127)
            header += struct.pack(">Q", length)
        with self._write_lock:
            try:
                self._wfile.write(bytes(header))
                if data:
                    self._wfile.write(data)
                self._wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                self.closed = True
                raise _WsClosed()

    def close(self, code: int = 1000) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            payload = struct.pack(">H", code)
            self._send_frame(self.OPCODE_CLOSE, payload)
        except (_WsClosed, OSError):
            pass
        try:
            if self._sock is not None:
                self._sock.shutdown(2)  # SHUT_RDWR
        except OSError:
            pass


# ----- PTY lifecycle helpers ----------------------------------------

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
    with entry["_lock"]:
        entry["status"] = "ended"
        for q in entry["_subscribers"]:
            try:
                q.put_nowait(None)
            except _stdqueue.Full:
                pass
    return True


def _pty_summary(entry: dict) -> dict:
    return {
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
        for _, pid in ended[:to_drop]:
            PTYS.pop(pid, None)



def _load_improver_config() -> dict:
    """Read the optional ``improver:`` block from ``.ai/models.yaml`` and
    overlay it on ``_IMPROVER_DEFAULTS``. Always returns a fully populated
    config dict, even if the YAML block is missing or malformed.

    Honours ``AI_WORKFLOW_DISABLE_IMPROVER``: when that env var is truthy
    the config is returned with ``enabled=False`` regardless of YAML.
    Used by the pytest suite to stop the auto-improver from spawning real
    ``claude -p`` subprocesses (each one creates a new chat session in
    Claude Code's history)."""
    cfg = dict(_IMPROVER_DEFAULTS)
    if str(os.environ.get("AI_WORKFLOW_DISABLE_IMPROVER", "")).strip().lower() in {"1", "true", "yes", "on"}:
        cfg["enabled"] = False
        return cfg
    fields = _read_yaml_field(ROOT / ".ai" / "models.yaml", "improver")
    if not fields:
        return cfg
    for k in ("tool", "model"):
        v = fields.get(k)
        if v:
            cfg[k] = v
    for k in ("small_change_max_lines", "min_interval_seconds",
              "timeout_seconds", "revert_after_n_uses"):
        v = fields.get(k)
        if v is None or v == "":
            continue
        try:
            cfg[k] = int(v)
        except (TypeError, ValueError):
            continue
    v = fields.get("revert_margin")
    if v not in (None, ""):
        try:
            cfg["revert_margin"] = max(0.0, min(1.0, float(v)))
        except (TypeError, ValueError):
            pass
    if "enabled" in fields:
        cfg["enabled"] = str(fields["enabled"]).strip().lower() in {"true", "1", "yes", "on"}
    return cfg


def _last_improver_run_ts(skill_id: str) -> float:
    """Look at the audit ledger for the most recent improvement attempt
    against this skill, return epoch seconds (or 0 if never).

    Reads through ``_load_jsonl_cached`` so the auto-improver scheduler
    (which calls this for every project skill on every wake) doesn't
    re-parse the ledger N times per cycle. The cache preserves the prior
    silent-skip-on-corrupt-row behaviour the hand-rolled loop relied on.
    """
    last = 0.0
    for o in _load_jsonl_cached(IMPROVEMENTS_LEDGER):
        if not isinstance(o, dict) or o.get("skill") != skill_id:
            continue
        ts = _iso_to_epoch(o.get("ts") or "")
        if ts > last:
            last = ts
    return last


def _project_skill_index() -> dict[str, Path]:
    """Map canonical skill name -> SKILL.md path for every project skill
    under ``.claude/skills/``. The improver only edits skills in this map."""
    out: dict[str, Path] = {}
    for e in _scan_skills_dir(ROOT / ".claude" / "skills"):
        # The on-disk dir name is the canonical id; frontmatter ``name`` is
        # for display only. Use the dir name to avoid collisions.
        try:
            p = (ROOT / e["path"]).resolve()
            p.relative_to(ROOT.resolve())
        except (ValueError, OSError):
            continue
        out[p.parent.name] = p
    return out


def _build_improver_prompt(skill_id: str, skill_content: str,
                           metrics: dict, job_id: str,
                           log_excerpt: str) -> str:
    """Craft the one-shot prompt sent to the model.

    The schema and ``no change`` example are intentionally front-loaded so
    smaller models (Haiku) don't drift into prose. The skill content and
    log are delimited with ``<<<...>>>`` markers (not triple-backticks)
    because SKILL.md itself often contains fenced code blocks."""
    rate = round((metrics.get("success_rate") or 0.0) * 100) if metrics else None
    summary = (
        f"success_rate={rate}% over {metrics.get('total_jobs',0)} jobs"
        f", avg_cost=${metrics.get('avg_cost_usd',0):.4f}"
        f", avg_duration={int((metrics.get('avg_duration_ms') or 0)/1000)}s"
    ) if metrics and metrics.get("total_jobs") else "no telemetry yet"
    return (
        "OUTPUT FORMAT (STRICT): Respond with ONE JSON object. NO prose, "
        "NO commentary, NO markdown fences. If you write anything other "
        "than a JSON object, the output is INVALID.\n\n"
        "Schema:\n"
        '  {"change_summary": "<short str>", "rationale": "<short str>", '
        '"new_content": <full new SKILL.md as string OR null>}\n\n'
        'When no change is warranted: '
        '{"change_summary":"none","rationale":"<why>","new_content":null}\n\n'
        "ROLE: You are reviewing a project skill after one of its "
        "invocations. Be conservative — most invocations need no change. "
        "Only propose a refinement if there is CLEAR evidence in the log "
        "excerpt that the skill caused ambiguity, repeated failure, or "
        "missing guardrails. Keep edits small (≤ ~6 line delta) and keep "
        "frontmatter name/description intact.\n\n"
        f"SKILL: {skill_id}\n"
        f"TELEMETRY: {summary}\n"
        f"JOB: {job_id}\n\n"
        "=== Current SKILL.md (between markers) ===\n"
        f"<<<SKILL\n{skill_content}\nSKILL>>>\n\n"
        "=== Job log excerpt (between markers, may be empty) ===\n"
        f"<<<LOG\n{log_excerpt}\nLOG>>>\n\n"
        "Now respond with ONLY the JSON object."
    )


def _read_log_excerpt(log_path: str | None, max_bytes: int = 6144) -> str:
    """Tail the job's log/transcript so the improver has recent evidence.
    Returns an empty string if the path is missing or unreadable."""
    if not log_path:
        return ""
    try:
        p = Path(log_path)
        if not p.is_file():
            return ""
        size = p.stat().st_size
        with p.open("rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
            data = f.read()
        return data.decode("utf-8", errors="replace")
    except OSError:
        return ""


def _parse_improver_output(stdout: str) -> dict | None:
    """Robustly extract one JSON object from the model's free-form output.

    Handles three common shapes:
      1. ``stdout`` IS JSON (no prose around it)
      2. Fenced block: ```` ```json ... ``` ````
      3. JSON embedded in prose — scanned with a brace counter that
         respects strings + backslash escapes, so ``{`` characters inside
         JSON string values don't confuse the search.

    Returns ``None`` if no parseable object is found."""
    if not stdout:
        return None
    s = stdout.strip()

    # 1. Whole output IS JSON.
    try:
        obj = json.loads(s)
        if isinstance(obj, dict):
            return obj
    except (json.JSONDecodeError, ValueError):
        pass

    # 2. Fenced block from a chatty model.
    fence = re.search(r"```(?:json)?\s*\n?(.+?)\n?```", s, re.DOTALL)
    if fence:
        try:
            obj = json.loads(fence.group(1).strip())
            if isinstance(obj, dict):
                return obj
        except (json.JSONDecodeError, ValueError):
            pass

    # 3. String-aware brace counter; tries each top-level ``{...}`` slice.
    n = len(s)
    i = 0
    while i < n:
        if s[i] != "{":
            i += 1
            continue
        depth = 0
        j = i
        in_str = False
        escape = False
        while j < n:
            c = s[j]
            if escape:
                escape = False
            elif c == "\\" and in_str:
                escape = True
            elif c == '"':
                in_str = not in_str
            elif not in_str:
                if c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            obj = json.loads(s[i:j + 1])
                            if isinstance(obj, dict):
                                return obj
                        except (json.JSONDecodeError, ValueError):
                            pass
                        break
            j += 1
        i += 1
    return None


def _diff_line_count(a: str, b: str) -> int:
    """Count net edited lines (whitespace-only changes don't count) so the
    auto-apply heuristic ignores no-op reformatting."""
    import difflib
    al = [ln.rstrip() for ln in (a or "").splitlines()]
    bl = [ln.rstrip() for ln in (b or "").splitlines()]
    count = 0
    for line in difflib.unified_diff(al, bl, lineterm=""):
        if line.startswith(("+++", "---", "@@")):
            continue
        if line.startswith("+") or line.startswith("-"):
            count += 1
    return count


def _audit_improvement(skill_id: str, status: str, reason: str,
                       proposal_id: str | None, backup_path: str | None,
                       diff_lines: int, source: str = "auto") -> None:
    """Append one row to ``IMPROVEMENTS_LEDGER``. ``status`` is one of:
    applied, pending, rejected, no_change, failed, skipped."""
    row = {
        "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "skill": skill_id,
        "status": status,
        "source": source,
        "reason": reason or "",
        "diff_lines": diff_lines,
        "proposal_id": proposal_id,
        "backup": backup_path,
    }
    try:
        IMPROVEMENTS_LEDGER.parent.mkdir(parents=True, exist_ok=True)
        with _IMPROVEMENTS_LEDGER_LOCK:
            with IMPROVEMENTS_LEDGER.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, default=str) + "\n")
    except OSError as e:
        # Audit ledger is best-effort; never break the improver pipeline.
        # Log so a silently-dropped audit row is traceable.
        print(f"[serve] audit_improvement write failed for {skill_id}: {e}", flush=True)


def _write_proposal(skill_id: str, skill_path: Path, old: str, new: str,
                    parsed: dict, diff_lines: int, job_id: str) -> dict:
    """Persist a (proposal.json, .old.md, .new.md) triple under
    ``SKILL_PROPOSALS_DIR`` and return the proposal summary dict."""
    SKILL_PROPOSALS_DIR.mkdir(parents=True, exist_ok=True)
    ts_dt = _dt.datetime.now(_dt.timezone.utc)
    slug = re.sub(r"[^a-z0-9]+", "-", skill_id.lower()).strip("-") or "skill"
    pid = f"{slug}-{ts_dt.strftime('%Y%m%d-%H%M%S')}"
    payload = {
        "id": pid,
        "skill": skill_id,
        "skill_path": str(skill_path.relative_to(ROOT)).replace("\\", "/"),
        "ts": ts_dt.isoformat(timespec="seconds"),
        "job_id": job_id,
        "change_summary": parsed.get("change_summary", "") or "",
        "rationale": parsed.get("rationale", "") or "",
        "diff_lines": diff_lines,
        "status": "pending",
        "applied_at": None,
        "applied_via": None,
        "backup_path": None,
        "kind": "improve",
    }
    try:
        (SKILL_PROPOSALS_DIR / f"{pid}.json").write_text(
            json.dumps(payload, indent=2), encoding="utf-8")
        (SKILL_PROPOSALS_DIR / f"{pid}.old.md").write_text(old, encoding="utf-8")
        (SKILL_PROPOSALS_DIR / f"{pid}.new.md").write_text(new, encoding="utf-8")
    except OSError as e:
        # Background improver thread: a raised OSError would kill it
        # silently with no operator-facing trace. Log + re-raise so the
        # caller's outer try/except (around the whole improver run) can
        # record the failure in the audit ledger.
        print(f"[serve] _write_proposal {pid} failed: {e}", flush=True)
        raise
    return payload


def _apply_improvement(skill_path: Path, new_content: str, source: str,
                       reason: str, proposal_id: str | None,
                       skill_id: str, diff_lines: int) -> bool:
    """Backup -> overwrite -> audit. Returns True on success. Skill files
    are git-tracked so a `git diff` is always available as a second safety
    net beyond the on-disk .bak."""
    SKILL_BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    ts = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
    slug = re.sub(r"[^a-z0-9]+", "-", skill_id.lower()).strip("-") or "skill"
    backup_path = SKILL_BACKUPS_DIR / f"{slug}-{ts}.md.bak"
    try:
        # ``errors="replace"`` so a SKILL.md that has been hand-edited with
        # a non-UTF-8 sequence doesn't crash the apply path with a
        # ``UnicodeDecodeError`` (manifested as an unrelated 500). The
        # replaced bytes get backed up too — operators can recover from the
        # .bak. Better than losing the whole proposal flow.
        original = skill_path.read_text(encoding="utf-8", errors="replace")
        backup_path.write_text(original, encoding="utf-8")
        tmp_path = skill_path.with_name(skill_path.name + ".tmp")
        tmp_path.write_text(new_content, encoding="utf-8")
        os.replace(str(tmp_path), str(skill_path))
    except OSError as e:
        _audit_improvement(skill_id, "failed", f"write error: {e}",
                           proposal_id, None, diff_lines, source=source)
        return False
    _audit_improvement(skill_id, "applied", reason, proposal_id,
                       str(backup_path), diff_lines, source=source)
    if proposal_id:
        pj = SKILL_PROPOSALS_DIR / f"{proposal_id}.json"
        if pj.is_file():
            try:
                obj = json.loads(pj.read_text(encoding="utf-8"))
                obj["status"] = "applied"
                obj["applied_at"] = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
                obj["applied_via"] = source
                obj["backup_path"] = str(backup_path.relative_to(ROOT)).replace("\\", "/")
                pj.write_text(json.dumps(obj, indent=2), encoding="utf-8")
            except (OSError, json.JSONDecodeError) as e:
                # On-disk proposal stays "pending" while we already applied the
                # SKILL.md change. Best-effort here so caller still completes,
                # but operators need to see this drift.
                print(f"[serve] failed to write proposal {pj} (apply): {e}", flush=True)
    return True


def _check_skill_regression(skill_id: str, cfg: dict) -> dict | None:
    """Decide whether the last ``applied`` improvement to this skill
    regressed enough to warrant auto-revert.

    Rules:
      * Find the most recent ``applied`` audit row for the skill.
      * Skip if there's already a later ``rolled_back`` / ``revert_failed``
        row for the same proposal (don't loop on the same revert).
      * Partition ``SKILL_METRICS_FILE`` rows into pre- and post-apply.
      * Need at least ``revert_after_n_uses`` post rows AND at least 1 pre
        row (so we have a baseline to compare against).
      * If pre_rate - post_rate >= ``revert_margin``, return the decision
        dict; else None.

    Returns ``{skill, proposal_id, backup_path, pre_rate, post_rate, n_pre,
    n_post}`` when a revert should fire, otherwise ``None``."""
    # Route both ledger reads through ``_load_jsonl_cached`` — the
    # auto-revert sweep runs this for every applied proposal on every
    # wake; the cache turns a 100MB re-parse into a single ``stat()``.
    rows = [
        o for o in _load_jsonl_cached(IMPROVEMENTS_LEDGER)
        if isinstance(o, dict) and o.get("skill") == skill_id
    ]
    if not rows:
        return None

    last_applied = None
    for r in reversed(rows):
        if r.get("status") == "applied":
            last_applied = r
            break
    if not last_applied:
        return None
    apply_ts = _iso_to_epoch(last_applied.get("ts") or "")
    proposal_id = last_applied.get("proposal_id")
    backup = last_applied.get("backup")
    if not backup:
        return None
    # Already rolled back / revert tried for this proposal?
    for r in rows:
        if r.get("proposal_id") != proposal_id:
            continue
        if r.get("status") in ("rolled_back", "revert_failed") \
                and _iso_to_epoch(r.get("ts") or "") > apply_ts:
            return None

    pre: list[dict] = []
    post: list[dict] = []
    for m in _load_jsonl_cached(SKILL_METRICS_FILE):
        if not isinstance(m, dict):
            continue
        # Match either the raw id or the canonical short name so we
        # don't miss rows recorded with the plugin prefix.
        if m.get("name") != skill_id and m.get("skill") != skill_id:
            continue
        ts = _iso_to_epoch(m.get("ts") or "")
        if ts < apply_ts:
            pre.append(m)
        else:
            post.append(m)

    n_threshold = int(cfg.get("revert_after_n_uses", 5))
    margin = float(cfg.get("revert_margin", 0.2))
    if len(post) < n_threshold or len(pre) == 0:
        return None

    def _rate(samples: list[dict]) -> float:
        if not samples:
            return 0.0
        succ = sum(1 for s in samples if s.get("outcome") == "done")
        return succ / len(samples)

    pre_rate = _rate(pre)
    post_rate = _rate(post)
    if (pre_rate - post_rate) < margin:
        return None
    return {
        "skill": skill_id,
        "proposal_id": proposal_id,
        "backup_path": backup,
        "pre_rate": round(pre_rate, 4),
        "post_rate": round(post_rate, 4),
        "n_pre": len(pre),
        "n_post": len(post),
    }


def _auto_revert_skill(decision: dict) -> bool:
    """Restore the SKILL.md from its .bak and audit the rollback.

    Cross-checks the proposal JSON to find the canonical ``skill_path``
    (the audit row only stores the absolute backup path, not the target).
    Best-effort: any failure becomes a ``revert_failed`` audit row.
    Returns True when the revert succeeded."""
    skill_id = decision["skill"]
    proposal_id = decision.get("proposal_id") or ""
    backup_str = decision.get("backup_path") or ""
    backup_path = Path(backup_str)

    pj = SKILL_PROPOSALS_DIR / f"{proposal_id}.json" if proposal_id else None
    if not pj or not pj.is_file():
        _audit_improvement(skill_id, "revert_failed", "proposal json missing",
                           proposal_id or None, backup_str or None, 0,
                           source="auto")
        return False
    try:
        obj = json.loads(pj.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        _audit_improvement(skill_id, "revert_failed", f"proposal parse: {e}",
                           proposal_id, backup_str, 0, source="auto")
        return False
    rel = obj.get("skill_path") or ""
    try:
        skill_path = (ROOT / rel).resolve()
        skill_path.relative_to(ROOT.resolve())
    except (ValueError, OSError):
        _audit_improvement(skill_id, "revert_failed", "invalid skill_path",
                           proposal_id, backup_str, 0, source="auto")
        return False
    if not skill_path.is_file():
        _audit_improvement(skill_id, "revert_failed", "skill file missing",
                           proposal_id, backup_str, 0, source="auto")
        return False
    if not backup_path.is_file():
        _audit_improvement(skill_id, "revert_failed", "backup missing",
                           proposal_id, backup_str, 0, source="auto")
        return False

    try:
        backup_content = backup_path.read_text(encoding="utf-8")
        skill_path.write_text(backup_content, encoding="utf-8")
    except OSError as e:
        _audit_improvement(skill_id, "revert_failed", f"write error: {e}",
                           proposal_id, backup_str, 0, source="auto")
        return False

    reason = (f"auto-revert: success_rate pre={decision['pre_rate']:.2f} "
              f"({decision['n_pre']}j) post={decision['post_rate']:.2f} "
              f"({decision['n_post']}j)")
    _audit_improvement(skill_id, "rolled_back", reason, proposal_id,
                       backup_str, int(obj.get("diff_lines") or 0),
                       source="auto")
    try:
        obj["status"] = "rolled_back"
        obj["rolled_back_at"] = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
        obj["regression"] = {
            "pre_rate": decision["pre_rate"],
            "post_rate": decision["post_rate"],
            "n_pre": decision["n_pre"],
            "n_post": decision["n_post"],
        }
        pj.write_text(json.dumps(obj, indent=2), encoding="utf-8")
    except OSError as e:
        # Proposal already rolled back on disk; the .json record is now stale.
        print(f"[serve] failed to write proposal {pj} (rollback): {e}", flush=True)
    return True


def _post_job_skill_actions(job_id: str, skill_ids: list[str]) -> None:
    """Combined post-job hook: revert-first, then improve.

    Order is intentional. If a skill just regressed and we revert it, the
    throttle (which counts ALL improver audit rows including rolled_back)
    blocks the immediate re-improvement on the same job."""
    cfg = _load_improver_config()
    proj = _project_skill_index()
    canonical: list[str] = []
    seen: set[str] = set()
    for raw in skill_ids:
        n = _skill_name_canonical(raw)
        if n in seen:
            continue
        seen.add(n)
        if n in proj:
            canonical.append(n)
    # 1. Auto-revert pass — synchronous (cheap, in-process).
    for sid in canonical:
        try:
            decision = _check_skill_regression(sid, cfg)
        except Exception as e:  # noqa: BLE001 — never crash the runner
            print(f"[serve] regression check failed for {sid}: {e}", flush=True)
            continue
        if not decision:
            continue
        try:
            _auto_revert_skill(decision)
        except Exception as e:  # noqa: BLE001 — never crash the runner
            print(f"[serve] auto-revert failed for {sid}: {e}", flush=True)
            continue
    # 2. Improver pass — spawns one daemon thread per eligible skill.
    _trigger_improvers_for_job(job_id, skill_ids)


def _purge_claude_transcript(session_id: str | None) -> None:
    """Delete the per-session JSONL Claude Code wrote for a one-shot
    background call (e.g. an improver run). Without this every improver
    invocation pollutes ``~/.claude/projects/<slug>/`` with a stray
    "OUTPUT FORMAT (STRICT)" session row in the user's chat history.
    Best-effort: missing dir / missing file / OS errors are swallowed."""
    if not session_id:
        return
    try:
        tdir = _transcripts_dir_for_cwd(ROOT)
        if tdir is None:
            return
        f = tdir / f"{session_id}.jsonl"
        if f.is_file():
            f.unlink()
    except OSError as e:
        # Best-effort delete (file may be locked on Windows, or removed
        # by a concurrent caller). Log so the operator can see why a
        # stale transcript stuck around.
        print(f"[serve] transcript delete failed for {session_id}: {e}", flush=True)


def _run_improver_for_skill(skill_id: str, skill_md_path: Path,
                            job_id: str, log_path: str | None,
                            cfg: dict) -> None:
    """End-to-end: read skill -> call LLM -> parse JSON -> persist proposal
    -> auto-apply if small. Best-effort: any failure is audited and the
    function returns silently. When the tool is ``claude`` we generate a
    dedicated ``--session-id`` and delete the resulting transcript at
    exit so background improver runs never show up in the chat list."""
    try:
        skill_content = skill_md_path.read_text(encoding="utf-8")
    except OSError as e:
        _audit_improvement(skill_id, "failed", f"read error: {e}", None, None, 0)
        return
    metrics = _aggregate_skill_metrics().get(skill_id) or {}
    log_excerpt = _read_log_excerpt(log_path)
    prompt = _build_improver_prompt(skill_id, skill_content, metrics, job_id, log_excerpt)

    # IMPORTANT (Windows): pass the prompt via stdin, not argv. Long prompts
    # on argv silently fail (claude emits only a "status:ready" stub and never
    # processes the request) — observed empirically. stdin works for any size.
    tool_bin = shutil.which(cfg["tool"]) or cfg["tool"]
    argv = [tool_bin, "-p", "--model", cfg["model"]]
    # Pin a session id ONLY for claude — codex doesn't write per-session
    # JSONLs into ~/.claude/projects/ so it doesn't have the same pollution
    # problem. The id lets _purge_claude_transcript know exactly which file
    # to delete in the finally block below.
    improver_sid: str | None = None
    if cfg.get("tool") == "claude":
        improver_sid = str(uuid.uuid4())
        argv += ["--session-id", improver_sid]
    try:
        try:
            proc = subprocess.run(
                argv, cwd=str(ROOT), input=prompt,
                capture_output=True, text=True,
                timeout=cfg.get("timeout_seconds", 120), encoding="utf-8",
                errors="replace",
            )
        except (subprocess.TimeoutExpired, OSError) as e:
            _audit_improvement(skill_id, "failed", f"subprocess error: {e}", None, None, 0)
            return
        if proc.returncode != 0:
            _audit_improvement(skill_id, "failed",
                               f"exit {proc.returncode}: {(proc.stderr or '')[:200]}",
                               None, None, 0)
            return

        parsed = _parse_improver_output(proc.stdout or "")
        if not parsed:
            _audit_improvement(skill_id, "no_change", "improver returned unparseable output",
                               None, None, 0)
            return
        new_content = parsed.get("new_content")
        if not isinstance(new_content, str) or not new_content.strip():
            _audit_improvement(skill_id, "no_change",
                               parsed.get("rationale") or "improver returned null",
                               None, None, 0)
            return

        diff_lines = _diff_line_count(skill_content, new_content)
        if diff_lines == 0:
            _audit_improvement(skill_id, "no_change", "no effective change", None, None, 0)
            return

        try:
            proposal = _write_proposal(skill_id, skill_md_path, skill_content,
                                       new_content, parsed, diff_lines, job_id)
        except OSError as e:
            # _write_proposal already logged the underlying cause; record
            # a "failed" audit row so the operator-facing ledger reflects
            # the dropped improver run rather than appearing to succeed.
            _audit_improvement(skill_id, "failed", f"proposal write error: {e}",
                               None, None, diff_lines)
            return
        if diff_lines <= int(cfg.get("small_change_max_lines", 6)):
            _apply_improvement(skill_md_path, new_content, source="auto",
                               reason=parsed.get("change_summary", "") or "",
                               proposal_id=proposal["id"], skill_id=skill_id,
                               diff_lines=diff_lines)
        else:
            _audit_improvement(skill_id, "pending",
                               parsed.get("change_summary", "") or "",
                               proposal["id"], None, diff_lines)
    finally:
        _purge_claude_transcript(improver_sid)


def _trigger_improvers_for_job(job_id: str, skill_ids: list[str]) -> None:
    """Spawn one improver thread per project skill invoked by ``job_id``.
    Throttled via ``IMPROVEMENTS_LEDGER`` and config-gated."""
    cfg = _load_improver_config()
    if not cfg.get("enabled"):
        return
    if not shutil.which(cfg["tool"]):
        return  # CLI not on PATH; silently skip
    proj = _project_skill_index()
    with JOBS_LOCK:
        j = JOBS.get(job_id)
        log_path = j.get("log_path") if j else None
    seen: set[str] = set()
    for raw in skill_ids:
        name = _skill_name_canonical(raw)
        if name in seen:
            continue
        seen.add(name)
        path = proj.get(name)
        if not path:
            continue
        # Throttle: don't re-improve within min_interval_seconds.
        last = _last_improver_run_ts(name)
        if last and (time.time() - last) < int(cfg.get("min_interval_seconds", 300)):
            continue
        threading.Thread(
            target=_run_improver_for_skill,
            args=(name, path, job_id, log_path, cfg),
            daemon=True,
            name=f"improver-{name}",
        ).start()


_STOPWORDS = frozenset({
    # English filler/imperatives that say nothing about the work itself.
    "a","an","the","is","are","be","to","of","for","on","in","with","and","or",
    "i","you","we","it","this","that","these","those","do","does","did","done",
    "have","has","had","not","no","my","your","our","at","by","as","also","but",
    "if","when","then","else","so","just","want","wanted","need","ok","please",
    "help","add","make","build","create","run","fix","new","now","one","two",
    "three","what","how","why","into","from","over","very","more","most",
    # Portuguese (the user's language)
    "o","a","os","as","um","uma","de","do","da","dos","das","e","ou","que","quero",
    "para","com","em","no","na","nos","nas","ao","aos","á","à","é","ser","ter","tem",
    "também","tambem","mais","tarde","apos","após","depois","cada","sobre","fazer",
    "vou","podes","pode","tens","esta","este","isto","como","onde","quando","ja",
    "já","aqui","ali","ai","aí","ate","até","sim","nao","não","mas","só","so",
    "tudo","nada","muito","pouco","bem","mal","todos","todas","seu","sua","seus",
    "suas","meu","minha","meus","minhas","nosso","nossa","vamos","vai","faz",
})


def _tokenize_task(s: str) -> set[str]:
    """Lowercase + strip non-alphanumeric + drop stopwords/short tokens.
    Returns a set of canonical tokens used for Jaccard similarity."""
    if not s:
        return set()
    cleaned = re.sub(r"[^0-9A-Za-zÀ-ÿ]+", " ", s.lower())
    return {t for t in cleaned.split() if len(t) >= 3 and t not in _STOPWORDS}


def _iso_to_epoch(s: str) -> float:
    """Lossy ISO-8601 -> epoch seconds; returns 0 on parse failure."""
    if not s:
        return 0.0
    try:
        return _dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return 0.0


def _load_unique_jobs(max_age_days: int = 30) -> list[dict]:
    """Replay ``JOBS_PERSIST_FILE`` keeping the last snapshot per id and
    filtering to ``max_age_days``. Skips jobs without a meaningful task
    (test-mode placeholders)."""
    snapshots: dict[str, dict] = {}
    for o in _load_jsonl_cached(JOBS_PERSIST_FILE):
        jid = o.get("id")
        if jid:
            snapshots[jid] = o  # last write wins
    cutoff_epoch = _dt.datetime.now(_dt.timezone.utc).timestamp() - max_age_days * 86400
    keep: list[dict] = []
    for o in snapshots.values():
        task = (o.get("task") or "").strip()
        if not task or task.lower() in {"(noop)", "noop", "test", "test job", "x"}:
            continue
        created = o.get("created_at") or o.get("started_at") or ""
        ts = _iso_to_epoch(created)
        if ts and ts < cutoff_epoch:
            continue
        keep.append(o)
    return keep


def _detect_skill_suggestions(threshold: float = 0.5, min_cluster: int = 3,
                              max_age_days: int = 30) -> list[dict]:
    """Greedy cluster of recent jobs by task-token Jaccard similarity.

    Each surviving cluster surfaces as "this looks repeated, maybe make a
    skill out of it". Pure read-only: works off ``jobs.jsonl`` + the
    skill_metrics ledger. Returns clusters sorted by size desc, then most
    recently seen first."""
    jobs = _load_unique_jobs(max_age_days=max_age_days)
    fps: list[tuple[dict, set[str]]] = []
    for j in jobs:
        toks = _tokenize_task(j.get("task") or "")
        if len(toks) < 2:
            continue
        fps.append((j, toks))

    # Optional skill-sequence index per job (helps when tasks are short).
    skill_seqs: dict[str, list[str]] = {}
    for row in _load_jsonl_cached(SKILL_METRICS_FILE):
        jid = row.get("job_id")
        sk = row.get("name") or row.get("skill")
        if jid and sk:
            skill_seqs.setdefault(jid, []).append(sk)

    used = [False] * len(fps)
    clusters: list[dict] = []
    for i in range(len(fps)):
        if used[i]:
            continue
        ji, ti = fps[i]
        group_idx = [i]
        for j in range(i + 1, len(fps)):
            if used[j]:
                continue
            jj, tj = fps[j]
            inter = len(ti & tj)
            union = len(ti | tj) or 1
            jaccard = inter / union
            same_skills = (
                skill_seqs.get(ji.get("id"))
                and skill_seqs.get(ji.get("id")) == skill_seqs.get(jj.get("id"))
            )
            if jaccard >= threshold or same_skills:
                group_idx.append(j)
        if len(group_idx) < min_cluster:
            continue
        for idx in group_idx:
            used[idx] = True
        cluster_jobs = [fps[k][0] for k in group_idx]
        token_counter: dict[str, int] = {}
        for k in group_idx:
            for t in fps[k][1]:
                token_counter[t] = token_counter.get(t, 0) + 1
        top_tokens = sorted(token_counter.items(), key=lambda kv: (-kv[1], kv[0]))[:6]
        kinds = sorted({(j.get("kind") or "") for j in cluster_jobs if j.get("kind")})
        skills_in_cluster: set[str] = set()
        for j in cluster_jobs:
            for sk in skill_seqs.get(j.get("id") or "", []):
                skills_in_cluster.add(sk)
        last_seen = max(
            (j.get("ended_at") or j.get("started_at") or j.get("created_at") or "")
            for j in cluster_jobs
        )
        sample_tasks: list[str] = []
        seen_samples: set[str] = set()
        for j in cluster_jobs:
            t = (j.get("task") or "").strip()
            short = t[:140]
            if short and short not in seen_samples:
                sample_tasks.append(short)
                seen_samples.add(short)
            if len(sample_tasks) >= 3:
                break
        suggested_name = "-".join(t for t, _ in top_tokens[:3]) or "repeated-task"
        clusters.append({
            "id": suggested_name,
            "suggested_name": suggested_name,
            "size": len(cluster_jobs),
            "top_tokens": [t for t, _ in top_tokens],
            "sample_tasks": sample_tasks,
            "kinds": kinds,
            "skills_invoked": sorted(skills_in_cluster),
            "last_seen": last_seen,
            "job_ids": [j.get("id") for j in cluster_jobs],
        })

    # Filter out clusters that have already been addressed: either a project
    # skill with the same slug exists, or a previous draft proposal for this
    # cluster was installed / accepted / rejected. Keeps the suggestions
    # panel relevant — no nagging about work the user already did.
    covered_cluster_ids: set[str] = set()
    covered_names: set[str] = set(_project_skill_index().keys())
    try:
        if SKILL_PROPOSALS_DIR.is_dir():
            for pj in SKILL_PROPOSALS_DIR.glob("*.json"):
                try:
                    o = json.loads(pj.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if o.get("kind") != "draft":
                    continue
                if o.get("status") not in ("installed", "accepted", "rejected"):
                    continue
                cid = o.get("cluster_id")
                if cid:
                    covered_cluster_ids.add(cid)
                slug = o.get("skill") or o.get("suggested_name")
                if slug:
                    covered_names.add(slug)
    except OSError as e:
        # Best-effort filter — losing a few "covered" entries just means
        # the user sees a previously-addressed cluster again. Log so a
        # systemic problem (permissions, missing dir) is visible.
        print(f"[serve] proposals coverage scan failed: {e}", flush=True)
    clusters = [
        c for c in clusters
        if c.get("id") not in covered_cluster_ids
           and c.get("suggested_name") not in covered_names
    ]

    clusters.sort(key=lambda c: (-c["size"], -_iso_to_epoch(c.get("last_seen") or "")))
    return clusters


def _skill_name_canonical(raw: str) -> str:
    """Strip plugin namespace prefix from a skill id (``a:b:c`` -> ``c``)."""
    if not raw:
        return ""
    return raw.rsplit(":", 1)[-1].strip()


def _extract_skills_from_stream_json(path: Path) -> dict[str, int]:
    """Scan a stream-json log/transcript and return ``{skill_id: count}``
    aggregating every ``tool_use`` with ``name == "Skill"``. Returns an
    empty dict on any failure (missing file, parse errors, etc.).

    The ``Skill`` tool's input is shaped ``{"skill": "<id>"}``; we walk
    every top-level JSON object looking for nested ``tool_use`` blocks in
    common message shapes (Anthropic stream-json + IDE transcripts both
    nest tool_use inside ``message.content[]`` arrays)."""
    counts: dict[str, int] = {}
    try:
        if not path.is_file():
            return counts
    except OSError:
        return counts

    def _visit(node) -> None:
        if isinstance(node, dict):
            if node.get("type") == "tool_use" and node.get("name") == "Skill":
                sid = (node.get("input") or {}).get("skill")
                if isinstance(sid, str) and sid:
                    counts[sid] = counts.get(sid, 0) + 1
            for v in node.values():
                _visit(v)
        elif isinstance(node, list):
            for v in node:
                _visit(v)

    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or not line.startswith("{"):
                    continue
                try:
                    obj = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                _visit(obj)
    except OSError:
        return counts
    return counts


def _record_skill_metrics(job_id: str) -> int:
    """Append one row per skill invoked in a finished job to
    ``SKILL_METRICS_FILE``. Returns the number of rows written.

    Sources:
      * Entry-skill from job ``kind`` (orchestrate/plan jobs always credit
        their entry skill even when the log isn't parseable JSON).
      * Stream-json ``Skill`` tool_use events in the job's log/transcript.

    Best-effort: any OS or parse failure swallows silently so the runner
    never crashes."""
    with JOBS_LOCK:
        j = JOBS.get(job_id)
        if not j:
            return 0
        snapshot = {
            "job_id": j["id"],
            "kind": j.get("kind") or "",
            "status": j.get("status") or "",
            "exit_code": j.get("exit_code"),
            "started_at": j.get("started_at"),
            "ended_at": j.get("ended_at"),
            "log_path": j.get("log_path"),
            "cost": j.get("cost") or {},
            "session_id": j.get("session_id"),
            "model": j.get("model"),
        }

    counts: dict[str, int] = {}
    entry_skill = JOB_KINDS.get(snapshot["kind"])
    if entry_skill:
        counts[entry_skill] = counts.get(entry_skill, 0) + 1

    log_path = snapshot.get("log_path")
    if log_path:
        try:
            scanned = _extract_skills_from_stream_json(Path(log_path))
        except Exception as e:  # noqa: BLE001 - never crash the runner
            # Log so operators can find malformed stream-json transcripts.
            print(f"[serve] skill scan failed for {log_path}: {e}", flush=True)
            scanned = {}
        for sid, n in scanned.items():
            counts[sid] = counts.get(sid, 0) + n

    if not counts:
        return 0

    cost = snapshot["cost"] if isinstance(snapshot["cost"], dict) else {}
    outcome = "done" if snapshot["exit_code"] == 0 else (snapshot["status"] or "failed")
    ts = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    rows = []
    for sid, n in counts.items():
        rows.append({
            "ts": ts,
            "skill": sid,
            "name": _skill_name_canonical(sid),
            "job_id": snapshot["job_id"],
            "kind": snapshot["kind"],
            "outcome": outcome,
            "exit_code": snapshot["exit_code"],
            "duration_ms": int(cost.get("duration_ms") or 0),
            "cost_usd": float(cost.get("cost_usd") or 0.0),
            "turns": int(cost.get("turns") or 0),
            "invocations": n,
            "session_id": snapshot.get("session_id"),
            "model": snapshot.get("model"),
        })

    try:
        SKILL_METRICS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with _SKILL_METRICS_LOCK:
            with SKILL_METRICS_FILE.open("a", encoding="utf-8") as f:
                for row in rows:
                    f.write(json.dumps(row, default=str) + "\n")
    except OSError:
        return 0

    # Phase 2/auto-revert hook: revert-first, then improve. Both are
    # throttled + config-gated inside the helper so this call is cheap and
    # safe even when the improver is disabled.
    try:
        _post_job_skill_actions(job_id, list(counts.keys()))
    except Exception as e:  # noqa: BLE001 - never break the runner
        print(f"[serve] post-job skill actions failed for {job_id}: {e}", flush=True)

    return len(rows)


def _aggregate_skill_metrics() -> dict[str, dict]:
    """Roll up ``SKILL_METRICS_FILE`` into per-skill summaries the dashboard
    can render in skill cards. Keyed by both the raw skill id and the
    canonical short name so callers can look up either flavor."""
    by_skill: dict[str, dict] = {}
    for row in _load_jsonl_cached(SKILL_METRICS_FILE):
        sid = row.get("skill") or ""
        if not sid:
            continue
        agg = by_skill.setdefault(sid, {
            "skill": sid,
            "name": row.get("name") or _skill_name_canonical(sid),
            "total_jobs": 0,
            "total_invocations": 0,
            "successes": 0,
            "failures": 0,
            "total_cost_usd": 0.0,
            "total_duration_ms": 0,
            "last_used": None,
            "last_outcome": None,
            "recent": [],
        })
        agg["total_jobs"] += 1
        agg["total_invocations"] += int(row.get("invocations") or 1)
        if row.get("outcome") == "done":
            agg["successes"] += 1
        else:
            agg["failures"] += 1
        agg["total_cost_usd"] += float(row.get("cost_usd") or 0.0)
        agg["total_duration_ms"] += int(row.get("duration_ms") or 0)
        ts = row.get("ts")
        if ts and (agg["last_used"] is None or ts > agg["last_used"]):
            agg["last_used"] = ts
            agg["last_outcome"] = row.get("outcome")
        agg["recent"].append({
            "ts": ts,
            "job_id": row.get("job_id"),
            "kind": row.get("kind"),
            "outcome": row.get("outcome"),
            "cost_usd": row.get("cost_usd"),
            "duration_ms": row.get("duration_ms"),
        })
    for agg in by_skill.values():
        total = agg["total_jobs"] or 1
        agg["success_rate"] = round(agg["successes"] / total, 4)
        agg["avg_cost_usd"] = round(agg["total_cost_usd"] / total, 6)
        agg["avg_duration_ms"] = int(agg["total_duration_ms"] / total)
        agg["recent"] = sorted(agg["recent"], key=lambda r: r.get("ts") or "", reverse=True)[:10]
    return by_skill


def _scan_agents_dir(agents_dir: Path, *, recursive: bool = False) -> list[dict]:
    """Return one record per ``<agents_dir>/<name>.md`` (or recursively,
    when ``recursive=True``, for plugin trees nested as
    ``.../agents/<name>.md``). Each record carries frontmatter fields
    ``name``, ``description``, ``tools``, ``model`` plus a repo-relative
    path.

    Tolerates missing dirs and unreadable files — returns ``[]`` rather
    than raising so callers can compose results across multiple roots.
    Agent files are single ``.md`` files (unlike skills which are
    directories with a ``SKILL.md`` inside)."""
    out: list[dict] = []
    try:
        if not agents_dir.is_dir():
            return out
        if recursive:
            files = sorted(agents_dir.glob("**/agents/*.md"))
        else:
            files = sorted(p for p in agents_dir.iterdir() if p.suffix == ".md")
    except OSError:
        return out
    for fp in files:
        try:
            if not fp.is_file():
                continue
        except OSError:
            continue
        name = fp.stem
        desc = ""
        tools = ""
        model = ""
        try:
            text = fp.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        lines = text.splitlines()
        if lines and lines[0].strip() == "---":
            for ln in lines[1:]:
                if ln.strip() == "---":
                    break
                m = re.match(r"^(name|description|tools|model|color)\s*:\s*(.+)$", ln)
                if m:
                    key, val = m.group(1), m.group(2).strip().strip('"\'')
                    if key == "name":
                        name = val
                    elif key == "description":
                        desc = val
                    elif key == "tools":
                        tools = val
                    elif key == "model":
                        model = val
        try:
            rel = str(fp.relative_to(ROOT)).replace("\\", "/")
        except ValueError:
            rel = str(fp).replace("\\", "/")
        out.append({
            "name": name,
            "description": desc,
            "tools": tools,
            "model": model,
            "path": rel,
        })
    return out


def _scan_skills_dir(skills_dir: Path) -> list[dict]:
    """Return one record per ``<skills_dir>/<name>/SKILL.md`` containing
    the frontmatter ``name`` + ``description`` and a repo-relative path
    (or absolute path if the dir lives outside the repo).

    Tolerates missing dirs and unreadable files — returns ``[]`` rather
    than raising so callers can compose results across multiple roots."""
    out: list[dict] = []
    try:
        if not skills_dir.is_dir():
            return out
        subs = sorted(skills_dir.iterdir())
    except OSError:
        return out
    for sub in subs:
        try:
            if not sub.is_dir():
                continue
        except OSError:
            continue
        skill_md = sub / "SKILL.md"
        if not skill_md.is_file():
            continue
        name = sub.name
        desc = ""
        try:
            text = skill_md.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # Minimal frontmatter parse: lines between leading ``---``.
        lines = text.splitlines()
        if lines and lines[0].strip() == "---":
            for ln in lines[1:]:
                if ln.strip() == "---":
                    break
                m = re.match(r"^(name|description)\s*:\s*(.+)$", ln)
                if m:
                    key, val = m.group(1), m.group(2).strip().strip('"\'')
                    if key == "name":
                        name = val
                    elif key == "description":
                        desc = val
        try:
            rel = str(skill_md.relative_to(ROOT)).replace("\\", "/")
        except ValueError:
            rel = str(skill_md).replace("\\", "/")
        out.append({"name": name, "description": desc, "path": rel})
    return out


def _read_yaml_field(path: Path, field: str) -> dict:
    """Minimal YAML helper to pull a top-level mapping like `session: {...}`.

    Avoids a PyYAML dependency. Only handles the simple two-line shape used in
    .ai/models.yaml. Returns an empty dict on any failure.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    out: dict[str, str] = {}
    in_block = False
    for line in text.splitlines():
        stripped = line.rstrip()
        if not in_block:
            if stripped.startswith(field + ":"):
                in_block = True
            continue
        if not stripped:
            break
        if not stripped.startswith((" ", "\t")):
            break
        m = re.match(r"^\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*:\s*(\S.*)?$", stripped)
        if not m:
            continue
        val = (m.group(2) or "").strip()
        val = val.split("#", 1)[0].strip()  # strip inline comment
        out[m.group(1)] = val.strip('"\'')
    return out


def _spawn_job(
    job_id: str,
    kind: str,
    task: str,
    resume_session_id: str | None = None,
    permission_mode: str | None = None,
    fork_session_id: str | None = None,
    model_override: str | None = None,
) -> None:
    """Spawn a job in a worker thread and capture stdout+stderr to a log file.

    Dispatches by kind:
      * ``orchestrate`` / ``plan`` -> one-shot ``claude -p <prompt>``.
      * ``chat``                   -> long-lived ``claude --print
                                       --input-format stream-json
                                       --output-format stream-json`` session
                                       driven by JSON messages on stdin.

    ``model_override`` lets the caller pin a specific model for this job
    (e.g. the dashboard's "New terminal" picker). When absent, falls back
    to ``session.model`` from ``.ai/models.yaml``.
    """
    session = _read_yaml_field(ROOT / ".ai" / "models.yaml", "session")
    model = model_override or session.get("model") or "claude-sonnet-4-6"
    claude_bin = shutil.which("claude") or "claude"

    if kind == "chat":
        # Three possible session strategies:
        #  - fresh:  generate a new session_id, no --resume.
        #  - resume: reuse the same session_id (continues that conversation).
        #  - fork:   --resume + --fork-session keeps the history but writes
        #            new turns into a freshly-generated session_id.
        source_sid = fork_session_id or resume_session_id
        session_id = source_sid or str(uuid.uuid4())
        argv = _build_chat_argv(
            model=model,
            session_id=session_id,
            claude_bin=claude_bin,
            resume=bool(source_sid),
            fork=bool(fork_session_id),
            permission_mode=permission_mode,
        )
        # Whether new or resumed, the operator's task is always the first
        # user turn fed to claude as a stream-json envelope. (For pure
        # "just resume to view history" the caller would have to bypass
        # this endpoint - the HTTP layer requires task.)
        initial_stdin = _chat_user_message(task) if task else None
        with JOBS_LOCK:
            JOBS[job_id]["command"] = " ".join(argv[1:])
            JOBS[job_id]["session_id"] = session_id
            JOBS[job_id]["model"] = model
        _start_subprocess_job(
            job_id=job_id,
            kind=kind,
            task=task,
            argv=argv,
            initial_stdin=initial_stdin,
        )
        return

    if kind == "chat-codex":
        codex_session = session if isinstance(session, dict) else {}
        # Codex uses its own session/model defaults; fall back to the claude
        # session.model if no separate codex one is configured. An explicit
        # ``model_override`` from the caller still wins over both.
        codex_model = model_override or codex_session.get("codex_model") or model
        codex_bin = shutil.which("codex") or "codex"
        argv = _build_codex_chat_argv(
            model=codex_model,
            session_id=resume_session_id,
            codex_bin=codex_bin,
        )
        initial_stdin = (task + "\n").encode("utf-8")  # prompt over stdin
        with JOBS_LOCK:
            JOBS[job_id]["command"] = " ".join(argv[1:])
            JOBS[job_id]["session_id"] = resume_session_id or ""
            JOBS[job_id]["model"] = codex_model
        _start_subprocess_job(
            job_id=job_id,
            kind=kind,
            task=task,
            argv=argv,
            initial_stdin=initial_stdin,
        )
        return

    skill = JOB_KINDS[kind]
    prompt = (
        f"Use the {skill} skill.\n"
        f"Task: {task}\n\n"
        "Run non-interactively: never ask the user a clarifying question. "
        "If you genuinely cannot proceed, emit the Escalation block defined "
        "in .ai/workflow/dispatch.md and exit non-zero."
    )

    with JOBS_LOCK:
        JOBS[job_id]["command"] = f"{claude_bin} -p ... --model {model}"
        JOBS[job_id]["model"] = model

    _start_subprocess_job(
        job_id=job_id,
        kind=kind,
        task=task,
        argv=[claude_bin, "-p", prompt, "--model", model],
    )


_VALID_PERMISSION_MODES = {"acceptEdits", "auto", "bypassPermissions", "default", "dontAsk", "plan"}


def _build_chat_argv(
    model: str,
    session_id: str,
    claude_bin: str | None = None,
    resume: bool = False,
    permission_mode: str | None = None,
    fork: bool = False,
) -> list[str]:
    """Build the argv for an interactive ``claude`` chat session that
    communicates via JSON on stdin and JSON events on stdout.

    Every flag is load-bearing:
      * ``--print``                  -> required by stream-json modes
      * ``--input-format  stream-json`` -> we feed user turns as JSON
      * ``--output-format stream-json`` -> claude emits events as JSON
      * ``--include-partial-messages``  -> token-by-token streaming
      * ``--verbose``                -> required alongside stream-json output
      * ``--session-id``             -> stable id so the conversation can be
                                        resumed later via ``--resume``
      * ``--dangerously-skip-permissions`` -> dashboard sessions can't
                                        answer permission prompts; the
                                        operator is the only consent gate.
    """
    if claude_bin is None:
        claude_bin = shutil.which("claude") or "claude"
    argv = [
        claude_bin,
        "--print",
        "--input-format", "stream-json",
        "--output-format", "stream-json",
        "--include-partial-messages",
        "--verbose",
        "--model", model,
    ]
    # Permission strategy: explicit mode wins; otherwise fall back to the
    # bypass flag (the dashboard subprocess has no UI to answer prompts).
    if permission_mode and permission_mode in _VALID_PERMISSION_MODES:
        argv += ["--permission-mode", permission_mode]
    else:
        argv += ["--dangerously-skip-permissions"]
    if resume:
        argv += ["--resume", session_id]
        if fork:
            argv += ["--fork-session"]
    else:
        argv += ["--session-id", session_id]
    return argv


def _chat_user_message(text_or_blocks) -> bytes:
    """Wrap a user turn as a stream-json envelope. Accepts either a plain
    string (single text block) or a list of pre-built content blocks
    (text / image / etc) so the composer can send multimodal messages."""
    if isinstance(text_or_blocks, list):
        content = text_or_blocks
    else:
        content = [{"type": "text", "text": text_or_blocks}]
    obj = {"type": "user", "message": {"role": "user", "content": content}}
    return (json.dumps(obj) + "\n").encode("utf-8")


def _build_codex_chat_argv(model: str, session_id: str | None, codex_bin: str | None = None) -> list[str]:
    """Build the argv for one turn of a codex chat.

    Codex is one-shot per invocation: each user turn spawns its own process.
    The first turn uses ``codex exec --json``; subsequent turns use ``codex
    exec resume <session_id> --json`` to continue the same conversation.

    The prompt is passed via stdin (``-`` in the codex CLI convention) so
    we don't have to shell-quote arbitrary user text.
    """
    if codex_bin is None:
        codex_bin = shutil.which("codex") or "codex"
    argv = [codex_bin, "exec"]
    if session_id:
        argv += ["resume", session_id]
    argv += [
        "--json",
        "--skip-git-repo-check",
        "-m", model,
        "-",  # read prompt from stdin
    ]
    return argv


def _start_subprocess_job(
    job_id: str,
    kind: str,
    task: str,
    argv: list[str],
    initial_stdin: bytes | None = None,
) -> None:
    """Launch a subprocess in a worker thread, with stdin/stdout wired for
    interactive streaming.

    Output is written to a per-job log file AND broadcast to any SSE listeners
    via ``_publish_chunk``. Stdin is kept open as a PIPE so callers can write
    to it via :func:`_send_to_stdin`.

    This is the underlying primitive behind :func:`_spawn_job`. Tests inject
    their own ``argv`` to avoid depending on the ``claude`` CLI.
    """
    # Make sure the registry entry exists (HTTP-spawned jobs already do this;
    # tests that call this helper directly may not).
    with JOBS_LOCK:
        if job_id not in JOBS:
            JOBS[job_id] = {
                "id": job_id,
                "kind": kind,
                "task": task,
                "status": "queued",
                "created_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
                "pid": None,
                "log_path": None,
                "exit_code": None,
                "started_at": None,
                "ended_at": None,
            }
        sid_for_path = JOBS[job_id].get("session_id")

    # For chat jobs with a known session_id we route ``log_path`` to
    # claude's own transcript file (which claude writes anyway via
    # ``--session-id``). This avoids duplicating storage in
    # ``.ai/dashboard/jobs/`` — the transcript is the single source of
    # truth for the conversation. The pump still forwards stdout chunks
    # to SSE subscribers so live streaming keeps working, but it no
    # longer writes them to a local ``.log`` file for chat jobs.
    log_path: Path
    write_log_to_disk = True
    if kind == "chat" and sid_for_path:
        tdir = _transcripts_dir_for_cwd(ROOT)
        if tdir is not None:
            log_path = tdir / f"{sid_for_path}.jsonl"
            write_log_to_disk = False
        else:
            JOBS_DIR.mkdir(parents=True, exist_ok=True)
            log_path = JOBS_DIR / f"{job_id}.log"
    else:
        JOBS_DIR.mkdir(parents=True, exist_ok=True)
        log_path = JOBS_DIR / f"{job_id}.log"

    with JOBS_LOCK:
        JOBS[job_id]["status"] = "running"
        JOBS[job_id]["started_at"] = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
        JOBS[job_id]["log_path"] = str(log_path)
        JOBS[job_id]["proc"] = None
        JOBS[job_id]["stdin_lock"] = threading.Lock()
        JOBS[job_id]["subscribers"] = []

    def runner() -> None:
        try:
            # Binary mode + manual UTF-8 encoding so Windows doesn't translate
            # `\n` to `\r\n` on write (which compounds with the subprocess
            # already emitting `\r\n` on Windows, producing `\r\r\n` on disk).
            logf = log_path.open("wb") if write_log_to_disk else None
            try:
                header = f"# job {job_id} kind={kind}\n# task: {task}\n\n"
                if logf is not None:
                    logf.write(header.encode("utf-8"))
                    logf.flush()
                _publish_chunk(job_id, header)

                proc = subprocess.Popen(
                    argv,
                    cwd=str(ROOT),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    bufsize=0,
                )
                with JOBS_LOCK:
                    JOBS[job_id]["pid"] = proc.pid
                    JOBS[job_id]["proc"] = proc

                # Send the initial stdin payload (e.g. the first user
                # message for a chat job) before we start pumping output.
                if initial_stdin is not None and proc.stdin is not None:
                    try:
                        proc.stdin.write(initial_stdin)
                        proc.stdin.flush()
                    except (BrokenPipeError, OSError):
                        pass

                # Pump stdout bytes -> log file (if enabled) + subscribers.
                # Normalise CRLF/CR to LF so the SSE stream and the on-disk
                # log show the same canonical line endings regardless of
                # platform. For chat jobs the log file is skipped — the
                # transcript that claude itself writes is the persistent
                # record.
                #
                # IMPORTANT: publish only COMPLETE lines (everything up to
                # the last ``\n``), keeping any trailing partial line in the
                # buffer for the next iteration. claude emits one JSON record
                # per line; if we forwarded raw 1024-byte chunks, a long
                # record (the 8KB SessionStart hook context) would arrive at
                # the client split mid-string, and any consumer that does
                # JSON.parse per line would choke on the partial halves.
                #
                # For chat-kind jobs we ALSO inspect each complete line for
                # ``type=result`` events and update the live cost counter as
                # they arrive (so cost stays accurate even though we no
                # longer keep a local log file to scan post-mortem).
                assert proc.stdout is not None
                line_buf = ""
                track_cost = kind in {"chat", "chat-codex"}
                while True:
                    chunk = proc.stdout.read(1024)
                    if not chunk:
                        break
                    text = chunk.decode("utf-8", errors="replace")
                    text = text.replace("\r\n", "\n").replace("\r", "\n")
                    line_buf += text
                    # Split off everything up to (and including) the last newline.
                    last_nl = line_buf.rfind("\n")
                    if last_nl < 0:
                        # No newline yet — keep accumulating, publish nothing.
                        continue
                    publishable = line_buf[: last_nl + 1]
                    line_buf = line_buf[last_nl + 1:]
                    if logf is not None:
                        logf.write(publishable.encode("utf-8"))
                        logf.flush()
                    _publish_chunk(job_id, publishable)
                    if track_cost:
                        for line in publishable.split("\n"):
                            line = line.strip()
                            if not line.startswith("{"):
                                continue
                            try:
                                obj = json.loads(line)
                            except (json.JSONDecodeError, ValueError):
                                continue
                            if obj.get("type") == "result":
                                _update_job_cost(job_id, obj)
                            # Codex emits a ``session_meta`` event on its first
                            # JSON line with ``payload.id`` = the rollout/session
                            # id. We capture it once so the next-turn handler
                            # can ``codex exec resume <sid>`` to continue the
                            # same conversation without spawning a brand-new
                            # session every message.
                            if kind == "chat-codex" and obj.get("type") == "session_meta":
                                sid = (obj.get("payload") or {}).get("id")
                                if sid:
                                    with JOBS_LOCK:
                                        if not JOBS[job_id].get("session_id"):
                                            JOBS[job_id]["session_id"] = sid

                # Flush any final partial line (no trailing newline). Add a
                # synthetic ``\n`` so downstream line-based parsers can still
                # cleanly bound it. This is the EOF tail — typically empty.
                if line_buf:
                    final = line_buf + "\n"
                    if logf is not None:
                        logf.write(final.encode("utf-8"))
                        logf.flush()
                    _publish_chunk(job_id, final)

                exit_code = proc.wait()
            finally:
                if logf is not None:
                    logf.close()

            with JOBS_LOCK:
                j = JOBS[job_id]
                if j["status"] == "cancelling":
                    j["status"] = "cancelled"
                else:
                    j["status"] = "done" if exit_code == 0 else "failed"
                j["exit_code"] = exit_code
                j["ended_at"] = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
            _persist_job(job_id)  # final state -> ledger
            try:
                _record_skill_metrics(job_id)
            except Exception as e:  # noqa: BLE001 - never break the runner
                print(f"[serve] record_skill_metrics failed for {job_id}: {e}", flush=True)
            _publish_chunk(job_id, None)  # sentinel: stream end
        except Exception as e:  # noqa: BLE001 (don't crash the server)
            print(f"[serve] job runner crashed for {job_id}: {e}", flush=True)
            with JOBS_LOCK:
                JOBS[job_id]["status"] = "failed"
                JOBS[job_id]["error"] = str(e)
                JOBS[job_id]["ended_at"] = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
            _persist_job(job_id)
            _publish_chunk(job_id, None)

    threading.Thread(target=runner, daemon=True, name=f"job-{job_id}").start()


def _publish_chunk(job_id: str, chunk: str | None) -> None:
    """Push a log chunk (or None=EOF) to every SSE subscriber of this job."""
    with JOBS_LOCK:
        subs = list(JOBS.get(job_id, {}).get("subscribers") or [])
    for q in subs:
        try:
            q.put_nowait(chunk)
        except _stdqueue.Full:
            # Subscriber queue at maxsize=1024 — slow client. Drop this chunk
            # for that subscriber rather than blocking the runner; the SSE
            # heartbeat loop will reap them on the next disconnect.
            pass
        except Exception as e:  # noqa: BLE001
            # Defensive: any other queue / GC race. Log so a runaway
            # subscriber pattern is visible in the server log.
            print(f"[serve] publish_chunk subscriber put_nowait failed for job {job_id}: {e}", flush=True)


def _send_chat_blocks(job_id: str, blocks: list[dict]) -> tuple[bool, str]:
    """Write a pre-built stream-json content-block array to the subprocess
    stdin (used when the composer sends images / inlined files)."""
    with JOBS_LOCK:
        j = JOBS.get(job_id)
        if not j:
            return False, "not found"
        if j["status"] != "running":
            return False, "job not running"
        proc = j.get("proc")
        lock = j.get("stdin_lock")
    if not proc or proc.stdin is None or proc.stdin.closed:
        return False, "stdin not available"
    payload = _chat_user_message(blocks)
    try:
        with lock:
            proc.stdin.write(payload)
            proc.stdin.flush()
    except (BrokenPipeError, OSError) as e:
        return False, f"write failed: {e}"
    return True, ""


def _interrupt_chat_turn(job_id: str) -> tuple[bool, str]:
    """Ask the running chat subprocess to abort the current generation.

    Uses the Claude Agent SDK's stream-json ``control_request`` protocol:
    a JSON envelope with ``subtype:"interrupt"`` written to stdin. The
    subprocess stays alive — only the in-flight turn is cancelled, so the
    session can keep going with the next user message.
    Returns ``(ok, error)``."""
    with JOBS_LOCK:
        j = JOBS.get(job_id)
        if not j:
            return False, "not found"
        if j["status"] != "running":
            return False, "job not running"
        if j.get("kind") != "chat":
            return False, "interrupt is only supported for chat-kind jobs"
        proc = j.get("proc")
        lock = j.get("stdin_lock")
    if not proc or proc.stdin is None or proc.stdin.closed:
        return False, "stdin not available"
    payload = json.dumps({
        "type": "control_request",
        "request_id": str(uuid.uuid4()),
        "request": {"subtype": "interrupt"},
    }).encode("utf-8") + b"\n"
    try:
        with lock:
            proc.stdin.write(payload)
            proc.stdin.flush()
    except (BrokenPipeError, OSError) as e:
        return False, f"write failed: {e}"
    return True, ""


def _send_to_stdin(job_id: str, text: str) -> tuple[bool, str]:
    """Write a user turn to the subprocess stdin. For ``chat`` jobs the
    text is JSON-wrapped as a stream-json user envelope; otherwise it's
    written as a plain line. Returns (ok, error)."""
    with JOBS_LOCK:
        j = JOBS.get(job_id)
        if not j:
            return False, "not found"
        if j["status"] != "running":
            return False, "job not running"
        proc = j.get("proc")
        lock = j.get("stdin_lock")
        kind = j.get("kind", "")
    if not proc or proc.stdin is None or proc.stdin.closed:
        return False, "stdin not available"
    if kind == "chat":
        payload = _chat_user_message(text)
    else:
        payload = (text if text.endswith("\n") else text + "\n").encode("utf-8", "replace")
    try:
        with lock:
            proc.stdin.write(payload)
            proc.stdin.flush()
    except (BrokenPipeError, OSError) as e:
        return False, f"write failed: {e}"
    return True, ""


def _cancel_job(job_id: str) -> bool:
    with JOBS_LOCK:
        j = JOBS.get(job_id)
        if not j or j["status"] not in {"running", "queued"}:
            return False
        j["status"] = "cancelling"
        pid = j.get("pid")
    _persist_job(job_id)
    if pid:
        try:
            if os.name == "nt":
                # `capture_output=True` keeps the noise out of the server's
                # stdout; the stderr tail is only worth printing when the
                # kill actually fails (process gone, ACL, etc.). Without this
                # log a stuck job that wouldn't die looked identical to one
                # that died cleanly — operators had no signal.
                tk = subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    check=False, capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=10,
                )
                if tk.returncode != 0:
                    print(
                        f"[serve] taskkill rc={tk.returncode} pid={pid} "
                        f"stderr={(tk.stderr or '').strip()[:200]}",
                        flush=True,
                    )
            else:
                os.kill(pid, 15)
        except (ProcessLookupError, OSError) as e:
            print(f"[serve] cancel kill failed pid={pid}: {e}", flush=True)
        except subprocess.TimeoutExpired:
            print(f"[serve] taskkill timed out for pid={pid}", flush=True)
    return True


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


def _reconcile_running_pids() -> int:
    """Flip jobs marked ``running`` / ``queued`` / ``cancelling`` whose
    tracked PID is no longer alive into ``failed``. Jobs whose ``proc``
    handle is still ours and still reports no exit are left alone — the
    runner thread will close them out. Returns the number of jobs
    reconciled so the caller can log it."""
    flipped: list[str] = []
    with JOBS_LOCK:
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


# Record types considered "interesting" for a chat-pane catch-up dump.
# Hook outputs, queue ops, file snapshots and attachment frames are
# operational metadata the human reader never wants to see.
_CHAT_CATCHUP_INCLUDE_TYPES = frozenset({
    "user",            # user turn (own + injected wrappers — client filters further)
    "assistant",       # assistant turn (text/tool_use/thinking blocks)
    "tool_use_result", # tool result (replaces pill state on client)
    "tool_result",
    "result",          # turn-done meta (cost / duration / num_turns)
    "system",          # init/shutdown/error subtypes (client filters subtype)
    "ai-title",        # session title (renames pane header)
})


def _tail_chat_catchup(path: Path, max_records: int = 100) -> str:
    """Return up to ``max_records`` recent conversation records from a chat
    transcript / log file, suitable for SSE catch-up.

    Filters out IDE-transcript noise (attachment / queue-operation /
    file-history-snapshot / last-prompt / summary / compaction) so the
    browser only has to render the actual conversation, not megabytes of
    plumbing. Records keep their original encoded line; we just decide
    which ones survive."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return ""
    kept: list[str] = []
    for raw in lines:
        stripped = raw.strip()
        if not stripped:
            continue
        if not stripped.startswith("{"):
            # Plain-text noise (deprecation warnings, etc) — drop from catch-up.
            continue
        try:
            obj = json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            continue
        t = obj.get("type")
        if t not in _CHAT_CATCHUP_INCLUDE_TYPES:
            continue
        kept.append(raw if raw.endswith("\n") else raw + "\n")
    if len(kept) > max_records:
        kept = kept[-max_records:]
    return "".join(kept)


def _lookup_session_task(session_id: str) -> str | None:
    """Best-effort: read the first user message from the Claude transcript
    matching this session_id so the timeline row can show what the run was
    about. Returns None when the transcript is missing or unreadable —
    callers must treat that as "unknown task" without erroring."""
    if not session_id or session_id == "unknown":
        return None
    try:
        tdir = _transcripts_dir_for_cwd(ROOT)
    except OSError:
        return None
    if tdir is None or not tdir.is_dir():
        return None
    f = tdir / f"{session_id}.jsonl"
    if not f.is_file():
        return None
    try:
        with f.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("type") != "user":
                    continue
                content = (rec.get("message") or {}).get("content")
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
                    text = " ".join(p for p in parts if p)
                else:
                    text = ""
                text = " ".join(text.split())  # collapse whitespace
                if not text:
                    continue
                # Skip IDE/system-injected "user" messages so the row shows the
                # first REAL prompt. Claude Code wraps editor state, system
                # reminders, command output, and tool results in <tag>...</tag>
                # envelopes that arrive as type=user but aren't what the
                # operator typed. Tag pattern: lowercase letters / underscores
                # / hyphens. If a user prompt legitimately starts with '<'
                # (e.g. a code snippet), it would have a space or quote before
                # the closing '>'.
                if re.match(r"^<[a-z][a-z0-9_-]*>", text):
                    continue
                return text[:120] + ("…" if len(text) > 120 else "")
    except OSError:
        return None
    return None


def _load_auto_select_ranking(max_records: int = 200, min_samples: int = 3) -> dict:
    """Aggregate METRICS_FILE into per-(phase, size, risk, budget) rankings.

    Mirrors the planner's adaptive scorer from
    `.claude/skills/planner/SKILL.md` "Adaptive scoring" so the dashboard
    surfaces the same information the scorer would see.

    Schema returned::

        {
          "samples": <int>,         # total records considered (post-tail)
          "groups": [
            {
              "key": {
                "phase": "execute" | ...,
                "size":  "small" | ... | null,
                "risk":  "low" | "elevated" | null,
                "budget": "low" | "medium" | "high" | null
              },
              "candidates": [
                {
                  "tool": "<str>",
                  "model": "<str>",
                  "reasoning_effort": "<str|null>",
                  "samples": <int>,
                  "success_rate": <float 0..1>,
                  "mean_duration_ms": <int>,
                  "score": <float 0..1>     # 0.6 sr + 0.2 (1-norm_dur) + 0.2 budget_align(1)
                },
                ... up to top 3 ...
              ]
            },
            ...
          ]
        }

    `success_rate` counts records where `exit_code == 0` AND
    `handoff_complete` is `True` or `None` AND `review_verdict` is `approve`,
    `null`, or absent. `mean_duration_ms` averages `duration_ms` across the
    group. Candidates with fewer than `min_samples` records are dropped; if a
    group ends up empty it is omitted.
    """
    empty = {
        "samples": 0,
        "min_samples": min_samples,
        "groups": [],
        "dropped_candidates": 0,
        "last_record_ts": None,
    }
    # Route through the mtime-keyed JSONL cache so repeated /api/auto-select
    # polls don't re-parse the whole ledger. The helper preserves the prior
    # ``errors="replace"`` invariant (so a half-written concurrent append
    # from the orchestrate skill can't surface as UnicodeDecodeError) and
    # silently skips malformed rows — mirroring this function's previous
    # hand-rolled behaviour.
    rows = _load_jsonl_cached(METRICS_FILE)
    if not rows:
        return empty
    tail = rows[-max_records:] if len(rows) > max_records else rows

    groups: dict[tuple, dict[tuple, list[dict]]] = {}
    sample_count = 0
    last_ts: str | None = None
    for rec in tail:
        if not isinstance(rec, dict):
            continue
        phase = rec.get("phase")
        if not phase:
            continue
        sample_count += 1
        ts = rec.get("ts")
        if isinstance(ts, str) and (last_ts is None or ts > last_ts):
            last_ts = ts
        group_key = (
            phase,
            rec.get("size"),
            rec.get("risk"),
            rec.get("budget"),
        )
        cand_key = (
            rec.get("tool") or "unknown",
            rec.get("model") or "unknown",
            rec.get("reasoning_effort"),
        )
        groups.setdefault(group_key, {}).setdefault(cand_key, []).append(rec)

    out_groups: list[dict] = []
    dropped = 0
    for gkey, cands in groups.items():
        scored: list[dict] = []
        for ckey, records in cands.items():
            n = len(records)
            if n < min_samples:
                dropped += 1
                continue
            successes = sum(
                1
                for r in records
                if r.get("exit_code") == 0
                and r.get("handoff_complete") in (True, None)
                and r.get("review_verdict") in (None, "approve")
            )
            sr = successes / n
            durations = [r.get("duration_ms") for r in records if isinstance(r.get("duration_ms"), int)]
            mean_dur = int(sum(durations) / len(durations)) if durations else 0
            scored.append({
                "tool": ckey[0],
                "model": ckey[1],
                "reasoning_effort": ckey[2],
                "samples": n,
                "success_rate": round(sr, 3),
                "mean_duration_ms": mean_dur,
            })
        if not scored:
            continue
        # Normalize duration across this group to compute score, then keep top 3.
        durs = [c["mean_duration_ms"] for c in scored]
        dmin, dmax = min(durs), max(durs)
        spread = (dmax - dmin) or 1
        for c in scored:
            norm_dur = (c["mean_duration_ms"] - dmin) / spread
            # budget_alignment baseline = 1 (controller already filtered by budget)
            c["score"] = round(0.6 * c["success_rate"] + 0.2 * (1 - norm_dur) + 0.2, 3)
        scored.sort(key=lambda c: c["score"], reverse=True)
        out_groups.append({
            "key": {
                "phase": gkey[0],
                "size": gkey[1],
                "risk": gkey[2],
                "budget": gkey[3],
            },
            "candidates": scored[:3],
        })

    out_groups.sort(key=lambda g: (g["key"]["phase"], g["key"]["size"] or "", g["key"]["risk"] or "", g["key"]["budget"] or ""))
    return {
        "samples": sample_count,
        "min_samples": min_samples,
        "groups": out_groups,
        "dropped_candidates": dropped,
        "last_record_ts": last_ts,
    }


def _load_timeline_runs(max_events: int = 500) -> list[dict]:
    """Aggregate `phase_dispatch` events from EVENTS_FILE into per-session runs.

    Each returned run has the shape::

        {
          "session_id": "<id or 'unknown'>",
          "task": "<first user message from transcript, or None>",
          "tag":  "<primary 'tool/model' for the run, or 'mixed'>",
          "started_at":       "<ISO ts of first phase>",
          "ended_at":         "<ISO ts of last phase>",
          "total_duration_ms": <int>,   # ended_at - started_at
          "phases": [
            {
              "phase": "plan" | "execute" | ... | "unknown",
              "tool":  "claude" | "codex",
              "model": "<str>",
              "exit_code": 0 | <int> | None,
              "end_ts": "<ISO ts>",
              "duration_ms": <int>,   # ts diff vs previous phase in same session; 0 for the first
              "status": "success" | "failure" | "pending",
            },
            ...
          ],
        }

    Runs are sorted by `ended_at` descending (newest first). Only the most
    recent `max_events` lines of the file are scanned so the endpoint stays
    cheap regardless of historical volume.
    """
    # Route through the mtime-keyed JSONL cache so repeated /api/timeline
    # polls don't re-parse the whole ledger. The helper preserves the prior
    # ``errors="replace"`` invariant (so a half-written concurrent append
    # from log_event.py — writes are not atomic at the byte level — can't
    # surface as UnicodeDecodeError) and silently skips malformed rows.
    rows = _load_jsonl_cached(EVENTS_FILE)
    if not rows:
        return []
    tail = rows[-max_events:] if len(rows) > max_events else rows

    by_session: dict[str, list[dict]] = {}
    for ev in tail:
        if not isinstance(ev, dict):
            continue
        if ev.get("kind") != "phase_dispatch":
            continue
        sid = ev.get("session_id") or "unknown"
        by_session.setdefault(sid, []).append(ev)

    runs: list[dict] = []
    for sid, events in by_session.items():
        events.sort(key=lambda e: _parse_iso_ts(e.get("ts")) or _dt.datetime.min.replace(tzinfo=_dt.timezone.utc))
        phases: list[dict] = []
        prev_dt: _dt.datetime | None = None
        tag_counter: dict[str, int] = {}
        for ev in events:
            ts = ev.get("ts") or ""
            cur_dt = _parse_iso_ts(ts)
            if cur_dt is None or prev_dt is None:
                duration_ms = 0
            else:
                duration_ms = max(0, int((cur_dt - prev_dt).total_seconds() * 1000))
            exit_code = ev.get("exit_code")
            if exit_code is None:
                status = "pending"
            elif exit_code == 0:
                status = "success"
            else:
                status = "failure"
            tool = ev.get("tool") or "unknown"
            model = ev.get("model") or "unknown"
            tag_counter[f"{tool}/{model}"] = tag_counter.get(f"{tool}/{model}", 0) + 1
            phases.append({
                "phase": ev.get("phase") or "unknown",
                "tool": tool,
                "model": model,
                "exit_code": exit_code,
                "end_ts": ts,
                "duration_ms": duration_ms,
                "status": status,
            })
            if cur_dt is not None:
                prev_dt = cur_dt
        if not phases:
            continue

        start_dt = _parse_iso_ts(phases[0]["end_ts"])
        end_dt = _parse_iso_ts(phases[-1]["end_ts"])
        if start_dt and end_dt:
            total_duration_ms = max(0, int((end_dt - start_dt).total_seconds() * 1000))
        else:
            total_duration_ms = 0

        if len(tag_counter) == 1:
            tag = next(iter(tag_counter))
        elif tag_counter:
            tag = "mixed"
        else:
            tag = "unknown"

        runs.append({
            "session_id": sid,
            "task": _lookup_session_task(sid),
            "tag": tag,
            "started_at": phases[0]["end_ts"],
            "ended_at": phases[-1]["end_ts"],
            "total_duration_ms": total_duration_ms,
            "phases": phases,
        })

    runs.sort(key=lambda r: r["ended_at"] or "", reverse=True)
    return runs


# ---------- Agent suggestions (helpers) ----------------------------------
#
# These power /api/agents/suggest. The skills equivalent (_detect_skill_
# suggestions + _handle_suggestion_draft) feeds on telemetry; agents have no
# per-agent telemetry, so we lean on three cheap signals instead — git log,
# recent jobs, and the editable agent catalog (so the LLM doesn't propose
# duplicates).

def _load_editable_agent_names() -> set[str]:
    """Return the set of agent slug names (filename stem) present in either
    the project (``<repo>/.claude/agents``) or user (``~/.claude/agents``)
    scope. Plugin agents are intentionally excluded — they are namespaced
    differently and we will never write into plugin paths anyway."""
    names: set[str] = set()
    for d in (ROOT / ".claude" / "agents",
              Path.home() / ".claude" / "agents"):
        try:
            if not d.is_dir():
                continue
            for f in d.glob("*.md"):
                stem = f.stem.strip().lower()
                if stem:
                    names.add(stem)
        except OSError:
            continue
    return names


def _recent_job_tasks(max_jobs: int = 50) -> list[str]:
    """Most-recent ``task`` strings from ``JOBS_DIR/*.json``, deduped while
    preserving first-seen order. Bad JSON / missing files are skipped
    silently — this signal is best-effort context for the LLM."""
    if not JOBS_DIR.is_dir():
        return []
    try:
        entries = sorted(JOBS_DIR.glob("*.json"),
                         key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for p in entries[:max_jobs * 2]:  # over-fetch in case many are blank
        try:
            obj = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        task = (obj.get("task") or "").strip()
        if not task or task in seen:
            continue
        seen.add(task)
        out.append(task)
        if len(out) >= max_jobs:
            break
    return out


def _git_log_excerpt(max_commits: int = 50) -> str:
    """``git log --oneline -N`` from the repo root, with a hard 10s timeout
    and silent OS-error fallback. Returns "" when git isn't available, the
    repo has no commits, or anything else goes wrong — the suggester prompt
    handles an empty section."""
    try:
        proc = subprocess.run(
            ["git", "log", "--oneline", f"-{max_commits}"],
            cwd=str(ROOT), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=10,
        )
        if proc.returncode != 0:
            return ""
        return proc.stdout or ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _build_agent_suggester_prompt(git_log: str, recent_tasks: list[str],
                                  existing_agent_names: set[str]) -> str:
    """Strict-JSON-output prompt that frames the agent-improver's
    Suggest-new-agents mode. Cap each task at 200 chars so the prompt stays
    small even with many jobs."""
    existing_block = "\n".join(sorted(existing_agent_names)) or "(none)"
    bullets = "\n".join(f"- {t[:200]}" for t in recent_tasks) or "(none)"
    git_block = git_log.strip() or "(none)"
    return (
        "OUTPUT FORMAT (STRICT): Respond with ONE JSON object on a single "
        "line. NO prose, NO commentary, NO markdown fences. If you write "
        "anything else, the output is INVALID.\n\n"
        "Schema:\n"
        '  {"suggestions": [{"name": "<lowercase-slug>", '
        '"description": "<one-sentence trigger description>", '
        '"trigger_phrasings": ["...", "..."], '
        '"rationale": "<why a dedicated agent helps>", '
        '"tools": "<comma-separated tool names, or empty string>", '
        '"confidence": "high|medium|low", '
        '"body": "<full agent file body that goes AFTER the YAML '
        'frontmatter; markdown ok>"}]}\n\n'
        "If there is no meaningful pattern to surface, return: "
        '{"suggestions": []}\n\n'
        "ROLE: You are the agent-improver in Suggest-new-agents mode. Look "
        "at the user's recent git activity and recent dashboard jobs and "
        "propose NEW agents that would capture repeated workflows. "
        "Cross-check against the existing agent catalogue to avoid "
        "duplicates. Be CONSERVATIVE — return [] rather than weak ideas. "
        "At most 3 suggestions.\n\n"
        "Each suggestion's \"body\" MUST start with a short purpose "
        "statement (1-2 sentences) and a short workflow (3-5 bullet "
        "steps). Do NOT include YAML frontmatter in \"body\" — the server "
        "adds the frontmatter from the JSON fields.\n\n"
        "=== Existing agent names (do NOT propose duplicates) ===\n"
        f"<<<EXISTING\n{existing_block}\nEXISTING>>>\n\n"
        "=== Recent git activity (oneline) ===\n"
        f"<<<GIT\n{git_block}\nGIT>>>\n\n"
        "=== Recent dashboard job tasks ===\n"
        f"<<<JOBS\n{bullets}\nJOBS>>>\n\n"
        "Now respond with ONLY the JSON object."
    )


def _parse_agent_suggestions_output(stdout: str) -> list[dict] | None:
    """Extract and validate the suggester's JSON output. Returns a list of
    valid suggestions (possibly empty) or ``None`` if the output is not a
    parseable object with the expected shape. Drops individual items that
    are missing required fields — partial responses still yield the valid
    subset rather than failing the whole call."""
    obj = _parse_improver_output(stdout)
    if obj is None or not isinstance(obj, dict):
        return None
    raw = obj.get("suggestions")
    if not isinstance(raw, list):
        return None
    out: list[dict] = []
    for s in raw:
        if not isinstance(s, dict):
            continue
        name = (s.get("name") or "").strip().lower()
        slug = re.sub(r"[^a-z0-9-]+", "-", name).strip("-")
        if not slug or len(slug) > 80:
            continue
        desc = (s.get("description") or "").strip()
        if not desc:
            continue
        triggers = s.get("trigger_phrasings") or []
        if not isinstance(triggers, list):
            continue
        triggers = [str(t).strip() for t in triggers if str(t).strip()]
        confidence = (s.get("confidence") or "").strip().lower()
        if confidence not in ("high", "medium", "low"):
            confidence = "medium"
        body = s.get("body") or ""
        if not isinstance(body, str) or not body.strip():
            continue
        tools = s.get("tools") or ""
        if not isinstance(tools, str):
            tools = ""
        rationale = (s.get("rationale") or "").strip()
        out.append({
            "name": slug,
            "slug": slug,
            "description": desc,
            "trigger_phrasings": triggers,
            "rationale": rationale,
            "tools": tools.strip(),
            "confidence": confidence,
            "body": body,
        })
    return out


def _persist_agent_proposal(suggestion: dict, *, source_signal: dict) -> str | None:
    """Write the ``{pid}.json`` + ``{pid}.body.md`` pair under
    ``AGENT_PROPOSALS_DIR`` and return the proposal id. ``None`` on any
    OS-level write failure so the caller can skip and continue."""
    slug = suggestion["slug"]
    ts_dt = _dt.datetime.now(_dt.timezone.utc)
    pid = f"_agent-{slug}-{ts_dt.strftime('%Y%m%d-%H%M%S')}"
    payload = {
        "id": pid,
        "kind": "agent-draft",
        "name": suggestion["name"],
        "slug": slug,
        "description": suggestion["description"],
        "trigger_phrasings": suggestion["trigger_phrasings"],
        "rationale": suggestion["rationale"],
        "tools": suggestion["tools"],
        "confidence": suggestion["confidence"],
        "ts": ts_dt.isoformat(timespec="seconds"),
        "source_signal": source_signal,
        "status": "pending",
        "target_path": f".claude/agents/{slug}.md",
        "applied_at": None,
        "installed_path": None,
    }
    try:
        AGENT_PROPOSALS_DIR.mkdir(parents=True, exist_ok=True)
        (AGENT_PROPOSALS_DIR / f"{pid}.json").write_text(
            json.dumps(payload, indent=2), encoding="utf-8")
        (AGENT_PROPOSALS_DIR / f"{pid}.body.md").write_text(
            suggestion["body"], encoding="utf-8")
    except OSError as e:
        # Best-effort persistence; the caller silently skips this proposal
        # when we return None. Log so operators see the underlying cause
        # (disk full, permissions, file locked on Windows) rather than
        # just observing "fewer agent suggestions than expected".
        print(f"[serve] persist_agent_proposal {pid} failed: {e}", flush=True)
        return None
    return pid


class Handler(http.server.SimpleHTTPRequestHandler):
    # Sensitive paths that MUST NOT be served by the static handler. Resolved
    # at class load so symlinks and Windows case differences cannot bypass.
    # The dashboard intentionally reads other project files (.ai/memory.md,
    # .ai/decisions.md, .ai/project.yaml, .ai/models.yaml, .ai/plans/*,
    # .ai/specs/*, .ai/packets/*, .ai/events.jsonl, .claude/skills/*) via this
    # handler — those must keep working, so we blocklist instead of allowlist.
    _BLOCKED_PATHS = tuple(
        os.path.normcase(os.path.realpath(str(p)))
        for p in (
            ROOT / ".git",
            ROOT / ".claude" / "settings.json",
            ROOT / ".aws",
            ROOT / ".ssh",
            ROOT / ".docker",
            ROOT / "secrets",
        )
    )
    # Basename patterns that must never be served regardless of location.
    # Normcased (lowercase on Windows) so we compare consistently after
    # os.path.normcase on the resolved path.
    _BLOCKED_NAMES = frozenset({
        ".env", ".env.local", ".env.production", ".env.development",
        ".env.staging", ".env.test",
        ".npmrc", ".netrc",
        "credentials",
    })
    _BLOCKED_NAME_PREFIXES = ("id_",)
    _BLOCKED_NAME_SUFFIXES = (".pem", ".key")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def translate_path(self, path):
        real = super().translate_path(path)
        resolved = os.path.normcase(os.path.realpath(real))
        base = os.path.basename(resolved)
        if (base in self._BLOCKED_NAMES
                or base.startswith(self._BLOCKED_NAME_PREFIXES)
                or base.endswith(self._BLOCKED_NAME_SUFFIXES)):
            return os.path.join(real, "__blocked_sensitive_path__")
        for blocked in self._BLOCKED_PATHS:
            if resolved == blocked or resolved.startswith(blocked + os.sep):
                return os.path.join(real, "__blocked_sensitive_path__")
        return real

    # ----- routing -----
    def do_GET(self):  # noqa: N802 (stdlib signature)
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/list":
            self._handle_list(urllib.parse.parse_qs(parsed.query))
            return
        if parsed.path == "/api/jobs":
            self._handle_jobs_list()
            return
        if parsed.path == "/api/sessions":
            self._handle_sessions_list()
            return
        if parsed.path == "/api/transcripts":
            self._handle_transcripts_list()
            return
        if parsed.path == "/api/usage/total":
            self._handle_usage_total()
            return
        if parsed.path == "/api/timeline":
            self._handle_timeline()
            return
        if parsed.path == "/api/auto-select":
            self._handle_auto_select(parsed)
            return
        if parsed.path == "/api/system/info":
            self._handle_system_info()
            return
        if parsed.path == "/api/settings":
            self._handle_settings_get()
            return
        m = re.fullmatch(r"/api/transcripts/([0-9a-fA-F-]+)/stream", parsed.path)
        if m:
            self._handle_transcript_stream(m.group(1))
            return
        if parsed.path == "/api/skills":
            self._handle_skills_list()
            return
        if parsed.path == "/api/skills/all":
            self._handle_skills_all()
            return
        if parsed.path == "/api/agents/all":
            self._handle_agents_all()
            return
        if parsed.path == "/api/agents/content":
            self._handle_agent_content(urllib.parse.parse_qs(parsed.query))
            return
        if parsed.path == "/api/agents/proposals":
            self._handle_agent_proposals_list()
            return
        m = re.fullmatch(r"/api/agents/proposals/([A-Za-z0-9_\-]+)", parsed.path)
        if m:
            self._handle_agent_proposal_get(m.group(1))
            return
        if parsed.path == "/api/skills/metrics":
            self._handle_skills_metrics(urllib.parse.parse_qs(parsed.query))
            return
        if parsed.path == "/api/skills/suggestions":
            self._handle_skills_suggestions(urllib.parse.parse_qs(parsed.query))
            return
        if parsed.path == "/api/skills/content":
            self._handle_skill_content(urllib.parse.parse_qs(parsed.query))
            return
        if parsed.path == "/api/skills/improvements":
            self._handle_skill_improvements(urllib.parse.parse_qs(parsed.query))
            return
        if parsed.path == "/api/skills/proposals":
            self._handle_proposals_list()
            return
        m = re.fullmatch(r"/api/skills/proposals/([A-Za-z0-9_\-]+)", parsed.path)
        if m:
            self._handle_proposal_get(m.group(1))
            return
        if parsed.path == "/api/files/list":
            self._handle_files_list(urllib.parse.parse_qs(parsed.query))
            return
        if parsed.path == "/api/files/read":
            self._handle_file_read(urllib.parse.parse_qs(parsed.query))
            return
        m = re.fullmatch(r"/api/jobs/([0-9a-f-]+)/stream", parsed.path)
        if m:
            self._handle_job_stream(m.group(1))
            return
        m = re.fullmatch(r"/api/jobs/([0-9a-f-]+)", parsed.path)
        if m:
            self._handle_job_get(m.group(1), urllib.parse.parse_qs(parsed.query))
            return
        # PTY (real shell) sessions live alongside chat jobs; same shape.
        if parsed.path == "/api/ptys":
            self._handle_ptys_list()
            return
        m = re.fullmatch(r"/api/ptys/([0-9a-f-]+)/io", parsed.path)
        if m:
            self._handle_pty_ws(m.group(1))
            return
        m = re.fullmatch(r"/api/ptys/([0-9a-f-]+)", parsed.path)
        if m:
            self._handle_pty_get(m.group(1))
            return
        super().do_GET()

    def do_POST(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if not self._csrf_guard():
            return
        body = self._read_json_body()
        if body is None:
            return  # already responded with 400
        if parsed.path == "/api/memory":
            self._handle_memory(body)
        elif parsed.path == "/api/decisions":
            self._handle_decisions(body)
        elif parsed.path == "/api/events/clear":
            self._handle_events_clear()
        elif parsed.path == "/api/models/dispatch_mode":
            self._handle_dispatch_mode(body)
        elif parsed.path == "/api/models/phase":
            self._handle_phase_update(body)
        elif parsed.path == "/api/jobs":
            self._handle_jobs_create(body)
        elif parsed.path == "/api/workflow/check":
            self._handle_workflow_check()
        elif parsed.path == "/api/workflow/update":
            self._handle_workflow_update()
        elif parsed.path == "/api/settings/improver":
            self._handle_improver_update(body)
        elif parsed.path == "/api/settings/auto_select":
            self._handle_auto_select_update(body)
        elif parsed.path == "/api/ptys":
            self._handle_pty_create(body)
        else:
            m = re.fullmatch(r"/api/jobs/([0-9a-f-]+)/cancel", parsed.path)
            if m:
                self._handle_job_cancel(m.group(1))
                return
            m = re.fullmatch(r"/api/jobs/([0-9a-f-]+)/input", parsed.path)
            if m:
                self._handle_job_input(m.group(1), body)
                return
            m = re.fullmatch(r"/api/ptys/([0-9a-f-]+)/kill", parsed.path)
            if m:
                self._handle_pty_kill(m.group(1))
                return
            m = re.fullmatch(r"/api/jobs/([0-9a-f-]+)/interrupt", parsed.path)
            if m:
                self._handle_job_interrupt(m.group(1))
                return
            m = re.fullmatch(r"/api/skills/proposals/([A-Za-z0-9_\-]+)/(accept|reject)", parsed.path)
            if m:
                self._handle_proposal_decision(m.group(1), m.group(2))
                return
            m = re.fullmatch(r"/api/skills/suggestions/([A-Za-z0-9_\-]+)/draft", parsed.path)
            if m:
                self._handle_suggestion_draft(m.group(1))
                return
            if parsed.path == "/api/agents/suggest":
                self._handle_agent_suggest()
                return
            m = re.fullmatch(r"/api/agents/proposals/([A-Za-z0-9_\-]+)/(accept|reject)", parsed.path)
            if m:
                self._handle_agent_proposal_decision(m.group(1), m.group(2))
                return
            self._json(404, {"error": "unknown endpoint", "path": parsed.path})

    # ----- helpers -----
    def end_headers(self) -> None:  # noqa: N802 (stdlib signature)
        # Prevent stale HTML/CSS/JS after dashboard upgrades. The dashboard is
        # served on localhost so cache invalidation cost is negligible, and
        # otherwise a Ctrl+F5 is required after every change.
        self.send_header("Cache-Control", "no-store, must-revalidate")
        super().end_headers()

    def _csrf_guard(self) -> bool:
        if not _origin_allowed(self.headers):
            self._json(403, {"error": "origin not allowed"})
            return False
        return True

    def _read_json_body(self) -> dict | None:
        # Reject oversized bodies up front using only the Content-Length
        # header so we never allocate the buffer for a DoS payload. The
        # 1 MiB ceiling (``MAX_JSON_BODY``) is well above legitimate
        # dashboard traffic; the largest known payload is the chat
        # composer with inlined files, which is capped client-side at
        # ~256 KB.
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except (TypeError, ValueError):
            self._json(411, {"error": "missing or invalid Content-Length"})
            return None
        if length <= 0:
            return {}
        if length > MAX_JSON_BODY:
            self._json(413, {"detail": "request body too large", "error": "payload too large"})
            return None
        try:
            raw = self.rfile.read(length).decode("utf-8")
            return json.loads(raw) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            # Don't leak parser internals to the client. Log the real reason
            # server-side so operators can still diagnose malformed bodies.
            print(f"[serve] bad JSON body: {e}", flush=True)
            self._json(400, {"error": "invalid JSON", "detail": "invalid JSON in request body"})
            return None

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # Defense-in-depth: stop a misconfigured proxy / antivirus
        # MIME-sniffing a JSON response into ``text/html`` and rendering it.
        # The API never returns HTML; nosniff makes that a hard contract.
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:  # quieter logs
        sys.stderr.write(f"[dashboard] {fmt % args}\n")

    # ----- GET handlers -----
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

    def _handle_events_clear(self) -> None:
        path = ROOT / ".ai" / "events.jsonl"
        try:
            if path.exists():
                path.unlink()
        except OSError as e:
            self._json(500, {"error": "could not clear events", "detail": str(e)})
            return
        self._json(200, {"ok": True})

    # ----- jobs -----
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
        if resume_session_id and len(resume_session_id) > 80:
            self._json(400, {"error": "resume_session_id must be 80 chars or fewer"})
            return
        # Optional fork: like resume but adds --fork-session so the new
        # turns land in a fresh session id instead of overwriting the
        # original transcript.
        fork_session_id = (body.get("fork_session_id") or "").strip() or None
        if fork_session_id and len(fork_session_id) > 80:
            self._json(400, {"error": "fork_session_id must be 80 chars or fewer"})
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
        if not shutil.which(required_bin):
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
            task = _lookup_session_task(p.stem) if idx < TASK_PREVIEW_LIMIT else None
            items.append({
                "session_id": p.stem,
                "size_bytes": st.st_size,
                "modified": _dt.datetime.fromtimestamp(st.st_mtime, _dt.timezone.utc).isoformat(timespec="seconds"),
                "path": str(p.relative_to(tdir.parent)),
                "task": task,
            })
        self._json(200, {"transcripts": items, "dir": str(tdir)})

    def _handle_usage_total(self) -> None:
        """Aggregate token usage across every Claude transcript for this
        repo. Powers the overview's "Tokens used" card."""
        self._json(200, _aggregate_project_token_usage())

    def _handle_timeline(self) -> None:
        """Pipeline Gantt data — phase_dispatch events from .ai/events.jsonl
        grouped per session_id. Powers the Timeline view."""
        self._json(200, {"runs": _load_timeline_runs()})

    def _handle_auto_select(self, parsed) -> None:
        """Auto-select scorer ranking — aggregated from .ai/metrics.jsonl.
        Powers the Auto-select view. Accepts `?min_samples=N` (clamp 1..50,
        default 3); invalid values fall back to the default."""
        raw = urllib.parse.parse_qs(parsed.query or "").get("min_samples", [None])[0]
        try:
            min_samples = max(1, min(50, int(raw)))
        except (TypeError, ValueError):
            min_samples = 3
        self._json(200, _load_auto_select_ranking(min_samples=min_samples))

    # ----- settings (workflow update) helpers -----
    #
    # /api/workflow/{check,update} clone the template upstream into a temporary
    # directory on every call and run update-workflow.sh from there. This is
    # deliberately different from the old /api/git/* endpoints (which did a
    # plain `git pull` on the host project repo): in a project that just
    # *consumes* the workflow, the host repo's history has nothing to do with
    # workflow updates, so a pull there was either a no-op or — worse — pulled
    # unrelated project commits.

    def _run_subprocess(
        self,
        args: list[str],
        cwd: str | None = None,
        timeout: int = 60,
    ) -> tuple[int, str, str]:
        try:
            proc = subprocess.run(
                args,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout,
                encoding="utf-8",
                errors="replace",
            )
            return proc.returncode, proc.stdout, proc.stderr
        except subprocess.TimeoutExpired:
            return -1, "", f"{args[0]} timed out after {timeout}s"
        except FileNotFoundError:
            return -2, "", f"{args[0]} not found on PATH"

    @staticmethod
    def _find_bash() -> str | None:
        # On Windows, prefer Git-for-Windows bash. The System32\bash.exe shipped
        # with WSL won't see Windows-style C:\... paths the way update-workflow.sh
        # expects.
        if sys.platform == "win32":
            for guess in (
                r"C:\Program Files\Git\bin\bash.exe",
                r"C:\Program Files\Git\usr\bin\bash.exe",
                r"C:\Program Files (x86)\Git\bin\bash.exe",
            ):
                if os.path.isfile(guess):
                    return guess
            candidate = shutil.which("bash")
            if candidate and "system32" not in candidate.lower():
                return candidate
            return None
        return shutil.which("bash")

    def _is_template_repo(self) -> bool:
        # True when the dashboard is being served from a checkout of the
        # template itself (e.g. during workflow development), in which case a
        # one-click "update" would clobber in-progress local edits with the
        # upstream copy. Compared case-insensitively and ignoring a trailing
        # ".git" so https/ssh/path variants all collapse to the same key.
        rc, out, _ = self._run_subprocess(
            ["git", "config", "--get", "remote.origin.url"],
            cwd=str(ROOT),
            timeout=5,
        )
        if rc != 0:
            return False
        url = out.strip().lower().removesuffix(".git")
        template = WORKFLOW_TEMPLATE_URL.lower().removesuffix(".git")
        return bool(url) and url == template

    def _clone_template(self, dest: str, depth: int = 1) -> tuple[int, str, str]:
        return self._run_subprocess(
            ["git", "clone", f"--depth={max(1, depth)}", WORKFLOW_TEMPLATE_URL, dest],
            timeout=120,
        )

    @staticmethod
    def _read_workflow_version() -> str | None:
        try:
            # ``errors="replace"`` so a corrupted/version-file edge case
            # never breaks the workflow-check endpoint; a non-decodable
            # SHA-shaped value won't pass the downstream regex anyway.
            return WORKFLOW_VERSION_FILE.read_text(encoding="utf-8", errors="replace").strip() or None
        except (FileNotFoundError, OSError):
            return None

    def _handle_workflow_check(self) -> None:
        tmp_parent = tempfile.mkdtemp(prefix="aiwt-check-")
        clone_dir = os.path.join(tmp_parent, "template")
        try:
            # Depth 20 so the recent-commits list has context even when the
            # project is far behind. Cheap for a small template repo.
            rc, _out, err = self._clone_template(clone_dir, depth=20)
            if rc != 0:
                self._json(200, {
                    "success": False,
                    "error": "clone_failed",
                    "message": "Could not clone the workflow template upstream.",
                    "output": err.strip(),
                })
                return

            rc_sha, sha_out, _ = self._run_subprocess(
                ["git", "-C", clone_dir, "rev-parse", "HEAD"], timeout=10
            )
            upstream_sha = sha_out.strip() if rc_sha == 0 else ""

            rc_log, log_out, _ = self._run_subprocess(
                ["git", "-C", clone_dir, "log", "--oneline", "--no-decorate", "-n", "20"],
                timeout=10,
            )
            commits = []
            if rc_log == 0:
                for line in log_out.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    parts = line.split(" ", 1)
                    commits.append({
                        "sha": parts[0],
                        "subject": parts[1] if len(parts) > 1 else "",
                    })

            current_sha = self._read_workflow_version()
            is_template = self._is_template_repo()
            # Treat the template checkout as always up-to-date: serving the
            # dashboard from the template repo itself means HEAD *is* upstream,
            # so a "newer version available" notice would be a false positive.
            has_updates = (
                bool(upstream_sha)
                and current_sha is not None
                and current_sha != upstream_sha
                and not is_template
            )

            if is_template:
                message = "Serving from the template repo itself — already on HEAD."
            elif current_sha is None:
                message = (
                    "No installed workflow version recorded yet — "
                    "apply update to record the current upstream sha."
                )
            elif has_updates:
                message = "New workflow version available upstream."
            else:
                message = "Workflow is up to date with upstream."

            self._json(200, {
                "success": True,
                "upstream_sha": upstream_sha,
                "current_sha": current_sha,
                "has_updates": has_updates,
                "is_template_repo": is_template,
                "template_url": WORKFLOW_TEMPLATE_URL,
                "commits": commits,
                "message": message,
            })
        finally:
            shutil.rmtree(tmp_parent, ignore_errors=True)

    def _handle_workflow_update(self) -> None:
        # Non-blocking acquire: if another client already triggered a workflow
        # update, refuse this one with 409 instead of stacking subprocesses
        # that would interleave file writes against the same tree.
        if not _WORKFLOW_UPDATE_LOCK.acquire(blocking=False):
            self._json(409, {"error": "workflow update already in progress"})
            return
        try:
            if self._is_template_repo():
                self._json(200, {
                    "success": False,
                    "error": "is_template_repo",
                    "message": (
                        "Refusing to self-update: this dashboard is being served "
                        "from a checkout of the template itself. Use `git pull` "
                        "on this checkout, then run "
                        "`bash update-workflow.sh <other-project>` from here."
                    ),
                    "output": "",
                })
                return

            bash_path = self._find_bash()
            if not bash_path:
                self._json(200, {
                    "success": False,
                    "error": "bash_not_found",
                    "message": (
                        "bash not found. Install Git for Windows (which bundles "
                        "bash) or ensure a POSIX bash is on PATH."
                    ),
                    "output": "",
                })
                return

            tmp_parent = tempfile.mkdtemp(prefix="aiwt-update-")
            clone_dir = os.path.join(tmp_parent, "template")
            try:
                rc, _out, err = self._clone_template(clone_dir)
                if rc != 0:
                    self._json(200, {
                        "success": False,
                        "error": "clone_failed",
                        "message": "Could not clone the workflow template upstream.",
                        "output": err.strip(),
                    })
                    return

                rc_sha, sha_out, _ = self._run_subprocess(
                    ["git", "-C", clone_dir, "rev-parse", "HEAD"], timeout=10
                )
                upstream_sha = sha_out.strip() if rc_sha == 0 else ""

                update_script = os.path.join(clone_dir, "update-workflow.sh")
                if not os.path.isfile(update_script):
                    self._json(200, {
                        "success": False,
                        "error": "missing_script",
                        "message": "Upstream checkout has no update-workflow.sh.",
                        "output": "",
                    })
                    return

                rc_u, out_u, err_u = self._run_subprocess(
                    [bash_path, update_script, str(ROOT)],
                    timeout=180,
                )
                output = out_u
                if err_u:
                    output = (output + "\n" + err_u).strip() if output else err_u.strip()
                output = output.strip()

                if rc_u != 0:
                    self._json(200, {
                        "success": False,
                        "error": "update_script_failed",
                        "message": "update-workflow.sh exited with a non-zero status.",
                        "output": output,
                        "exit_code": rc_u,
                    })
                    return

                # update-workflow.sh prints "Updated <path>" / "Created <path>" per
                # file it touched. Anything under .ai/dashboard/ means the running
                # serve.py / static assets just got overwritten — the user needs to
                # restart so the new code takes effect.
                restart_needed = False
                for line in output.splitlines():
                    if line.startswith(("Updated ", "Created ")) and "/.ai/dashboard/" in line.replace("\\", "/"):
                        restart_needed = True
                        break

                if upstream_sha:
                    try:
                        WORKFLOW_VERSION_FILE.parent.mkdir(parents=True, exist_ok=True)
                        _write_text_lf(WORKFLOW_VERSION_FILE, upstream_sha + "\n")
                    except OSError as e:
                        output = (output + f"\n[warn] could not write {WORKFLOW_VERSION_FILE}: {e}").strip()

                self._json(200, {
                    "success": True,
                    "message": "Workflow updated.",
                    "output": output,
                    "upstream_sha": upstream_sha,
                    "restart_dashboard": restart_needed,
                })
            finally:
                shutil.rmtree(tmp_parent, ignore_errors=True)
        finally:
            _WORKFLOW_UPDATE_LOCK.release()

    def _handle_system_info(self) -> None:
        try:
            improver_enabled = bool(_load_improver_config().get("enabled"))
        except Exception as e:
            print(f"[serve] system_info: improver config load failed: {e}", flush=True)
            improver_enabled = False
        try:
            host, port = self.server.server_address[0], self.server.server_address[1]
        except Exception as e:
            print(f"[serve] system_info: server_address read failed: {e}", flush=True)
            host, port = "127.0.0.1", BOUND_PORT
        self._json(200, {
            "host": host,
            "port": port,
            "configured_port": PORT,
            "repo_root": str(ROOT),
            "python_version": "%d.%d.%d" % sys.version_info[:3],
            "platform": sys.platform,
            "pid": os.getpid(),
            "uptime_seconds": int(time.time() - _SERVER_STARTED_AT),
            "auto_improver_enabled": improver_enabled,
            "events_file": str(EVENTS_FILE),
            "jobs_dir": str(JOBS_DIR),
        })

    # ----- workflow settings (improver / auto_select / per-phase tuning) -----

    _AUTO_SELECT_BUDGETS = {"low", "medium", "high"}
    _IMPROVER_INT_FIELDS = ("small_change_max_lines", "min_interval_seconds",
                            "timeout_seconds", "revert_after_n_uses")
    _IMPROVER_BOUNDS = {
        "small_change_max_lines": (1, 200),
        "min_interval_seconds":   (0, 86400),
        "timeout_seconds":        (10, 3600),
        "revert_after_n_uses":    (1, 1000),
    }

    def _handle_settings_get(self) -> None:
        # Read the improver block straight from YAML so saved values survive
        # the AI_WORKFLOW_DISABLE_IMPROVER env override (which only affects
        # _load_improver_config's runtime view). Fall back to defaults
        # field-by-field when YAML is missing or has no entry.
        models_path = ROOT / ".ai" / "models.yaml"
        imp_raw = _read_yaml_field(models_path, "improver")

        def _imp_get(key, default):
            v = imp_raw.get(key)
            if v is None or v == "":
                return default
            return v

        improver_enabled = (imp_raw.get("enabled") or
                            ("true" if _IMPROVER_DEFAULTS.get("enabled") else "false")).lower() == "true"
        improver_disabled_by_env = str(
            os.environ.get("AI_WORKFLOW_DISABLE_IMPROVER", "")
        ).strip().lower() in {"1", "true", "yes", "on"}

        auto_raw = _read_yaml_field(models_path, "auto_select")
        auto_enabled = (auto_raw.get("enabled") or "false").lower() == "true"
        auto_token_budget = auto_raw.get("token_budget") or "medium"
        phases_raw = auto_raw.get("phases") or ""
        phases_list = [
            p.strip().strip('"\'') for p in phases_raw.strip("[]").split(",") if p.strip()
        ]

        phase_names = ("plan", "execute", "review", "rescue", "maintenance", "bootstrap")
        phases_out = {}
        for ph in phase_names:
            block = _read_yaml_field(models_path, ph)
            phases_out[ph] = {
                "tool":             block.get("tool", ""),
                "model":            block.get("model", ""),
                "mode":             block.get("mode", ""),
                "reasoning_effort": block.get("reasoning_effort", ""),
                "timeout_seconds":  block.get("timeout_seconds", ""),
            }

        self._json(200, {
            "improver": {
                "enabled":                improver_enabled,
                "small_change_max_lines": _imp_get("small_change_max_lines", _IMPROVER_DEFAULTS["small_change_max_lines"]),
                "min_interval_seconds":   _imp_get("min_interval_seconds",   _IMPROVER_DEFAULTS["min_interval_seconds"]),
                "timeout_seconds":        _imp_get("timeout_seconds",        _IMPROVER_DEFAULTS["timeout_seconds"]),
                "revert_after_n_uses":    _imp_get("revert_after_n_uses",    _IMPROVER_DEFAULTS["revert_after_n_uses"]),
                "disabled_by_env":        improver_disabled_by_env,
            },
            "auto_select": {
                "enabled":      auto_enabled,
                "token_budget": auto_token_budget,
                "phases":       phases_list,
            },
            "phases": phases_out,
        })

    def _handle_improver_update(self, body: dict) -> None:
        updates: dict[str, str | None] = {}
        if "enabled" in body:
            val = body.get("enabled")
            updates["enabled"] = "true" if val in (True, "true", "True", 1, "1") else "false"
        for f in self._IMPROVER_INT_FIELDS:
            if f in body:
                raw = body.get(f)
                if raw == "" or raw is None:
                    updates[f] = None
                    continue
                try:
                    n = int(raw)
                except (TypeError, ValueError):
                    self._json(400, {"error": f"{f} must be an integer or empty"})
                    return
                lo, hi = self._IMPROVER_BOUNDS[f]
                if n < lo or n > hi:
                    self._json(400, {"error": f"{f} must be in [{lo}, {hi}]"})
                    return
                updates[f] = str(n)
        if not updates:
            self._json(400, {"error": "no updatable fields (enabled, small_change_max_lines, "
                                       "min_interval_seconds, timeout_seconds, revert_after_n_uses)"})
            return
        path = ROOT / ".ai" / "models.yaml"
        if not path.exists():
            self._json(404, {"error": "models.yaml not found"})
            return
        try:
            new_text = _patch_or_create_block(
                # ``errors="replace"`` aligns with the other models.yaml
                # read paths — a stray non-UTF-8 byte in a hand-edited
                # config must not 500 the improver-config update endpoint.
                path.read_text(encoding="utf-8", errors="replace"),
                "improver",
                updates,
                creator_template="improver:\n  enabled: true\n",
            )
        except ValueError as e:
            self._json(500, {"error": str(e)})
            return
        _write_text_lf(path, new_text)
        self._json(200, {"ok": True, "updated": updates})

    def _handle_auto_select_update(self, body: dict) -> None:
        updates: dict[str, str | None] = {}
        if "enabled" in body:
            val = body.get("enabled")
            updates["enabled"] = "true" if val in (True, "true", "True", 1, "1") else "false"
        if "token_budget" in body:
            tb = (body.get("token_budget") or "").strip().lower()
            if tb not in self._AUTO_SELECT_BUDGETS:
                self._json(400, {"error": f"token_budget must be one of {sorted(self._AUTO_SELECT_BUDGETS)}"})
                return
            updates["token_budget"] = tb
        if "phases" in body:
            phases = body.get("phases")
            if not isinstance(phases, list):
                self._json(400, {"error": "phases must be a list of phase names"})
                return
            allowed = {"plan", "execute", "review", "rescue", "maintenance", "bootstrap"}
            cleaned = []
            for p in phases:
                if not isinstance(p, str) or p not in allowed:
                    self._json(400, {"error": f"phases must contain only {sorted(allowed)}"})
                    return
                if p not in cleaned:
                    cleaned.append(p)
            updates["phases"] = "[" + ", ".join(cleaned) + "]"
        if not updates:
            self._json(400, {"error": "no updatable fields (enabled, token_budget, phases)"})
            return
        path = ROOT / ".ai" / "models.yaml"
        if not path.exists():
            self._json(404, {"error": "models.yaml not found"})
            return
        try:
            new_text = _patch_or_create_block(
                # ``errors="replace"`` aligns with the other models.yaml
                # read paths — see _handle_improver_update.
                path.read_text(encoding="utf-8", errors="replace"),
                "auto_select",
                updates,
                creator_template="auto_select:\n  enabled: false\n  token_budget: medium\n  phases: [execute, review, rescue]\n",
            )
        except ValueError as e:
            self._json(500, {"error": str(e)})
            return
        _write_text_lf(path, new_text)
        self._json(200, {"ok": True, "updated": updates})

    # ----- composer helpers (skills, files) -----

    def _handle_skills_list(self) -> None:
        """List slash-command skills the composer can autocomplete. Reads
        the ``name`` + ``description`` frontmatter from every
        ``.claude/skills/<name>/SKILL.md`` in the repo."""
        skills_dir = ROOT / ".claude" / "skills"
        items = [
            {"name": e["name"], "description": e["description"]}
            for e in _scan_skills_dir(skills_dir)
        ]
        self._json(200, {"skills": items})

    def _handle_skills_all(self) -> None:
        """Consolidated skill catalog across both models.

        Reads three locations and emits one flat list plus a per-source
        summary so the dashboard can render group cards + a filterable grid:
          * ``project``       -> ``<repo>/.claude/skills``  (workflow skills)
          * ``claude_global`` -> ``~/.claude/skills``       (Claude user skills)
          * ``codex_global``  -> ``~/.codex/skills``        (Codex user skills)

        Each entry carries ``metrics: null`` as a forward-looking hook for
        the auto skill-improver that will record per-skill performance
        after jobs."""
        home = Path.home()
        sources = [
            ("project",       "Project workflow", "claude", ROOT / ".claude" / "skills"),
            ("claude_global", "Claude (global)",  "claude", home / ".claude" / "skills"),
            ("codex_global",  "Codex (global)",   "codex",  home / ".codex"   / "skills"),
        ]
        metrics_by_skill = _aggregate_skill_metrics()
        # Build a secondary index keyed by canonical short name so on-disk
        # skills (which usually carry no plugin prefix) still find matching
        # telemetry rows recorded with the prefix.
        metrics_by_name: dict[str, dict] = {}
        for agg in metrics_by_skill.values():
            n = agg.get("name") or ""
            if n and n not in metrics_by_name:
                metrics_by_name[n] = agg
        all_skills: list[dict] = []
        source_meta: dict[str, dict] = {}
        for src_id, label, tool, path in sources:
            entries = _scan_skills_dir(path)
            for e in entries:
                metrics = metrics_by_skill.get(e["name"]) or metrics_by_name.get(e["name"])
                all_skills.append({
                    "name": e["name"],
                    "description": e["description"],
                    "path": e["path"],
                    "source": src_id,
                    "source_label": label,
                    "tool": tool,
                    "metrics": metrics,
                })
            source_meta[src_id] = {
                "label": label,
                "tool": tool,
                "path": str(path),
                "exists": path.is_dir(),
                "count": len(entries),
            }
        self._json(200, {"skills": all_skills, "sources": source_meta})

    def _handle_agent_content(self, qs: dict[str, list[str]]) -> None:
        """Return the raw markdown of an agent file by path.

        Security: the requested path is resolved to an absolute path and
        verified to live under one of the four catalog roots returned by
        ``_handle_agents_all``. Anything outside those roots is rejected
        (403). This is the same trust boundary the catalog itself uses —
        plugin trees are read-only by design, but `.md` content is safe
        to surface for inspection."""
        raw = (qs.get("path") or [""])[0]
        if not raw:
            self._json(400, {"error": "missing path"})
            return
        home = Path.home()
        allowed_roots = [
            ROOT / ".claude" / "agents",
            home / ".claude" / "agents",
            home / ".claude" / "plugins" / "marketplaces",
            home / ".claude" / "plugins" / "cache",
        ]
        # Accept repo-relative paths (catalog returns those for project
        # agents) and absolute paths (catalog returns those for user +
        # plugin agents).
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = ROOT / candidate
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError):
            self._json(404, {"error": "agent file not found", "path": raw})
            return
        if resolved.suffix != ".md":
            self._json(400, {"error": "not a .md file"})
            return
        ok = False
        for root in allowed_roots:
            try:
                root_resolved = root.resolve(strict=False)
            except (OSError, RuntimeError):
                continue
            try:
                resolved.relative_to(root_resolved)
                ok = True
                break
            except ValueError:
                continue
        if not ok:
            self._json(403, {"error": "path is outside the agent catalog roots"})
            return
        try:
            text = resolved.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            self._json(500, {"error": "read failed", "detail": str(e)})
            return
        max_bytes = 256 * 1024
        truncated = False
        if len(text.encode("utf-8", errors="replace")) > max_bytes:
            text = text[: max_bytes // 2]
            truncated = True
        self._json(200, {"content": text, "truncated": truncated, "path": str(resolved)})

    def _handle_agents_all(self) -> None:
        """Consolidated agent catalog across project + user + plugin scopes.

        Reads four locations and emits one flat list plus a per-source
        summary so the dashboard can render group cards + a filterable grid:
          * ``project``       -> ``<repo>/.claude/agents/*.md``      (editable)
          * ``user``          -> ``~/.claude/agents/*.md``           (editable)
          * ``plugin_market`` -> ``~/.claude/plugins/marketplaces/**/agents/*.md`` (read-only)
          * ``plugin_cache``  -> ``~/.claude/plugins/cache/**/agents/*.md``        (read-only)

        Plugin agents are surfaced so the user can spot duplication with
        their own agents but are never editable from the dashboard."""
        home = Path.home()
        sources = [
            ("project",       "Project",          True,  ROOT / ".claude" / "agents",                       False),
            ("user",          "User (global)",    True,  home / ".claude" / "agents",                       False),
            ("plugin_market", "Plugin (market)",  False, home / ".claude" / "plugins" / "marketplaces",     True),
            ("plugin_cache",  "Plugin (cache)",   False, home / ".claude" / "plugins" / "cache",            True),
        ]
        all_agents: list[dict] = []
        source_meta: dict[str, dict] = {}
        for src_id, label, editable, path, recursive in sources:
            entries = _scan_agents_dir(path, recursive=recursive)
            for e in entries:
                all_agents.append({
                    "name": e["name"],
                    "description": e["description"],
                    "tools": e["tools"],
                    "model": e["model"],
                    "path": e["path"],
                    "source": src_id,
                    "source_label": label,
                    "editable": editable,
                })
            source_meta[src_id] = {
                "label": label,
                "editable": editable,
                "path": str(path),
                "exists": path.is_dir(),
                "count": len(entries),
            }
        # Duplicate-name detection across all sources for cross-scope hints.
        name_counts: dict[str, int] = {}
        for a in all_agents:
            name_counts[a["name"]] = name_counts.get(a["name"], 0) + 1
        for a in all_agents:
            a["duplicate"] = name_counts[a["name"]] > 1
        self._json(200, {"agents": all_agents, "sources": source_meta})

    def _handle_skills_suggestions(self, qs: dict[str, list[str]]) -> None:
        """Detect clusters of repeated work in the persistent job ledger and
        propose them as candidate skills. Pure read; no LLM call.

        Tunable via query params: ``threshold`` (0..1, default 0.5),
        ``min_cluster`` (default 3), ``days`` (default 30)."""
        def _qfloat(key: str, default: float) -> float:
            try:
                return float(qs.get(key, [str(default)])[0])
            except (TypeError, ValueError):
                return default

        def _qint(key: str, default: int) -> int:
            try:
                return int(qs.get(key, [str(default)])[0])
            except (TypeError, ValueError):
                return default

        threshold = max(0.0, min(1.0, _qfloat("threshold", 0.5)))
        min_cluster = max(2, _qint("min_cluster", 3))
        days = max(1, min(365, _qint("days", 30)))
        try:
            clusters = _detect_skill_suggestions(
                threshold=threshold,
                min_cluster=min_cluster,
                max_age_days=days,
            )
        except Exception as e:  # noqa: BLE001 - never break the dashboard
            self._json(500, {"error": "detector failed", "detail": str(e)})
            return
        self._json(200, {
            "suggestions": clusters,
            "params": {"threshold": threshold, "min_cluster": min_cluster, "days": days},
        })

    def _handle_proposals_list(self) -> None:
        """List every proposal under ``SKILL_PROPOSALS_DIR`` with status."""
        items: list[dict] = []
        if SKILL_PROPOSALS_DIR.is_dir():
            for p in sorted(SKILL_PROPOSALS_DIR.glob("*.json"),
                            key=lambda x: x.stat().st_mtime, reverse=True):
                try:
                    obj = json.loads(p.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                # Compact summary; full body is fetched via the detail endpoint.
                items.append({
                    "id": obj.get("id"),
                    "skill": obj.get("skill"),
                    "skill_path": obj.get("skill_path"),
                    "ts": obj.get("ts"),
                    "kind": obj.get("kind") or "improve",
                    "status": obj.get("status") or "pending",
                    "change_summary": obj.get("change_summary", ""),
                    "diff_lines": obj.get("diff_lines"),
                    "applied_at": obj.get("applied_at"),
                    "applied_via": obj.get("applied_via"),
                    "job_id": obj.get("job_id"),
                })
        self._json(200, {"proposals": items})

    def _handle_proposal_get(self, proposal_id: str) -> None:
        """Return one proposal with old + new content for diff rendering."""
        pj = SKILL_PROPOSALS_DIR / f"{proposal_id}.json"
        if not pj.is_file():
            self._json(404, {"error": "proposal not found"})
            return
        try:
            obj = json.loads(pj.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            self._json(500, {"error": "could not read proposal", "detail": str(e)})
            return
        for key, fname in (("old_content", f"{proposal_id}.old.md"),
                           ("new_content", f"{proposal_id}.new.md")):
            p = SKILL_PROPOSALS_DIR / fname
            try:
                obj[key] = p.read_text(encoding="utf-8") if p.is_file() else ""
            except OSError:
                obj[key] = ""
        self._json(200, obj)

    def _handle_proposal_decision(self, proposal_id: str, decision: str) -> None:
        """Apply or reject a pending proposal. Accept writes the new content
        to the skill path (with .bak backup); reject just marks the proposal
        rejected and leaves the skill untouched."""
        pj = SKILL_PROPOSALS_DIR / f"{proposal_id}.json"
        if not pj.is_file():
            self._json(404, {"error": "proposal not found"})
            return
        try:
            obj = json.loads(pj.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            self._json(500, {"error": "could not read proposal", "detail": str(e)})
            return
        # Drafts may be in status="accepted" from the older proposal-only
        # behaviour. Allow re-accepting those so the user can retro-install.
        is_redo_draft = (decision == "accept"
                         and obj.get("kind") == "draft"
                         and obj.get("status") == "accepted")
        if obj.get("status") not in (None, "pending") and not is_redo_draft:
            self._json(409, {"error": f"proposal already {obj.get('status')}"})
            return

        if decision == "reject":
            obj["status"] = "rejected"
            obj["applied_at"] = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
            try:
                pj.write_text(json.dumps(obj, indent=2), encoding="utf-8")
            except OSError as e:
                print(f"[serve] failed to write proposal {pj} (reject): {e}", flush=True)
                self._json(500, {"error": "write failed", "detail": str(e)})
                return
            _audit_improvement(obj.get("skill") or "", "rejected",
                               obj.get("change_summary", ""),
                               proposal_id, None,
                               int(obj.get("diff_lines") or 0),
                               source="manual")
            self._json(200, {"ok": True, "id": proposal_id, "status": "rejected"})
            return

        # decision == "accept"
        kind = obj.get("kind") or "improve"
        if kind == "draft":
            # New-skill draft: create the real skill file at
            # .claude/skills/<slug>/SKILL.md. Refuse to overwrite an
            # existing skill — the user must reject + re-draft (or rename)
            # if there's a collision.
            slug_raw = obj.get("skill") or obj.get("suggested_name") or ""
            slug = re.sub(r"[^a-z0-9-]+", "-", slug_raw.lower()).strip("-")
            if not slug or len(slug) > 80:
                self._json(400, {"error": f"invalid skill slug: {slug_raw!r}"})
                return
            target_dir = ROOT / ".claude" / "skills" / slug
            target_md = target_dir / "SKILL.md"
            if target_md.is_file():
                self._json(409, {
                    "error": "skill already exists at target path",
                    "target_path": f".claude/skills/{slug}/SKILL.md",
                    "hint": "Reject this draft and rename the slug, or "
                            "delete the existing skill first.",
                })
                return
            new_md = SKILL_PROPOSALS_DIR / f"{proposal_id}.new.md"
            try:
                new_content = new_md.read_text(encoding="utf-8")
            except OSError as e:
                self._json(500, {"error": "could not read draft body", "detail": str(e)})
                return
            try:
                target_dir.mkdir(parents=True, exist_ok=True)
                target_md.write_text(new_content, encoding="utf-8")
            except OSError as e:
                self._json(500, {"error": "write failed", "detail": str(e)})
                return
            target_rel = f".claude/skills/{slug}/SKILL.md"
            obj["status"] = "installed"
            obj["applied_at"] = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
            obj["applied_via"] = "manual"
            obj["target_path"] = target_rel
            obj["installed_path"] = target_rel
            try:
                pj.write_text(json.dumps(obj, indent=2), encoding="utf-8")
            except OSError as e:
                # SKILL.md already on disk; status stays "pending" in the
                # proposal file. Audit still runs so the ledger reflects truth.
                print(f"[serve] failed to write proposal {pj} (installed draft): {e}", flush=True)
            _audit_improvement(slug, "installed",
                               f"draft installed -> {target_rel}",
                               proposal_id, None,
                               int(obj.get("diff_lines") or 0),
                               source="manual")
            self._json(200, {
                "ok": True, "id": proposal_id, "status": "installed",
                "installed_path": target_rel,
                "note": f"Skill created at {target_rel}.",
            })
            return

        # kind == "improve": apply to the actual skill file.
        rel = obj.get("skill_path") or ""
        try:
            skill_path = (ROOT / rel).resolve()
            skill_path.relative_to(ROOT.resolve())
        except (ValueError, OSError):
            self._json(400, {"error": "invalid skill_path on proposal"})
            return
        if not skill_path.is_file():
            self._json(404, {"error": "skill file no longer exists", "path": rel})
            return
        new_md = SKILL_PROPOSALS_DIR / f"{proposal_id}.new.md"
        try:
            new_content = new_md.read_text(encoding="utf-8")
        except OSError as e:
            self._json(500, {"error": "could not read proposal body", "detail": str(e)})
            return
        ok = _apply_improvement(
            skill_path, new_content,
            source="manual",
            reason=obj.get("change_summary", "") or "",
            proposal_id=proposal_id,
            skill_id=obj.get("skill") or skill_path.parent.name,
            diff_lines=int(obj.get("diff_lines") or 0),
        )
        if not ok:
            self._json(500, {"error": "apply failed (see improvements.jsonl)"})
            return
        self._json(200, {"ok": True, "id": proposal_id, "status": "applied"})

    def _handle_suggestion_draft(self, cluster_id: str) -> None:
        """Phase 5: dispatch an LLM to draft a SKILL.md from a suggestion
        cluster. Saves the result as a ``kind=draft`` proposal — never
        writes into ``.claude/skills/`` directly."""
        # Global cap on concurrent draft/suggest subprocesses — both this
        # endpoint and /api/agents/suggest share the same `claude -p` / `codex`
        # binary and each can pin a request thread for `timeout_seconds`
        # (default 120s). Without the cap, N concurrent clients exhaust the
        # thread pool. Reply 429 with Retry-After so the UI can back off.
        if not _SUGGESTION_SEMAPHORE.acquire(blocking=False):
            self.send_response(429)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Retry-After", "30")
            body = json.dumps({"error": "too many concurrent draft requests; try again later"}).encode("utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        try:
            # Find the cluster (clusters are computed on demand, no persistence).
            clusters = _detect_skill_suggestions()
            cluster = next((c for c in clusters if c.get("id") == cluster_id), None)
            if not cluster:
                self._json(404, {"error": "cluster not found", "id": cluster_id})
                return
            cfg = _load_improver_config()
            if not shutil.which(cfg["tool"]):
                self._json(503, {"error": f"`{cfg['tool']}` CLI not on PATH"})
                return

            samples = "\n".join(f"- {s}" for s in (cluster.get("sample_tasks") or []))
            tokens = ", ".join(cluster.get("top_tokens") or [])
            skills = ", ".join(cluster.get("skills_invoked") or []) or "(none recorded)"
            prompt = (
                "You are drafting a NEW project skill (SKILL.md) for a repeated "
                "pattern of work detected in the user's recent jobs.\n\n"
                f"## Pattern fingerprint\n- Repetitions: {cluster.get('size')}\n"
                f"- Top tokens: {tokens}\n"
                f"- Skills invoked across cluster: {skills}\n"
                f"- Suggested slug: `{cluster.get('suggested_name')}`\n\n"
                f"## Sample tasks\n{samples}\n\n"
                "## Required output\n"
                "Return ONLY a JSON object on a single line — no prose, no fences.\n"
                "Schema:\n"
                '  {"name": "<lowercase-slug>", "description": "<one sentence trigger>", '
                '"new_content": "<full SKILL.md content with --- frontmatter>"}\n'
                "The SKILL.md must start with YAML frontmatter (name, description), then "
                "be a short, opinionated guide to executing this pattern. Keep it under "
                "~40 lines."
            )
            # Same stdin trick as the improver: long argv prompts fail silently on Windows.
            tool_bin = shutil.which(cfg["tool"]) or cfg["tool"]
            # Cap the request-thread wait at _SUGGESTION_HTTP_TIMEOUT_MAX
            # so a long ``cfg["timeout_seconds"]`` (up to 3600s) can't
            # park the dashboard via this interactive endpoint.
            http_timeout = min(
                int(cfg.get("timeout_seconds", 120)),
                _SUGGESTION_HTTP_TIMEOUT_MAX,
            )
            try:
                proc = subprocess.run(
                    [tool_bin, "-p", "--model", cfg["model"]],
                    cwd=str(ROOT), input=prompt,
                    capture_output=True, text=True,
                    timeout=http_timeout,
                    encoding="utf-8", errors="replace",
                )
            except (subprocess.TimeoutExpired, OSError) as e:
                self._json(500, {"error": "subprocess error", "detail": str(e)})
                return
            if proc.returncode != 0:
                self._json(500, {"error": f"exit {proc.returncode}",
                                 "stderr": (proc.stderr or "")[:300]})
                return
            parsed = _parse_improver_output(proc.stdout or "")
            if not parsed or not isinstance(parsed.get("new_content"), str):
                self._json(500, {"error": "draft output unparseable",
                                 "stdout_tail": (proc.stdout or "")[-300:]})
                return

            # Persist as a kind=draft proposal.
            SKILL_PROPOSALS_DIR.mkdir(parents=True, exist_ok=True)
            ts_dt = _dt.datetime.now(_dt.timezone.utc)
            slug_in = parsed.get("name") or cluster.get("suggested_name") or "new-skill"
            slug = re.sub(r"[^a-z0-9]+", "-", slug_in.lower()).strip("-") or "new-skill"
            pid = f"_new-{slug}-{ts_dt.strftime('%Y%m%d-%H%M%S')}"
            new_content = parsed["new_content"]
            payload = {
                "id": pid,
                "kind": "draft",
                "skill": slug,
                "suggested_name": slug,
                "skill_path": None,
                "target_path": f".claude/skills/{slug}/SKILL.md",
                "ts": ts_dt.isoformat(timespec="seconds"),
                "cluster_id": cluster_id,
                "cluster_size": cluster.get("size"),
                "description": parsed.get("description", ""),
                "change_summary": parsed.get("description", "")
                                  or f"Draft from cluster of {cluster.get('size')} jobs",
                "rationale": f"Detected pattern across {cluster.get('size')} repeated jobs",
                "diff_lines": len(new_content.splitlines()),
                "status": "pending",
                "applied_at": None,
                "applied_via": None,
            }
            try:
                (SKILL_PROPOSALS_DIR / f"{pid}.json").write_text(
                    json.dumps(payload, indent=2), encoding="utf-8")
                (SKILL_PROPOSALS_DIR / f"{pid}.old.md").write_text("", encoding="utf-8")
                (SKILL_PROPOSALS_DIR / f"{pid}.new.md").write_text(new_content, encoding="utf-8")
            except OSError as e:
                # Partial write: at least one of the three files may have
                # landed but the proposal is incomplete and the modal will
                # 500 trying to open it. Log + 500 so the operator sees the
                # cause rather than getting an opaque "unparseable" later.
                print(f"[serve] persist draft proposal {pid} failed: {e}", flush=True)
                self._json(500, {"error": "could not persist draft proposal", "detail": str(e)})
                return
            _audit_improvement(slug, "pending",
                               f"draft from cluster {cluster_id}",
                               pid, None, payload["diff_lines"], source="manual")
            self._json(201, payload)
        finally:
            _SUGGESTION_SEMAPHORE.release()

    # ----- Agent suggestions (agent-improver "Suggest-new-agents" mode) -----
    #
    # The skills auto-improver runs on telemetry: every job emits per-skill
    # success rows, and clusters of repeated tasks become "draft a SKILL.md"
    # proposals. Agents don't have that signal — no agent_metrics.jsonl, no
    # per-agent success rate. Instead this flow asks an LLM to look at three
    # cheap signals (git log + recent job task descriptions + existing agent
    # catalog) and propose new agents on demand. One-shot, never automatic.
    #
    # Reuses the improver config block from .ai/models.yaml (tool, model,
    # timeout). Persists each suggestion as a {pid}.json + {pid}.body.md pair
    # under AGENT_PROPOSALS_DIR. Accept writes the actual agent file at
    # .claude/agents/<slug>.md (refusing to overwrite). Reject just marks
    # status="rejected".

    def _handle_agent_suggest(self) -> None:
        """POST /api/agents/suggest — spawn a one-shot LLM that proposes new
        agents based on recent git + recent jobs + existing agents. Persists
        zero or more {pid}.json + .body.md proposals. Returns the count and
        the new proposal ids so the UI can refresh the list."""
        # Shares the rate-limit budget with _handle_suggestion_draft above
        # (same CLI binary, same long subprocess timeout). 429 + Retry-After
        # when the global budget is saturated.
        if not _SUGGESTION_SEMAPHORE.acquire(blocking=False):
            self.send_response(429)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Retry-After", "30")
            body = json.dumps({"error": "too many concurrent draft requests; try again later"}).encode("utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        try:
            cfg = _load_improver_config()
            tool_bin = shutil.which(cfg["tool"])
            if not tool_bin:
                self._json(503, {"error": f"`{cfg['tool']}` CLI not on PATH"})
                return
            existing = _load_editable_agent_names()
            recent_tasks = _recent_job_tasks()
            git_log = _git_log_excerpt()
            prompt = _build_agent_suggester_prompt(git_log, recent_tasks, existing)
            argv = [tool_bin, "-p", "--model", cfg["model"]]
            improver_sid: str | None = None
            if cfg["tool"] == "claude":
                improver_sid = str(uuid.uuid4())
                argv += ["--session-id", improver_sid]
            # Mirror _handle_suggestion_draft: cap the wall-clock so a long
            # ``cfg["timeout_seconds"]`` can't park the dashboard via this
            # interactive endpoint.
            http_timeout = min(
                int(cfg.get("timeout_seconds", 120)),
                _SUGGESTION_HTTP_TIMEOUT_MAX,
            )
            try:
                try:
                    proc = subprocess.run(
                        argv,
                        cwd=str(ROOT), input=prompt,
                        capture_output=True, text=True,
                        timeout=http_timeout,
                        encoding="utf-8", errors="replace",
                    )
                except (subprocess.TimeoutExpired, OSError) as e:
                    self._json(500, {"error": "subprocess error", "detail": str(e)})
                    return
                if proc.returncode != 0:
                    self._json(500, {"error": f"exit {proc.returncode}",
                                     "stderr": (proc.stderr or "")[:300]})
                    return
                suggestions = _parse_agent_suggestions_output(proc.stdout or "")
                if suggestions is None:
                    self._json(500, {"error": "suggester output unparseable",
                                     "stdout_tail": (proc.stdout or "")[-300:]})
                    return
                signal_summary = {
                    "commits": len([l for l in (git_log or "").splitlines() if l.strip()]),
                    "jobs": len(recent_tasks),
                    "existing": len(existing),
                }
                if not suggestions:
                    self._json(200, {"count": 0, "proposal_ids": [],
                                     "note": "no suggestions",
                                     "signal_summary": signal_summary})
                    return
                ids: list[str] = []
                for s in suggestions:
                    pid = _persist_agent_proposal(s, source_signal=signal_summary)
                    if pid:
                        ids.append(pid)
                self._json(200, {"count": len(ids), "proposal_ids": ids,
                                 "signal_summary": signal_summary})
            finally:
                _purge_claude_transcript(improver_sid)
        finally:
            _SUGGESTION_SEMAPHORE.release()

    def _handle_agent_proposals_list(self) -> None:
        """GET /api/agents/proposals — compact summary of every proposal on
        disk, newest first. Body content is fetched separately via the
        detail endpoint to keep the list response small."""
        items: list[dict] = []
        if AGENT_PROPOSALS_DIR.is_dir():
            for p in sorted(AGENT_PROPOSALS_DIR.glob("*.json"),
                            key=lambda x: x.stat().st_mtime, reverse=True):
                try:
                    obj = json.loads(p.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                items.append({
                    "id": obj.get("id"),
                    "name": obj.get("name"),
                    "slug": obj.get("slug"),
                    "description": obj.get("description"),
                    "trigger_phrasings": obj.get("trigger_phrasings") or [],
                    "confidence": obj.get("confidence"),
                    "ts": obj.get("ts"),
                    "status": obj.get("status") or "pending",
                    "applied_at": obj.get("applied_at"),
                    "installed_path": obj.get("installed_path"),
                    "target_path": obj.get("target_path"),
                })
        self._json(200, {"proposals": items})

    def _handle_agent_proposal_get(self, proposal_id: str) -> None:
        """GET /api/agents/proposals/<id> — full payload + body for the
        proposal modal. Path-validates the id to prevent traversal."""
        if not re.fullmatch(r"_agent-[a-z0-9-]+-\d{8}-\d{6}", proposal_id):
            self._json(400, {"error": "invalid proposal id"})
            return
        pj = AGENT_PROPOSALS_DIR / f"{proposal_id}.json"
        if not pj.is_file():
            self._json(404, {"error": "proposal not found"})
            return
        try:
            obj = json.loads(pj.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            self._json(500, {"error": "could not read proposal", "detail": str(e)})
            return
        body_path = AGENT_PROPOSALS_DIR / f"{proposal_id}.body.md"
        try:
            obj["body"] = body_path.read_text(encoding="utf-8") if body_path.is_file() else ""
        except OSError:
            obj["body"] = ""
        self._json(200, obj)

    def _handle_agent_proposal_decision(self, proposal_id: str, decision: str) -> None:
        """POST /api/agents/proposals/<id>/(accept|reject).

        Accept materialises the agent at .claude/agents/<slug>.md (refusing
        to overwrite an existing file — the user must reject + rename to
        re-create). Reject just flips the status and leaves the proposal on
        disk so it stays auditable."""
        if not re.fullmatch(r"_agent-[a-z0-9-]+-\d{8}-\d{6}", proposal_id):
            self._json(400, {"error": "invalid proposal id"})
            return
        pj = AGENT_PROPOSALS_DIR / f"{proposal_id}.json"
        if not pj.is_file():
            self._json(404, {"error": "proposal not found"})
            return
        try:
            obj = json.loads(pj.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            self._json(500, {"error": "could not read proposal", "detail": str(e)})
            return
        if obj.get("status") not in (None, "pending"):
            self._json(409, {"error": f"proposal already {obj.get('status')}"})
            return

        if decision == "reject":
            obj["status"] = "rejected"
            obj["applied_at"] = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
            try:
                pj.write_text(json.dumps(obj, indent=2), encoding="utf-8")
            except OSError as e:
                print(f"[serve] failed to write proposal {pj} (agent reject): {e}", flush=True)
                self._json(500, {"error": "write failed", "detail": str(e)})
                return
            self._json(200, {"ok": True, "id": proposal_id, "status": "rejected"})
            return

        # decision == "accept" — materialise the agent file.
        slug = (obj.get("slug") or "").strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,79}", slug):
            self._json(400, {"error": f"invalid slug: {slug!r}"})
            return
        agents_root = (ROOT / ".claude" / "agents").resolve()
        target = (agents_root / f"{slug}.md").resolve()
        try:
            target.relative_to(agents_root)
        except ValueError:
            self._json(400, {"error": "slug escapes agents directory"})
            return
        target_rel = f".claude/agents/{slug}.md"
        if target.is_file():
            self._json(409, {
                "error": "agent already exists",
                "target_path": target_rel,
                "hint": "Reject this proposal and rename the slug, or "
                        "delete the existing agent first.",
            })
            return
        # Build the agent file from the proposal payload. Only emit `tools:`
        # when non-empty so we don't accidentally pin an empty allowlist.
        front_lines = ["---", f"name: {slug}",
                       f"description: {(obj.get('description') or '').strip()}",
                       "model: sonnet"]
        tools = (obj.get("tools") or "").strip()
        if tools:
            front_lines.append(f"tools: {tools}")
        front_lines += ["---", ""]
        body = obj.get("body") or ""
        if not body:
            body_path = AGENT_PROPOSALS_DIR / f"{proposal_id}.body.md"
            try:
                body = body_path.read_text(encoding="utf-8") if body_path.is_file() else ""
            except OSError:
                body = ""
        content = "\n".join(front_lines) + body.lstrip("\n")
        if not content.endswith("\n"):
            content += "\n"
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            _write_text_lf(target, content)
        except OSError as e:
            self._json(500, {"error": "write failed", "detail": str(e)})
            return
        obj["status"] = "installed"
        obj["applied_at"] = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
        obj["installed_path"] = target_rel
        try:
            pj.write_text(json.dumps(obj, indent=2), encoding="utf-8")
        except OSError as e:
            # Agent .md is already on disk so the install is effectively done;
            # the proposal JSON just won't reflect "installed" until next
            # decision. Log so a chronic write failure (Windows file-lock,
            # permissions drift) is discoverable rather than silent.
            print(f"[serve] failed to write proposal {pj} (agent installed): {e}", flush=True)
        self._json(200, {
            "ok": True, "id": proposal_id, "status": "installed",
            "installed_path": target_rel,
            "note": f"Agent created at {target_rel}.",
        })

    def _handle_skill_content(self, qs: dict[str, list[str]]) -> None:
        """Return the SKILL.md content for one skill identified by
        ``source`` (project / claude_global / codex_global) + ``name``
        (directory name). Reads any of the three known roots, including
        global skill dirs that live outside the repo."""
        source = (qs.get("source", [""])[0] or "").strip()
        name = (qs.get("name", [""])[0] or "").strip()
        if not source or not name:
            self._json(400, {"error": "source and name are required"})
            return
        if not re.fullmatch(r"[a-zA-Z0-9_:\-.]+", name):
            self._json(400, {"error": "invalid skill name"})
            return
        home = Path.home()
        roots = {
            "project":       ROOT / ".claude" / "skills",
            "claude_global": home / ".claude" / "skills",
            "codex_global":  home / ".codex"  / "skills",
        }
        root = roots.get(source)
        if root is None:
            self._json(400, {"error": f"unknown source: {source}"})
            return
        skill_md = root / name / "SKILL.md"
        try:
            if not skill_md.is_file():
                self._json(404, {"error": "skill not found",
                                 "source": source, "name": name})
                return
            content = skill_md.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            self._json(500, {"error": str(e)})
            return
        # Cap at ~256KB so a huge file doesn't blow up the modal.
        cap = 256 * 1024
        truncated = len(content) > cap
        if truncated:
            content = content[:cap]
        self._json(200, {
            "source": source, "name": name,
            "path": str(skill_md),
            "content": content,
            "truncated": truncated,
        })

    def _handle_skill_improvements(self, qs: dict[str, list[str]]) -> None:
        """Return all rows from ``IMPROVEMENTS_LEDGER`` for one skill
        (matched on canonical name). Used by the skill detail modal to show
        the per-skill audit trail."""
        skill = (qs.get("skill", [""])[0] or "").strip()
        if not skill:
            self._json(400, {"error": "skill is required"})
            return
        if not re.fullmatch(r"[a-zA-Z0-9_:\-.]+", skill):
            self._json(400, {"error": "invalid skill name"})
            return
        rows = [o for o in _load_jsonl_cached(IMPROVEMENTS_LEDGER) if o.get("skill") == skill]
        # Sort a copy — never mutate the cached list, the next caller would see
        # rows in reverse-chronological order without going through this filter.
        rows = sorted(rows, key=lambda r: r.get("ts") or "", reverse=True)
        self._json(200, {"skill": skill, "improvements": rows})

    def _handle_skills_metrics(self, qs: dict[str, list[str]]) -> None:
        """Return per-skill aggregated metrics. With ``?skill=<id>`` returns
        a single skill's detail (incl. ``recent`` invocations)."""
        all_metrics = _aggregate_skill_metrics()
        skill = (qs.get("skill", [""])[0] or "").strip()
        if skill:
            agg = all_metrics.get(skill)
            if not agg:
                for v in all_metrics.values():
                    if v.get("name") == skill:
                        agg = v
                        break
            if not agg:
                self._json(404, {"error": "no metrics for skill", "skill": skill})
                return
            self._json(200, agg)
            return
        # Strip the ``recent`` array on the list response to keep payload small.
        compact = []
        for agg in all_metrics.values():
            row = {k: v for k, v in agg.items() if k != "recent"}
            compact.append(row)
        compact.sort(key=lambda r: r.get("last_used") or "", reverse=True)
        self._json(200, {"metrics": compact})

    def _handle_files_list(self, qs: dict[str, list[str]]) -> None:
        """Return repo-relative file paths that match ``prefix`` for the
        ``@`` autocomplete. Uses ``git ls-files`` for fast indexed search
        when available; falls back to a glob."""
        prefix = (qs.get("prefix", [""])[0] or "").lower()
        limit = 30
        files: list[str] = []
        git = shutil.which("git")
        if git:
            try:
                out = subprocess.run(
                    [git, "ls-files"], cwd=str(ROOT), capture_output=True,
                    text=True, timeout=5,
                )
                if out.returncode == 0:
                    for line in out.stdout.splitlines():
                        if not line:
                            continue
                        if prefix and prefix not in line.lower():
                            continue
                        files.append(line)
                        if len(files) >= limit:
                            break
            except (subprocess.TimeoutExpired, OSError):
                files = []
        # Fallback: walk the repo when ``git ls-files`` isn't available
        # (no-git checkouts, broken HEAD, etc.). ``SKIP_DIRS`` keeps the
        # walk off the obvious hot paths (``.git/objects`` alone can be
        # hundreds of thousands of entries) and stops the autocomplete
        # endpoint leaking ``.venv`` / ``node_modules`` paths into the
        # suggestion list.
        if not files:
            try:
                for p in ROOT.rglob("*"):
                    try:
                        parts = p.relative_to(ROOT).parts
                    except ValueError:
                        continue
                    if any(part in SKIP_DIRS for part in parts):
                        continue
                    if not p.is_file():
                        continue
                    rel = "/".join(parts)
                    if prefix and prefix not in rel.lower():
                        continue
                    files.append(rel)
                    if len(files) >= limit:
                        break
            except OSError as e:
                print(f"[serve] files-list fallback walk failed: {e}", flush=True)
        self._json(200, {"files": files})

    def _handle_file_read(self, qs: dict[str, list[str]]) -> None:
        """Read a repo-relative file's content. Refuses paths that escape
        the repo root."""
        rel = (qs.get("path", [""])[0] or "").strip()
        if not rel:
            self._json(400, {"error": "path is required"})
            return
        try:
            resolved = (ROOT / rel).resolve()
            resolved.relative_to(ROOT.resolve())
        except (ValueError, OSError):
            self._json(403, {"error": "path outside repo root"})
            return
        if not resolved.is_file():
            self._json(404, {"error": "not a file"})
            return
        try:
            data = resolved.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            self._json(500, {"error": str(e)})
            return
        # Cap response to ~256KB so a giant file can't blow up the chat.
        cap = 256 * 1024
        truncated = len(data) > cap
        if truncated:
            data = data[:cap]
        self._json(200, {"path": rel, "content": data, "truncated": truncated})

    def _handle_transcript_stream(self, session_id: str) -> None:
        """SSE: tail an IDE transcript JSONL file. Emits any existing
        content first (catch-up), then continues forwarding bytes as the
        file grows (live mirror of the ongoing IDE session)."""
        tdir = _transcripts_dir_for_cwd(ROOT)
        path = (tdir / f"{session_id}.jsonl") if tdir else None
        if not path or not path.is_file():
            self._json(404, {"error": "transcript not found", "session_id": session_id})
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        # Catch-up: flush existing content first.
        try:
            with path.open("rb") as fh:
                existing = fh.read()
                pos = fh.tell()
        except OSError:
            return
        if existing:
            if not self._write_sse_frame(existing.decode("utf-8", "replace").replace("\r\n", "\n")):
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
        while idle_ticks < max_idle_ticks:
            if time.monotonic() - session_start > MAX_SSE_SESSION_S:
                self._write_sse_event("end", '{"reason":"max_session"}')
                return
            try:
                size = path.stat().st_size
            except OSError:
                break
            if size > last_size:
                idle_ticks = 0
                try:
                    with path.open("rb") as fh:
                        fh.seek(last_size)
                        chunk = fh.read(size - last_size)
                except OSError:
                    break
                if not self._write_sse_frame(chunk.decode("utf-8", "replace").replace("\r\n", "\n")):
                    return
                last_size = size
            else:
                idle_ticks += 1
                try:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    return
            time.sleep(1.0)
        self._write_sse_event("end", "{}")

    def _handle_sessions_list(self) -> None:
        """List chat sessions (claude + codex) that have a session_id, so the
        dashboard can offer "Resume" picker entries."""
        with JOBS_LOCK:
            sessions = []
            for j in JOBS.values():
                if j.get("kind") not in {"chat", "chat-codex"}:
                    continue
                sid = j.get("session_id")
                if not sid:
                    continue
                sessions.append({
                    "session_id": sid,
                    "kind": j.get("kind"),
                    "task": (j.get("task") or "")[:120],
                    "model": j.get("model"),
                    "started_at": j.get("started_at"),
                    "ended_at": j.get("ended_at"),
                    "status": j.get("status"),
                    "last_job_id": j.get("id"),
                })
        sessions.sort(key=lambda s: s.get("started_at") or "", reverse=True)
        self._json(200, {"sessions": sessions})

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

    # ----- PTY endpoints (real shell sessions) --------------------------

    def _handle_ptys_list(self) -> None:
        with PTYS_LOCK:
            out = [_pty_summary(e) for e in PTYS.values()]
        out.sort(key=lambda x: x.get("created_at") or "", reverse=True)
        self._json(200, {"ptys": out})

    def _handle_pty_get(self, pty_id: str) -> None:
        with PTYS_LOCK:
            entry = PTYS.get(pty_id)
            summary = _pty_summary(entry) if entry else None
        if not summary:
            self._json(404, {"error": "pty not found"})
            return
        self._json(200, summary)

    def _handle_pty_create(self, body: dict) -> None:
        shell = body.get("shell")
        if shell is not None and not isinstance(shell, str):
            self._json(400, {"error": "shell must be a string"})
            return
        cwd_raw = body.get("cwd")
        if cwd_raw is not None and not isinstance(cwd_raw, str):
            self._json(400, {"error": "cwd must be a string"})
            return
        # Constrain ``cwd`` to inside the repo so the dashboard can't be
        # used as a generic remote shell over the LAN. Empty/None falls
        # back to the repo root.
        cwd = None
        if cwd_raw:
            try:
                resolved = (ROOT / cwd_raw).resolve()
                resolved.relative_to(ROOT.resolve())
                if not resolved.is_dir():
                    self._json(404, {"error": f"cwd not found or not a directory: {cwd_raw}"})
                    return
                cwd = str(resolved)
            except (ValueError, OSError):
                self._json(403, {"error": "cwd must be inside the repo"})
                return
        try:
            cols = int(body.get("cols") or 80)
            rows = int(body.get("rows") or 24)
        except (TypeError, ValueError):
            self._json(400, {"error": "cols/rows must be integers"})
            return
        cols = max(20, min(500, cols))
        rows = max(5,  min(200, rows))
        try:
            entry = _pty_spawn(shell, cwd, cols, rows)
        except ImportError as e:
            # Windows without pywinpty installed.
            self._json(503, {"error": str(e)})
            return
        except FileNotFoundError as e:
            self._json(503, {"error": f"shell not found: {e}"})
            return
        except Exception as e:
            self._json(500, {"error": f"failed to spawn PTY: {e}"})
            return
        self._json(201, _pty_summary(entry))

    def _handle_pty_kill(self, pty_id: str) -> None:
        if _pty_kill(pty_id):
            self._json(200, {"ok": True})
        else:
            self._json(404, {"error": "pty not found"})

    def _handle_pty_ws(self, pty_id: str) -> None:
        """WebSocket endpoint: bidirectional byte stream + JSON control
        messages. Frames:
          * binary  -> bytes written to the PTY master (keystrokes)
          * text    -> JSON control: {"type":"resize","cols":N,"rows":M}
        Server -> client:
          * binary  -> bytes read off the PTY master
          * text    -> JSON: {"type":"exit"} on EOF
        """
        with PTYS_LOCK:
            entry = PTYS.get(pty_id)
        if not entry:
            self.send_error(404, "pty not found")
            return
        ws = WebSocket.accept(self)
        if ws is None:
            return  # handshake already sent its own error
        q: _stdqueue.Queue = _stdqueue.Queue(maxsize=4096)
        # Catch-up: dump the ring buffer first so the client sees existing
        # output even if it attached after the session started.
        with entry["_lock"]:
            if entry["_ring"]:
                try:
                    ws.send_binary(bytes(entry["_ring"]))
                except _WsClosed:
                    return
            entry["_subscribers"].append(q)
            ended = entry.get("status") == "ended"
        if ended:
            try:
                ws.send_text(json.dumps({"type": "exit"}))
            except _WsClosed:
                pass
            ws.close()
            with entry["_lock"]:
                try: entry["_subscribers"].remove(q)
                except ValueError: pass
            return

        # Stop event so the writer thread can clean up when the reader
        # bails (or vice versa).
        stop = threading.Event()

        def pump_outbound():
            try:
                while not stop.is_set():
                    try:
                        chunk = q.get(timeout=15)
                    except _stdqueue.Empty:
                        # Heartbeat ping keeps proxies / browsers happy.
                        try:
                            ws._send_frame(WebSocket.OPCODE_PING, b"")
                        except _WsClosed:
                            return
                        continue
                    if chunk is None:
                        try:
                            ws.send_text(json.dumps({"type": "exit"}))
                        except _WsClosed:
                            pass
                        return
                    try:
                        ws.send_binary(chunk)
                    except _WsClosed:
                        return
            finally:
                stop.set()

        writer = threading.Thread(target=pump_outbound, daemon=True)
        writer.start()

        try:
            while not stop.is_set():
                try:
                    opcode, data = ws.recv()
                except _WsClosed:
                    break
                if opcode == WebSocket.OPCODE_BIN:
                    pty = entry.get("_pty")
                    if pty is not None:
                        try:
                            pty.write(data)
                        except (OSError, ValueError) as e:
                            # OSError covers EBADF / EPIPE on a dead PTY;
                            # ValueError covers writes against a closed fd.
                            # Either way we can't recover — break the loop
                            # but record the cause so a flood of "WS closed
                            # unexpectedly" reports has context.
                            print(f"[serve] pty_ws write({pty_id}) failed: {e}", flush=True)
                            break
                elif opcode == WebSocket.OPCODE_TEXT:
                    # Control message (resize, etc.) JSON-encoded.
                    try:
                        msg = json.loads(data.decode("utf-8", errors="replace"))
                    except (ValueError, UnicodeDecodeError):
                        continue
                    if msg.get("type") == "resize":
                        try:
                            cols = max(20, min(500, int(msg.get("cols") or 80)))
                            rows = max(5,  min(200, int(msg.get("rows") or 24)))
                        except (TypeError, ValueError):
                            continue
                        pty = entry.get("_pty")
                        if pty is not None:
                            try:
                                pty.resize(cols, rows)
                            except (OSError, ValueError) as e:
                                # resize is best-effort; some backends throw
                                # on a stale handle. Don't break the loop —
                                # the user can still type / read — but log.
                                print(f"[serve] pty_ws resize({pty_id}) failed: {e}", flush=True)
                        with entry["_lock"]:
                            entry["cols"] = cols
                            entry["rows"] = rows
        finally:
            stop.set()
            with entry["_lock"]:
                try: entry["_subscribers"].remove(q)
                except ValueError: pass
            try:
                ws.close()
            except (OSError, _WsClosed):
                # Already closed by the peer — common path, not worth logging.
                pass

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
            ok, err = _send_to_stdin(job_id, text)
        if not ok:
            code = 404 if err == "not found" else 409
            self._json(code, {"error": err})
            return
        self._json(200, {"ok": True})

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
        import queue as _queue

        session_start = time.monotonic()

        with JOBS_LOCK:
            j = JOBS.get(job_id)
            if not j:
                self._json(404, {"error": "job not found"})
                return
            log_path = j.get("log_path")
            status = j.get("status")
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
            with JOBS_LOCK:
                catchup_kind = JOBS.get(job_id, {}).get("kind")
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

            if status not in {"running", "queued"}:
                # Job already finished — close immediately after catch-up.
                self._write_sse_event("end", "{}")
                return

            # 2. Live tail until EOF sentinel arrives.
            while True:
                # Hard session cap — emit a final SSE event so the client
                # can distinguish "server rotated me" from a network drop.
                if time.monotonic() - session_start > MAX_SSE_SESSION_S:
                    self._write_sse_event("end", '{"reason":"max_session"}')
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
                if not self._write_sse_frame(chunk):
                    return
        finally:
            with JOBS_LOCK:
                try:
                    subs.remove(q)
                except ValueError:
                    pass

    def _write_sse_frame(self, text: str) -> bool:
        """Encode ``text`` as one SSE ``data:`` frame; one logical line per
        SSE ``data:`` field. Returns False if the client disconnected."""
        out = []
        # Per SSE spec, each newline in the payload becomes a separate data: line.
        for line in text.split("\n"):
            out.append("data: " + line + "\n")
        out.append("\n")
        try:
            self.wfile.write("".join(out).encode("utf-8", "replace"))
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return False
        return True

    def _write_sse_event(self, event: str, data: str) -> bool:
        try:
            self.wfile.write(f"event: {event}\ndata: {data}\n\n".encode("utf-8"))
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return False
        return True

    def _handle_dispatch_mode(self, body: dict) -> None:
        mode = (body.get("mode") or "").strip()
        if mode not in {"auto", "manual"}:
            self._json(400, {"error": "mode must be 'auto' or 'manual'"})
            return
        path = ROOT / ".ai" / "models.yaml"
        if not path.exists():
            self._json(404, {"error": "models.yaml not found"})
            return
        # ``errors="replace"`` so an editor-induced non-UTF-8 byte in
        # models.yaml doesn't 500 a config-change request — the patch
        # regex still matches the ``dispatch_mode:`` line.
        text = path.read_text(encoding="utf-8", errors="replace")
        # Replace existing `dispatch_mode: <value>` line (with optional inline comment), or insert near top.
        line_re = re.compile(r"^(dispatch_mode:\s*)\S+(\s*(?:#.*)?)$", re.M)
        if line_re.search(text):
            new_text = line_re.sub(rf"\g<1>{mode}\g<2>", text, count=1)
        else:
            # Insert after the first non-comment, non-blank line — keep it simple.
            new_text = f"dispatch_mode: {mode}    # auto | manual\n\n" + text
        _write_text_lf(path, new_text)
        self._json(200, {"ok": True, "mode": mode})

    # ----- phase config edit -----
    _PHASES = {"session", "plan", "execute", "review", "rescue", "maintenance", "bootstrap"}
    _TOOLS = {"claude", "codex"}
    _PHASE_MODES = {"inline", "agent", "dispatcher"}
    # Claude `--effort` accepts {low, medium, high, xhigh, max}; codex
    # `model_reasoning_effort` accepts {low, medium, high, xhigh}. We accept
    # the union here and let the dispatcher omit/translate per tool.
    _REASONING = {"xhigh", "high", "medium", "low", "max"}

    def _handle_phase_update(self, body: dict) -> None:
        phase = (body.get("phase") or "").strip()
        if phase not in self._PHASES:
            self._json(400, {"error": f"phase must be one of {sorted(self._PHASES)}"})
            return
        # All fields optional; only those present are updated.
        updates: dict[str, str | None] = {}
        if "tool" in body:
            tool = (body.get("tool") or "").strip()
            if tool not in self._TOOLS:
                self._json(400, {"error": f"tool must be one of {sorted(self._TOOLS)}"})
                return
            updates["tool"] = tool
        if "model" in body:
            model = (body.get("model") or "").strip()
            if not model or len(model) > 80 or not re.fullmatch(r"[A-Za-z0-9._\-]+", model):
                self._json(400, {"error": "model must be 1-80 chars [A-Za-z0-9._-]"})
                return
            updates["model"] = model
        if "mode" in body:
            mode = (body.get("mode") or "").strip()
            if mode and mode not in self._PHASE_MODES:
                self._json(400, {"error": f"mode must be one of {sorted(self._PHASE_MODES)} or empty"})
                return
            updates["mode"] = mode or None  # empty => remove the line
        if "reasoning_effort" in body:
            re_eff = (body.get("reasoning_effort") or "").strip()
            if re_eff and re_eff not in self._REASONING:
                self._json(400, {"error": f"reasoning_effort must be one of {sorted(self._REASONING)} or empty"})
                return
            updates["reasoning_effort"] = re_eff or None
        if "timeout_seconds" in body:
            raw = body.get("timeout_seconds")
            if raw == "" or raw is None:
                updates["timeout_seconds"] = None
            else:
                try:
                    ts = int(raw)
                except (TypeError, ValueError):
                    self._json(400, {"error": "timeout_seconds must be an integer (30-7200) or empty"})
                    return
                if ts < 30 or ts > 7200:
                    self._json(400, {"error": "timeout_seconds must be in [30, 7200]"})
                    return
                updates["timeout_seconds"] = str(ts)

        if not updates:
            self._json(400, {"error": "no updatable fields provided (tool, model, mode, reasoning_effort, timeout_seconds)"})
            return

        path = ROOT / ".ai" / "models.yaml"
        if not path.exists():
            self._json(404, {"error": "models.yaml not found"})
            return
        try:
            new_text = _patch_phase_block(path.read_text(encoding="utf-8", errors="replace"), phase, updates)
        except ValueError as e:
            self._json(404, {"error": str(e)})
            return
        _write_text_lf(path, new_text)
        self._json(200, {"ok": True, "phase": phase, "updated": updates})


def _patch_or_create_block(text: str, name: str, updates: dict[str, str | None],
                           creator_template: str = "") -> str:
    """Same as _patch_phase_block but appends a fresh block if the header is missing.

    creator_template is the initial YAML to insert (e.g. ``improver:\\n  enabled: true\\n``).
    """
    try:
        return _patch_phase_block(text, name, updates)
    except ValueError:
        if not creator_template:
            creator_template = f"{name}:\n"
        seed = text.rstrip("\n") + "\n\n" + creator_template
        if not seed.endswith("\n"):
            seed += "\n"
        return _patch_phase_block(seed, name, updates)


def _patch_phase_block(text: str, phase: str, updates: dict[str, str | None]) -> str:
    """Update fields under a top-level YAML mapping like ``plan:\\n  tool: ...``.

    For each key in updates:
      - value is a string -> replace existing `  <key>: <old>` line, or insert
        as the first child line after the header
      - value is None     -> remove the `  <key>: ...` line if present
    """
    lines = text.splitlines(keepends=False)
    n = len(lines)
    header_idx = None
    for i, ln in enumerate(lines):
        if re.match(rf"^{re.escape(phase)}\s*:\s*(#.*)?$", ln):
            header_idx = i
            break
    if header_idx is None:
        raise ValueError(f"phase block `{phase}:` not found in models.yaml")
    # Find end of this block (next non-indented, non-blank line)
    end_idx = n
    for j in range(header_idx + 1, n):
        ln = lines[j]
        if ln.strip() == "":
            continue
        if not ln.startswith((" ", "\t")):
            end_idx = j
            break
    block = lines[header_idx + 1 : end_idx]
    # Track existing keys
    key_re = re.compile(r"^(\s+)([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(\S.*)?$")
    indent = "  "
    for ln in block:
        m = key_re.match(ln)
        if m:
            indent = m.group(1)
            break

    def render(key: str, val: str) -> str:
        return f"{indent}{key}: {val}"

    new_block: list[str] = list(block)
    for key, val in updates.items():
        existing_idx = None
        for k, ln in enumerate(new_block):
            m = key_re.match(ln)
            if m and m.group(2) == key:
                existing_idx = k
                break
        if val is None:
            if existing_idx is not None:
                new_block.pop(existing_idx)
            continue
        if existing_idx is not None:
            # Preserve inline comment if any
            ln = new_block[existing_idx]
            m = re.match(r"^(\s+[A-Za-z_][A-Za-z0-9_]*\s*:\s*)\S+(\s*(?:#.*)?)$", ln)
            if m:
                new_block[existing_idx] = f"{m.group(1)}{val}{m.group(2)}"
            else:
                new_block[existing_idx] = render(key, val)
        else:
            # Insert as the last non-empty child line
            insert_at = len(new_block)
            while insert_at > 0 and new_block[insert_at - 1].strip() == "":
                insert_at -= 1
            new_block.insert(insert_at, render(key, val))

    new_lines = lines[: header_idx + 1] + new_block + lines[end_idx:]
    out = "\n".join(new_lines)
    if text.endswith("\n") and not out.endswith("\n"):
        out += "\n"
    return out


class _ThreadedServer(socketserver.ThreadingTCPServer):
    """Threaded HTTP server with daemon worker threads.

    Without ``daemon_threads = True``, in-flight request threads survive
    Ctrl+C (e.g. a 120s improver subprocess.run blocks shutdown). Users
    typically Ctrl+C a second time which kills threads mid-write to the
    JSONL ledgers, corrupting them. With daemon threads the process exits
    cleanly on Ctrl+C and threads are torn down with the process.

    Port-exclusivity is platform-specific. On POSIX, ``SO_REUSEADDR`` lets
    the dashboard restart immediately after Ctrl+C without waiting for the
    TIME_WAIT window to expire, *without* breaking exclusivity — two
    processes can never both bind to the same loopback address. On Windows
    the same flag has opposite semantics: two processes that both set
    ``SO_REUSEADDR`` silently share the address and the kernel splits
    incoming connections between them unpredictably. We therefore disable
    ``SO_REUSEADDR`` on Windows and set ``SO_EXCLUSIVEADDRUSE`` in
    ``server_bind`` instead — that flag enforces exclusivity while still
    permitting fast restart. Net effect: when a second ``python serve.py``
    launches in another project, its bind to the configured port fails
    cleanly with WSAEADDRINUSE and ``main()``'s dynamic port fallback
    actually fires.
    """

    daemon_threads = True
    allow_reuse_address = (sys.platform != "win32")

    def server_bind(self) -> None:
        if sys.platform == "win32":
            self.socket.setsockopt(
                socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1
            )
        super().server_bind()


def main() -> None:
    # Replay the on-disk job ledger so sessions, costs and history
    # survive `python serve.py` restarts.
    _load_persisted_jobs()
    # Prune stale per-job .log files. Chat jobs route to claude's
    # transcript now so the dir mostly holds demo/orchestrate/codex logs;
    # this keeps it bounded.
    pruned = _prune_old_logs(JOBS_DIR, max_age_days=7, keep_newest=50)
    if pruned:
        print(f"[dashboard] pruned {pruned} old .log file(s) from {JOBS_DIR}")
    # Dynamic port selection: prefer the configured PORT, fall back across a
    # window of consecutive ports if it's busy, and finally let the OS pick an
    # ephemeral port (bind to 0) so the dashboard always launches.
    httpd = None
    bound: int | None = None
    last_err: OSError | None = None
    candidates = [PORT + i for i in range(20)] + [0]
    for candidate in candidates:
        try:
            httpd = _ThreadedServer(("127.0.0.1", candidate), Handler)
            bound = httpd.server_address[1]
            break
        except OSError as e:
            last_err = e
            continue
    if httpd is None or bound is None:
        raise SystemExit(f"could not bind to any port starting at {PORT}: {last_err}")
    # Publish the bound port so _origin_allowed (CSRF) and /api/system/info
    # validate against the port the server is actually listening on, not
    # the configured one. Critical when the fallback above picked a
    # different candidate.
    global BOUND_PORT
    BOUND_PORT = bound
    with httpd:
        url = f"http://localhost:{bound}/.ai/dashboard/"
        if bound != PORT:
            print(f"[dashboard] configured port {PORT} unavailable; using {bound}")
        print(f"AI workflow dashboard: {url}")
        print("Press Ctrl+C to stop.")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nstopped.")


if __name__ == "__main__":
    main()
