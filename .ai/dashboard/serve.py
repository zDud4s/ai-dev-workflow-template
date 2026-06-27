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

import atexit
import datetime as _dt
import http.server
import importlib
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
import auto_select_scorer  # noqa: E402 — scripts/ helper
import session_registry  # noqa: E402 — scripts/ helper
import session_lock  # noqa: E402 — scripts/ helper

from _improver_transcript_policy import classify_transcript, load_ledger_rows  # noqa: E402

PORT = int(os.environ.get("DASHBOARD_PORT", "8765"))
# The actually-bound port (BOUND_PORT) and the Origin allowlist that keys on
# it now live in server/runtime.py (re-exported via the shim below). main()
# publishes the real bound port through set_bound_port() once the socket is
# open. Moved out so server/ws.py can share the allowlist without importing
# serve.

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
_RE_SESSION_INTERRUPT = re.compile(r"/api/sessions/([^/]+)/interrupt")
_RE_SESSION_BRANCH = re.compile(r"/api/sessions/([^/]+)/branch")
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

# _RE_TASKLIST_PID (the Windows tasklist-CSV PID matcher) moved to
# server/jobs_reaper.py with the rest of the PID-liveness layer.
_SERVER_STARTED_AT = time.time()
# Validation / path helpers (URL allowlist, safe-which, trusted-dir, ISO ts,
# path normalisation) now live in server/validation.py; re-exported here so
# `serve._x` and `from serve import _x` keep resolving unchanged.
_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
from server.validation import (  # noqa: E402
    _ALLOWED_TEMPLATE_HOSTS,
    _DEFAULT_WORKFLOW_TEMPLATE_URL,
    _is_under_trusted_dir,
    _iso_to_epoch,
    _normalise_path_for_match,
    _parse_iso_ts,
    _safe_which,
    _skill_name_canonical,
    _validate_template_url,
)
from server.storage import (  # noqa: E402
    _JSONL_CACHE,
    _JSONL_CACHE_LOCK,
    _PATH_CACHE_MAX,
    _bound_path_cache,
    _load_jsonl_cached,
)
from server.agent_runs import (  # noqa: E402
    _AGENT_RUN_PARSE_CACHE,
    _AGENT_RUN_PARSE_LOCK,
    _agent_run_slug_date,
    _markdown_section,
    _first_section_value,
    _line_value,
    _normalise_agent_run_key,
    _strip_agent_run_value,
    _parse_agent_run_depends_on,
    _agent_run_node,
    _split_markdown_table_row,
    _is_markdown_table_separator,
    _parse_agent_run_dag_table,
    _parse_agent_run_inline_fields,
    _parse_agent_run_dag_yamlish,
    _parse_agent_run_dag,
    _extract_handoff_synthesis_ts,
    _extract_handoff_field,
    _extract_agent_run_success,
    _parse_agent_run,
    _agent_run_metrics_by_slug,
    _list_agent_runs,
)
from server.pty import (  # noqa: E402
    PTYS,
    PTYS_LOCK,
    PTYS_MAX,
    PTY_RING_BYTES,
    _pty_spawn,
    _pty_reader_loop,
    _pty_kill,
    _pty_summary,
    _evict_old_ptys,
    _shutdown_all_ptys,
    _pty_idle_loop,
)
from server.transcript_paths import (  # noqa: E402
    _CLAUDE_PROJECTS_ROOT_OVERRIDE,
    _CODEX_SESSIONS_ROOT_OVERRIDE,
    _TRANSCRIPTS_DIR_NEG_TTL_S,
    _TRANSCRIPTS_DIR_CACHE,
    _claude_projects_root,
    _transcripts_dir_for_cwd,
    _codex_sessions_root,
)
from server.usage import (  # noqa: E402
    _CODEX_FILE_AGG_CACHE,
    _CODEX_FILE_AGG_LOCK,
    _USAGE_CACHE,
    _USAGE_TTL_SECONDS,
    _aggregate_claude_usage,
    _aggregate_codex_usage,
    _fill_percent_shares,
    _CLAUDE_CREDENTIALS_PATH_OVERRIDE,
    _CLAUDE_USAGE_CACHE,
    _CLAUDE_USAGE_TTL_SECONDS,
    _CLAUDE_USAGE_ERROR_TTL_SECONDS,
    _CLAUDE_USAGE_LAST_GOOD,
    _CLAUDE_OAUTH_USAGE_URL,
    _CREDENTIALS_READ_RETRIES,
    _CREDENTIALS_READ_RETRY_SLEEP_S,
    _read_credentials_json,
    _read_claude_oauth_token,
    _usage_cache_ttl,
    _fetch_claude_oauth_usage,
    _aggregate_project_token_usage,
)
from server.paths import (  # noqa: E402
    ROOT,
    WORKFLOW_VERSION_FILE,
    JOBS_DIR,
    JOBS_PERSIST_FILE,
    EVENTS_FILE,
    METRICS_FILE,
    AGENT_RUNS_DIR,
    PIPELINES_DIR,
    SKILL_METRICS_FILE,
    TODOS_FILE,
    SKILL_PROPOSALS_DIR,
    SKILL_BACKUPS_DIR,
    IMPROVEMENTS_LEDGER,
    AGENT_PROPOSALS_DIR,
)
from server.transcripts import (  # noqa: E402
    _CODEX_ROLLOUT_PATH_CACHE,
    _CODEX_ROLLOUT_PATH_LOCK,
    _lookup_session_task,
    _lookup_session_title,
    _lookup_session_model,
    _summarise_tool_use,
    _lookup_session_activity,
    _codex_rollout_path,
    _summarise_codex_call,
    _lookup_codex_activity,
)
from server.runtime import (  # noqa: E402
    BOUND_PORT,
    set_bound_port,
    _origin_allowed,
    _browser_cross_origin_blocked,
)
import server.runtime as _runtime  # noqa: E402 — for live reads of the mutable BOUND_PORT
from server.ws import (  # noqa: E402
    MAX_WS_PAYLOAD,
    WS_GUID,
    _ws_accept_key,
    _WsClosed,
    WebSocket,
)
from server.llm_output import _parse_improver_output  # noqa: E402
from server.config import _read_yaml_field  # noqa: E402
from server.jobs_state import (  # noqa: E402
    JOB_KINDS,
    JOBS,
    JOBS_LOCK,
    JOBS_MAX,
    _JOB_RUNTIME_FIELDS,
    _TERMINAL_JOB_STATUSES,
)
from server.jobs_persistence import (  # noqa: E402
    _COST_EXTRACT_CACHE,
    _COST_EXTRACT_LOCK,
    _DEFAULT_JOBS_PERSIST_FILE,
    _JOBS_PERSIST_LOCK,
    _extract_cost_from_log,
    _load_persisted_jobs,
    _persist_job,
    _prune_old_logs,
    _update_job_cost,
)
from server.jobs_reaper import (  # noqa: E402
    JOB_REAP_INTERVAL_S,
    _PID_ALIVE_CACHE,
    _PID_ALIVE_TTL_SECONDS,
    _RE_TASKLIST_PID,
    _batch_prime_pid_cache_windows,
    _evict_old_jobs,
    _job_reaper_loop,
    _job_reaper_tick,
    _pid_is_alive,
    _reconcile_running_pids,
)
from server.jobs import (  # noqa: E402
    SESSION_LOCK,
    SESSION_REGISTRY,
    STDOUT_CORROBORATION_WINDOW_S,
    WATCH_INTERVAL_S,
    ForeignWriteWatcher,
    _CHAT_CATCHUP_INCLUDE_TYPES,
    _DROP_COUNTS,
    _DROP_COUNTS_LOCK,
    _DROP_THRESHOLD,
    _ResumeEngineAdapter,
    _VALID_PERMISSION_MODES,
    _build_chat_argv,
    _build_codex_chat_argv,
    _cancel_job,
    _chat_user_message,
    _copy_transcript_with_new_sid,
    _interrupt_chat_turn,
    _maybe_capture_forked_sid,
    _maybe_mark_session_turn_done,
    _publish_chunk,
    _send_chat_blocks,
    _send_to_stdin,
    _session_engine_factory,
    _spawn_job,
    _start_subprocess_job,
    _tail_chat_catchup,
    _watcher_loop,
)
import server.jobs as _jobs  # noqa: E402 — for installing the metrics hook below
from server.metrics import (  # noqa: E402
    PHASE_TO_SKILL,
    _aggregate_skill_metrics,
    _phase_metric_rows,
)
from server.skills_config import _scan_agents_dir, _scan_skills_dir  # noqa: E402
from server.skill_tree import (  # noqa: E402
    _BRIDGE_SKILLS_NO_MIRROR,
    _create_skill_in_both_trees,
    _mirror_claude_skill_to_agents,
)
from server.improver_io import (  # noqa: E402
    _IMPROVEMENTS_LEDGER_LOCK,
    _RECENT_FAILURE_MAX_AGE_DAYS,
    _apply_improvement,
    _audit_improvement,
    _auto_revert_skill,
    _build_improver_prompt,
    _check_held_out_gate,
    _check_skill_regression,
    _has_audit_signal,
    _last_improver_run_ts,
    _recent_rejected_proposals,
    _supersede_prior_pending,
    _write_proposal,
)


