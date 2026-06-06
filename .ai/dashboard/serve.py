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
    POST /api/events/clear                      ->  truncates .ai/ledgers/events.jsonl
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
import atexit
import datetime as _dt
import hashlib
import http.server
import json
import os
import queue as _stdqueue
import re
import secrets
import select
import signal
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

# Helper modules live in the sibling `scripts/` folder. Inject it onto
# sys.path so direct invocation (`python .ai/dashboard/serve.py`) and
# tests that load serve via importlib both resolve the imports below.
_SCRIPTS_DIR = str(Path(__file__).resolve().parent / "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

# PTY helper (cross-platform). The Terminals page can spawn real shell
# sessions in addition to the existing chat-claude / chat-codex panes;
# this module wraps POSIX `pty.fork` and Windows `pywinpty.PtyProcess`
# behind one interface.
import pty_session as _pty_session  # noqa: E402 — scripts/ helper
import todos_parser as _todos_parser  # noqa: E402 — scripts/ helper
import session_registry  # noqa: E402 — scripts/ helper

from _improver_transcript_policy import classify_transcript, load_ledger_rows  # noqa: E402

PORT = int(os.environ.get("DASHBOARD_PORT", "8765"))
# The actually-bound port. Diverges from PORT when main()'s dynamic-port
# fallback picks a different candidate (e.g. another project already holds
# PORT). CSRF allowlist and /api/system/info read this so a second
# concurrent dashboard validates Origins against its real port instead of
# the stale configured one.
BOUND_PORT = PORT

# Pre-compiled URL-routing patterns. Previously each do_GET / do_POST
# invocation rebuilt these via inline `re.fullmatch(r"...", path)` calls.
# Dashboard boot fires ~7 GETs in parallel and the user can spam-click
# job/PTY actions, so the per-request compile cost added up. Compiling
# once at import time drops the routing overhead to a single dispatch
# table lookup + a hashed regex execution.
_RE_TRANSCRIPT_STREAM = re.compile(r"/api/transcripts/([0-9a-fA-F-]+)/stream")
_RE_SESSION_STREAM = re.compile(r"/api/sessions/([0-9a-fA-F-]+)/stream")
_RE_SESSION_INPUT = re.compile(r"/api/sessions/([0-9a-fA-F-]+)/input")
_RE_SESSION_RELEASE = re.compile(r"/api/sessions/([0-9a-fA-F-]+)/release")
_RE_AGENT_PROPOSAL_GET = re.compile(r"/api/agents/proposals/([A-Za-z0-9_\-]+)")
_RE_SKILL_PROPOSAL_GET = re.compile(r"/api/skills/proposals/([A-Za-z0-9_\-]+)")
_RE_JOB_STREAM = re.compile(r"/api/jobs/([0-9a-f-]+)/stream")
_RE_JOB_GET = re.compile(r"/api/jobs/([0-9a-f-]+)")
_RE_PTY_IO = re.compile(r"/api/ptys/([0-9a-f-]+)/io")
_RE_PTY_GET = re.compile(r"/api/ptys/([0-9a-f-]+)")
_RE_JOB_CANCEL = re.compile(r"/api/jobs/([0-9a-f-]+)/cancel")
_RE_JOB_INPUT = re.compile(r"/api/jobs/([0-9a-f-]+)/input")
_RE_PTY_KILL = re.compile(r"/api/ptys/([0-9a-f-]+)/kill")
_RE_JOB_INTERRUPT = re.compile(r"/api/jobs/([0-9a-f-]+)/interrupt")
_RE_SKILL_PROPOSAL_DECIDE = re.compile(r"/api/skills/proposals/([A-Za-z0-9_\-]+)/(accept|reject)")
_RE_SKILL_SUGGESTION_DRAFT = re.compile(r"/api/skills/suggestions/([A-Za-z0-9_\-]+)/draft")
_RE_SKILL_IMPROVE_NOW = re.compile(r"/api/skills/([A-Za-z0-9_\-]+)/improve")
_RE_AGENT_PROPOSAL_DECIDE = re.compile(r"/api/agents/proposals/([A-Za-z0-9_\-]+)/(accept|reject)")

# Windows `tasklist /NH /FO CSV` lines look like:
#   "ImageName","PID","SessionName","Session#","MemUsage"
# We only need the PID to know who's alive — match the second CSV field
# without splitting the whole row.
_RE_TASKLIST_PID = re.compile(r'"[^"]*","(\d+)"')
ROOT = Path(__file__).resolve().parents[2]  # repo root
_SERVER_STARTED_AT = time.time()
# Source of truth for the workflow template. /api/workflow/check and
# /api/workflow/update clone this fresh on each call so a one-click update from
# the dashboard always reflects the latest upstream version. Override via
# AI_WORKFLOW_TEMPLATE_URL (useful for forks or hosted test mirrors).
_DEFAULT_WORKFLOW_TEMPLATE_URL = "https://github.com/zDud4s/ai-dev-workflow-template.git"
# Allowlisted scheme + host pairs for AI_WORKFLOW_TEMPLATE_URL.
# https://github.com / https://gitlab.com / https://codeberg.org cover the
# common fork hosts; git+https keeps explicit Git transport URLs available.
# Anything else (file://, http://, git://, ssh://, http://attacker/) is rejected
# and the default is used so a tampered env var can't redirect every dashboard
# click to a hostile clone.
_ALLOWED_TEMPLATE_HOSTS = {
    ("https", "github.com"),
    ("https", "gitlab.com"),
    ("https", "codeberg.org"),
    ("git+https", "github.com"),
    ("git+https", "gitlab.com"),
    ("git+https", "codeberg.org"),
}


def _validate_template_url(url: str) -> str:
    try:
        from urllib.parse import urlparse
        p = urlparse(url)
    except (ValueError, TypeError):
        return _DEFAULT_WORKFLOW_TEMPLATE_URL
    if p.scheme == "file":
        print(
            f"[serve] AI_WORKFLOW_TEMPLATE_URL rejected (file:// scheme not allowed): {url!r}",
            flush=True,
        )
        return _DEFAULT_WORKFLOW_TEMPLATE_URL
    if (p.scheme, (p.hostname or "").lower()) in _ALLOWED_TEMPLATE_HOSTS:
        return url
    print(
        f"[serve] AI_WORKFLOW_TEMPLATE_URL rejected (scheme/host not allowlisted): {url!r}",
        flush=True,
    )
    return _DEFAULT_WORKFLOW_TEMPLATE_URL


WORKFLOW_TEMPLATE_URL = _validate_template_url(
    os.environ.get("AI_WORKFLOW_TEMPLATE_URL", _DEFAULT_WORKFLOW_TEMPLATE_URL)
)


def _safe_which(name: str) -> str | None:
    """Hardened wrapper around ``shutil.which``.

    Drops obviously hostile / accidental PATH entries (empty, ``.``,
    relative, $TEMP, $HOME/Downloads) BEFORE the lookup so a planted
    binary in those locations can't shadow the real tool. Returns the
    absolute resolved path, or ``None`` if no acceptable match was
    found. Falls back to ``None`` even when the unfiltered ``which``
    would have matched — callers MUST handle ``None``.
    """
    raw_path = os.environ.get("PATH", "")
    if not raw_path:
        return None
    sep = os.pathsep
    bad_dirs: set[str] = set()
    for envvar in ("TEMP", "TMP", "TMPDIR"):
        val = os.environ.get(envvar)
        if val:
            try:
                bad_dirs.add(os.path.normcase(os.path.realpath(val)))
            except OSError:
                bad_dirs.add(os.path.normcase(val))
    home = os.path.expanduser("~")
    if home and home != "~":
        for sub in ("Downloads", "Desktop"):
            cand = os.path.join(home, sub)
            try:
                bad_dirs.add(os.path.normcase(os.path.realpath(cand)))
            except OSError:
                bad_dirs.add(os.path.normcase(cand))
    cleaned: list[str] = []
    for entry in raw_path.split(sep):
        if not entry or entry in (".", ".."):
            continue
        if not os.path.isabs(entry):
            continue
        try:
            resolved = os.path.normcase(os.path.realpath(entry))
        except OSError:
            continue
        if resolved in bad_dirs:
            continue
        cleaned.append(entry)
    if not cleaned:
        return None
    return shutil.which(name, path=sep.join(cleaned))
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
JOBS_PERSIST_FILE = ROOT / ".ai" / "ledgers" / "jobs.jsonl"
# Append-only telemetry stream written by .ai/dashboard/scripts/log_event.py
# (a PostToolUse hook). The /api/timeline endpoint aggregates phase_dispatch
# events from this file. Tests override it via monkeypatch.
EVENTS_FILE = ROOT / ".ai" / "ledgers" / "events.jsonl"
# Append-only metrics stream written by the orchestrate skill, one line per
# dispatched phase. Powers the /api/auto-select ranking. See the orchestrate
# skill "## Metrics logging" section for the schema.
METRICS_FILE = ROOT / ".ai" / "ledgers" / "metrics.jsonl"
# Filled agent-dispatch packets produced by the agent orchestrator.
AGENT_RUNS_DIR = ROOT / ".ai" / "agent-runs"
PIPELINES_DIR = ROOT / ".ai" / "pipelines"
# Append-only ledger of per-(skill, job) invocations. The auto skill-improver
# (Phase 2+) reads this to decide which skills need adapting. One line per
# unique skill invoked in a job; the entry-skill of orchestrate/plan jobs is
# always credited even when the log isn't stream-json.
SKILL_METRICS_FILE = ROOT / ".ai" / "ledgers" / "skill_metrics.jsonl"
# Todos ledger. `scripts/todos_parser.py` owns the canonical read path for the
# Todos tab; the analytics aggregation reads the same file by this constant so
# all six analytics ledgers are uniform and monkeypatchable by name in tests.
TODOS_FILE = ROOT / ".ai" / "ledgers" / "todos.jsonl"
# Auto-improver storage. Proposals are dropped here as JSON + .old.md + .new.md
# triples so the dashboard can render a diff and the user can Accept / Reject.
# Backups of overwritten SKILL.md content go to SKILL_BACKUPS_DIR; every
# decision (auto-apply, manual-apply, reject, skip) is appended to the
# ledger for forensic readability.
SKILL_PROPOSALS_DIR  = ROOT / ".ai" / "dashboard" / "proposals" / "skills"
SKILL_BACKUPS_DIR    = ROOT / ".ai" / "dashboard" / "proposals" / "skill_backups"
IMPROVEMENTS_LEDGER  = ROOT / ".ai" / "ledgers" / "improvements.jsonl"
_JOBS_PERSIST_LOCK = threading.Lock()
_IMPROVEMENTS_LEDGER_LOCK = threading.Lock()
_IMPROVER_TRACKED_SIDS: set[str] = set()
_IMPROVER_TRACKED_SIDS_LOCK = threading.Lock()
_IMPROVER_SHUTDOWN_HANDLERS_INSTALLED = False
_SKILL_METRICS_LOCK = threading.Lock()
# Serialises /api/workflow/update so two concurrent clients can't both spawn
# update-workflow.sh against the same tree at the same time (interleaved file
# writes corrupt the workflow core). Non-blocking acquire — second caller gets
# 409.
_WORKFLOW_UPDATE_LOCK = threading.Lock()
_GIT_LSFILES_CACHE: dict[str, tuple[float, int, list[str]]] = {}
_GIT_LSFILES_LOCK = threading.Lock()
_GIT_LSFILES_TTL_S = 10.0
_CODEX_FILE_AGG_CACHE: dict[str, tuple[int, dict]] = {}
_CODEX_FILE_AGG_LOCK = threading.Lock()
_COST_EXTRACT_CACHE: dict[str, tuple[int, dict | None]] = {}
_COST_EXTRACT_LOCK = threading.Lock()
# Max entries for the path-keyed parse caches below. They are mtime-keyed
# (re-reading the same file overwrites its entry), so growth comes only from
# distinct files/sessions seen — but over a long-lived server that is still
# unbounded. Evict oldest-inserted entries past this cap, mirroring the
# _PID_ALIVE_CACHE bound.
_PATH_CACHE_MAX = 1024


def _bound_path_cache(cache: dict, max_size: int = _PATH_CACHE_MAX) -> None:
    # Plain dicts preserve insertion order, so popping the front drops the
    # least-recently-added entries. Call under the cache's own lock.
    while len(cache) > max_size:
        try:
            cache.pop(next(iter(cache)))
        except (StopIteration, KeyError):
            break
_TRANSCRIPT_PREVIEW_CACHE: dict[str, tuple[int, str | None, str | None]] = {}
_TRANSCRIPT_PREVIEW_LOCK = threading.Lock()
# mtime-keyed cache of parsed agent-run .md files: (str(path), st.st_mtime_ns) -> parsed dict.
_AGENT_RUN_PARSE_CACHE: dict[str, tuple[int, dict]] = {}
_AGENT_RUN_PARSE_LOCK = threading.Lock()
# Memo of resolved ~/.claude/projects/<slug> dir (None included), keyed by
# (str(cwd), str(projects_root)) so a changed projects-root override never
# returns a dir resolved against a stale root.
_TRANSCRIPTS_DIR_CACHE: dict[tuple[str, "str | None"], "Path | None"] = {}
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
AGENT_PROPOSALS_DIR  = ROOT / ".ai" / "dashboard" / "proposals" / "agents"
# Defaults used when models.yaml has no `improver:` block. The improver
# only edits skills under PROJECT (.claude/skills/) — global skills are
# never modified.
_IMPROVER_DEFAULTS = {
    "enabled": True,
    "tool": "claude",
    "model": "claude-haiku-4-5",
    "small_change_max_lines": 6,    # auto-apply threshold (added+removed lines)
    "min_interval_seconds": 300,    # per-skill throttle (job-triggered runs)
    "timeout_seconds": 120,         # subprocess wall-clock cap
    # Periodic structural audit: visits every project skill on this cadence
    # regardless of whether the skill was invoked by any job. Without it the
    # job-triggered improver never wakes for skills the user doesn't run
    # (catch-22: a buggy skill nobody calls never gets fixed). 21600s = 6h.
    "sweep_interval_seconds": 21600,
    # Cap how many skills the sweep audits per wake. Keeps the sweep cheap
    # on first run after a long idle (where every skill is throttle-eligible)
    # and bounds concurrent LLM cost. Audited skills are picked by oldest
    # last-improver-run first, so over multiple wakes the sweep makes a full
    # pass.
    "sweep_batch_max": 4,
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

# Per-PUT cap for /api/pipelines/<slug>. Pipeline YAMLs are tiny —
# a few nodes, kilobytes at most. Capping at 256 KB keeps the
# generic 1 MiB ceiling for other endpoints while making it cheap
# to reject obviously-malformed PUTs to this specific route.
MAX_PIPELINE_PUT_BYTES = 256 * 1024  # 256KB hard cap on PUT body

# Cap on a single inbound WebSocket frame payload. The WS framing format
# allows a 64-bit extended length, so without an explicit cap a client
# can declare a multi-GB payload and pin the reader thread on
# ``self._rfile.read(length)`` while attempting allocation. PTY input is
# keystrokes (tens of bytes); chat composer frames are JSON capped
# client-side. 1 MiB matches ``MAX_JSON_BODY`` and is well above any
# legitimate WS traffic the dashboard sends.
MAX_WS_PAYLOAD = 1024 * 1024  # 1 MiB

# Hard upper bound on a single Server-Sent Events session, regardless of
# whether the subscriber is idle or not. ``_handle_job_stream`` already
# bails on a 4-minute idle window, but a chatty job could keep a single
# connection open indefinitely otherwise — and the SSE response holds a
# request thread, a queue subscriber slot, and a TCP connection for the
# whole lifetime. Clients reconnect transparently, so a forced rotation
# is observationally invisible.
MAX_SSE_SESSION_S = 1800  # 30 minutes

# Upper bound on the initial catch-up flush in ``_handle_transcript_stream``.
# Transcript JSONLs grow into the tens of MB over long IDE sessions and the
# old code did one unbounded ``fh.read()`` per SSE subscriber, so N parallel
# streams scaled memory pressure linearly with file size. We cap the catch-up
# at 4 MiB and tail from the last line boundary inside that window — live tail
# then picks up from EOF so new records still arrive.
MAX_TRANSCRIPT_CATCHUP_BYTES = 4 * 1024 * 1024  # 4 MiB


def _jsonl_line_to_session_event(line: str, seq: int) -> "dict | None":
    """Normalize one JSONL line from a Claude transcript into a SessionEvent dict.

    Returns None for lines that should be skipped (empty, parse errors, unknown
    types). The seq counter is supplied by the caller and incremented externally.

    SessionEvent schema:
      {"seq": int, "kind": str, "role": str|null, "text": str|null,
       "partial": bool, "state": str|null}
    """
    line = line.strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None

    msg_type = obj.get("type")
    message = obj.get("message") or {}
    role = message.get("role") or obj.get("role")

    # user / assistant message lines
    if msg_type in ("user", "assistant"):
        content = message.get("content")
        text: "str | None" = None
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            # Concatenate all text blocks; handle tool_use / tool_result blocks.
            text_parts = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text":
                    text_parts.append(block.get("text") or "")
                elif btype == "tool_use":
                    # Emit a separate tool_use event.
                    return {
                        "seq": seq,
                        "kind": "tool_use",
                        "role": role,
                        "text": block.get("name"),
                        "partial": False,
                        "state": None,
                    }
                elif btype == "tool_result":
                    result_content = block.get("content")
                    result_text: "str | None" = None
                    if isinstance(result_content, str):
                        result_text = result_content
                    elif isinstance(result_content, list):
                        result_text = " ".join(
                            b.get("text") or "" for b in result_content
                            if isinstance(b, dict) and b.get("type") == "text"
                        )
                    return {
                        "seq": seq,
                        "kind": "tool_result",
                        "role": role,
                        "text": result_text,
                        "partial": False,
                        "state": None,
                    }
            text = "".join(text_parts) if text_parts else None
        return {
            "seq": seq,
            "kind": "message",
            "role": role or msg_type,
            "text": text,
            "partial": False,
            "state": None,
        }

    # system / init lines
    if msg_type in ("system", "init"):
        content = obj.get("content") or message.get("content") or ""
        if isinstance(content, list):
            content = " ".join(
                b.get("text") or "" for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            )
        return {
            "seq": seq,
            "kind": "system",
            "role": "system",
            "text": content if isinstance(content, str) else None,
            "partial": False,
            "state": None,
        }

    # Unknown or empty type — skip
    return None


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
    # Tail-bound at 10k rows — older entries dropped on parse.
    rows_dq: deque[dict] = deque(maxlen=10000)
    # Per-line cap: a hostile or wedged producer that emits one giant line
    # would otherwise be ingested whole here and replicated across every
    # cached parse. 1 MiB per row is generous for legitimate JSONL events.
    max_line_bytes = 1 * 1024 * 1024
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                # Text mode yields str, so measure UTF-8 bytes to honour the
                # byte cap — len(line) would count code points, letting a line
                # of multi-byte UTF-8 reach ~4x the intended on-disk size.
                if len(line.encode("utf-8", errors="replace")) > max_line_bytes:
                    continue
                line = line.strip()
                if not line:
                    continue
                try:
                    rows_dq.append(json.loads(line))
                except (json.JSONDecodeError, ValueError):
                    # Mirror the prior hand-rolled behaviour: skip malformed
                    # rows silently rather than failing the whole endpoint.
                    continue
    except OSError:
        # If the file vanished or became unreadable between ``stat()`` and
        # ``open()`` (a rare race during rotation), treat as empty. Don't
        # cache the empty result — the next call will retry the stat.
        return []
    rows = list(rows_dq)
    with _JSONL_CACHE_LOCK:
        _JSONL_CACHE[key] = (st.st_mtime_ns, rows)
    return rows


def _is_under_trusted_dir(path, trusted_dir) -> bool:
    """Return True when ``path`` resolves inside ``trusted_dir``."""
    try:
        path_real = os.path.normcase(os.path.realpath(str(path)))
        trusted_real = os.path.normcase(os.path.realpath(str(trusted_dir)))
        return os.path.commonpath([path_real, trusted_real]) == trusted_real
    except (OSError, ValueError):
        return False


def _agent_run_slug_date(path: Path) -> tuple[str, str | None]:
    stem = path.stem
    match = re.fullmatch(r"(?P<date>\d{4}-\d{2}-\d{2})-(?P<slug>.+)", stem)
    if match:
        slug = match.group("slug")
        date = match.group("date")
    else:
        slug = stem
        date = None
    slug = re.sub(r"-\d+$", "", slug)
    return slug or stem, date


def _markdown_section(text: str, heading: str) -> str:
    pattern = re.compile(rf"(?im)^##\s+{re.escape(heading)}\s*$")
    match = pattern.search(text)
    if not match:
        return ""
    next_match = re.search(r"(?m)^##\s+", text[match.end():])
    end = match.end() + next_match.start() if next_match else len(text)
    return text[match.end():end].strip()


def _first_section_value(section: str) -> str | None:
    for line in section.splitlines():
        value = line.strip()
        if not value or value.startswith("<!--"):
            continue
        if value.startswith("- "):
            value = value[2:].strip()
        return value or None
    return None


def _line_value(text: str, label: str) -> str | None:
    pattern = re.compile(rf"(?im)^\s*{re.escape(label)}\s*:\s*(.*?)\s*$")
    match = pattern.search(text)
    if not match:
        return None
    value = match.group(1).strip()
    if not value or value.startswith("<!--"):
        return None
    return value


def _normalise_agent_run_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", key.strip().lower()).strip("_")


def _strip_agent_run_value(value: str) -> str:
    value = value.strip().strip(",")
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    return value.strip()


def _parse_agent_run_depends_on(value: str | None) -> list[str]:
    if not value:
        return []
    raw = _strip_agent_run_value(value)
    if not raw or raw.lower() in {"none", "null", "n/a", "na", "-", "[]"}:
        return []
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1].strip()
    parts = re.split(r"\s*,\s*|\s+", raw)
    out: list[str] = []
    for part in parts:
        item = _strip_agent_run_value(part.strip().strip("[]"))
        if item and item.lower() not in {"none", "null", "n/a", "na", "-"}:
            out.append(item)
    return out


def _agent_run_node(fields: dict[str, str]) -> dict:
    def pick(*keys: str) -> str | None:
        for key in keys:
            value = fields.get(key)
            if value:
                return value
        return None

    status = pick("status") or "pending"
    return {
        "id": pick("id", "task_id", "subtask_id"),
        "agent": pick("agent", "subagent", "subagent_type"),
        "status": status,
        "expected_output": pick("expected_output", "expected", "output"),
        "depends_on": _parse_agent_run_depends_on(pick("depends_on", "depends")),
    }


def _split_markdown_table_row(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _is_markdown_table_separator(line: str) -> bool:
    cells = _split_markdown_table_row(line)
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell.strip()) for cell in cells)


