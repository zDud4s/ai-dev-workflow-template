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
    POST /api/events/clear                      ->  truncates .ai/local/ledgers/events.jsonl
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

# Shared helper scripts live in `.ai/scripts/` (workflow-level — usable with
# or without the dashboard). Inject it onto sys.path so direct invocation
# (`python .ai/dashboard/serve.py`) and tests that load serve via importlib
# resolve the bare `import todos_parser` / `import auto_select_scorer` below
# (and the same imports inside server/analytics.py and server/handlers/project.py).
_SCRIPTS_DIR = str(Path(__file__).resolve().parent.parent / "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

# PTY helper (cross-platform). The Terminals page can spawn real shell
# sessions in addition to the existing chat-claude / chat-codex panes;
# this module wraps POSIX `pty.fork` and Windows `pywinpty.PtyProcess`
# behind one interface.
from server.pty import session as _pty_session  # noqa: E402
from server.sessions import registry as session_registry, lock as session_lock  # noqa: E402
from server._improver_transcript_policy import classify_transcript, load_ledger_rows  # noqa: E402

# Shared scripts, imported by name from .ai/scripts/ (on sys.path above).
import todos_parser as _todos_parser  # noqa: E402
import auto_select_scorer  # noqa: E402

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

# The blocks below re-export names from the server/* package so that ``serve._x``
# and ``from serve import _x`` keep resolving for callers and the tests that
# monkeypatch them by name; the implementations live in the server/* modules.
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
from server.transcripts.paths import (  # noqa: E402
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
    PORT,
    WORKFLOW_TEMPLATE_URL,
    set_bound_port,
    _SERVER_STARTED_AT,
    _WORKFLOW_UPDATE_LOCK,
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
from server.skills.config import (  # noqa: E402
    _AGENTS_ALL_CACHE,
    _CATALOG_TTL_SECONDS,
    _SKILLS_ALL_CACHE,
    _scan_agents_dir,
    _scan_skills_dir,
)
from server.skills.tree import (  # noqa: E402
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
from server.sessions.events import _jsonl_line_to_session_events  # noqa: E402
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
    _SUGGESTION_HTTP_TIMEOUT_MAX,
    _SUGGESTION_SEMAPHORE,
)
from server.handlers.pipelines import PipelineRoutes  # noqa: E402 — Handler mixin
from server.handlers.analytics import AnalyticsRoutes  # noqa: E402 — Handler mixin
from server.handlers.project import ProjectStateRoutes  # noqa: E402 — Handler mixin
from server.handlers.jobs import JobRoutes  # noqa: E402 — Handler mixin
from server.handlers.sessions import SessionRoutes  # noqa: E402 — Handler mixin
from server.handlers.transcripts import TranscriptRoutes  # noqa: E402 — Handler mixin
from server.handlers.pty import PtyRoutes  # noqa: E402 — Handler mixin
from server.handlers.skills import SkillRoutes  # noqa: E402 — Handler mixin
from server.handlers.proposals import ProposalRoutes  # noqa: E402 — Handler mixin
from server.handlers.agent_suggest import AgentSuggestRoutes  # noqa: E402 — Handler mixin
from server.handlers.files import FileRoutes  # noqa: E402 — Handler mixin
from server.handlers.workflow import WorkflowSettingsRoutes  # noqa: E402 — Handler mixin
from server.handlers.dispatch import DispatchPhaseRoutes  # noqa: E402 — Handler mixin


# Install the metrics hook on server.jobs: its job runner calls this when a job
# finishes so skill-usage metrics get recorded, without jobs.py importing serve
# (which would be circular — serve imports jobs above via the shim).
_jobs.record_skill_metrics_hook = _record_skill_metrics


class Handler(DispatchPhaseRoutes, WorkflowSettingsRoutes, FileRoutes, AgentSuggestRoutes, ProposalRoutes, SkillRoutes, PtyRoutes, TranscriptRoutes, SessionRoutes, JobRoutes, ProjectStateRoutes, AnalyticsRoutes, PipelineRoutes, http.server.SimpleHTTPRequestHandler):
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
    # .ai/specs/*, .ai/packets/*, .ai/local/ledgers/events.jsonl, .claude/skills/*) via this
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

    # ----- phase config edit -----
    _PHASES = {"session", "plan", "execute", "review", "rescue", "maintenance", "bootstrap"}
    _TOOLS = {"claude", "codex"}
    _PHASE_MODES = {"inline", "agent", "dispatcher"}
    # Claude `--effort` accepts {low, medium, high, xhigh, max}; codex
    # `model_reasoning_effort` accepts {low, medium, high, xhigh}. We accept
    # the union here and let the dispatcher omit/translate per tool.
    _REASONING = {"xhigh", "high", "medium", "low", "max"}


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
