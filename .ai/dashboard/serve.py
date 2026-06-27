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
    _write_text_lf,
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
    _TRANSCRIPT_ACTIVITY_CACHE,
    _TRANSCRIPT_ACTIVITY_LOCK,
    _TRANSCRIPT_MODEL_CACHE,
    _TRANSCRIPT_MODEL_LOCK,
    _TRANSCRIPT_PREVIEW_CACHE,
    _TRANSCRIPT_PREVIEW_LOCK,
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
from server.improver import (  # noqa: E402
    _IMPROVER_DEFAULTS,
    _IMPROVER_SHUTDOWN_HANDLERS_INSTALLED,
    _IMPROVER_SWEEP_LOCK,
    _IMPROVER_TRACKED_SIDS,
    _IMPROVER_TRACKED_SIDS_LOCK,
    _SKILL_METRICS_LOCK,
    _STOPWORDS,
    _chain_improver_shutdown_signal,
    _detect_skill_suggestions,
    _diff_line_count,
    _extract_skills_from_stream_json,
    _install_improver_shutdown_handlers,
    _load_improver_config,
    _load_unique_jobs,
    _periodic_improver_loop,
    _periodic_improver_sweep,
    _periodic_transcript_purge_loop,
    _post_job_skill_actions,
    _project_skill_index,
    _purge_all_tracked_improver_sids,
    _purge_claude_transcript,
    _purge_stale_improver_transcripts_once,
    _read_log_excerpt,
    _record_skill_metrics,
    _run_improver_for_skill,
    _snapshot_tracked_improver_sids,
    _tokenize_task,
    _trigger_improvers_for_job,
)
import server.improver as _improver  # noqa: E402 — live reads of reassigned globals (_IMPROVER_SHUTDOWN_HANDLERS_INSTALLED)
from server.agent_suggest import (  # noqa: E402
    _build_agent_suggester_prompt,
    _git_log_excerpt,
    _load_editable_agent_names,
    _parse_agent_suggestions_output,
    _persist_agent_proposal,
    _recent_job_tasks,
)
from server.analytics import (  # noqa: E402
    _ANALYTICS_RANGES,
    _aggregate_analytics,
    _analytics_in_range,
    _analytics_parse_ts,
    _analytics_range_bounds,
    _load_auto_select_ranking,
    _load_timeline_runs,
)
from server.models_catalog import (  # noqa: E402
    _MODELS_CATALOG_FALLBACK,
    _patch_or_create_block,
    _patch_phase_block,
    _read_models_catalog,
)
from server.pipelines import _list_pipelines  # noqa: E402
from server.session_events import _jsonl_line_to_session_events  # noqa: E402
from server.git_utils import (  # noqa: E402
    _GIT_LSFILES_CACHE,
    _GIT_LSFILES_LOCK,
    _GIT_LSFILES_TTL_S,
    _git_lsfiles_cached,
    _git_lsfiles_put,
)
from server.http_base import (  # noqa: E402
    MAX_JSON_BODY,
    MAX_PIPELINE_PUT_BYTES,
    MAX_SSE_SESSION_S,
    MAX_TRANSCRIPT_CATCHUP_BYTES,
    SKIP_DIRS,
)
from server.pipelines_handlers import PipelineRoutes  # noqa: E402 — Handler mixin
from server.analytics_handlers import AnalyticsRoutes  # noqa: E402 — Handler mixin
from server.project_handlers import ProjectStateRoutes  # noqa: E402 — Handler mixin
from server.jobs_handlers import JobRoutes  # noqa: E402 — Handler mixin
from server.sessions_handlers import SessionRoutes  # noqa: E402 — Handler mixin
from server.transcripts_handlers import TranscriptRoutes  # noqa: E402 — Handler mixin
from server.pty_handlers import PtyRoutes  # noqa: E402 — Handler mixin


WORKFLOW_TEMPLATE_URL = _validate_template_url(
    os.environ.get("AI_WORKFLOW_TEMPLATE_URL", _DEFAULT_WORKFLOW_TEMPLATE_URL)
)