def _parse_agent_run_dag_table(section: str) -> list[dict]:
    lines = [line for line in section.splitlines() if line.strip()]
    for idx, line in enumerate(lines[:-1]):
        if "|" not in line or not _is_markdown_table_separator(lines[idx + 1]):
            continue
        headers = [_normalise_agent_run_key(cell) for cell in _split_markdown_table_row(line)]
        nodes: list[dict] = []
        for row in lines[idx + 2:]:
            if "|" not in row:
                break
            cells = _split_markdown_table_row(row)
            if len(cells) < len(headers):
                cells.extend([""] * (len(headers) - len(cells)))
            fields = {
                key: _strip_agent_run_value(value)
                for key, value in zip(headers, cells)
                if key
            }
            if not any(fields.values()):
                continue
            nodes.append(_agent_run_node(fields))
        if nodes:
            return nodes
    return []


def _parse_agent_run_inline_fields(value: str) -> dict[str, str]:
    raw = value.strip()
    if raw.startswith("{") and raw.endswith("}"):
        raw = raw[1:-1]
    fields: dict[str, str] = {}
    for item in re.split(r",\s*(?=[A-Za-z_][A-Za-z0-9 _-]*\s*:)", raw):
        if ":" not in item:
            continue
        key, val = item.split(":", 1)
        fields[_normalise_agent_run_key(key)] = _strip_agent_run_value(val)
    return fields


def _parse_agent_run_dag_yamlish(section: str) -> list[dict]:
    nodes: list[dict] = []
    current: dict[str, str] | None = None
    last_key: str | None = None

    def flush() -> None:
        nonlocal current, last_key
        if current and any(current.values()):
            nodes.append(_agent_run_node(current))
        current = None
        last_key = None

    for line in section.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("<!--"):
            continue
        if stripped.startswith("- "):
            rest = stripped[2:].strip()
            if current is not None and last_key and ":" not in rest:
                existing = current.get(last_key, "")
                current[last_key] = f"{existing}, {rest}" if existing else rest
                continue
            flush()
            current = {}
            if rest:
                current.update(_parse_agent_run_inline_fields(rest))
                last_key = next(reversed(current), None) if current else None
            continue
        if current is None or ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        last_key = _normalise_agent_run_key(key)
        current[last_key] = _strip_agent_run_value(value)
    flush()
    return nodes


def _parse_agent_run_dag(section: str, path: Path) -> list[dict]:
    dag = _parse_agent_run_dag_table(section)
    if dag:
        return dag
    dag = _parse_agent_run_dag_yamlish(section)
    if dag:
        return dag
    meaningful = [
        line.strip() for line in section.splitlines()
        if line.strip() and not line.strip().startswith("<!--")
    ]
    if meaningful:
        print(f"[serve] agent-run DAG parse failed for {path}", flush=True)
    return []


def _extract_handoff_synthesis_ts(handoff: str) -> str | None:
    timestamp = r"\d{4}-\d{2}-\d{2}[T ][0-9:.]+(?:Z|[+-]\d{2}:\d{2})?"
    pattern = re.compile(
        rf"(?im)^\s*(?:synthesis[_ -]?ts|synthesis timestamp|"
        rf"synthesis completed(?: at)?|completed_at)\s*:\s*({timestamp})\s*$"
    )
    match = pattern.search(handoff)
    return match.group(1) if match else None


def _extract_handoff_field(handoff: str, label: str) -> str | None:
    labels = (
        "Synthesis output",
        "Per-subtask results",
        "Failed subtasks",
        "Memory updates",
        "Phase execution log",
    )
    next_label = "|".join(re.escape(item) for item in labels if item != label)
    pattern = re.compile(
        rf"(?ims)^\s*{re.escape(label)}\s*:\s*(.*?)"
        rf"(?=^\s*(?:{next_label})\s*:|^##\s+|\Z)"
    )
    match = pattern.search(handoff)
    if not match:
        return None
    return match.group(1).strip()


def _extract_agent_run_success(handoff: str) -> bool | None:
    explicit = re.search(r"(?im)^\s*(?:success|succeeded)\s*:\s*(true|false|yes|no|1|0)\s*$", handoff)
    if explicit:
        return explicit.group(1).lower() in {"true", "yes", "1"}
    status = re.search(r"(?im)^\s*status\s*:\s*(success|succeeded|done|failed|error)\s*$", handoff)
    if status:
        return status.group(1).lower() in {"success", "succeeded", "done"}
    failed = _extract_handoff_field(handoff, "Failed subtasks")
    if failed is None:
        return None
    cleaned = re.sub(r"(?m)^\s*[-*]\s*", "", failed).strip().lower()
    if cleaned in {"none", "n/a", "na", "null", "[]", "-"}:
        return True
    if cleaned:
        return False
    return None


def _parse_agent_run(path: Path) -> dict:
    task_slug, date = _agent_run_slug_date(path)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        print(f"[serve] agent-run read failed for {path}: {e}", flush=True)
        return {
            "task_slug": task_slug,
            "date": date,
            "objective": None,
            "output_hint": None,
            "dag": [],
            "handoff": "",
        }
    objective = _first_section_value(_markdown_section(text, "Objective")) or _line_value(text, "Objective")
    output_hint = _first_section_value(_markdown_section(text, "Output hint")) or _line_value(text, "Output hint")
    dag_section = _markdown_section(text, "Subtask DAG")
    handoff = _markdown_section(text, "Handoff")
    return {
        "task_slug": task_slug,
        "date": date,
        "objective": objective,
        "output_hint": output_hint,
        "dag": _parse_agent_run_dag(dag_section, path) if dag_section else [],
        "handoff": handoff,
    }


def _agent_run_metrics_by_slug() -> dict[str, list[dict]]:
    by_slug: dict[str, list[dict]] = {}
    for row in _load_jsonl_cached(METRICS_FILE):
        if not isinstance(row, dict):
            continue
        slug = row.get("task_slug")
        if isinstance(slug, str) and slug:
            by_slug.setdefault(slug, []).append(row)
    return by_slug


def _list_agent_runs() -> list[dict]:
    if not AGENT_RUNS_DIR.is_dir():
        return []
    metrics_by_slug = _agent_run_metrics_by_slug()
    trusted_root = os.path.realpath(str(AGENT_RUNS_DIR))
    runs: list[dict] = []
    for path in AGENT_RUNS_DIR.glob("*.md"):
        if path.name == ".gitkeep":
            continue
        try:
            resolved = path.resolve(strict=True)
            if not _is_under_trusted_dir(resolved, trusted_root):
                print(f"[serve] agent-run outside trusted dir skipped: {path}", flush=True)
                continue
            st = path.stat()
        except OSError as e:
            print(f"[serve] agent-run stat failed for {path}: {e}", flush=True)
            continue
        parse_key = str(path)
        mtime_ns = st.st_mtime_ns
        with _AGENT_RUN_PARSE_LOCK:
            cached = _AGENT_RUN_PARSE_CACHE.get(parse_key)
            if cached is not None and cached[0] == mtime_ns:
                parsed = cached[1]
            else:
                parsed = _parse_agent_run(path)
                _AGENT_RUN_PARSE_CACHE[parse_key] = (mtime_ns, parsed)
                _bound_path_cache(_AGENT_RUN_PARSE_CACHE)
        slug = parsed.get("task_slug")
        handoff = parsed.get("handoff") or ""
        plan_ts = _dt.datetime.fromtimestamp(st.st_mtime, _dt.timezone.utc).isoformat(timespec="seconds")
        try:
            rel_path = str(path.relative_to(ROOT)).replace("\\", "/")
        except ValueError:
            rel_path = str(path)
        runs.append({
            "task_slug": slug,
            "date": parsed.get("date"),
            "plan_ts": plan_ts.replace("+00:00", "Z"),
            "dispatch_count": len(parsed.get("dag") or []),
            "synthesis_ts": _extract_handoff_synthesis_ts(handoff),
            "success": _extract_agent_run_success(handoff),
            "path": rel_path,
            "metrics": metrics_by_slug.get(slug, []) if isinstance(slug, str) else [],
        })
    runs.sort(key=lambda row: row.get("plan_ts") or "", reverse=True)
    return runs


def _list_pipelines() -> list[dict]:
    """List pipeline files for the dashboard. Excludes .gitkeep. Newest mtime first."""
    import yaml  # local import — PyYAML is only needed by this helper
    if not PIPELINES_DIR.is_dir():
        return []
    rows: list[dict] = []
    for p in PIPELINES_DIR.glob("*.yaml"):
        try:
            text = p.read_text(encoding="utf-8")
            parsed = yaml.safe_load(text) or {}
        except (OSError, yaml.YAMLError):
            continue
        nodes = parsed.get("nodes") or []
        sink_kinds = ("synthesize", "collect", "passthrough")
        sink_kind = next(
            (n.get("kind") for n in nodes
             if isinstance(n, dict) and n.get("kind") in sink_kinds),
            "",
        )
        agent_count = sum(
            1 for n in nodes if isinstance(n, dict) and n.get("agent")
        )
        try:
            rel_path = str(p.relative_to(ROOT)).replace("\\", "/")
        except ValueError:
            rel_path = str(p).replace("\\", "/")
        rows.append({
            "slug": p.stem,
            "path": rel_path,
            "description": parsed.get("description") or "",
            "node_count": agent_count,
            "output_mode": sink_kind,
            "mtime": p.stat().st_mtime,
        })
    rows.sort(key=lambda r: r["mtime"], reverse=True)
    return rows


def _git_lsfiles_cached(cwd: Path) -> list[str] | None:
    try:
        st = (cwd / ".git" / "index").stat()
    except OSError:
        return None
    with _GIT_LSFILES_LOCK:
        entry = _GIT_LSFILES_CACHE.get(str(cwd))
        if entry is None:
            return None
        cached_at, index_mtime_ns, lines = entry
        if (time.monotonic() - cached_at) < _GIT_LSFILES_TTL_S and index_mtime_ns == st.st_mtime_ns:
            return lines
    return None


def _git_lsfiles_put(cwd: Path, lines: list[str]) -> None:
    try:
        st = (cwd / ".git" / "index").stat()
    except OSError:
        return
    with _GIT_LSFILES_LOCK:
        _GIT_LSFILES_CACHE[str(cwd)] = (time.monotonic(), st.st_mtime_ns, list(lines))