WORKFLOW_TEMPLATE_URL = _validate_template_url(
    os.environ.get("AI_WORKFLOW_TEMPLATE_URL", _DEFAULT_WORKFLOW_TEMPLATE_URL)
)

# Path constants (ROOT, ledger files, proposal dirs) now live in
# server/paths.py; re-exported above so `serve.METRICS_FILE` and the tests
# that monkeypatch these by name keep working.
# _IMPROVEMENTS_LEDGER_LOCK moved to server/improver_io.py (re-exported via shim).
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
_TRANSCRIPT_PREVIEW_CACHE: dict[str, tuple[int, str | None, str | None]] = {}
_TRANSCRIPT_PREVIEW_LOCK = threading.Lock()
# mtime-keyed cache of the per-session model for the status list:
# session_id -> (mtime_ns, model_str_or_None). Computed from a cheap head
# read (see _lookup_session_model) so an active multi-MB transcript isn't
# re-scanned on every /api/sessions poll.
_TRANSCRIPT_MODEL_CACHE: dict[str, tuple[int, str | None]] = {}
_TRANSCRIPT_MODEL_LOCK = threading.Lock()
# mtime-keyed cache of the per-session live activity for the status list:
# session_id -> (mtime_ns, {"text","kind"} | None). Tail-read per poll only
# when the transcript changed (see _lookup_session_activity).
_TRANSCRIPT_ACTIVITY_CACHE: dict[str, tuple[int, dict | None]] = {}
_TRANSCRIPT_ACTIVITY_LOCK = threading.Lock()
# Transcript location state moved to server.transcript_paths; re-exported above.
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