# Path constants (ROOT, ledger files, proposal dirs) now live in
# server/paths.py; re-exported above so `serve.METRICS_FILE` and the tests
# that monkeypatch these by name keep working.
# _IMPROVEMENTS_LEDGER_LOCK moved to server/improver_io.py (re-exported via shim).
# Improver subsystem state (_IMPROVER_TRACKED_SIDS(+_LOCK),
# _IMPROVER_SHUTDOWN_HANDLERS_INSTALLED, _SKILL_METRICS_LOCK) moved to
# server/improver.py (re-exported via shim).
# Serialises /api/workflow/update so two concurrent clients can't both spawn
# update-workflow.sh against the same tree at the same time (interleaved file
# writes corrupt the workflow core). Non-blocking acquire — second caller gets
# 409.
_WORKFLOW_UPDATE_LOCK = threading.Lock()
# _GIT_LSFILES_* cache state moved to server/git_utils.py (re-exported via shim).
# _TRANSCRIPT_{PREVIEW,MODEL,ACTIVITY}_{CACHE,LOCK} (the /api/sessions status-list
# caches shared by the session + transcript list handlers) moved to
# server/transcripts.py and are re-exported via the transcripts shim above.
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
# _IMPROVER_DEFAULTS moved to server/improver.py (re-exported via shim).

# HTTP-layer caps (MAX_JSON_BODY, MAX_PIPELINE_PUT_BYTES, MAX_SSE_SESSION_S,
# MAX_TRANSCRIPT_CATCHUP_BYTES, SKIP_DIRS) moved to server/http_base.py so the
# per-domain handler mixins can import them without a circular dependency on
# serve. Re-exported via the http_base shim above.
# MAX_WS_PAYLOAD (the inbound WebSocket frame cap) moved to server/ws.py with
# the rest of the WS framing; re-exported via the ws shim above.

# _jsonl_line_to_session_events moved to server/session_events.py (re-exported via shim).

# _JOB_RUNTIME_FIELDS + _TERMINAL_JOB_STATUSES moved to server/jobs_state.py
# (re-exported via the shim above) with the rest of the shared job registry.


# _list_pipelines moved to server/pipelines.py (re-exported via shim).



# _git_lsfiles_cached + _git_lsfiles_put (+ _GIT_LSFILES_* state) moved to
# server/git_utils.py and are re-exported via the shim above.



# _write_text_lf (LF-pinned text writer) moved to server/storage.py and is
# re-exported via the storage shim above; many handlers across domains use it.


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


# Auto-improver runtime (config, _project_skill_index, run/sweep/loops,
# signal handlers, transcript-purge, skill-suggestions, and
# _record_skill_metrics / _extract_skills_from_stream_json) moved to
# server/improver.py and are re-exported via the improver shim above.



# Install the metrics hook on server.jobs: its job runner calls this when a job
# finishes so skill-usage metrics get recorded, without jobs.py importing serve
# (which would be circular — serve imports jobs above via the shim).
_jobs.record_skill_metrics_hook = _record_skill_metrics


# Analytics page aggregation (_ANALYTICS_RANGES, _analytics_range_bounds,
# _analytics_parse_ts, _analytics_in_range, _aggregate_analytics) moved to
# server/analytics.py and are re-exported via the analytics shim above.



# PHASE_TO_SKILL + the per-skill telemetry rollup (_phase_metric_rows,
# _aggregate_skill_metrics) moved to server/metrics.py and are re-exported
# via the metrics shim above.


# _scan_agents_dir + _scan_skills_dir (skill/agent definition-tree scanners)
# moved to server/skills_config.py and are re-exported via the shim above.

# _read_yaml_field moved to server/config.py (re-exported via the shim above).