def _write_text_lf(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` with LF line endings, regardless of platform.

    Python's ``Path.write_text`` defaults to ``newline=None`` which translates
    ``\\n`` to the OS line terminator (``\\r\\n`` on Windows). The repo's
    ``.gitattributes`` pins ``*.yaml`` / ``*.md`` to ``eol=lf``, so writing
    those files through the dashboard previously produced spurious
    ``"CRLF will be replaced by LF"`` git warnings on Windows."""
    path.write_text(text, encoding="utf-8", newline="\n")


_DEFAULT_JOBS_PERSIST_FILE = JOBS_PERSIST_FILE


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
    # Key the memo by BOTH the cwd and the current projects root. The root is
    # an overridable module global (tests monkeypatch ``_CLAUDE_PROJECTS_ROOT_
    # OVERRIDE`` to point at a tmp tree); keying on cwd alone would hand back a
    # stale dir resolved against a previous root once that override changes.
    key = (str(cwd), str(root) if root is not None else None)
    if key in _TRANSCRIPTS_DIR_CACHE:
        return _TRANSCRIPTS_DIR_CACHE[key]
    if root is None:
        _TRANSCRIPTS_DIR_CACHE[key] = None
        return None
    s = str(cwd)
    slug_lower = (s[0].lower() + s[1:]).replace(":", "-").replace("\\", "-").replace("/", "-").replace(" ", "-")
    slug_upper = (s[0].upper() + s[1:]).replace(":", "-").replace("\\", "-").replace("/", "-").replace(" ", "-")
    for slug in (slug_lower, slug_upper, slug_lower.lower()):
        p = root / slug
        if p.is_dir():
            _TRANSCRIPTS_DIR_CACHE[key] = p
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
                            _TRANSCRIPTS_DIR_CACHE[key] = sub
                            return sub
                        break  # only peek first record per file
            except OSError:
                continue
    _TRANSCRIPTS_DIR_CACHE[key] = None
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

# Cached responses for /api/skills/all and /api/agents/all. Both endpoints
# walk 3-4 disk locations (.claude/skills, ~/.claude/skills, ~/.codex/skills
# for skills; project + user + plugin marketplaces/cache for agents) and the
# dashboard fires them in parallel at boot — combined ~500-1000ms of FS work
# on cold cache. A 15s TTL absorbs the boot storm + tab-switch refresh
# without making manual edits invisible for long: SKILL.md tweaks show up
# within 15 s, which matches the "auto-refresh" cadence elsewhere.
_SKILLS_ALL_CACHE: dict = {"at": 0.0, "data": None}
_AGENTS_ALL_CACHE: dict = {"at": 0.0, "data": None}
_CATALOG_TTL_SECONDS = 15.0


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
        try:
            with p.open("r", encoding="utf-8", errors="replace") as fh:
                # Count only transcripts we actually opened — a locked/deleted
                # file (OSError below) contributes no usage data and shouldn't
                # inflate the scanned-transcript count.
                out["transcripts"] += 1
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

    def parse_rollout_file(path: Path) -> dict:
        file_agg = {
            "_target": target,
            "cwd_matches": False,
            "tokens": [],
            "latest_rl": None,
        }
        cwd_matches = False
        current_model = "unknown"
        latest_file_rl: tuple[_dt.datetime, dict] | None = None
        try:
            with path.open("r", encoding="utf-8", errors="replace") as fh:
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
                            # Skip empty exhausted-quota snapshots so they do
                            # not clobber the last usable rate-limit payload.
                            has_payload = isinstance(rl.get("primary"), dict) or isinstance(rl.get("secondary"), dict)
                            if has_payload and (latest_file_rl is None or ts > latest_file_rl[0]):
                                latest_file_rl = (ts, rl)
                        if not cwd_matches:
                            continue
                        info = payload.get("info") or {}
                        last = info.get("last_token_usage")
                        if not isinstance(last, dict):
                            continue
                        file_agg["tokens"].append({
                            "ts": ts,
                            "model": current_model,
                            "last": dict(last),
                        })
        except OSError:
            return file_agg
        file_agg["cwd_matches"] = cwd_matches
        file_agg["latest_rl"] = latest_file_rl
        return file_agg

    def cached_rollout_file_agg(path: Path) -> dict | None:
        try:
            mtime_ns = path.stat().st_mtime_ns
        except OSError:
            return None
        with _CODEX_FILE_AGG_LOCK:
            cached = _CODEX_FILE_AGG_CACHE.get(str(path))
            if cached is not None and cached[0] == mtime_ns:
                agg = cached[1]
                if agg.get("_target") == target:
                    return agg
        agg = parse_rollout_file(path)
        with _CODEX_FILE_AGG_LOCK:
            _CODEX_FILE_AGG_CACHE[str(path)] = (mtime_ns, agg)
            _bound_path_cache(_CODEX_FILE_AGG_CACHE)
        return agg

    for p in files:
        out["sessions"] += 1
        file_agg = cached_rollout_file_agg(p)
        if file_agg is None:
            continue
        file_rl = file_agg.get("latest_rl")
        if file_rl is not None and (latest_rl is None or file_rl[0] > latest_rl[0]):
            latest_rl = file_rl
        if file_agg.get("cwd_matches"):
            out["matched_sessions"] += 1
        for token in file_agg.get("tokens") or []:
            ts = token.get("ts")
            model = token.get("model") or "unknown"
            last = token.get("last")
            if not isinstance(last, dict):
                continue
            try:
                add(out["all"], model, last)
                if ts is not None and ts >= cutoff_5h:
                    add(out["5h"], model, last)
                if ts is not None and ts >= cutoff_7d:
                    add(out["7d"], model, last)
            except (TypeError, ValueError):
                continue

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
# A *failed* or degraded (stale) usage result is cached only briefly so a
# transient blip (token mid-rewrite, a single upstream 429) clears on the next
# poll instead of pinning the overview card to "n/a" for the full success TTL.
_CLAUDE_USAGE_ERROR_TTL_SECONDS = 10.0
# Last genuinely-successful payload. Served (flagged ``stale``) when a later
# fetch fails, so one momentary error doesn't blank a previously-good reading.
_CLAUDE_USAGE_LAST_GOOD: dict = {"at": 0.0, "data": None}
_CLAUDE_OAUTH_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"


# Reading the credentials file can transiently fail when another Claude Code
# instance (a second project) is rewriting it to refresh the OAuth token:
# Windows raises a sharing violation (OSError/PermissionError) and a half-
# written file fails to parse (JSONDecodeError). Both are momentary, so retry a
# few times with a short backoff before treating a rewrite race as "signed out".
_CREDENTIALS_READ_RETRIES = 3
_CREDENTIALS_READ_RETRY_SLEEP_S = 0.05


def _read_credentials_json(path) -> dict | None:
    """Parse the Claude credentials JSON, retrying past the transient errors a
    concurrent rewrite produces. Returns the parsed dict, or ``None`` if the
    file stays unreadable/unparseable across every attempt."""
    for attempt in range(_CREDENTIALS_READ_RETRIES):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            if attempt < _CREDENTIALS_READ_RETRIES - 1:
                time.sleep(_CREDENTIALS_READ_RETRY_SLEEP_S)
    return None


def _read_claude_oauth_token() -> tuple[str | None, str | None]:
    """Read the local Claude Code OAuth access token plus subscription tier.

    Returns ``(token, tier)`` or ``(None, None)`` when the credentials file
    is missing, malformed, or the token has expired. ``tier`` is the
    ``rateLimitTier`` (e.g. ``default_claude_max_5x``); useful for UI hints
    but not required for the API call itself."""
    path = _CLAUDE_CREDENTIALS_PATH_OVERRIDE or (Path.home() / ".claude" / ".credentials.json")
    creds = _read_credentials_json(path)
    if not isinstance(creds, dict):
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


def _usage_cache_ttl(cached: dict | None) -> float:
    """Cache lifetime for a usage result. A fresh success holds for the full
    TTL; a failure or a degraded (stale) reading holds only briefly so the next
    poll retries soon and can recover real data instead of pinning the error."""
    if cached and cached.get("available") and not cached.get("stale"):
        return _CLAUDE_USAGE_TTL_SECONDS
    return _CLAUDE_USAGE_ERROR_TTL_SECONDS


def _fetch_claude_oauth_usage() -> dict:
    """Fetch real Claude session/weekly utilization from the OAuth usage
    endpoint Claude Code itself uses for ``/usage``. Caches the response for
    ``_CLAUDE_USAGE_TTL_SECONDS`` so a busy overview reload doesn't hammer
    the API. The token never leaves this process.

    On a transient failure (token mid-rewrite, an upstream 429/5xx) the last
    genuinely-successful payload is served instead, flagged ``stale`` with the
    error attached, and cached only for ``_CLAUDE_USAGE_ERROR_TTL_SECONDS`` so
    the card degrades gracefully and recovers on the next poll rather than
    blanking to "n/a" for a full minute.

    Returns ``{"available": bool, "data"?: {...}, "error"?: str, "tier"?: str,
    "stale"?: bool}``. Network errors are swallowed and surfaced as
    ``available=False`` (or a stale reading when one is available)."""
    import urllib.error
    import urllib.request

    now_mono = time.monotonic()
    cached = _CLAUDE_USAGE_CACHE["data"]
    if cached is not None and (now_mono - _CLAUDE_USAGE_CACHE["at"]) < _usage_cache_ttl(cached):
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

    if result.get("available"):
        # Remember the good reading so a later failure can degrade to it.
        _CLAUDE_USAGE_LAST_GOOD["data"] = result
        _CLAUDE_USAGE_LAST_GOOD["at"] = now_mono
    else:
        last = _CLAUDE_USAGE_LAST_GOOD["data"]
        if last is not None and last.get("data") is not None:
            # Degrade to the last-known-good reading rather than blanking the
            # card; flag it stale and keep the error for the "n/a" tooltip.
            result = {
                "available": True,
                "data": last["data"],
                "tier": last.get("tier", result.get("tier")),
                "stale": True,
                "error": result.get("error"),
            }

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

_DROP_THRESHOLD = 64
_DROP_COUNTS: dict[str, dict[int, int]] = {}
_DROP_COUNTS_LOCK = threading.Lock()

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


def _browser_cross_origin_blocked(headers) -> bool:
    """Return True when a long-lived GET (SSE) appears to be a cross-
    origin browser request and should be rejected.

    SSE endpoints can't go through ``_csrf_guard`` directly because we
    also want operator ``curl`` / ``wget`` to work — those send no
    Origin header at all. The actual threat is a cross-origin BROWSER
    page that issues ``new EventSource(...)``/``fetch(...)`` against
    a localhost SSE endpoint: the browser blocks reading the response,
    but the server already allocated a thread + queue slot. Repeated
    cross-origin requests exhaust the request-handling thread pool.

    Rule: reject only when Origin is set AND not in the loopback
    allowlist. Origin absent → not a browser context → allow.
    """
    origin = headers.get("Origin")
    if origin is None:
        return False
    allowed = {
        f"http://127.0.0.1:{BOUND_PORT}",
        f"http://localhost:{BOUND_PORT}",
        f"http://[::1]:{BOUND_PORT}",
    }
    return origin not in allowed


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
        # RFC 6455 §5.1: a server MUST fail the connection on any unmasked
        # frame from a client. Reject rather than silently process it.
        if not masked:
            raise _WsClosed()
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
        if length > MAX_WS_PAYLOAD:
            raise _WsClosed()
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
              "timeout_seconds", "revert_after_n_uses",
              "sweep_interval_seconds", "sweep_batch_max"):
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


# A failure only counts as a "concrete pain signal" if it is RECENT. Without
# this window, demo-seed or long-resolved failures sit in a rarely-run skill's
# last-N telemetry forever, so the periodic sweep re-audits that skill on every
# wake and the LLM emits a fresh speculative (often wrong) edit each time. The
# 7-day horizon mirrors the transcript-policy STALE_DAYS.
_RECENT_FAILURE_MAX_AGE_DAYS = 7


def _has_audit_signal(skill_id: str,
                      recent_outcomes: list[dict] | None,
                      *, now: float | None = None) -> tuple[bool, str]:
    """Decide whether a structural audit is worth invoking the LLM for.

    Returns ``(should_audit, reason)``. The periodic sweep uses this to
    skip skills with no concrete failure signal — without it, the LLM is
    rubric-bound to find SOMETHING in every healthy skill (the 7 criteria
    are broad enough that no skill satisfies all of them perfectly), so
    every audit pollutes the proposal queue with low-signal suggestions.

    Triggers (any one is enough):
    1. First-time audit (never visited before) — sanity sweep.
    2. ≥1 failure within the last ``_RECENT_FAILURE_MAX_AGE_DAYS`` days in
       ``recent_outcomes`` — concrete, *current* pain signal. Failures we can
       prove are older than that window are ignored; a failure whose ``ts`` is
       missing/unparseable is conservatively still counted.

    ``now`` (epoch seconds) is injectable for tests; defaults to wall clock.

    The manual "Improve now" button bypasses this gate (caller passes
    ``force=True`` to ``_run_improver_for_skill``) because the user's
    click is itself the signal."""
    if _last_improver_run_ts(skill_id) == 0:
        return True, "first-time audit"
    if recent_outcomes:
        now_epoch = time.time() if now is None else now
        cutoff = now_epoch - _RECENT_FAILURE_MAX_AGE_DAYS * 86400
        failed = 0
        for r in recent_outcomes:
            if not isinstance(r, dict):
                continue
            if str(r.get("outcome") or "").lower() not in {"failed", "error"}:
                continue
            ts = _iso_to_epoch(r.get("ts") or "")
            if ts and ts < cutoff:
                continue  # provably stale failure — not a current signal
            failed += 1
        if failed >= 1:
            return True, f"{failed} recent failure(s)"
    return False, "no failure signal (skipped to avoid speculative proposals)"


def _recent_rejected_proposals(skill_id: str, limit: int = 10) -> list[str]:
    """Reasons from recent ``rejected`` ledger rows for this skill, newest
    first, capped at ``limit``. Fed into the improver prompt so the LLM
    doesn't re-propose the same fix the operator already turned down."""
    rejected: list[tuple[float, str]] = []
    for o in _load_jsonl_cached(IMPROVEMENTS_LEDGER):
        if not isinstance(o, dict) or o.get("skill") != skill_id:
            continue
        if o.get("status") != "rejected":
            continue
        reason = (o.get("reason") or "").strip()
        if not reason:
            continue
        ts = _iso_to_epoch(o.get("ts") or "")
        rejected.append((ts, reason[:200]))
    rejected.sort(reverse=True)
    return [r for _, r in rejected[:limit]]


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
                           metrics: dict, job_id: str | None,
                           log_excerpt: str, *,
                           manual: bool = False,
                           recent_outcomes: list[dict] | None = None,
                           rejected_history: list[str] | None = None) -> str:
    """Craft the one-shot prompt sent to the model.

    The schema and ``no change`` example are intentionally front-loaded so
    smaller models (Haiku) don't drift into prose. The skill content and
    log are delimited with ``<<<...>>>`` markers (not triple-backticks)
    because SKILL.md itself often contains fenced code blocks.

    ``manual=True`` is set by the periodic batch sweep and the manual
    "Improve now" endpoint. In that mode the model is asked to audit the
    skill structurally (description quality, output format, allowlist fit,
    stale references) rather than gating on log-excerpt failure signals.
    Without this, the model returns ``no_change`` on essentially every
    healthy skill — the original prompt told it to do exactly that.

    ``recent_outcomes`` is the last N rows from the per-skill telemetry
    (most recent first). Aggregate ``success_rate`` alone hides
    deterioration: a skill at 80% overall might be at 30% over the last
    week. Listing recent outcomes lets the model reason about trend."""
    rate = round((metrics.get("success_rate") or 0.0) * 100) if metrics else None
    summary = (
        f"success_rate={rate}% over {metrics.get('total_jobs',0)} jobs"
        f", avg_cost=${metrics.get('avg_cost_usd',0):.4f}"
        f", avg_duration={int((metrics.get('avg_duration_ms') or 0)/1000)}s"
    ) if metrics and metrics.get("total_jobs") else "no telemetry yet"

    # Compact "done/failed/done/failed/..." line so a haiku-class model
    # can spot a recent-failure cluster at a glance. Truncated to 20.
    if recent_outcomes:
        recent_line = ", ".join(
            (r.get("outcome") or "?") for r in recent_outcomes[:20]
        )
    else:
        recent_line = "(none)"

    if manual:
        role = (
            "ROLE: You are auditing a project skill STRUCTURALLY against "
            "the rubric below. There is no single failing job to anchor "
            "this on — your job is to find the most impactful improvement "
            "the skill itself needs, regardless of whether the last run "
            "succeeded. Propose ONE focused edit when ANY criterion misses; "
            "return no_change only when the skill clearly satisfies all "
            "criteria.\n\n"
            "RUBRIC (one fix per pass, prioritise the lowest-scoring criterion):\n"
            "  1. Description quality: starts with a verb; trigger phrases "
            "cover both the explicit ask and implicit phrasings; specific "
            "not generic.\n"
            "  2. Output format declared: the skill states WHAT it returns "
            "to the caller (markdown report, JSON shape, file path, etc.).\n"
            "  3. Workflow / process steps are explicit (numbered phases, "
            "checklist, or clearly demarcated stages).\n"
            "  4. Edge-cases / refusal conditions named for known failure "
            "modes.\n"
            "  5. Tool allowlist matches what the body actually does (least "
            "privilege — review-only skill should not imply Write/Edit).\n"
            "  6. Currency: no references to paths, sibling skills, or "
            "commands that no longer exist.\n"
            "  7. Recent-failure trend: if the recent_outcomes line shows "
            "≥3 failures in the last 10 invocations, add a guardrail or "
            "tighten an instruction tied to the apparent failure mode.\n\n"
            "Keep edits small (≤ ~12 line delta for structural fixes; ≤6 "
            "for content tweaks). Preserve frontmatter name unchanged. "
            "You MAY tighten the description.\n\n"
        )
    else:
        role = (
            "ROLE: You are reviewing a project skill after one of its "
            "invocations. Propose a refinement when EITHER the log excerpt "
            "shows ambiguity / failure / missing guardrails, OR the recent "
            "outcomes line shows a failure cluster (≥3 failed in the last "
            "10) that the skill could address structurally. Be precise — "
            "do not rewrite working sections. Keep edits small (≤ ~6 line "
            "delta) and keep frontmatter name/description intact.\n\n"
        )
    return (
        "OUTPUT FORMAT (STRICT): Respond with ONE JSON object. NO prose, "
        "NO commentary, NO markdown fences. If you write anything other "
        "than a JSON object, the output is INVALID.\n\n"
        "Schema:\n"
        '  {"change_summary": "<short str>", "rationale": "<short str>", '
        '"new_content": <full new SKILL.md as string OR null>}\n\n'
        'When no change is warranted: '
        '{"change_summary":"none","rationale":"<why>","new_content":null}\n\n'
        f"{role}"
        f"SKILL: {skill_id}\n"
        f"TELEMETRY: {summary}\n"
        f"RECENT_OUTCOMES (last 20, newest first): {recent_line}\n"
        f"JOB: {job_id or '(manual structural review — no specific job)'}\n"
        f"MODE: {'manual structural audit' if manual else 'post-job review'}\n"
        + (
            "PRIOR REJECTED PROPOSALS for this skill (DO NOT re-propose these fixes — "
            "the operator already turned them down):\n  - "
            + "\n  - ".join(rejected_history)
            + "\n"
            if rejected_history else ""
        )
        + "\n"
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
                line = json.dumps(row, default=str) + "\n"
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
    merged_in = _supersede_prior_pending(skill_id, pid, "improve")
    if merged_in:
        payload["merged_from"] = merged_in
        try:
            (SKILL_PROPOSALS_DIR / f"{pid}.json").write_text(
                json.dumps(payload, indent=2), encoding="utf-8")
        except OSError as e:
            # Non-fatal: the new proposal is already on disk and usable;
            # the merged_from annotation is best-effort metadata.
            print(f"[serve] _write_proposal {pid} merged_from update failed: {e}", flush=True)
    return payload


def _supersede_prior_pending(skill_id: str, new_pid: str, new_kind: str) -> list[str]:
    """Mark every prior pending proposal targeting the same skill+kind as
    ``superseded`` so only the newest pending one survives in the dashboard.

    Each older proposal stays on disk (history is preserved) but flips out
    of the pending list. The new proposal absorbs them: we return their ids
    so the caller can record a ``merged_from`` field.

    Returns the list of superseded proposal ids (may be empty)."""
    if not skill_id or not SKILL_PROPOSALS_DIR.is_dir():
        return []
    superseded: list[str] = []
    now_iso = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    for pj in SKILL_PROPOSALS_DIR.glob("*.json"):
        try:
            obj = json.loads(pj.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if obj.get("id") == new_pid:
            continue
        if obj.get("skill") != skill_id:
            continue
        if (obj.get("kind") or "improve") != new_kind:
            continue
        if obj.get("status") not in (None, "pending"):
            continue
        obj["status"] = "superseded"
        obj["applied_at"] = now_iso
        obj["applied_via"] = "merged-into-newer"
        obj["superseded_by"] = new_pid
        try:
            pj.write_text(json.dumps(obj, indent=2), encoding="utf-8")
        except OSError as e:
            # Best-effort: a failed mark leaves the older proposal in the
            # pending list, which the list endpoint will dedupe again on
            # the next call. Log so the operator notices repeated failures.
            print(f"[serve] supersede {pj.name} failed: {e}", flush=True)
            continue
        superseded.append(obj.get("id") or pj.stem)
    return superseded


# Cross-tool skill mirror. The repo keeps two parallel trees:
#   .claude/skills/<name>/   (source of truth, consumed by Claude)
#   .agents/skills/<name>/   (mirror, consumed by Codex)
# The two are kept in sync by .ai/scripts/sync_skills.py — without an in-process
# mirror step here, an improver-applied edit to .claude/skills/<x>/SKILL.md
# would only be visible to Claude. Codex would keep using the stale .agents
# copy until the user remembered to re-run the sync script by hand.
#
# Skills whose contents are intentionally NOT mirrored — cross-call bridges
# whose claude/agents copies are deliberately different.
_BRIDGE_SKILLS_NO_MIRROR = frozenset({"codex", "claude"})


def _mirror_claude_skill_to_agents(claude_skill_md: Path) -> tuple[bool, str]:
    """Update the parallel ``.agents/skills/<name>/SKILL.md`` ONLY when
    that mirror already exists. Used as a post-apply hook so an edit to
    an existing dual-tree skill propagates to the Codex side; a Claude-
    only skill (no .agents counterpart) stays Claude-only — the improver
    must not invent a Codex mirror the user never asked for.

    Best-effort, never raises:
      * ``(True, "<rel>")`` — mirror file existed and was updated.
      * ``(False, "skipped: <reason>")`` for known no-op cases
        (not a project skill, bridge pair, agents dir absent, mirror
        file absent, identical content).
      * ``(False, "error: ...")`` when a write actually failed.

    For the "I just created a brand-new skill and want it on both sides"
    case, see ``_create_skill_in_both_trees`` — that's the only path
    that's allowed to materialise a new file under .agents/skills/."""
    try:
        claude_root = (ROOT / ".claude" / "skills").resolve()
        agents_root = (ROOT / ".agents" / "skills").resolve()
        target_under_claude = claude_skill_md.resolve()
        rel = target_under_claude.relative_to(claude_root)
    except (ValueError, OSError):
        return (False, "skipped: not a .claude/skills path")
    # rel is "<skill_name>/SKILL.md" (or deeper for reference files we
    # don't currently mirror through this hook — see _apply_improvement
    # caller, which only touches SKILL.md).
    parts = rel.parts
    if not parts:
        return (False, "skipped: empty relative path")
    skill_name = parts[0]
    if skill_name in _BRIDGE_SKILLS_NO_MIRROR:
        return (False, f"skipped: bridge skill {skill_name!r} intentionally not mirrored")
    if not agents_root.is_dir():
        return (False, "skipped: .agents/skills not on disk")
    dst = agents_root / rel
    # New: only mirror when the destination ALREADY exists. A skill that
    # lives only under .claude/skills/ stays that way — there's no
    # reason to invent a .agents/skills/ copy for a Claude-only skill,
    # and doing so silently is the bug the operator was hitting.
    if not dst.is_file():
        return (False, f"skipped: no .agents mirror exists for {skill_name!r}")
    try:
        new_bytes = target_under_claude.read_bytes()
    except OSError as e:
        return (False, f"error: read source failed: {e}")
    try:
        if dst.read_bytes() == new_bytes:
            return (False, "skipped: agents copy already matches")
    except OSError:
        # Unreadable mirror — fall through and overwrite it; the safe
        # default is to align to the source of truth.
        pass
    try:
        dst.write_bytes(new_bytes)
    except OSError as e:
        return (False, f"error: write mirror failed: {e}")
    rel_str = str(Path(".agents/skills") / rel).replace("\\", "/")
    return (True, rel_str)


def _create_skill_in_both_trees(slug: str, content: str) -> dict:
    """Materialise a brand-new project skill at
    ``.claude/skills/<slug>/SKILL.md`` AND (when it's not a cross-call
    bridge) at ``.agents/skills/<slug>/SKILL.md``. Used by the draft-
    install path where the operator explicitly wants the new skill on
    both sides of the dual tree.

    Returns a dict:
      ``{"claude_path": "<rel>", "agents_path": "<rel>" | None,
        "agents_skipped_reason": "<str>" | None}``

    Errors on the Claude side raise (caller responsibility — the entire
    install fails). Errors on the Agents side are reported via
    ``agents_skipped_reason`` so the caller can decide whether to
    surface as a warning."""
    claude_dir = ROOT / ".claude" / "skills" / slug
    claude_md = claude_dir / "SKILL.md"
    try:
        claude_dir.mkdir(parents=True, exist_ok=True)
        claude_md.write_text(content, encoding="utf-8")
    except OSError as e:
        # Source-of-truth write failed — propagate so the caller can fail
        # the install cleanly (the proposal stays pending, no audit row
        # claims success). The wrapping try gives the AST-level
        # "every write_text is OSError-guarded" invariant test a handler
        # to find — re-raising is the intended behaviour.
        print(f"[serve] dual-install: claude-side write failed for {slug}: {e}",
              flush=True)
        raise
    result = {
        "claude_path": f".claude/skills/{slug}/SKILL.md",
        "agents_path": None,
        "agents_skipped_reason": None,
    }
    if slug in _BRIDGE_SKILLS_NO_MIRROR:
        result["agents_skipped_reason"] = (
            f"bridge skill {slug!r} intentionally not mirrored to .agents"
        )
        return result
    agents_root = ROOT / ".agents" / "skills"
    if not agents_root.is_dir():
        # Claude-only project — don't invent a parallel tree the operator
        # never set up. The .claude side is enough for them.
        result["agents_skipped_reason"] = ".agents/skills not on disk"
        return result
    agents_dir = agents_root / slug
    agents_md = agents_dir / "SKILL.md"
    try:
        agents_dir.mkdir(parents=True, exist_ok=True)
        agents_md.write_text(content, encoding="utf-8")
    except OSError as e:
        # Best-effort: .claude/ install already committed, surface the
        # agents miss as a warning but don't fail the whole flow.
        print(f"[serve] dual-install: agents-side write failed for {slug}: {e}",
              flush=True)
        result["agents_skipped_reason"] = f"write error: {e}"
        return result
    result["agents_path"] = f".agents/skills/{slug}/SKILL.md"
    return result


def _apply_improvement(skill_path: Path, new_content: str, source: str,
                       reason: str, proposal_id: str | None,
                       skill_id: str, diff_lines: int) -> bool:
    """Backup -> overwrite -> audit -> mirror to .agents. Returns True on
    success of the overwrite. Skill files are git-tracked so a
    ``git diff`` is always available as a second safety net beyond the
    on-disk .bak. The codex-side mirror is best-effort: a failed mirror
    is logged but doesn't fail the apply (the .claude/ copy already has
    the new content; the operator can re-run .ai/scripts/sync_skills.py)."""
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
    # Mirror to the .agents/skills tree so the Codex side picks up the
    # change. Best-effort — log + continue on failure so a sync miss
    # doesn't roll back an otherwise-successful apply.
    mirrored, mirror_msg = _mirror_claude_skill_to_agents(skill_path)
    if mirrored:
        print(f"[serve] mirrored {skill_id} -> {mirror_msg}", flush=True)
    elif mirror_msg.startswith("error:"):
        print(f"[serve] mirror to .agents failed for {skill_id}: {mirror_msg}",
              flush=True)
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


def _purge_claude_transcript(session_id: str | None) -> bool:
    """Delete the per-session JSONL Claude Code wrote for a one-shot
    background call (e.g. an improver run). Without this every improver
    invocation pollutes ``~/.claude/projects/<slug>/`` with a stray
    "OUTPUT FORMAT (STRICT)" session row in the user's chat history.
    Best-effort: missing dir / missing file / OS errors are swallowed."""
    if not session_id:
        return False
    try:
        tdir = _transcripts_dir_for_cwd(ROOT)
        if tdir is None:
            return False
        f = tdir / f"{session_id}.jsonl"
        if not f.is_file():
            return True
        for attempt in range(3):
            try:
                os.unlink(f)
                if attempt:
                    print(
                        f"[serve] transcript deleted for {session_id} after {attempt + 1} attempts",
                        flush=True,
                    )
                return True
            except FileNotFoundError:
                return True
            except (PermissionError, OSError) as e:
                if attempt == 2:
                    print(f"[serve] transcript delete failed for {session_id}: {e}", flush=True)
                    return False
                time.sleep(0.05)
    except OSError as e:
        # Best-effort delete (file may be locked on Windows, or removed
        # by a concurrent caller). Log so the operator can see why a
        # stale transcript stuck around.
        print(f"[serve] transcript delete failed for {session_id}: {e}", flush=True)
        return False
    return True


def _snapshot_tracked_improver_sids() -> list[str]:
    with _IMPROVER_TRACKED_SIDS_LOCK:
        sids = sorted(_IMPROVER_TRACKED_SIDS)
        _IMPROVER_TRACKED_SIDS.clear()
    return sids


def _purge_all_tracked_improver_sids() -> None:
    for sid in _snapshot_tracked_improver_sids():
        _purge_claude_transcript(sid)


def _chain_improver_shutdown_signal(signum: int, frame, previous_handler) -> None:
    _purge_all_tracked_improver_sids()
    if callable(previous_handler):
        previous_handler(signum, frame)
        raise SystemExit(128 + signum)
    if previous_handler == signal.SIG_IGN:
        return
    if previous_handler == signal.SIG_DFL:
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)
    raise SystemExit(128 + signum)


def _install_improver_shutdown_handlers() -> None:
    global _IMPROVER_SHUTDOWN_HANDLERS_INSTALLED
    if _IMPROVER_SHUTDOWN_HANDLERS_INSTALLED:
        return
    _IMPROVER_SHUTDOWN_HANDLERS_INSTALLED = True
    atexit.register(_purge_all_tracked_improver_sids)

    signals = [signal.SIGINT]
    if hasattr(signal, "SIGTERM"):
        signals.append(signal.SIGTERM)
    for sig in signals:
        try:
            previous = signal.getsignal(sig)
            signal.signal(
                sig,
                lambda signum, frame, previous_handler=previous: _chain_improver_shutdown_signal(
                    signum,
                    frame,
                    previous_handler,
                ),
            )
        except (ValueError, OSError):
            continue


def _purge_stale_improver_transcripts_once() -> dict[str, int]:
    counts = {"orphan": 0, "resolved": 0, "unmatched_pre_audit": 0, "keep": 0, "failed": 0}
    tdir = _transcripts_dir_for_cwd(ROOT)
    if tdir is None:
        return counts
    ledger_rows = load_ledger_rows(IMPROVEMENTS_LEDGER)
    now = time.time()
    for path in sorted(tdir.glob("*.jsonl")):
        bucket = classify_transcript(path, ledger_rows, now)
        counts[bucket] += 1
        if bucket == "keep":
            continue
        try:
            path.unlink()
        except OSError as e:
            counts["failed"] += 1
            print(f"[serve] stale transcript delete failed for {path}: {e}", flush=True)
    return counts


def _periodic_transcript_purge_loop(interval_seconds: int = 86400, *, run_once: bool = False) -> None:
    """Daemon loop that purges stale improver transcript backlog daily."""
    if os.environ.get("AI_WORKFLOW_DISABLE_IMPROVER"):
        return
    while True:
        try:
            counts = _purge_stale_improver_transcripts_once()
            candidates = counts["orphan"] + counts["resolved"] + counts["unmatched_pre_audit"]
            if candidates or counts["failed"]:
                print(
                    "[serve] improver transcript purge: "
                    f"orphan={counts['orphan']} resolved={counts['resolved']} "
                    f"unmatched_pre_audit={counts['unmatched_pre_audit']} "
                    f"failed={counts['failed']}",
                    flush=True,
                )
        except Exception as e:  # noqa: BLE001 - loop must never die
            print(f"[serve] improver transcript purge loop error: {e}", flush=True)
        if run_once:
            return
        time.sleep(max(60, int(interval_seconds)))


def _run_improver_for_skill(skill_id: str, skill_md_path: Path,
                            job_id: str | None, log_path: str | None,
                            cfg: dict, *, manual: bool = False,
                            force: bool = False) -> dict:
    """End-to-end: read skill -> call LLM -> parse JSON -> persist proposal
    -> auto-apply if small. Best-effort: any failure is audited and the
    function returns a status dict (never raises). When the tool is
    ``claude`` we generate a dedicated ``--session-id`` and delete the
    resulting transcript at exit so background improver runs never show
    up in the chat list.

    ``manual=True`` (used by the manual /api/skills/<name>/improve
    endpoint and the periodic batch sweep) selects the structural-audit
    variant of the prompt and audits with ``source="manual"`` so the
    proposal is distinguishable from job-triggered runs.

    ``force=True`` bypasses the telemetry gate that normally skips
    structural audits on healthy skills with no failure signal. The
    "Improve now" button passes this because the operator's click is
    itself the signal; the periodic sweep does not.

    Returns a dict with at minimum ``{"status": <audit_status>}`` plus a
    ``proposal_id`` when one was created. Callers that don't care can
    ignore the return value."""
    source = "manual" if manual else "auto"
    try:
        skill_content = skill_md_path.read_text(encoding="utf-8")
    except OSError as e:
        _audit_improvement(skill_id, "failed", f"read error: {e}", None, None, 0,
                           source=source)
        return {"status": "failed", "reason": f"read error: {e}"}
    metrics = _aggregate_skill_metrics().get(skill_id) or {}
    recent_outcomes = metrics.get("recent") or []
    if manual and not force:
        should, reason = _has_audit_signal(skill_id, recent_outcomes)
        if not should:
            _audit_improvement(skill_id, "no_change", reason, None, None, 0,
                               source=source)
            return {"status": "no_change", "reason": reason}
    log_excerpt = _read_log_excerpt(log_path)
    rejected_history = _recent_rejected_proposals(skill_id)
    prompt = _build_improver_prompt(skill_id, skill_content, metrics, job_id,
                                    log_excerpt, manual=manual,
                                    recent_outcomes=recent_outcomes,
                                    rejected_history=rejected_history)

    # IMPORTANT (Windows): pass the prompt via stdin, not argv. Long prompts
    # on argv silently fail (claude emits only a "status:ready" stub and never
    # processes the request) — observed empirically. stdin works for any size.
    tool_bin = _safe_which(cfg["tool"]) or cfg["tool"]
    argv = [tool_bin, "-p", "--model", cfg["model"]]
    # Pin a session id ONLY for claude — codex doesn't write per-session
    # JSONLs into ~/.claude/projects/ so it doesn't have the same pollution
    # problem. The id lets _purge_claude_transcript know exactly which file
    # to delete in the finally block below.
    improver_sid: str | None = None
    if cfg.get("tool") == "claude":
        improver_sid = str(uuid.uuid4())
        with _IMPROVER_TRACKED_SIDS_LOCK:
            _IMPROVER_TRACKED_SIDS.add(improver_sid)
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
            _audit_improvement(skill_id, "failed", f"subprocess error: {e}",
                               None, None, 0, source=source)
            return {"status": "failed", "reason": f"subprocess error: {e}"}
        if proc.returncode != 0:
            reason = f"exit {proc.returncode}: {(proc.stderr or '')[:200]}"
            _audit_improvement(skill_id, "failed", reason, None, None, 0,
                               source=source)
            return {"status": "failed", "reason": reason}

        parsed = _parse_improver_output(proc.stdout or "")
        if not parsed:
            _audit_improvement(skill_id, "no_change",
                               "improver returned unparseable output",
                               None, None, 0, source=source)
            return {"status": "no_change",
                    "reason": "improver returned unparseable output"}
        new_content = parsed.get("new_content")
        if not isinstance(new_content, str) or not new_content.strip():
            reason = parsed.get("rationale") or "improver returned null"
            _audit_improvement(skill_id, "no_change", reason,
                               None, None, 0, source=source)
            return {"status": "no_change", "reason": reason}

        diff_lines = _diff_line_count(skill_content, new_content)
        if diff_lines == 0:
            _audit_improvement(skill_id, "no_change", "no effective change",
                               None, None, 0, source=source)
            return {"status": "no_change", "reason": "no effective change"}

        try:
            proposal = _write_proposal(skill_id, skill_md_path, skill_content,
                                       new_content, parsed, diff_lines, job_id)
        except OSError as e:
            # _write_proposal already logged the underlying cause; record
            # a "failed" audit row so the operator-facing ledger reflects
            # the dropped improver run rather than appearing to succeed.
            _audit_improvement(skill_id, "failed", f"proposal write error: {e}",
                               None, None, diff_lines, source=source)
            return {"status": "failed",
                    "reason": f"proposal write error: {e}"}
        # Manual triggers (the "Improve now" button) ALWAYS produce a
        # pending proposal — the operator clicked because they want to
        # review the change, so a small-diff auto-apply would be a
        # surprising silent write. Only background / job-triggered runs
        # use the size-based auto-apply shortcut.
        if not manual and diff_lines <= int(cfg.get("small_change_max_lines", 6)):
            _apply_improvement(skill_md_path, new_content, source=source,
                               reason=parsed.get("change_summary", "") or "",
                               proposal_id=proposal["id"], skill_id=skill_id,
                               diff_lines=diff_lines)
            return {"status": "applied", "proposal_id": proposal["id"],
                    "diff_lines": diff_lines,
                    "change_summary": parsed.get("change_summary", "") or ""}
        _audit_improvement(skill_id, "pending",
                           parsed.get("change_summary", "") or "",
                           proposal["id"], None, diff_lines, source=source)
        return {"status": "pending", "proposal_id": proposal["id"],
                "diff_lines": diff_lines,
                "change_summary": parsed.get("change_summary", "") or ""}
    finally:
        if improver_sid:
            with _IMPROVER_TRACKED_SIDS_LOCK:
                _IMPROVER_TRACKED_SIDS.discard(improver_sid)
        _purge_claude_transcript(improver_sid)


def _trigger_improvers_for_job(job_id: str, skill_ids: list[str]) -> None:
    """Spawn one improver thread per project skill invoked by ``job_id``.
    Throttled via ``IMPROVEMENTS_LEDGER`` and config-gated."""
    cfg = _load_improver_config()
    if not cfg.get("enabled"):
        return
    if not _safe_which(cfg["tool"]):
        return  # CLI not on PATH (or on an untrusted PATH entry); silently skip
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


# Last-wake timestamp for the periodic sweep loop. Module-level (not in
# JOBS) because the sweep is global, not per-job. Initialised to 0 so the
# first wake of the loop always runs a sweep after the boot delay.
_LAST_IMPROVER_SWEEP_TS: float = 0.0
_IMPROVER_SWEEP_LOCK = threading.Lock()


def _periodic_improver_sweep(cfg: dict | None = None) -> dict:
    """Visit every project skill on a structural-audit pass. Picks the K
    most-stale skills (by last improver-run timestamp), then runs
    ``_run_improver_for_skill`` for each with ``manual=True``. Respects
    the same per-skill throttle as job-triggered runs so we don't double-
    audit a skill that the job hook just visited.

    Returns ``{"audited": [...], "skipped": [...]}`` so the caller (the
    loop, or a future on-demand "sweep now" endpoint) can log + surface a
    summary. Exceptions inside one skill's audit are swallowed so a
    single broken skill doesn't kill the whole sweep."""
    cfg = cfg or _load_improver_config()
    out = {"audited": [], "skipped": [], "disabled": False}
    if not cfg.get("enabled"):
        out["disabled"] = True
        return out
    if not _safe_which(cfg["tool"]):
        out["disabled"] = True
        return out
    proj = _project_skill_index()
    if not proj:
        return out
    throttle = int(cfg.get("min_interval_seconds", 300))
    now = time.time()
    # Sort skills by oldest last-run first so a long-lived dashboard
    # eventually covers every skill (rather than starving the alphabet
    # tail behind a "name < X" filter).
    candidates: list[tuple[float, str, Path]] = []
    for name, path in proj.items():
        last = _last_improver_run_ts(name)
        if last and (now - last) < throttle:
            out["skipped"].append({"skill": name, "reason": "throttled",
                                   "last_run_ago_s": int(now - last)})
            continue
        candidates.append((last or 0.0, name, path))
    candidates.sort(key=lambda t: t[0])  # oldest first
    cap = max(1, int(cfg.get("sweep_batch_max", 4)))
    for _, name, path in candidates[:cap]:
        try:
            result = _run_improver_for_skill(name, path, job_id=None,
                                             log_path=None, cfg=cfg,
                                             manual=True)
        except Exception as e:  # noqa: BLE001 — never crash the sweep
            print(f"[serve] sweep audit failed for {name}: {e}", flush=True)
            out["skipped"].append({"skill": name, "reason": f"crash: {e}"})
            continue
        out["audited"].append({"skill": name, "result": result})
    # Mark remaining (over-cap) candidates as deferred so the operator log
    # is honest about partial coverage.
    for _, name, _path in candidates[cap:]:
        out["skipped"].append({"skill": name, "reason": "over-batch-cap"})
    return out


def _periodic_improver_loop() -> None:
    """Daemon loop. Wakes every minute, runs the sweep when the
    configured ``sweep_interval_seconds`` has elapsed since the last
    sweep. Cheap idle path (one stat() through the cached ledger reads
    inside ``_periodic_improver_sweep``)."""
    global _LAST_IMPROVER_SWEEP_TS
    # Boot delay — let the server finish coming up before the first sweep
    # so a 0-skill window during initial imports doesn't get audited.
    time.sleep(30)
    while True:
        try:
            cfg = _load_improver_config()
            interval = max(60, int(cfg.get("sweep_interval_seconds", 21600)))
            with _IMPROVER_SWEEP_LOCK:
                due = (time.time() - _LAST_IMPROVER_SWEEP_TS) >= interval
            if due and cfg.get("enabled"):
                summary = _periodic_improver_sweep(cfg)
                with _IMPROVER_SWEEP_LOCK:
                    _LAST_IMPROVER_SWEEP_TS = time.time()
                n_aud = len(summary.get("audited") or [])
                n_skp = len(summary.get("skipped") or [])
                print(f"[serve] improver sweep: audited={n_aud} skipped={n_skp}",
                      flush=True)
        except Exception as e:  # noqa: BLE001 — loop must never die
            print(f"[serve] improver sweep loop error: {e}", flush=True)
        time.sleep(60)


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
                line = "".join(json.dumps(row, default=str) + "\n" for row in rows)
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


# ---------------------------------------------------------------------------
# Analytics page aggregation (see .ai/specs/2026-06-02-analytics-page-design.md).
# Pure, ``now``-injected so it is unit-testable; reads the six ledgers via the
# mtime-keyed _load_jsonl_cached. Never raises on null/missing/malformed rows.
# ---------------------------------------------------------------------------

_ANALYTICS_RANGES = {"7d": 7, "30d": 30, "90d": 90, "all": None}


def _analytics_range_bounds(now, range_key):
    """Return ``(current_period_start, previous_period_start)`` as tz-aware
    datetimes. ``"all"`` returns ``(None, None)``: no lower bound and no
    previous-period delta. Unknown keys fall back to 30d."""
    days = _ANALYTICS_RANGES.get(range_key, 30)
    if days is None:
        return None, None
    cur_start = now - _dt.timedelta(days=days)
    prev_start = now - _dt.timedelta(days=days * 2)
    return cur_start, prev_start


def _analytics_parse_ts(raw):
    """Parse an ISO-8601 ledger timestamp to a tz-aware datetime, or None.
    Accepts a trailing ``Z`` and explicit offsets; never raises."""
    if not raw or not isinstance(raw, str):
        return None
    try:
        d = _dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=_dt.timezone.utc)
    return d


def _analytics_in_range(raw_ts, start, now):
    """True if ``raw_ts`` falls within ``[start, now]``. ``start=None`` means no
    lower bound (the 'all' range). Unparseable / future timestamps are excluded."""
    d = _analytics_parse_ts(raw_ts)
    if d is None:
        return False
    if d > now:
        return False
    if start is not None and d < start:
        return False
    return True


def _aggregate_analytics(now, range_key):
    """Pure aggregation: read the six ledgers via the mtime cache, filter to the
    range, and return the chart-ready payload documented in the spec. Never raises
    on null/missing/malformed rows."""
    cur_start, prev_start = _analytics_range_bounds(now, range_key)

    metrics = list(_load_jsonl_cached(METRICS_FILE))

    def in_cur(row):
        return _analytics_in_range(row.get("ts"), cur_start, now)

    def in_prev(row):
        # previous window is [prev_start, cur_start); None bounds => empty
        if prev_start is None or cur_start is None:
            return False
        d = _analytics_parse_ts(row.get("ts"))
        return d is not None and prev_start <= d < cur_start

    cur_metrics = [r for r in metrics if in_cur(r)]
    prev_metrics = [r for r in metrics if in_prev(r)]

    def success_rate(rows):
        rated = [r for r in rows if r.get("exit_code") is not None]
        if not rated:
            return None
        return round(sum(1 for r in rated if r.get("exit_code") == 0) / len(rated), 4)

    def avg_duration(rows):
        ds = [r["duration_ms"] for r in rows if isinstance(r.get("duration_ms"), (int, float))]
        return int(sum(ds) / len(ds)) if ds else None

    payload = {
        "range": range_key if range_key in _ANALYTICS_RANGES else "30d",
        "generated_at": now.isoformat(),
        "kpis": {
            "phase_runs": {"value": len(cur_metrics), "prev": len(prev_metrics)},
            "success_rate": {"value": success_rate(cur_metrics), "prev": success_rate(prev_metrics)},
            "avg_duration": {"value": avg_duration(cur_metrics), "prev": avg_duration(prev_metrics), "unit": "ms"},
            # filled in by the cost / backlog blocks below:
            "total_spend": {"value": 0.0, "prev": 0.0, "unit": "usd"},
            "open_todos": {"value": 0},
            "pending_proposals": {"value": 0},
        },
        "cost": {"spend_over_time": [], "by_model": [], "duration_by_phase": []},
        "health": {"runs_over_time": [], "outcomes": {}, "retries_over_time": [], "review_verdicts": {}},
        "skills": {"top_by_invocations": [], "table": [], "cost_by_skill": []},
        "backlog": {"proposal_status": {}, "todo_burndown": [], "recent_activity": []},
    }

    # ----- Cost & efficiency -------------------------------------------------
    jobs = list(_load_jsonl_cached(JOBS_PERSIST_FILE))
    skill_rows = list(_load_jsonl_cached(SKILL_METRICS_FILE))

    def job_ts(r):
        # jobs rows are stamped with `created_at`, NOT `ts`. Do not reuse the
        # `in_prev` closure (keyed on `ts`) for jobs — use job_ts everywhere.
        return r.get("created_at") or r.get("ts")

    def cost_events(rows, ts_getter, cost_getter):
        out = []
        for r in rows:
            ts = ts_getter(r)
            if not _analytics_in_range(ts, cur_start, now):
                continue
            usd = cost_getter(r)
            if isinstance(usd, (int, float)):
                out.append((ts, r.get("model") or "unknown", float(usd)))
        return out

    cur_costs = (
        cost_events(jobs, job_ts, lambda r: (r.get("cost") or {}).get("cost_usd"))
        + cost_events(skill_rows, lambda r: r.get("ts"), lambda r: r.get("cost_usd"))
    )
    by_model = {}
    for _ts, model, usd in cur_costs:
        by_model[model] = by_model.get(model, 0.0) + usd
    payload["kpis"]["total_spend"]["value"] = round(sum(by_model.values()), 6)
    payload["cost"]["by_model"] = [
        {"model": m, "usd": round(v, 6)} for m, v in sorted(by_model.items(), key=lambda kv: -kv[1])
    ]
    # spend_over_time: bucket by date (from the parsed datetime, robust vs format)
    by_day = {}
    for ts, _m, usd in cur_costs:
        d = _analytics_parse_ts(ts)
        if d is None:
            continue
        day = d.date().isoformat()
        by_day[day] = by_day.get(day, 0.0) + usd
    payload["cost"]["spend_over_time"] = [
        {"date": day, "usd": round(by_day[day], 6)} for day in sorted(by_day)
    ]
    # duration_by_phase from metrics (duration only — no cost source per spec)
    phase_dur, phase_cnt = {}, {}
    for r in cur_metrics:
        ph = r.get("phase") or "unknown"
        if isinstance(r.get("duration_ms"), (int, float)):
            phase_dur[ph] = phase_dur.get(ph, 0) + r["duration_ms"]
            phase_cnt[ph] = phase_cnt.get(ph, 0) + 1
    payload["cost"]["duration_by_phase"] = [
        {"phase": ph, "duration_ms": phase_dur[ph], "runs": phase_cnt[ph]} for ph in sorted(phase_dur)
    ]

    # previous-period spend for the KPI delta
    def in_prev_jobs(r):
        if prev_start is None or cur_start is None:
            return False
        d = _analytics_parse_ts(job_ts(r))     # jobs use created_at, hence job_ts
        return d is not None and prev_start <= d < cur_start

    prev_spend = sum(
        float((r.get("cost") or {}).get("cost_usd") or 0) for r in jobs if in_prev_jobs(r)
    ) + sum(float(r.get("cost_usd") or 0) for r in skill_rows if in_prev(r))
    payload["kpis"]["total_spend"]["prev"] = round(prev_spend, 6)

    # ----- Workflow health ---------------------------------------------------
    # Outcomes: exit_code 0 -> done, non-zero -> failed. No "cancelled" bucket —
    # that is a jobs-level status absent from metrics.jsonl (see spec).
    outcomes = {}
    runs_by_day = {}
    retries_by_day = {}
    verdicts = {}
    for r in cur_metrics:
        code = r.get("exit_code")
        day = None
        d = _analytics_parse_ts(r.get("ts"))
        if d is not None:
            day = d.date().isoformat()
        if code is not None:
            label = "done" if code == 0 else "failed"
            outcomes[label] = outcomes.get(label, 0) + 1
            if day is not None:
                bucket = runs_by_day.setdefault(day, {"date": day, "done": 0, "failed": 0})
                bucket[label] += 1
        retries = r.get("retries")
        if isinstance(retries, (int, float)) and day is not None:
            rb = retries_by_day.setdefault(day, {"date": day, "retries": 0})
            rb["retries"] += retries
        verdict = r.get("review_verdict") or "none"
        verdicts[verdict] = verdicts.get(verdict, 0) + 1
    payload["health"]["outcomes"] = outcomes
    payload["health"]["runs_over_time"] = [runs_by_day[k] for k in sorted(runs_by_day)]
    payload["health"]["retries_over_time"] = [retries_by_day[k] for k in sorted(retries_by_day)]
    payload["health"]["review_verdicts"] = verdicts

    # ----- Skills & agents ---------------------------------------------------
    cur_skill_rows = [r for r in skill_rows if _analytics_in_range(r.get("ts"), cur_start, now)]
    by_skill = {}
    for r in cur_skill_rows:
        sid = r.get("skill") or ""
        if not sid:
            continue
        agg = by_skill.setdefault(sid, {
            "skill": sid, "runs": 0, "successes": 0, "invocations": 0,
            "total_cost_usd": 0.0, "_by_day": {},
        })
        agg["runs"] += 1
        agg["invocations"] += int(r.get("invocations") or 1)
        if r.get("outcome") == "done":
            agg["successes"] += 1
        agg["total_cost_usd"] += float(r.get("cost_usd") or 0.0)
        d = _analytics_parse_ts(r.get("ts"))
        if d is not None:
            day = d.date().isoformat()
            agg["_by_day"][day] = agg["_by_day"].get(day, 0) + 1
    table = []
    for agg in by_skill.values():
        runs = agg["runs"] or 1
        spark = [agg["_by_day"][day] for day in sorted(agg["_by_day"])]
        table.append({
            "skill": agg["skill"],
            "runs": agg["runs"],
            "success_rate": round(agg["successes"] / runs, 4),
            "avg_cost_usd": round(agg["total_cost_usd"] / runs, 6),
            "spark": spark,
        })
    payload["skills"]["table"] = sorted(table, key=lambda t: -t["runs"])
    payload["skills"]["top_by_invocations"] = sorted(
        [{"skill": a["skill"], "invocations": a["invocations"], "success": a["successes"]}
         for a in by_skill.values()],
        key=lambda t: -t["invocations"],
    )
    payload["skills"]["cost_by_skill"] = sorted(
        [{"skill": a["skill"], "usd": round(a["total_cost_usd"], 6)} for a in by_skill.values()],
        key=lambda t: -t["usd"],
    )

    # ----- Improvements & backlog --------------------------------------------
    improvements = list(_load_jsonl_cached(IMPROVEMENTS_LEDGER))
    todos = list(_load_jsonl_cached(TODOS_FILE))
    events = list(_load_jsonl_cached(EVENTS_FILE))

    # proposal_status: 5 explicit buckets so none are silently dropped.
    # `installed` folds into `applied`.
    _PROPOSAL_BUCKETS = {
        "pending": "pending", "applied": "applied", "installed": "applied",
        "rejected": "rejected", "no_change": "no_change", "failed": "failed",
    }
    proposal_status = {"pending": 0, "applied": 0, "rejected": 0, "no_change": 0, "failed": 0}
    for r in improvements:
        if not _analytics_in_range(r.get("ts"), cur_start, now):
            continue
        bucket = _PROPOSAL_BUCKETS.get(r.get("status"))
        if bucket:
            proposal_status[bucket] += 1
    payload["backlog"]["proposal_status"] = proposal_status

    # todo burndown: per-day open vs resolved counts (range-filtered by created_at).
    def todo_ts(r):
        return r.get("created_at") or r.get("updated_at") or r.get("ts")

    burn = {}
    for r in todos:
        d = _analytics_parse_ts(todo_ts(r))
        if d is None or not _analytics_in_range(todo_ts(r), cur_start, now):
            continue
        day = d.date().isoformat()
        b = burn.setdefault(day, {"date": day, "open": 0, "resolved": 0})
        status = (r.get("status") or "").lower()
        if status == "open":
            b["open"] += 1
        elif status.startswith("resolved"):
            b["resolved"] += 1
    payload["backlog"]["todo_burndown"] = [burn[k] for k in sorted(burn)]

    # recent_activity: newest ~15 events. The events ledger has no summary/ref
    # fields, so synthesise a readable summary and use session_id as the ref.
    def event_summary(r):
        parts = [str(r.get("kind") or "event")]
        for key in ("phase", "tool", "model"):
            val = r.get(key)
            if val and val != "unknown":
                parts.append(str(val))
        return " · ".join(parts)

    sorted_events = sorted(events, key=lambda r: r.get("ts") or "", reverse=True)
    payload["backlog"]["recent_activity"] = [
        {"ts": r.get("ts"), "kind": r.get("kind"),
         "summary": event_summary(r), "ref": r.get("session_id") or ""}
        for r in sorted_events[:15]
    ]

    # ----- Current-state KPIs (whole-ledger totals, NOT range-filtered) ------
    payload["kpis"]["open_todos"]["value"] = sum(
        1 for r in todos if (r.get("status") or "").lower() == "open"
    )
    payload["kpis"]["pending_proposals"]["value"] = sum(
        1 for r in improvements if r.get("status") == "pending"
    )

    return payload


# A dispatched phase in ``METRICS_FILE`` maps back to the project skill that
# fulfils it, so phase-only skills (planner/reviewer/rescue) accrue per-skill
# telemetry even though they never run as standalone dashboard jobs. ``execute``
# is omitted on purpose: the executor is a configured tool/model, not a
# SKILL.md, so there is nothing for the improver to edit.
PHASE_TO_SKILL = {
    "plan": "planner",
    "review": "reviewer",
    "rescue": "rescue",
}


def _phase_metric_rows() -> list[dict]:
    """Re-key ``METRICS_FILE`` phase rows into ``skill_metrics``-shaped rows.

    One metrics.jsonl row == one dispatched phase. Rows whose phase has no
    project skill (e.g. ``execute``) or no usable ``exit_code`` are dropped so
    we never fabricate a skill or a phantom failure signal. Each survivor is
    shaped exactly like a ``SKILL_METRICS_FILE`` row so ``_aggregate_skill_metrics``
    can fold it into the same rollup."""
    out: list[dict] = []
    for row in _load_jsonl_cached(METRICS_FILE):
        if not isinstance(row, dict):
            continue
        sid = PHASE_TO_SKILL.get(str(row.get("phase") or ""))
        if not sid:
            continue
        ec = row.get("exit_code")
        if ec is None:
            continue  # no outcome recorded — not a signal either way
        out.append({
            "ts": row.get("ts"),
            "skill": sid,
            "name": _skill_name_canonical(sid),
            "job_id": row.get("task_slug"),
            "kind": f"phase:{row.get('phase')}",
            "outcome": "done" if ec == 0 else "failed",
            "exit_code": ec,
            "duration_ms": int(row.get("duration_ms") or 0),
            "cost_usd": 0.0,
            "turns": 0,
            "invocations": 1,
            "session_id": row.get("session_id"),
            "model": row.get("model"),
        })
    return out


def _aggregate_skill_metrics() -> dict[str, dict]:
    """Roll up per-skill telemetry into summaries the dashboard renders in
    skill cards (and the auto-improver gates on).

    Sources: job-scoped rows from ``SKILL_METRICS_FILE`` PLUS phase-scoped rows
    bridged from ``METRICS_FILE`` (see ``_phase_metric_rows``) so skills that
    run only as dispatched phases are represented too. The two sources never
    overlap on a skill (orchestrate has no matching phase; plan/review/rescue
    have no job kind), so merging cannot double-count."""
    by_skill: dict[str, dict] = {}
    for row in [*_load_jsonl_cached(SKILL_METRICS_FILE), *_phase_metric_rows()]:
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
    claude_bin = _safe_which("claude") or "claude"

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
        codex_bin = _safe_which("codex") or "codex"
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
        claude_bin = _safe_which("claude") or "claude"
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
        codex_bin = _safe_which("codex") or "codex"
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
            with _DROP_COUNTS_LOCK:
                _DROP_COUNTS.pop(job_id, None)
        except Exception as e:  # noqa: BLE001 (don't crash the server)
            print(f"[serve] job runner crashed for {job_id}: {e}", flush=True)
            with JOBS_LOCK:
                JOBS[job_id]["status"] = "failed"
                JOBS[job_id]["error"] = str(e)
                JOBS[job_id]["ended_at"] = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
            _persist_job(job_id)
            _publish_chunk(job_id, None)
            with _DROP_COUNTS_LOCK:
                _DROP_COUNTS.pop(job_id, None)

    threading.Thread(target=runner, daemon=True, name=f"job-{job_id}").start()


def _publish_chunk(job_id: str, chunk: str | None) -> None:
    """Push a log chunk (or None=EOF) to every SSE subscriber of this job."""
    with JOBS_LOCK:
        subs = list(JOBS.get(job_id, {}).get("subscribers") or [])
    for q in subs:
        qid = id(q)
        try:
            q.put_nowait(chunk)
            if chunk is None:
                with _DROP_COUNTS_LOCK:
                    counts = _DROP_COUNTS.get(job_id)
                    if counts:
                        counts.pop(qid, None)
                        if not counts:
                            _DROP_COUNTS.pop(job_id, None)
        except _stdqueue.Full:
            # Subscriber queue at maxsize=1024 - slow client. Drop this chunk
            # for that subscriber rather than blocking the runner. After a
            # sustained run of drops, ask the client to reconnect and catch up.
            with _DROP_COUNTS_LOCK:
                counts = _DROP_COUNTS.setdefault(job_id, {})
                drops = counts.get(qid, 0) + 1
                counts[qid] = drops
            if drops >= _DROP_THRESHOLD:
                resync_dropped = False
                try:
                    q.put_nowait({"type": "resync", "reason": "slow"})
                except _stdqueue.Full:
                    resync_dropped = True
                eof_dropped = False
                try:
                    q.put_nowait(None)
                except _stdqueue.Full:
                    eof_dropped = True
                if not (resync_dropped or eof_dropped):
                    with _DROP_COUNTS_LOCK:
                        counts = _DROP_COUNTS.get(job_id)
                        if counts:
                            counts.pop(qid, None)
                            if not counts:
                                _DROP_COUNTS.pop(job_id, None)
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


# ---------------------------------------------------------------------------
# SessionRegistry + EngineProtocol adapter
#
# _session_engine_factory(sid, model) returns an adapter that implements
# EngineProtocol (submit / interrupt / kill / is_ready) over a "chat" job
# started with ``claude --resume <sid>``.
# SESSION_REGISTRY is instantiated once at module level; the endpoints
# for Tasks 6-8 will call SESSION_REGISTRY.submit_turn() / .release().
# ---------------------------------------------------------------------------

class _ResumeEngineAdapter:
    """EngineProtocol adapter over a dashboard resume job.

    At construction, immediately launches ``claude --resume <sid>`` via
    _start_subprocess_job (kind="chat"). submit(), interrupt(), and kill()
    delegate to the existing stdin/cancel helpers.
    """

    def __init__(self, job_id: str):
        self._job_id = job_id

    # --- EngineProtocol -------------------------------------------------------

    def submit(self, turn: dict) -> None:
        """Write a user turn to the stdin of the resume subprocess.

        turn is a dict such as {"text": "..."} (and eventually images/files;
        for now only text is handled).
        """
        text = turn.get("text") or ""
        # Reuses _send_to_stdin which already handles JSON-wrapping for kind=chat.
        _send_to_stdin(self._job_id, text)

    def interrupt(self) -> None:
        """Send an interrupt envelope to the resume subprocess."""
        _interrupt_chat_turn(self._job_id)

    def kill(self) -> None:
        """Cancel/terminate the resume job."""
        _cancel_job(self._job_id)

    def is_ready(self) -> bool:
        """True as soon as the subprocess is alive (proc set in JOBS)."""
        with JOBS_LOCK:
            j = JOBS.get(self._job_id)
            if not j:
                return False
            return j.get("proc") is not None


def _session_engine_factory(sid: str, model: str) -> _ResumeEngineAdapter:
    """Build an EngineProtocol adapter for session ``sid``.

    Generates a fresh job_id, registers the job in JOBS with session_id=sid so
    that _start_subprocess_job resolves the correct transcript file, and
    starts the ``claude --resume <sid>`` subprocess in the background.
    """
    job_id = str(uuid.uuid4())
    argv = _build_chat_argv(model=model, session_id=sid, resume=True)

    # Pre-populate JOBS with session_id before calling _start_subprocess_job
    # so the runner can determine log_path from the transcript.
    with JOBS_LOCK:
        JOBS[job_id] = {
            "id": job_id,
            "kind": "chat",
            "task": f"session-resume:{sid}",
            "status": "queued",
            "created_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
            "pid": None,
            "log_path": None,
            "exit_code": None,
            "started_at": None,
            "ended_at": None,
            "session_id": sid,
            "model": model,
        }

    _start_subprocess_job(
        job_id=job_id,
        kind="chat",
        task=f"session-resume:{sid}",
        argv=argv,
    )

    return _ResumeEngineAdapter(job_id)


SESSION_REGISTRY = session_registry.SessionRegistry(
    engine_factory=_session_engine_factory,
)

# ---------------------------------------------------------------------------

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


def _lookup_session_title(session_id: str) -> str | None:
    """Best-effort: extract the Claude-Code-generated ``ai-title`` record
    from a session transcript so the dashboard can label a collapsed
    transcript pane with the IDE's own chat name instead of relying on
    the first user message (or the bare UUID).

    The ai-title is a meta record Claude writes a few lines into the
    JSONL once it's picked a display title — same string the IDE shows
    in its sessions sidebar. Latest one wins (Claude can rename mid-
    session). Bounded scan keeps the picker snappy on multi-MB
    transcripts; if the title hasn't been written yet, the caller falls
    back to the first-user-message preview."""
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
    MAX_LINES = 200
    MAX_BYTES = 64 * 1024
    title: str | None = None
    try:
        with f.open("r", encoding="utf-8", errors="replace") as fh:
            lines_seen = 0
            bytes_seen = 0
            for line in fh:
                lines_seen += 1
                bytes_seen += len(line)
                if lines_seen > MAX_LINES or bytes_seen > MAX_BYTES:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("type") != "ai-title":
                    continue
                at = rec.get("aiTitle")
                if isinstance(at, str) and at.strip():
                    title = at.strip()[:120]
    except OSError:
        return None
    return title


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
        by_session.setdefault(sid, []).append(dict(ev))

    runs: list[dict] = []
    for sid, events in by_session.items():
        for ev in events:
            ev["_dt"] = _parse_iso_ts(ev.get("ts"))
        events.sort(key=lambda e: e["_dt"] or _dt.datetime.min.replace(tzinfo=_dt.timezone.utc))
        phases: list[dict] = []
        prev_dt: _dt.datetime | None = None
        tag_counter: dict[str, int] = {}
        for ev in events:
            ts = ev.get("ts") or ""
            cur_dt = ev["_dt"]
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

        start_dt = events[0]["_dt"]
        end_dt = events[-1]["_dt"]
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
    extensions_map = {
        **http.server.SimpleHTTPRequestHandler.extensions_map,
        ".md": "text/plain; charset=utf-8",
        ".yaml": "text/plain; charset=utf-8",
        ".yml": "text/plain; charset=utf-8",
        ".jsonl": "text/plain; charset=utf-8",
    }
    # Sensitive paths that MUST NOT be served by the static handler. Resolved
    # at class load so symlinks and Windows case differences cannot bypass.
    # The dashboard intentionally reads other project files (.ai/memory.md,
    # .ai/decisions.md, .ai/project.yaml, .ai/models.yaml, .ai/plans/*,
    # .ai/specs/*, .ai/packets/*, .ai/ledgers/events.jsonl, .claude/skills/*) via this
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
        ".git-credentials", ".npmrc", ".npmrc-backup", ".netrc",
        "auth.json", "credentials", "id_ed25519", "id_rsa", "tokens.txt",
        # settings.local.json holds local permission allow-lists, env values
        # and hook commands — at least as sensitive as settings.json, which
        # is already in _BLOCKED_PATHS. Block the basename (and any sibling
        # *.local.json) across the static handler, file_read, and files-list.
        "settings.local.json",
    })
    _BLOCKED_NAME_PREFIXES = ("id_",)
    _BLOCKED_NAME_SUFFIXES = (".pem", ".key", ".pfx", ".p12", ".token", ".kdbx", ".local.json")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def translate_path(self, path):
        real = super().translate_path(path)
        # On Windows the http path uses `/` but os.sep is `\`. Normalize
        # both sides before the prefix sweep so `.git/objects/...` does
        # not slip past a `c:\...\.git` comparison just because the
        # incoming path was forward-slash-separated.
        resolved = os.path.normcase(os.path.realpath(real)).replace("/", os.sep)
        base = os.path.basename(resolved)
        if (base in self._BLOCKED_NAMES
                or base.startswith(self._BLOCKED_NAME_PREFIXES)
                or base.endswith(self._BLOCKED_NAME_SUFFIXES)):
            return os.path.join(real, "__blocked_sensitive_path__")
        for blocked in self._BLOCKED_PATHS:
            blocked_norm = blocked.replace("/", os.sep)
            if resolved == blocked_norm or resolved.startswith(blocked_norm + os.sep):
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
        if parsed.path == "/api/analytics":
            self._handle_analytics(parsed)
            return
        if parsed.path == "/api/events":
            self._handle_events_list(urllib.parse.parse_qs(parsed.query))
            return
        if parsed.path == "/api/todos":
            self._handle_todos_list(urllib.parse.parse_qs(parsed.query))
            return
        if parsed.path == "/api/todos/config":
            self._json(200, _todos_parser.load_config(ROOT))
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
        m = _RE_TRANSCRIPT_STREAM.fullmatch(parsed.path)
        if m:
            self._handle_transcript_stream(m.group(1))
            return
        m = _RE_SESSION_STREAM.fullmatch(parsed.path)
        if m:
            self._handle_session_stream(m.group(1))
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
        if parsed.path == "/api/agent-orchestrations":
            self._handle_agent_orchestrations_list()
            return
        if parsed.path.startswith("/api/agent-orchestrations/"):
            slug = urllib.parse.unquote(parsed.path[len("/api/agent-orchestrations/"):])
            self._handle_agent_orchestration_get(slug)
            return
        if parsed.path == "/api/pipelines":
            self._handle_pipelines_list()
            return
        if parsed.path.startswith("/api/pipelines/"):
            slug = urllib.parse.unquote(parsed.path[len("/api/pipelines/"):])
            self._handle_pipeline_get(slug)
            return
        if parsed.path == "/api/agents/content":
            self._handle_agent_content(urllib.parse.parse_qs(parsed.query))
            return
        if parsed.path == "/api/agents/proposals":
            self._handle_agent_proposals_list()
            return
        m = _RE_AGENT_PROPOSAL_GET.fullmatch(parsed.path)
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
        m = _RE_SKILL_PROPOSAL_GET.fullmatch(parsed.path)
        if m:
            self._handle_proposal_get(m.group(1))
            return
        if parsed.path == "/api/files/list":
            self._handle_files_list(urllib.parse.parse_qs(parsed.query))
            return
        if parsed.path == "/api/files/read":
            self._handle_file_read(urllib.parse.parse_qs(parsed.query))
            return
        m = _RE_JOB_STREAM.fullmatch(parsed.path)
        if m:
            self._handle_job_stream(m.group(1))
            return
        m = _RE_JOB_GET.fullmatch(parsed.path)
        if m:
            self._handle_job_get(m.group(1), urllib.parse.parse_qs(parsed.query))
            return
        # PTY (real shell) sessions live alongside chat jobs; same shape.
        if parsed.path == "/api/ptys":
            self._handle_ptys_list()
            return
        m = _RE_PTY_IO.fullmatch(parsed.path)
        if m:
            self._handle_pty_ws(m.group(1), urllib.parse.parse_qs(parsed.query))
            return
        m = _RE_PTY_GET.fullmatch(parsed.path)
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
        elif parsed.path == "/api/todos":
            self._handle_todo_create(body)
        elif parsed.path == "/api/todos/scan":
            self._handle_todos_scan()
        elif parsed.path == "/api/todos/config":
            if not isinstance(body, dict):
                self._json(400, {"error": "invalid request body"})
            else:
                self._json(200, _todos_parser.save_config(ROOT, body))
        elif parsed.path.startswith("/api/todos/") and parsed.path.endswith("/status"):
            self._handle_todo_status(parsed.path, body)
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
            m = _RE_JOB_CANCEL.fullmatch(parsed.path)
            if m:
                self._handle_job_cancel(m.group(1))
                return
            m = _RE_JOB_INPUT.fullmatch(parsed.path)
            if m:
                self._handle_job_input(m.group(1), body)
                return
            m = _RE_PTY_KILL.fullmatch(parsed.path)
            if m:
                self._handle_pty_kill(m.group(1))
                return
            m = _RE_JOB_INTERRUPT.fullmatch(parsed.path)
            if m:
                self._handle_job_interrupt(m.group(1))
                return
            m = _RE_SKILL_PROPOSAL_DECIDE.fullmatch(parsed.path)
            if m:
                self._handle_proposal_decision(m.group(1), m.group(2))
                return
            m = _RE_SKILL_SUGGESTION_DRAFT.fullmatch(parsed.path)
            if m:
                self._handle_suggestion_draft(m.group(1))
                return
            # Manual "Improve now" — bypasses the per-skill throttle and
            # selects the structural-audit prompt variant. The skill name
            # is validated against the project skill index (so plugin /
            # user-scope skills can't be edited through this endpoint).
            # POST /api/skills/<name>/improve
            m = _RE_SKILL_IMPROVE_NOW.fullmatch(parsed.path)
            if m:
                self._handle_skill_improve_now(m.group(1))
                return
            if parsed.path == "/api/agents/suggest":
                self._handle_agent_suggest()
                return
            m = _RE_AGENT_PROPOSAL_DECIDE.fullmatch(parsed.path)
            if m:
                self._handle_agent_proposal_decision(m.group(1), m.group(2))
                return
            m = _RE_SESSION_INPUT.fullmatch(parsed.path)
            if m:
                self._handle_session_input(m.group(1), body)
                return
            m = _RE_SESSION_RELEASE.fullmatch(parsed.path)
            if m:
                self._handle_session_release(m.group(1))
                return
            self._json(404, {"error": "unknown endpoint", "path": parsed.path})

    def do_PUT(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        # CSRF guard is invoked inside each handler so the handler can also
        # apply its own size/body policy before reading the request stream.
        if parsed.path.startswith("/api/pipelines/"):
            slug = urllib.parse.unquote(parsed.path[len("/api/pipelines/"):])
            self._handle_pipeline_put(slug)
            return
        self._json(404, {"error": "unknown endpoint", "path": parsed.path})

    def do_DELETE(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path.startswith("/api/pipelines/"):
            slug = urllib.parse.unquote(parsed.path[len("/api/pipelines/"):])
            self._handle_pipeline_delete(slug)
            return
        self._json(404, {"error": "unknown endpoint", "path": parsed.path})

    # ----- helpers -----
    def end_headers(self) -> None:  # noqa: N802 (stdlib signature)
        # Prevent stale HTML/CSS/JS after dashboard upgrades. The dashboard is
        # served on localhost so cache invalidation cost is negligible, and
        # otherwise a Ctrl+F5 is required after every change.
        self.send_header("Cache-Control", "no-store, must-revalidate")
        try:
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            # CSP: 'unsafe-inline' kept for script/style-src because SPA uses
            # inline event handlers; TODO: tighten by extracting inline JS/CSS
            # or using hashes.
            csp = (
                "default-src 'self'; "
                "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
                "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdn.jsdelivr.net; "
                "font-src 'self' https://fonts.gstatic.com https://cdn.jsdelivr.net; "
                "img-src 'self' data:; "
                "connect-src 'self'; "
                "frame-ancestors 'none'"
            )
            self.send_header("Content-Security-Policy", csp)
        except Exception:
            pass  # never fail a response over headers
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
        raw_len = self.headers.get("Content-Length") or "0"
        # Reject obviously-malformed headers (negatives, "+0", whitespace
        # padding, plus-prefixed positives) up front — int() would happily
        # parse "+ 5\n" or "-100" otherwise. Length must be a bare
        # non-negative integer per RFC 9110.
        if not raw_len.lstrip("0").isdigit() and raw_len.strip() != "0":
            self._json(411, {"error": "missing or invalid Content-Length"})
            return None
        try:
            length = int(raw_len)
        except (TypeError, ValueError):
            self._json(411, {"error": "missing or invalid Content-Length"})
            return None
        if length < 0:
            self._json(411, {"error": "missing or invalid Content-Length"})
            return None
        if length == 0:
            return {}
        if length > MAX_JSON_BODY:
            self._json(413, {"detail": "request body too large", "error": "payload too large"})
            return None
        try:
            raw = self.rfile.read(length).decode("utf-8")
            parsed = json.loads(raw) if raw else {}
            # Enforce object-ness once here so every POST handler can call
            # body.get(...) safely. A non-dict body ([], "x", 5) would
            # otherwise raise AttributeError → unhandled 500 in the handlers
            # that don't carry their own isinstance guard.
            if not isinstance(parsed, dict):
                self._json(400, {"error": "request body must be a JSON object",
                                 "detail": "request body must be a JSON object"})
                return None
            return parsed
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
    def _todos_latest(self) -> dict:
        rows = _todos_parser._load_jsonl(_todos_parser._todos_path(ROOT))
        return _todos_parser._fold_latest(rows)

    def _todos_banner(self) -> str | None:
        path = ROOT / ".ai" / "todos-banner.txt"
        try:
            if path.is_file():
                return path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return "TODO banner unavailable"
        return None

    def _clean_todo_tags(self, raw) -> list[str]:
        if not isinstance(raw, list):
            return []
        out: list[str] = []
        seen: set[str] = set()
        for value in raw:
            tag = re.sub(r"[^a-z0-9_-]+", "", str(value).strip().lower())[:30]
            if tag and tag not in seen:
                seen.add(tag)
                out.append(tag)
        return out

    def _handle_todos_list(self, qs: dict[str, list[str]]) -> None:
        latest = list(self._todos_latest().values())
        counts = {"open": 0, "resolved-suggested": 0, "resolved": 0, "archived": 0}
        for todo in latest:
            status = todo.get("status")
            if status in counts:
                counts[status] += 1

        status_filter = (qs.get("status") or [""])[0]
        tag_filter = (qs.get("tag") or [""])[0]

        def matches(todo: dict) -> bool:
            if status_filter and todo.get("status") != status_filter:
                return False
            if tag_filter:
                tags = todo.get("tags") or []
                if not isinstance(tags, list) or tag_filter not in {str(t) for t in tags}:
                    return False
            return True

        todos = sorted((dict(todo) for todo in latest if matches(todo)), key=lambda row: row.get("id", ""))
        self._json(200, {"todos": todos, "counts": counts, "banner": self._todos_banner()})

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
    def _handle_todo_create(self, body: dict) -> None:
        if not isinstance(body, dict):
            self._json(400, {"error": "invalid request body"})
            return
        title = " ".join(str(body.get("title") or "").split())
        if not title or len(title) > 280:
            self._json(400, {"error": "title must be 1-280 characters"})
            return

        # Optional free-form detail. Collapse trailing whitespace but preserve
        # internal newlines so multi-line notes survive the round trip; the
        # frontend renders it as plain text (textContent), so no markup escaping
        # is needed here.
        description = str(body.get("description") or "").strip()
        if len(description) > 2000:
            self._json(400, {"error": "description must be 2000 characters or fewer"})
            return

        now = _todos_parser._utc_now()
        latest = self._todos_latest()
        source_ref = " ".join(str(body.get("source_ref") or "manual").split()) or "manual"
        todo = {
            "id": _todos_parser._allocate_id(latest, now),
            "title": title,
            "description": description,
            "tags": self._clean_todo_tags(body.get("tags") or []),
            "source": source_ref,
            "source_ref": source_ref,
            "status": "open",
            "created_at": now,
            "updated_at": now,
            "captured_by": "manual",
            "dedup_hash": _todos_parser._dedup_hash(source_ref, title),
            "resolution": None,
            "rejected_hashes": [],
        }
        _todos_parser._append_jsonl(_todos_parser._todos_path(ROOT), todo)
        regen = _todos_parser.regen_markdown(ROOT)
        payload = {"id": todo["id"], "todo": todo}
        if not regen.get("ok", False):
            payload["banner"] = regen.get("banner", "TODO.md export stale")
        self._json(201, payload)

    def _handle_todo_status(self, path: str, body: dict) -> None:
        if not isinstance(body, dict):
            self._json(400, {"error": "invalid request body"})
            return
        todo_id = path[len("/api/todos/"):-len("/status")].strip("/")
        if not re.fullmatch(r"td_\d{4}-\d{2}-\d{2}_\d{3}", todo_id):
            self._json(400, {"error": "invalid todo id"})
            return
        action = body.get("action")
        if action not in {"done", "archive", "reopen", "accept-suggest", "reject-suggest"}:
            self._json(400, {"error": "invalid action"})
            return

        current = self._todos_latest().get(todo_id)
        if current is None:
            self._json(404, {"error": "todo not found"})
            return

        now = _todos_parser._utc_now()
        todo = dict(current)
        todo["updated_at"] = now
        if action == "done":
            todo["status"] = "resolved"
            todo["resolution"] = {"by": "manual", "at": now}
        elif action == "archive":
            todo["status"] = "archived"
        elif action == "reopen":
            todo["status"] = "open"
            todo["resolution"] = None
        elif action == "accept-suggest":
            evidence = (current.get("resolution") or {}).get("evidence")
            todo["status"] = "resolved"
            todo["resolution"] = {"by": "manual-accept", "at": now}
            if evidence:
                todo["resolution"]["evidence"] = evidence
        elif action == "reject-suggest":
            evidence = (current.get("resolution") or {}).get("evidence")
            rejected = list(current.get("rejected_hashes") or [])
            if evidence and evidence not in rejected:
                rejected.append(evidence)
            todo["status"] = "open"
            todo["resolution"] = None
            todo["rejected_hashes"] = rejected

        _todos_parser._append_jsonl(_todos_parser._todos_path(ROOT), todo)
        regen = _todos_parser.regen_markdown(ROOT)
        payload = {"todo": todo}
        if not regen.get("ok", False):
            payload["banner"] = regen.get("banner", "TODO.md export stale")
        self._json(200, payload)

    def _handle_todos_scan(self) -> None:
        scan = _todos_parser.scan_and_append(ROOT, captured_by="scan-now")
        resolved = _todos_parser.auto_resolve(ROOT)
        self._json(200, {
            "added": int(scan.get("added", 0)),
            "suggested": int(resolved.get("suggested", 0)),
        })

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

    def _handle_events_list(self, qs: dict[str, list[str]]) -> None:
        """GET /api/events?tail=N — parsed events.jsonl with optional tail.

        Replaces the previous client-side approach of fetching the raw
        .ai/ledgers/events.jsonl static file and re-parsing every line each poll.
        With ``tail=N`` (default 2000, max 5000) only the most recent N
        rows are returned, so a 100k-event ledger no longer triggers a
        multi-second freeze on every 5s refresh.
        """
        try:
            tail = int((qs.get("tail") or ["2000"])[0])
        except (TypeError, ValueError):
            tail = 2000
        tail = max(1, min(5000, tail))
        rows = _load_jsonl_cached(EVENTS_FILE)
        total = len(rows)
        truncated = total > tail
        if truncated:
            rows = rows[-tail:]
        self._json(200, {
            "events": rows,
            "total": total,
            "returned": len(rows),
            "truncated": truncated,
        })

    def _handle_events_clear(self) -> None:
        path = EVENTS_FILE
        # Audit-log the truncation BEFORE doing it. /api/events/clear is a
        # CSRF-gated POST but it's still an audit-erasing primitive — record
        # who/when so a future investigator can see when the ledger was wiped.
        try:
            size = path.stat().st_size if path.exists() else 0
        except OSError:
            size = -1
        print(
            f"[serve] AUDIT: events.jsonl cleared "
            f"(prior_size={size} bytes, client={self.client_address[0]})",
            flush=True,
        )
        try:
            if path.exists():
                path.unlink()
        except OSError as e:
            print(f"[serve] events.jsonl clear failed: {e}", flush=True)
            self._json(500, {"error": "could not clear events"})
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

    def _handle_usage_total(self) -> None:
        """Aggregate token usage across every Claude transcript for this
        repo. Powers the overview's "Tokens used" card."""
        self._json(200, _aggregate_project_token_usage())

    def _handle_timeline(self) -> None:
        """Pipeline Gantt data — phase_dispatch events from .ai/ledgers/events.jsonl
        grouped per session_id. Powers the Timeline view."""
        self._json(200, {"runs": _load_timeline_runs()})

    def _handle_analytics(self, parsed) -> None:
        """Chart-ready aggregation of the six ledgers for the Analytics tab.
        Query param ``range`` is one of 7d/30d/90d/all (defaults to 30d)."""
        qs = urllib.parse.parse_qs(parsed.query)
        range_key = (qs.get("range", ["30d"])[0] or "30d")
        now = _dt.datetime.now(_dt.timezone.utc)
        try:
            payload = _aggregate_analytics(now, range_key)
        except Exception as exc:  # never 500 the whole dashboard
            # Log server-side; return a generic message so an unexpected error
            # (e.g. an OSError carrying a filesystem path) can't leak internals
            # to the client — matching _read_json_body's convention.
            print(f"[serve] analytics aggregation failed: {exc}", flush=True)
            self._json(500, {"error": "analytics aggregation failed"})
            return
        self._json(200, payload)

    def _agent_orchestrations_origin_guard(self) -> bool:
        if _browser_cross_origin_blocked(self.headers):
            self._json(403, {"error": "origin not allowed"})
            return False
        return True

    def _pipelines_origin_guard(self) -> bool:
        if _browser_cross_origin_blocked(self.headers):
            self._json(403, {"error": "origin not allowed"})
            return False
        return True

    def _handle_pipelines_list(self) -> None:
        if not self._pipelines_origin_guard():
            return
        self._json(200, {"pipelines": _list_pipelines()})

    def _handle_pipeline_get(self, slug: str) -> None:
        if not self._pipelines_origin_guard():
            return
        if not re.fullmatch(r"[a-z0-9-]+", slug or ""):
            self._json(400, {"error": "invalid slug"})
            return
        candidate = PIPELINES_DIR / f"{slug}.yaml"
        if not candidate.is_file():
            self._json(404, {"error": "pipeline not found", "slug": slug})
            return
        try:
            resolved_realpath = os.path.realpath(str(candidate.resolve(strict=True)))
        except OSError:
            self._json(404, {"error": "pipeline not found", "slug": slug})
            return
        dir_realpath = os.path.realpath(str(PIPELINES_DIR))
        if not _is_under_trusted_dir(resolved_realpath, dir_realpath):
            self._json(400, {"error": "path outside trusted dir"})
            return
        try:
            import yaml as _yaml_mod  # local import — keeps top-level free of PyYAML
            parsed = _yaml_mod.safe_load(candidate.read_text(encoding="utf-8")) or {}
        except Exception as e:
            self._json(400, {"error": f"yaml parse error: {e}"})
            return
        # A truthy non-mapping root (list/scalar) would make {**parsed} raise
        # TypeError outside the try → unhandled 500. Mirror the PUT handler's
        # guard. (safe_load(...) or {} only coerces falsy/None.)
        if not isinstance(parsed, dict):
            self._json(400, {"error": "pipeline root must be a mapping", "slug": slug})
            return
        payload = {"slug": slug, **parsed}
        self._json(200, payload)

    def _handle_pipeline_put(self, slug: str) -> None:
        # PUT is state-changing: CSRF-guarded (which also enforces origin).
        if not self._csrf_guard():
            return
        if not re.fullmatch(r"[a-z0-9-]+", slug or ""):
            self._json(400, {"error": "invalid slug"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_PIPELINE_PUT_BYTES:
            # DoS guard: declared Content-Length already disqualifies this
            # request, so we never decode or validate the payload. Drain the
            # inbound bytes (bounded by the cap + a small margin) in 8 KiB
            # chunks before responding so Windows doesn't reset the TCP
            # connection mid-receive (visible to the client as a
            # ConnectionAbortedError instead of the expected 400). Close the
            # connection after the response so we don't keep state for what
            # is — by declaration — an abusive request.
            if length > 0:
                drain_cap = MAX_PIPELINE_PUT_BYTES + 8 * 1024
                remaining = min(length, drain_cap)
                try:
                    while remaining > 0:
                        chunk = self.rfile.read(min(8192, remaining))
                        if not chunk:
                            break
                        remaining -= len(chunk)
                except OSError:
                    pass
            self.close_connection = True
            self._json(400, {"error": "missing or oversized body"})
            return
        raw = self.rfile.read(length)
        try:
            request = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._json(400, {"error": "invalid json body"})
            return
        yaml_text = request.get("yaml") if isinstance(request, dict) else None
        if not isinstance(yaml_text, str):
            self._json(400, {"error": "missing 'yaml' field"})
            return
        import yaml as _yaml_mod
        try:
            parsed = _yaml_mod.safe_load(yaml_text)
        except _yaml_mod.YAMLError as e:
            self._json(400, {"error": f"yaml parse error: {e}"})
            return
        if not isinstance(parsed, dict):
            self._json(400, {"error": "yaml root must be a mapping"})
            return
        # Validator lives next to serve.py in .ai/dashboard/. Local import so
        # serve.py doesn't pay the import cost on every other handler.
        from pipeline_schema import validate as _validate_pipeline_yaml
        ok, errors = _validate_pipeline_yaml(parsed)
        if not ok:
            self._json(400, {"errors": [{"message": e} for e in errors]})
            return
        target = PIPELINES_DIR / f"{slug}.yaml"
        target_realpath = os.path.realpath(str(target))
        dir_realpath = os.path.realpath(str(PIPELINES_DIR))
        if not _is_under_trusted_dir(target_realpath, dir_realpath):
            self._json(400, {"error": "path outside trusted dir"})
            return
        PIPELINES_DIR.mkdir(parents=True, exist_ok=True)
        canonical = _yaml_mod.safe_dump(parsed, sort_keys=False, default_flow_style=False)
        try:
            target.write_text(canonical, encoding="utf-8")
        except OSError as e:
            print(f"[serve] failed to write pipeline {slug}: {e}", flush=True)
            self._json(500, {"error": f"could not write pipeline: {e}"})
            return
        # Best-effort repo-relative path for client display. Falls back to
        # the absolute path if PIPELINES_DIR has been monkey-patched outside
        # ROOT (the unit tests do this with tmp_path).
        try:
            rel = str(target.relative_to(ROOT)).replace("\\", "/")
        except ValueError:
            rel = str(target).replace("\\", "/")
        self._json(200, {"slug": slug, "path": rel})

    def _handle_pipeline_delete(self, slug: str) -> None:
        if not self._csrf_guard():
            return
        if not re.fullmatch(r"[a-z0-9-]+", slug or ""):
            self._json(400, {"error": "invalid slug"})
            return
        target = PIPELINES_DIR / f"{slug}.yaml"
        if not target.is_file():
            self._json(404, {"error": "pipeline not found", "slug": slug})
            return
        try:
            target_realpath = os.path.realpath(str(target.resolve(strict=True)))
        except OSError:
            self._json(404, {"error": "pipeline not found", "slug": slug})
            return
        dir_realpath = os.path.realpath(str(PIPELINES_DIR))
        if not _is_under_trusted_dir(target_realpath, dir_realpath):
            self._json(400, {"error": "path outside trusted dir"})
            return
        target.unlink()
        self._json(200, {"slug": slug, "deleted": True})

    def _handle_agent_orchestrations_list(self) -> None:
        if not self._agent_orchestrations_origin_guard():
            return
        self._json(200, {"runs": _list_agent_runs()})

    def _handle_agent_orchestration_get(self, slug: str) -> None:
        if not self._agent_orchestrations_origin_guard():
            return
        if not re.fullmatch(r"[A-Za-z0-9._-]+", slug or ""):
            self._json(400, {"error": "invalid task slug"})
            return
        match = None
        for run in _list_agent_runs():
            if run.get("task_slug") == slug:
                match = run
                break
        if match is None:
            self._json(404, {"error": "agent orchestration not found", "task_slug": slug})
            return
        try:
            candidate = ROOT / str(match.get("path") or "")
            resolved = candidate.resolve(strict=True)
        except OSError:
            self._json(404, {"error": "agent orchestration file not found", "task_slug": slug})
            return
        resolved_realpath = os.path.realpath(str(resolved))
        runs_realpath = os.path.realpath(str(AGENT_RUNS_DIR))
        if not _is_under_trusted_dir(resolved_realpath, runs_realpath):
            self._json(400, {"error": "agent orchestration path is outside trusted dir"})
            return
        parsed = _parse_agent_run(candidate)
        self._json(200, {
            "task_slug": parsed.get("task_slug"),
            "date": parsed.get("date"),
            "objective": parsed.get("objective"),
            "output_hint": parsed.get("output_hint"),
            "dag": parsed.get("dag") or [],
            "handoff": parsed.get("handoff") or "",
            "metrics": match.get("metrics") or _agent_run_metrics_by_slug().get(slug, []),
        })

    def _handle_auto_select(self, parsed) -> None:
        """Auto-select scorer ranking — aggregated from .ai/ledgers/metrics.jsonl.
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
            candidate = _safe_which("bash")
            if candidate and "system32" not in candidate.lower():
                return candidate
            return None
        return _safe_which("bash")

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
            print(f"[serve] auto_select yaml update failed: {e}", flush=True)
            self._json(500, {"error": "failed to update auto_select config"})
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
        after jobs.

        Cached for ``_CATALOG_TTL_SECONDS`` because dashboard boot fires
        this endpoint in parallel with /api/agents/all and /api/usage/total
        — without the cache the FS walks across 3 dirs + the metrics
        aggregator add ~300-500 ms to first paint."""
        now_mono = time.monotonic()
        if _SKILLS_ALL_CACHE["data"] is not None and (now_mono - _SKILLS_ALL_CACHE["at"]) < _CATALOG_TTL_SECONDS:
            self._json(200, _SKILLS_ALL_CACHE["data"])
            return
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
        payload = {"skills": all_skills, "sources": source_meta}
        _SKILLS_ALL_CACHE["data"] = payload
        _SKILLS_ALL_CACHE["at"] = now_mono
        self._json(200, payload)

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
            print(f"[serve] agent read failed for {resolved}: {e}", flush=True)
            self._json(500, {"error": "read failed"})
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
        their own agents but are never editable from the dashboard.

        Cached for ``_CATALOG_TTL_SECONDS`` — the plugin_market and
        plugin_cache scans use recursive ``glob("**/agents/*.md")`` which
        can walk thousands of plugin files; without the cache, every
        dashboard tab switch back to Agents would re-walk them."""
        now_mono = time.monotonic()
        if _AGENTS_ALL_CACHE["data"] is not None and (now_mono - _AGENTS_ALL_CACHE["at"]) < _CATALOG_TTL_SECONDS:
            self._json(200, _AGENTS_ALL_CACHE["data"])
            return
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
        payload = {"agents": all_agents, "sources": source_meta}
        _AGENTS_ALL_CACHE["data"] = payload
        _AGENTS_ALL_CACHE["at"] = now_mono
        self._json(200, payload)

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
        """List every proposal under ``SKILL_PROPOSALS_DIR`` with status.

        Defensive merge pass: legacy duplicates (multiple pending proposals
        for the same skill+kind) are collapsed here too — the newest wins,
        the rest are marked ``superseded`` on disk so the next call sees a
        clean state. New writes already supersede prior pending via
        ``_supersede_prior_pending`` at creation time."""
        items: list[dict] = []
        if SKILL_PROPOSALS_DIR.is_dir():
            loaded: list[tuple[Path, dict]] = []
            for p in sorted(SKILL_PROPOSALS_DIR.glob("*.json"),
                            key=lambda x: x.stat().st_mtime, reverse=True):
                try:
                    obj = json.loads(p.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                loaded.append((p, obj))
            # First pass: collapse legacy same-skill+kind pending duplicates.
            # `loaded` is mtime-desc, so the FIRST occurrence per (kind, skill)
            # is the newest and survives; the rest get superseded.
            seen_pending: dict[tuple[str, str], dict] = {}
            now_iso = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
            for p, obj in loaded:
                status = obj.get("status") or "pending"
                if status != "pending":
                    continue
                skill = obj.get("skill") or ""
                kind = obj.get("kind") or "improve"
                key = (kind, skill)
                if not skill:
                    continue
                winner = seen_pending.get(key)
                if winner is None:
                    seen_pending[key] = obj
                    continue
                # `obj` is older than `winner` — mark it superseded.
                obj["status"] = "superseded"
                obj["applied_at"] = now_iso
                obj["applied_via"] = "merged-into-newer"
                obj["superseded_by"] = winner.get("id")
                try:
                    p.write_text(json.dumps(obj, indent=2), encoding="utf-8")
                except OSError as e:
                    print(f"[serve] list-time supersede {p.name} failed: {e}",
                          flush=True)
                # Bump the winner's merged_from list so the UI can show
                # how many proposals collapsed into this one.
                merged_from = list(winner.get("merged_from") or [])
                older_id = obj.get("id")
                if older_id and older_id not in merged_from:
                    merged_from.append(older_id)
                    winner["merged_from"] = merged_from
                    # Find the winner's path to persist the bump.
                    for wp, wobj in loaded:
                        if wobj is winner:
                            try:
                                wp.write_text(json.dumps(winner, indent=2),
                                              encoding="utf-8")
                            except OSError as e:
                                print(f"[serve] list-time merged_from update "
                                      f"{wp.name} failed: {e}", flush=True)
                            break
            # Second pass: build the response summary.
            for _, obj in loaded:
                merged_from = obj.get("merged_from") or []
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
                    "merged_count": len(merged_from) + 1 if merged_from else 1,
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
                print(f"[serve] could not read draft body {new_md}: {e}", flush=True)
                self._json(500, {"error": "could not read draft body"})
                return
            try:
                install_info = _create_skill_in_both_trees(slug, new_content)
            except OSError as e:
                print(f"[serve] draft install write failed for {target_md}: {e}", flush=True)
                self._json(500, {"error": "write failed"})
                return
            target_rel = install_info["claude_path"]
            obj["status"] = "installed"
            obj["applied_at"] = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
            obj["applied_via"] = "manual"
            obj["target_path"] = target_rel
            obj["installed_path"] = target_rel
            if install_info["agents_path"]:
                obj["agents_installed_path"] = install_info["agents_path"]
            try:
                pj.write_text(json.dumps(obj, indent=2), encoding="utf-8")
            except OSError as e:
                # SKILL.md already on disk; status stays "pending" in the
                # proposal file. Audit still runs so the ledger reflects truth.
                print(f"[serve] failed to write proposal {pj} (installed draft): {e}", flush=True)
            audit_reason = f"draft installed -> {target_rel}"
            if install_info["agents_path"]:
                audit_reason += f" (+ {install_info['agents_path']})"
            _audit_improvement(slug, "installed",
                               audit_reason,
                               proposal_id, None,
                               int(obj.get("diff_lines") or 0),
                               source="manual")
            note = f"Skill created at {target_rel}."
            if install_info["agents_path"]:
                note += f" Also mirrored to {install_info['agents_path']}."
            elif install_info["agents_skipped_reason"]:
                note += f" (.agents skipped: {install_info['agents_skipped_reason']})"
            self._json(200, {
                "ok": True, "id": proposal_id, "status": "installed",
                "installed_path": target_rel,
                "agents_installed_path": install_info["agents_path"],
                "note": note,
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
            self._json(500, {"error": "apply failed (see .ai/ledgers/improvements.jsonl)"})
            return
        self._json(200, {"ok": True, "id": proposal_id, "status": "applied"})

    def _handle_skill_improve_now(self, skill_name: str) -> None:
        """Manual structural-audit trigger for one project skill. Bypasses
        the per-skill throttle (the operator is asking explicitly) and
        selects the ``manual=True`` prompt variant so the model audits
        the skill structurally rather than gating on a job log.

        Shares ``_SUGGESTION_SEMAPHORE`` with /draft and /agents/suggest:
        all three spawn one ``claude -p`` / ``codex`` subprocess on the
        request thread; without the cap a handful of concurrent clients
        can exhaust the thread pool. Returns the audit outcome inline so
        the UI can show "applied / pending / no_change" without a second
        round-trip to /api/skills/proposals."""
        cfg = _load_improver_config()
        if not cfg.get("enabled"):
            self._json(409, {"error": "improver disabled",
                             "hint": "Set improver.enabled=true in .ai/models.yaml"})
            return
        if not _safe_which(cfg["tool"]):
            self._json(503, {"error": "improver CLI not on PATH",
                             "tool": cfg.get("tool")})
            return
        proj = _project_skill_index()
        canonical = _skill_name_canonical(skill_name)
        path = proj.get(canonical) or proj.get(skill_name)
        if not path:
            self._json(404, {"error": "skill not found in project scope",
                             "skill": skill_name,
                             "hint": "Manual improve only edits .claude/skills/"
                                     " — plugin and user-scope skills are read-only."})
            return
        if not _SUGGESTION_SEMAPHORE.acquire(blocking=False):
            self.send_response(429)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Retry-After", "30")
            body = json.dumps({"error": "too many concurrent improver requests; try again later"}).encode("utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        # Cap the subprocess timeout the same way /draft does so a long
        # cfg.timeout_seconds can't pin a request thread.
        cfg_capped = dict(cfg)
        cfg_capped["timeout_seconds"] = min(
            int(cfg.get("timeout_seconds", 120)),
            _SUGGESTION_HTTP_TIMEOUT_MAX,
        )
        try:
            result = _run_improver_for_skill(
                canonical, path, job_id=None, log_path=None,
                cfg=cfg_capped, manual=True, force=True,
            )
        except Exception as e:  # noqa: BLE001 — never 500 silently
            print(f"[serve] manual improve crashed for {canonical}: {e}", flush=True)
            self._json(500, {"error": "improver crashed", "detail": str(e)})
            return
        finally:
            _SUGGESTION_SEMAPHORE.release()
        self._json(200, {
            "ok": True,
            "skill": canonical,
            "status": result.get("status"),
            "proposal_id": result.get("proposal_id"),
            "diff_lines": result.get("diff_lines"),
            "change_summary": result.get("change_summary") or "",
            "reason": result.get("reason") or "",
        })

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
            if not _safe_which(cfg["tool"]):
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
            tool_bin = _safe_which(cfg["tool"]) or cfg["tool"]
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
                print(f"[serve] improver subprocess error: {e}", flush=True)
                self._json(500, {"error": "subprocess error"})
                return
            if proc.returncode != 0:
                print(f"[serve] improver exit {proc.returncode}: {(proc.stderr or '')[:300]}", flush=True)
                self._json(500, {"error": f"exit {proc.returncode}"})
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
            merged_in = _supersede_prior_pending(slug, pid, "draft")
            if merged_in:
                payload["merged_from"] = merged_in
                try:
                    (SKILL_PROPOSALS_DIR / f"{pid}.json").write_text(
                        json.dumps(payload, indent=2), encoding="utf-8")
                except OSError as e:
                    # Same best-effort policy as _write_proposal: the new
                    # draft is already on disk; merged_from is metadata.
                    print(f"[serve] draft {pid} merged_from update failed: {e}", flush=True)
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
            tool_bin = _safe_which(cfg["tool"])
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
            print(f"[serve] agent install write failed for {target}: {e}", flush=True)
            self._json(500, {"error": "write failed"})
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
        # Reject `..` and `.` in any segment: the regex below already
        # forbids `/` and `\`, but `..` would otherwise resolve outside the
        # skills root (e.g. `?source=codex_global&name=..` -> ~/.codex/SKILL.md).
        if not re.fullmatch(r"[a-zA-Z0-9_:-][a-zA-Z0-9_:\-.]*", name) or ".." in name.split("."):
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
            # Containment check: even with the regex above, resolve() +
            # relative_to() is the canonical defense against symlink/junction
            # escapes inside the skills tree.
            skill_md.resolve(strict=False).relative_to(root.resolve())
        except ValueError:
            self._json(403, {"error": "path is outside the skills root"})
            return
        except OSError as e:
            print(f"[serve] skill content resolve failed for {skill_md}: {e}", flush=True)
            self._json(500, {"error": "resolve failed"})
            return
        try:
            if not skill_md.is_file():
                self._json(404, {"error": "skill not found",
                                 "source": source, "name": name})
                return
            content = skill_md.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            print(f"[serve] skill content read failed for {skill_md}: {e}", flush=True)
            self._json(500, {"error": "read failed"})
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
        # Track whether the git path actually produced a file list. The rglob
        # fallback must fire only when git is unavailable/failed — NOT merely
        # when git succeeded with zero matches for this prefix (the normal
        # autocomplete case), which would otherwise trigger a full repo walk
        # per keystroke and surface untracked files git never lists.
        git_ok = False
        git = _safe_which("git")
        if git:
            # Cache hits are invalidated by .git/index mtime so autocomplete
            # doesn't spawn ``git ls-files`` on every keystroke.
            lines = _git_lsfiles_cached(ROOT)
            if lines is None:
                try:
                    out = subprocess.run(
                        [git, "ls-files"], cwd=str(ROOT), capture_output=True,
                        text=True, timeout=5,
                    )
                    if out.returncode == 0:
                        lines = out.stdout.splitlines()
                        _git_lsfiles_put(ROOT, lines)
                except (subprocess.TimeoutExpired, OSError):
                    lines = None
            if lines is not None:
                git_ok = True
                for line in lines:
                    if not line:
                        continue
                    # Apply SKIP_DIRS to the git-fast path too — tracked
                    # secrets under .venv/ / node_modules/ / vendor/ used
                    # to be enumerable via ?prefix= because the filter
                    # only protected the slow rglob fallback.
                    if any(part in SKIP_DIRS for part in line.split("/")):
                        continue
                    # Don't reveal secret-named files in the autocomplete
                    # suggestion list either — _handle_file_read blocks
                    # reading them but mere discovery is also a leak.
                    base = line.rsplit("/", 1)[-1].lower()
                    if (base in self._BLOCKED_NAMES
                            or base.startswith(self._BLOCKED_NAME_PREFIXES)
                            or base.endswith(self._BLOCKED_NAME_SUFFIXES)):
                        continue
                    if prefix and prefix not in line.lower():
                        continue
                    files.append(line)
                    if len(files) >= limit:
                        break
        # Fallback: walk the repo when ``git ls-files`` isn't available
        # (no-git checkouts, broken HEAD, etc.). ``SKIP_DIRS`` keeps the
        # walk off the obvious hot paths (``.git/objects`` alone can be
        # hundreds of thousands of entries) and stops the autocomplete
        # endpoint leaking ``.venv`` / ``node_modules`` paths into the
        # suggestion list. Gate on ``git_ok`` (git unavailable/failed), not
        # ``not files`` (which also fires on a normal zero-match prefix).
        if not git_ok:
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
                    base = parts[-1].lower()
                    if (base in self._BLOCKED_NAMES
                            or base.startswith(self._BLOCKED_NAME_PREFIXES)
                            or base.endswith(self._BLOCKED_NAME_SUFFIXES)):
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

    def _is_blocked_path(self, resolved) -> bool:
        """Return True when the already-resolved path matches the secrets
        blocklist (basename in _BLOCKED_NAMES / prefix / suffix, or path
        under any _BLOCKED_PATHS prefix). Caller is responsible for the
        repo-root containment check; this helper only enforces the
        secrets-name/path policy used by ``_handle_file_read`` and the
        multimodal composer."""
        resolved_norm = os.path.normcase(str(resolved)).replace("/", os.sep)
        base = os.path.basename(resolved_norm)
        if (base in self._BLOCKED_NAMES
                or base.startswith(self._BLOCKED_NAME_PREFIXES)
                or base.endswith(self._BLOCKED_NAME_SUFFIXES)):
            return True
        for blocked in self._BLOCKED_PATHS:
            blocked_norm = blocked.replace("/", os.sep)
            if resolved_norm == blocked_norm or resolved_norm.startswith(blocked_norm + os.sep):
                return True
        return False

    def _handle_file_read(self, qs: dict[str, list[str]]) -> None:
        """Read a repo-relative file's content. Refuses paths that escape
        the repo root, and routes the same ``_BLOCKED_PATHS`` /
        ``_BLOCKED_NAMES`` blocklist the static handler uses so secrets
        (``.ssh``, ``.aws``, ``.env``, ``id_rsa``, ``*.pem`` ...) can't
        leak through this API endpoint either."""
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
        if self._is_blocked_path(resolved):
            self._json(403, {"error": "path is blocked"})
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

    def _handle_session_stream(self, session_id: str) -> None:
        """SSE: unified SessionEvent stream for a session.

        Emits a leading state_change frame with the session's current registry
        state, then tails the session's .jsonl normalizing each line via
        _jsonl_line_to_session_event.

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
        state_event = json.dumps({
            "seq": 0,
            "kind": "state_change",
            "role": None,
            "text": None,
            "partial": False,
            "state": state_label,
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
                evt = _jsonl_line_to_session_event(raw_line, seq)
                if evt is None:
                    continue
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
                        evt = _jsonl_line_to_session_event(raw_line, seq)
                        if evt is None:
                            continue
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
                else:
                    # New IDE-only entry.
                    by_sid[session_id] = {
                        # Back-compat keys.
                        "session_id": session_id,
                        "kind": "ide",
                        "task": (task or "")[:120],
                        "model": None,
                        "started_at": None,
                        "ended_at": None,
                        "status": None,
                        "last_job_id": None,
                        # New unified keys.
                        "sid": session_id,
                        "title": title,
                        "modified": modified,
                        "size": st.st_size,
                        "source": "ide",
                    }

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

        sessions = list(by_sid.values())
        # Sort: prefer modified timestamp (IDE), fall back to started_at (dashboard).
        sessions.sort(
            key=lambda s: s.get("modified") or s.get("started_at") or "",
            reverse=True,
        )
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
        # Constrain the INITIAL ``cwd`` to inside the repo. Note: this
        # only pins where the shell *starts*; once running, the shell
        # can `cd ..` freely — we don't chroot or pivot_root. Treat this
        # as a UX guardrail against accidents (paste a wrong path),
        # not as a sandbox boundary. Empty/None falls back to the repo
        # root.
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
            print(f"[serve] pty spawn missing binary: {e}", flush=True)
            self._json(503, {"error": "shell not found"})
            return
        except Exception as e:
            print(f"[serve] pty spawn failed: {e}", flush=True)
            self._json(500, {"error": "failed to spawn PTY"})
            return
        self._json(201, _pty_summary(entry, include_token=True))

    def _handle_pty_kill(self, pty_id: str) -> None:
        if _pty_kill(pty_id):
            self._json(200, {"ok": True})
        else:
            self._json(404, {"error": "pty not found"})

    def _handle_pty_ws(self, pty_id: str, qs: dict[str, list[str]] | None = None) -> None:
        """WebSocket endpoint: bidirectional byte stream + JSON control
        messages. Frames:
          * binary  -> bytes written to the PTY master (keystrokes)
          * text    -> JSON control: {"type":"resize","cols":N,"rows":M}
        Server -> client:
          * binary  -> bytes read off the PTY master
          * text    -> JSON: {"type":"exit"} on EOF

        Requires the per-PTY token issued by /api/ptys (POST). The token
        is passed either via the ``token`` query string parameter or the
        ``Sec-WebSocket-Protocol`` header value. Without a valid token
        the upgrade is refused with 403 so a malicious script on the
        dashboard origin can't enumerate PTYs and slurp their scrollback.
        """
        with PTYS_LOCK:
            entry = PTYS.get(pty_id)
        if not entry:
            self.send_error(404, "pty not found")
            return
        expected = entry.get("_token")
        provided = ""
        if qs:
            provided = (qs.get("token") or [""])[0] or ""
        if not provided:
            # Allow token via subprotocol header too — useful when the
            # JS client wants to keep the URL clean of secrets.
            provided = (self.headers.get("Sec-WebSocket-Protocol") or "").strip()
        if not expected or not provided or not secrets.compare_digest(str(expected), str(provided)):
            self.send_error(403, "pty token required")
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

    # UUID pattern used to validate session ids on the /api/sessions/* endpoints.
    _UUID_RE = re.compile(
        r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
    )

    def _handle_session_input(self, sid: str, body: dict) -> None:
        """POST /api/sessions/<sid>/input {text, model?}

        Validates that ``sid`` is a UUID and ``text`` is non-empty, then calls
        SESSION_REGISTRY.get_or_create + submit_turn.  Responds 200 {"status":
        "accepted"} on success.
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
        result = SESSION_REGISTRY.submit_turn(sid, {"text": text}, model_override)
        self._json(200, {"status": result})

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

    def _write_sse_frame(self, text: str) -> bool:
        """Encode ``text`` as one SSE ``data:`` frame; one logical line per
        SSE ``data:`` field. Returns False if the client disconnected."""
        if "\n" not in text:
            try:
                self.wfile.write(b"data: " + text.encode("utf-8", errors="replace") + b"\n\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                return False
            return True
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

    def _sse_client_gone(self) -> bool:
        """Non-blocking probe: has the SSE peer half-closed the socket?

        SSE is one-way (server -> client); the client never pushes bytes.
        So a readable signal on the request socket is either FIN (peer
        closed cleanly, MSG_PEEK returns b"") or RST (raises OSError).
        Both mean: drop the request thread now.

        Why this exists: ``wfile.write`` only surfaces a broken pipe once
        the OS has given up on the peer. On Windows that can be minutes
        as long as small outbound chunks still fit in the kernel send
        buffer — a chatty transcript or job stream therefore keeps
        feeding a phantom client and pins a thread + file handle until
        the 30-minute wall-clock cap. Polling the read side closes that
        gap to milliseconds.
        """
        try:
            readable, _, _ = select.select([self.connection], [], [], 0)
            if not readable:
                return False
            data = self.connection.recv(1, socket.MSG_PEEK)
            if not data:
                return True
            # Stray bytes from the client (SSE shouldn't have any). Drain
            # them so the next select doesn't keep firing on the same
            # buffered data, and treat the connection as still alive.
            try:
                self.connection.recv(4096)
            except (OSError, ValueError):
                return True
            return False
        except (OSError, ValueError):
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
    atexit.register(_shutdown_all_ptys)
    try:
        signal.signal(signal.SIGTERM, lambda *_: (_shutdown_all_ptys(), sys.exit(0)))
    except (ValueError, OSError):
        pass
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
        # _pty_idle_loop calls Pty.cleanup_idle() periodically for stale PTYs.
        threading.Thread(
            target=_pty_idle_loop,
            name="pty-idle-cleanup",
            daemon=True,
        ).start()
        # Periodic improver sweep: structural audit of every project skill
        # on a long cadence. Fills the gap left by the job-triggered
        # improver, which only fires for skills a job actually invoked —
        # uninvoked skills would otherwise never get audited.
        threading.Thread(
            target=_periodic_improver_loop,
            name="improver-sweep",
            daemon=True,
        ).start()
        _install_improver_shutdown_handlers()
        threading.Thread(
            target=_periodic_transcript_purge_loop,
            name="improver-transcript-purge",
            daemon=True,
        ).start()
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nstopped.")


if __name__ == "__main__":
    main()