# MAX_WS_PAYLOAD (the inbound WebSocket frame cap) moved to server/ws.py with
# the rest of the WS framing; re-exported via the ws shim above.

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


def _jsonl_line_to_session_events(line: str) -> "list[dict]":
    """Normalize one JSONL line from a Claude transcript into SessionEvent dicts.

    Returns a list (possibly empty) of events WITHOUT a ``seq`` field — the
    caller assigns ``seq`` per emitted event, since one transcript line can
    expand into several events (an assistant turn carries text + one or more
    tool_use blocks; a user turn carries tool_result blocks). Emitting one
    event per block — rather than collapsing the whole line into a single
    event — is what lets the canvas render a tool_use pill with its real name
    and input, and attach each tool_result to the pill it belongs to.

    SessionEvent schema (seq added by caller):
      message:     {"kind":"message","role":str,"text":str,"partial":False,"state":None}
      tool_use:    {"kind":"tool_use","role":str,"id":str,"name":str,"input":dict,"text":str}
      tool_result: {"kind":"tool_result","role":str,"tool_use_id":str,"is_error":bool,"content":str,"text":str}
      thinking:    {"kind":"thinking","role":str,"text":str,"partial":False,"state":None}
      system:      {"kind":"system","role":"system","text":str,"partial":False,"state":None}
    """
    line = line.strip()
    if not line:
        return []
    try:
        obj = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(obj, dict):
        return []

    msg_type = obj.get("type")
    message = obj.get("message") or {}
    role = message.get("role") or obj.get("role")

    # user / assistant message lines
    if msg_type in ("user", "assistant"):
        content = message.get("content")
        if isinstance(content, str):
            text = content.strip()
            if not text:
                return []
            return [{
                "kind": "message", "role": role or msg_type, "text": content,
                "partial": False, "state": None,
            }]
        if not isinstance(content, list):
            return []
        # One event per block, in transcript order: text -> message, tool_use ->
        # tool_use (with id/name/input so the pill renders), tool_result ->
        # tool_result (with tool_use_id so it binds to the pill).
        events: "list[dict]" = []
        text_buf: "list[str]" = []

        def _flush_text():
            joined = "".join(text_buf)
            text_buf.clear()
            if joined.strip():
                events.append({
                    "kind": "message", "role": role or msg_type, "text": joined,
                    "partial": False, "state": None,
                })

        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                text_buf.append(block.get("text") or "")
            elif btype == "thinking":
                # Chain-of-thought. Flush any buffered text first so the
                # thought lands in transcript order (thinking precedes the
                # answer), then emit it as its own event. The client renders
                # it as a collapsed <details> inside the assistant bubble, so
                # long monologues don't drown the answer but stay inspectable.
                _flush_text()
                thought = block.get("thinking")
                if isinstance(thought, str) and thought.strip():
                    events.append({
                        "kind": "thinking", "role": role or msg_type,
                        "text": thought, "partial": False, "state": None,
                    })
            elif btype == "tool_use":
                _flush_text()
                name = block.get("name") or "tool"
                tinput = block.get("input")
                if not isinstance(tinput, dict):
                    tinput = {}
                events.append({
                    "kind": "tool_use", "role": role,
                    "id": block.get("id") or "", "name": name, "input": tinput,
                    "text": name, "partial": False, "state": None,
                })
            elif btype == "tool_result":
                _flush_text()
                result_content = block.get("content")
                result_text = ""
                if isinstance(result_content, str):
                    result_text = result_content
                elif isinstance(result_content, list):
                    result_text = " ".join(
                        b.get("text") or "" for b in result_content
                        if isinstance(b, dict) and b.get("type") == "text"
                    )
                events.append({
                    "kind": "tool_result", "role": role,
                    "tool_use_id": block.get("tool_use_id") or "",
                    "is_error": bool(block.get("is_error")),
                    "content": result_text, "text": result_text,
                    "partial": False, "state": None,
                })
        _flush_text()
        return events

    # system / init lines
    if msg_type in ("system", "init"):
        content = obj.get("content") or message.get("content") or ""
        if isinstance(content, list):
            content = " ".join(
                b.get("text") or "" for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            )
        if not isinstance(content, str) or not content.strip():
            return []
        return [{
            "kind": "system", "role": "system", "text": content,
            "partial": False, "state": None,
        }]

    # Unknown or empty type — skip
    return []