# Hard-coded mirror of the ``catalog:`` block in .ai/models.yaml. Used ONLY
# as a last resort when the file is missing/unreadable or PyYAML is absent —
# _MODELS_CATALOG_FALLBACK + _read_models_catalog moved to server/models_catalog.py
# (re-exported via the shim above).



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

# _load_auto_select_ranking + _load_timeline_runs moved to server/analytics.py (re-exported via shim).



# ---------- Agent suggestions (helpers) ----------------------------------
#
# These power /api/agents/suggest. The skills equivalent (_detect_skill_
# suggestions + _handle_suggestion_draft) feeds on telemetry; agents have no
# per-agent telemetry, so we lean on three cheap signals instead — git log,
# recent jobs, and the editable agent catalog (so the LLM doesn't propose
# duplicates).

# Agent-suggestion pipeline (_load_editable_agent_names, _recent_job_tasks,
# _git_log_excerpt, _build_agent_suggester_prompt, _parse_agent_suggestions_output,
# _persist_agent_proposal) moved to server/agent_suggest.py, re-exported via shim.



class Handler(PtyRoutes, TranscriptRoutes, SessionRoutes, JobRoutes, ProjectStateRoutes, AnalyticsRoutes, PipelineRoutes, http.server.SimpleHTTPRequestHandler):
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
    # Project-state endpoints (TODO ledger _todos_latest/_todos_banner/_clean_todo_tags,
    # _handle_todos_list / _handle_list / _handle_todo_create / _handle_todo_status /
    # _handle_todos_scan; _handle_memory / _handle_decisions; _handle_events_list /
    # _handle_events_clear) moved to server/project_handlers.py (ProjectStateRoutes
    # mixin); Handler inherits them.

    # ----- jobs -----
    # Job lifecycle + streaming endpoints (_job_summary, _handle_jobs_create,
    # _handle_jobs_list, _handle_job_get, _handle_job_interrupt, _handle_job_cancel,
    # _compose_multimodal_blocks, _handle_job_input, _handle_job_stream) moved to
    # server/jobs_handlers.py (JobRoutes mixin); Handler inherits them. The 3
    # shared SSE helpers (_write_sse_frame/_write_sse_event/_sse_client_gone) stay
    # on Handler and are reached via self.

    # IDE transcript endpoints (_handle_transcripts_list, _handle_transcript_stream)
    # moved to server/transcripts_handlers.py (TranscriptRoutes mixin); Handler
    # inherits them. Shared SSE writers stay on Handler (used via self).

    # Analytics-family GET endpoints (_handle_usage_total, _handle_timeline,
    # _handle_analytics, _handle_auto_select) moved to server/analytics_handlers.py
    # (AnalyticsRoutes mixin); Handler inherits them.

    # Pipeline + agent-orchestration endpoints (_pipelines_origin_guard,
    # _agent_orchestrations_origin_guard, _handle_pipelines_list / _handle_pipeline_*,
    # _handle_agent_orchestrations_list / _handle_agent_orchestration_get) moved to
    # server/pipelines_handlers.py (PipelineRoutes mixin); Handler inherits them.

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

    # Session status/SSE/control endpoints (_handle_session_stream,
    # _handle_sessions_list, _handle_session_input, _handle_session_release,
    # _handle_session_interrupt, _handle_session_branch) moved to
    # server/sessions_handlers.py (SessionRoutes mixin); Handler inherits them.
    # The _UUID_RE class attr + shared SSE writers stay on Handler (used via self).

    # ----- PTY endpoints (real shell sessions) --------------------------

    # PTY (real shell) WebSocket endpoints (_handle_ptys_list, _handle_pty_get,
    # _handle_pty_create, _handle_pty_kill, _handle_pty_ws) moved to
    # server/pty_handlers.py (PtyRoutes mixin); Handler inherits them.

    # UUID pattern used to validate session ids on the /api/sessions/* endpoints.
    _UUID_RE = re.compile(
        r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
    )

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


# _patch_or_create_block + _patch_phase_block moved to server/models_catalog.py (re-exported via shim).


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