# Directories the fallback ``ROOT.rglob("*")`` walk in ``_handle_files_list``
# must not descend into. Without this, the autocomplete endpoint walks the
# entire repo on every keystroke when ``git ls-files`` is unavailable —
# slow on large repos and leaks dotfile paths (``.git/objects/*``,
# ``node_modules/**``, ``.venv/**``) into the suggestion list.
SKIP_DIRS = frozenset({
    ".git", "node_modules", "__pycache__", ".pytest_cache",
    ".venv", "venv", ".tox", ".mypy_cache", "tmp",
})

# _JOB_RUNTIME_FIELDS + _TERMINAL_JOB_STATUSES moved to server/jobs_state.py
# (re-exported via the shim above) with the rest of the shared job registry.


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


# Job persistence + cost extraction (_persist_job, _update_job_cost,
# _prune_old_logs, _extract_cost_from_log, _load_persisted_jobs + their state
# _JOBS_PERSIST_LOCK / _DEFAULT_JOBS_PERSIST_FILE / _COST_EXTRACT_*) moved to
# server/jobs_persistence.py and are re-exported via the shim above.

# Transcript location helpers/state moved to server.transcript_paths; re-exported above.


# Token usage aggregation moved to server.usage; re-exported above.

# Cached responses for /api/skills/all and /api/agents/all. Both endpoints
# walk 3-4 disk locations (.claude/skills, ~/.claude/skills, ~/.codex/skills
# for skills; project + user + plugin marketplaces/cache for agents) and the
# dashboard fires them in parallel at boot â€” combined ~500-1000ms of FS work
# on cold cache. A 15s TTL absorbs the boot storm + tab-switch refresh
# without making manual edits invisible for long: SKILL.md tweaks show up
# within 15 s, which matches the "auto-refresh" cadence elsewhere.
_SKILLS_ALL_CACHE: dict = {"at": 0.0, "data": None}
_AGENTS_ALL_CACHE: dict = {"at": 0.0, "data": None}
_CATALOG_TTL_SECONDS = 15.0

# JOB_KINDS + the in-memory job registry (JOBS / JOBS_LOCK / JOBS_MAX) moved to
# server/jobs_state.py and are re-exported via the shim above. Imported by
# reference so serve.py and the jobs package mutate one shared dict.


# ----- PTY sessions (real shell terminals via WebSocket) ----------------
#
# Separate from JOBS because the lifecycle, transport, and rendering are
# completely different: PTY sessions move raw bytes over WS rather than
# stream-json events over SSE. Each entry mirrors the JOBS shape just
# enough for the dashboard UI to list / open / close them.
# The PTY registry state (PTYS / PTYS_LOCK / PTYS_MAX / PTY_RING_BYTES) now
# lives in server/pty.py and is re-exported above.
#
# Job-stream SSE backpressure counters (_DROP_THRESHOLD / _DROP_COUNTS /
# _DROP_COUNTS_LOCK) moved to server/jobs.py alongside _publish_chunk and are
# re-exported via the jobs shim above (the job-stream SSE handler reads them).

# The RFC6455 WebSocket framing (WS_GUID, MAX_WS_PAYLOAD, _ws_accept_key,
# _WsClosed, class WebSocket) moved to server/ws.py and is re-exported via the
# ws shim above. The loopback Origin allowlist it enforces during the handshake
# (_origin_allowed / _browser_cross_origin_blocked) lives in server/runtime.py.


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


# Improver audit-signal + throttle reads (_last_improver_run_ts,
# _has_audit_signal, _recent_rejected_proposals, _RECENT_FAILURE_MAX_AGE_DAYS)
# moved to server/improver_io.py and are re-exported via the shim above.



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


# _build_improver_prompt moved to server/improver_io.py (re-exported via shim).



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


# _parse_improver_output (extract-one-JSON-object-from-LLM-stdout) moved to
# server/llm_output.py and is re-exported via the shim above.


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


# Improver audit ledger + proposal/apply/regression layer (_audit_improvement,
# _write_proposal, _supersede_prior_pending, _apply_improvement, _check_held_out_gate,
# _check_skill_regression, _auto_revert_skill) moved to server/improver_io.py and are
# re-exported via the shim above.



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


# _iso_to_epoch moved to server/validation.py (re-exported via the shim above).


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


# _skill_name_canonical moved to server/validation.py (re-exported via the shim above).


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


# Install the metrics hook on server.jobs: its job runner calls this when a job
# finishes so skill-usage metrics get recorded, without jobs.py importing serve
# (which would be circular — serve imports jobs above via the shim).
_jobs.record_skill_metrics_hook = _record_skill_metrics


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


# PHASE_TO_SKILL + the per-skill telemetry rollup (_phase_metric_rows,
# _aggregate_skill_metrics) moved to server/metrics.py and are re-exported
# via the metrics shim above.


# _scan_agents_dir + _scan_skills_dir (skill/agent definition-tree scanners)
# moved to server/skills_config.py and are re-exported via the shim above.

# _read_yaml_field moved to server/config.py (re-exported via the shim above).


# Hard-coded mirror of the ``catalog:`` block in .ai/models.yaml. Used ONLY
# as a last resort when the file is missing/unreadable or PyYAML is absent —
# the live source of truth is always the YAML. Keep the shape identical to
# what _read_models_catalog returns: {tool: [model_id, ...]}, newest-first.
_MODELS_CATALOG_FALLBACK: dict[str, list[str]] = {
    "claude": ["claude-opus-4-8", "claude-fable-5", "claude-opus-4-7", "claude-sonnet-4-6", "claude-haiku-4-5"],
    "codex":  ["gpt-5.5", "gpt-5.4", "gpt-5.4-mini"],
}


def _read_models_catalog(path: Path | None = None) -> dict[str, list[str]]:
    """Read the ``catalog:`` block from .ai/models.yaml — the single source of
    truth for which models exist per tool.

    Returns ``{tool: [model_id, ...]}`` newest-first. Each catalog entry is a
    mapping ``{id: ..., ...}``; only the ``id`` is surfaced here (notes/labels
    stay in the YAML as inline comments). Falls back to
    ``_MODELS_CATALOG_FALLBACK`` on any failure (missing file, no PyYAML, no
    ``catalog`` block, malformed shape) so model pickers never render empty.
    """
    if path is None:
        path = ROOT / ".ai" / "models.yaml"
    try:
        import yaml  # local import — PyYAML only needed by this helper
        parsed = yaml.safe_load(path.read_text(encoding="utf-8", errors="replace")) or {}
        catalog = parsed.get("catalog")
        if not isinstance(catalog, dict):
            return {k: list(v) for k, v in _MODELS_CATALOG_FALLBACK.items()}
        out: dict[str, list[str]] = {}
        for tool, entries in catalog.items():
            if not isinstance(entries, list):
                continue
            ids: list[str] = []
            for entry in entries:
                if isinstance(entry, dict):
                    mid = entry.get("id")
                elif isinstance(entry, str):
                    mid = entry
                else:
                    mid = None
                if isinstance(mid, str) and mid.strip():
                    ids.append(mid.strip())
            if ids:
                out[str(tool)] = ids
        return out or {k: list(v) for k, v in _MODELS_CATALOG_FALLBACK.items()}
    except Exception:  # noqa: BLE001 — a bad config must never break model pickers
        return {k: list(v) for k, v in _MODELS_CATALOG_FALLBACK.items()}


# ---------------------------------------------------------------------------
# Job lifecycle, streaming (SSE), stdin/cancel, and the session-resume engine
# (_spawn_job, _build_chat_argv / _build_codex_chat_argv / _chat_user_message,
# _start_subprocess_job, _publish_chunk, _send_chat_blocks, _send_to_stdin,
# _interrupt_chat_turn, _cancel_job, _ResumeEngineAdapter, _session_engine_factory,
# SESSION_REGISTRY / SESSION_LOCK, ForeignWriteWatcher / _watcher_loop, the
# _maybe_capture_forked_sid / _maybe_mark_session_turn_done baton callbacks,
# _copy_transcript_with_new_sid, _tail_chat_catchup + the _DROP_* / _VALID_* /
# _CHAT_CATCHUP_* constants) moved to server/jobs.py and are re-exported via the
# jobs shim above. serve installs _record_skill_metrics as the runner's metrics
# hook (see _jobs.record_skill_metrics_hook, set right after that function).
# ---------------------------------------------------------------------------


# Transcript reading/live-activity helpers and Codex rollout state moved to server.transcripts.

def _load_auto_select_ranking(max_records: int = 200, min_samples: int = 5) -> dict:
    """Aggregate METRICS_FILE into adaptive auto-select rankings."""
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
    return auto_select_scorer.score_groups(
        rows,
        min_samples=min_samples,
        effective_budget=None,
        per_group_tail=max_records,
        static_pick=None,
    )


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
            m = _RE_SESSION_INTERRUPT.fullmatch(parsed.path)
            if m:
                self._handle_session_interrupt(m.group(1))
                return
            m = _RE_SESSION_BRANCH.fullmatch(parsed.path)
            if m:
                self._handle_session_branch(m.group(1))
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
        default 5); invalid values fall back to the default."""
        raw = urllib.parse.parse_qs(parsed.query or "").get("min_samples", [None])[0]
        try:
            min_samples = max(1, min(50, int(raw)))
        except (TypeError, ValueError):
            min_samples = 5
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
            host, port = "127.0.0.1", _runtime.BOUND_PORT
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
            # Single source of truth for the model pickers — read live from the
            # ``catalog:`` block in models.yaml so the dashboard never carries a
            # second copy of the model list (see core.js applyModelCatalog).
            "catalog": _read_models_catalog(models_path),
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
        held_out = _check_held_out_gate(proposal_id)
        obj["held_out"] = held_out
        try:
            pj.write_text(json.dumps(obj, indent=2), encoding="utf-8")
        except OSError as e:
            print(f"[serve] failed to write proposal {pj} (held-out gate): {e}", flush=True)
        if held_out.get("decision") == "block":
            self._json(409, {
                "error": "proposal regresses the held-out set",
                "held_out": held_out,
            })
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
    # different candidate. (Lives in server.runtime now — set it there so the
    # allowlist functions, which read runtime's global, see the live value.)
    set_bound_port(bound)
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
        # Background watcher: detects foreign writes and file disappearance so
        # the session registry stays consistent without requiring HTTP requests.
        threading.Thread(
            target=_watcher_loop,
            name="foreign-write-watcher",
            daemon=True,
        ).start()
        # Background job reaper: reconciles dead-PID jobs and bounds the JOBS
        # dict on a fixed cadence. Without it, reaping only happens when a
        # browser polls /api/jobs, so an unattended server leaks finished
        # job state and dead subprocess handles indefinitely.
        threading.Thread(
            target=_job_reaper_loop,
            name="job-reaper",
            daemon=True,
        ).start()
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nstopped.")


if __name__ == "__main__":
    main()
