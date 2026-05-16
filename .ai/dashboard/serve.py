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
"""
from __future__ import annotations

import datetime as _dt
import http.server
import json
import os
import re
import shutil
import socketserver
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
from collections import deque
from pathlib import Path

PORT = int(os.environ.get("DASHBOARD_PORT", "8765"))
ROOT = Path(__file__).resolve().parents[2]  # repo root
JOBS_DIR = ROOT / ".ai" / "dashboard" / "jobs"
# Append-only ledger of job snapshots — every status transition adds one
# JSON line so the dashboard can rebuild the JOBS dict after a server
# restart. Last snapshot per ``id`` wins. Tests override this with a tmp
# path via monkeypatch.
JOBS_PERSIST_FILE = ROOT / ".ai" / "dashboard" / "jobs.jsonl"

# Fields that exist only at runtime inside the JOBS dict and must NOT be
# serialised to disk (they are either not JSON-encodable or meaningless
# after the subprocess dies).
_JOB_RUNTIME_FIELDS = frozenset({"proc", "subscribers", "stdin_lock"})


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
        with JOBS_PERSIST_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(snapshot, default=str) + "\n")
    except OSError:
        # Persistence is best-effort; never break the live pipeline.
        pass


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
            except OSError:
                pass
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
    try:
        if not JOBS_PERSIST_FILE.exists():
            return
        seen: dict[str, dict] = {}
        with JOBS_PERSIST_FILE.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                jid = obj.get("id")
                if jid:
                    seen[jid] = obj
    except OSError:
        return

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
                        except Exception:
                            continue
                        if str(obj.get("cwd") or "").lower() == target:
                            return sub
                        break  # only peek first record per file
            except OSError:
                continue
    return None

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


def _spawn_job(job_id: str, kind: str, task: str, resume_session_id: str | None = None) -> None:
    """Spawn a job in a worker thread and capture stdout+stderr to a log file.

    Dispatches by kind:
      * ``orchestrate`` / ``plan`` -> one-shot ``claude -p <prompt>``.
      * ``chat``                   -> long-lived ``claude --print
                                       --input-format stream-json
                                       --output-format stream-json`` session
                                       driven by JSON messages on stdin.
    """
    session = _read_yaml_field(ROOT / ".ai" / "models.yaml", "session")
    model = session.get("model") or "claude-sonnet-4-6"
    claude_bin = shutil.which("claude") or "claude"

    if kind == "chat":
        session_id = resume_session_id or str(uuid.uuid4())
        argv = _build_chat_argv(model=model, session_id=session_id, claude_bin=claude_bin, resume=bool(resume_session_id))
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
        # session.model if no separate codex one is configured.
        codex_model = codex_session.get("codex_model") or model
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


def _build_chat_argv(model: str, session_id: str, claude_bin: str | None = None, resume: bool = False) -> list[str]:
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
        "--dangerously-skip-permissions",
        "--model", model,
    ]
    if resume:
        argv += ["--resume", session_id]
    else:
        argv += ["--session-id", session_id]
    return argv


def _chat_user_message(text: str) -> bytes:
    """Wrap a plain-text user message as a stream-json user envelope, one
    JSON object per line, ready to write to ``claude``'s stdin."""
    obj = {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "text", "text": text}],
        },
    }
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
                # For chat-kind jobs we ALSO accumulate complete JSON lines
                # and update the running cost from ``type=result`` events as
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
                    if logf is not None:
                        logf.write(text.encode("utf-8"))
                        logf.flush()
                    _publish_chunk(job_id, text)

                    if track_cost:
                        line_buf += text
                        while "\n" in line_buf:
                            line, _, line_buf = line_buf.partition("\n")
                            line = line.strip()
                            if not line.startswith("{"):
                                continue
                            try:
                                obj = json.loads(line)
                            except (json.JSONDecodeError, ValueError):
                                continue
                            if obj.get("type") == "result":
                                _update_job_cost(job_id, obj)

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
            _publish_chunk(job_id, None)  # sentinel: stream end
        except Exception as e:  # noqa: BLE001 (don't crash the server)
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
        except Exception:  # noqa: BLE001
            pass


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
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], check=False, capture_output=True)
            else:
                os.kill(pid, 15)
        except (ProcessLookupError, OSError):
            pass
    return True


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


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

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
        m = re.fullmatch(r"/api/transcripts/([0-9a-fA-F-]+)/stream", parsed.path)
        if m:
            self._handle_transcript_stream(m.group(1))
            return
        m = re.fullmatch(r"/api/jobs/([0-9a-f-]+)/stream", parsed.path)
        if m:
            self._handle_job_stream(m.group(1))
            return
        m = re.fullmatch(r"/api/jobs/([0-9a-f-]+)", parsed.path)
        if m:
            self._handle_job_get(m.group(1), urllib.parse.parse_qs(parsed.query))
            return
        super().do_GET()

    def do_POST(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
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
        else:
            m = re.fullmatch(r"/api/jobs/([0-9a-f-]+)/cancel", parsed.path)
            if m:
                self._handle_job_cancel(m.group(1))
                return
            m = re.fullmatch(r"/api/jobs/([0-9a-f-]+)/input", parsed.path)
            if m:
                self._handle_job_input(m.group(1), body)
                return
            self._json(404, {"error": "unknown endpoint", "path": parsed.path})

    # ----- helpers -----
    def _read_json_body(self) -> dict | None:
        length = int(self.headers.get("Content-Length") or "0")
        if length <= 0:
            return {}
        if length > 64 * 1024:
            self._json(413, {"error": "payload too large"})
            return None
        try:
            raw = self.rfile.read(length).decode("utf-8")
            return json.loads(raw) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            self._json(400, {"error": "invalid JSON", "detail": str(e)})
            return None

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:  # quieter logs
        sys.stderr.write(f"[dashboard] {fmt % args}\n")

    # ----- GET handlers -----
    def _handle_list(self, qs: dict[str, list[str]]) -> None:
        rel = (qs.get("path", [""])[0] or "").lstrip("/").replace("\\", "/")
        target = (ROOT / rel).resolve()
        try:
            target.relative_to(ROOT)
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
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        if existing and not existing.endswith("\n"):
            existing += "\n"
        path.write_text(existing + line, encoding="utf-8")
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
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        if existing and not existing.endswith("\n"):
            existing += "\n"
        path.write_text(existing + entry, encoding="utf-8")
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
        }
        # Chat jobs surface aggregated cost so the UI can show running
        # totals. Prefer the live counter (updated by the pump in real
        # time); fall back to scanning the log file post-hoc for older
        # jobs whose live counter wasn't tracked.
        if j.get("kind") in {"chat", "chat-codex"}:
            live = j.get("cost")
            if isinstance(live, dict):
                out["cost"] = live
            elif j.get("log_path"):
                cost = _extract_cost_from_log(Path(j["log_path"]))
                if cost is not None:
                    out["cost"] = cost
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
            }
        _spawn_job(job_id, kind, task, resume_session_id=resume_session_id)
        _persist_job(job_id)  # capture the initial queued/running snapshot
        _evict_old_jobs()
        with JOBS_LOCK:
            self._json(201, self._job_summary(JOBS[job_id]))

    def _handle_jobs_list(self) -> None:
        with JOBS_LOCK:
            items = [self._job_summary(j) for j in JOBS.values()]
        items.sort(key=lambda x: x.get("created_at") or "", reverse=True)
        self._json(200, {"jobs": items})

    def _handle_transcripts_list(self) -> None:
        """List the IDE transcript files for the current repo - the JSONL
        files Claude Code (the VSCode/Cursor extension) writes for every
        session in ``~/.claude/projects/<slug>/<session_id>.jsonl``."""
        tdir = _transcripts_dir_for_cwd(ROOT)
        if tdir is None:
            self._json(200, {"transcripts": [], "note": "no ~/.claude/projects directory for this repo"})
            return
        files = sorted(tdir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        items = []
        for p in files:
            try:
                st = p.stat()
            except OSError:
                continue
            items.append({
                "session_id": p.stem,
                "size_bytes": st.st_size,
                "modified": _dt.datetime.fromtimestamp(st.st_mtime, _dt.timezone.utc).isoformat(timespec="seconds"),
                "path": str(p.relative_to(tdir.parent)),
            })
        self._json(200, {"transcripts": items, "dir": str(tdir)})

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

        # Live tail: poll for appended bytes. Exit when client disconnects
        # or after a long idle period (defensive cap).
        last_size = pos
        idle_ticks = 0
        max_idle_ticks = 240  # ~ 4 minutes at 1s; client will reconnect
        while idle_ticks < max_idle_ticks:
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

    def _handle_job_cancel(self, job_id: str) -> None:
        if _cancel_job(job_id):
            self._json(200, {"ok": True})
        else:
            self._json(409, {"error": "job not running or not found"})

    def _handle_job_input(self, job_id: str, body: dict) -> None:
        with JOBS_LOCK:
            exists = job_id in JOBS
        if not exists:
            self._json(404, {"error": "job not found"})
            return
        text = body.get("text")
        if not isinstance(text, str) or not text:
            self._json(400, {"error": "text is required"})
            return
        if len(text) > 8000:
            self._json(400, {"error": "text must be 8000 chars or fewer"})
            return
        ok, err = _send_to_stdin(job_id, text)
        if not ok:
            code = 404 if err == "not found" else 409
            self._json(code, {"error": err})
            return
        self._json(200, {"ok": True})

    def _handle_job_stream(self, job_id: str) -> None:
        """Server-Sent Events stream of the subprocess output.

        Strategy:
          1. Take a snapshot of the existing log file and flush it as one
             `data:` frame (so reconnecting clients catch up).
          2. Register a queue as a subscriber; each chunk written by the
             runner thread gets forwarded as a `data:` frame.
          3. Terminate when the runner publishes the EOF sentinel (None).
        """
        import queue as _queue

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

            # 1. Catch-up: flush whatever is already on disk.
            if log_path and Path(log_path).exists():
                try:
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
        text = path.read_text(encoding="utf-8")
        # Replace existing `dispatch_mode: <value>` line (with optional inline comment), or insert near top.
        line_re = re.compile(r"^(dispatch_mode:\s*)\S+(\s*(?:#.*)?)$", re.M)
        if line_re.search(text):
            new_text = line_re.sub(rf"\g<1>{mode}\g<2>", text, count=1)
        else:
            # Insert after the first non-comment, non-blank line — keep it simple.
            new_text = f"dispatch_mode: {mode}    # auto | manual\n\n" + text
        path.write_text(new_text, encoding="utf-8")
        self._json(200, {"ok": True, "mode": mode})

    # ----- phase config edit -----
    _PHASES = {"session", "plan", "execute", "review", "rescue", "maintenance", "bootstrap"}
    _TOOLS = {"claude", "codex"}
    _PHASE_MODES = {"inline", "agent", "dispatcher"}
    _REASONING = {"xhigh", "high", "medium", "low"}

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

        if not updates:
            self._json(400, {"error": "no updatable fields provided (tool, model, mode, reasoning_effort)"})
            return

        path = ROOT / ".ai" / "models.yaml"
        if not path.exists():
            self._json(404, {"error": "models.yaml not found"})
            return
        try:
            new_text = _patch_phase_block(path.read_text(encoding="utf-8"), phase, updates)
        except ValueError as e:
            self._json(404, {"error": str(e)})
            return
        path.write_text(new_text, encoding="utf-8")
        self._json(200, {"ok": True, "phase": phase, "updated": updates})


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
    last_err: OSError | None = None
    for candidate in (PORT, PORT + 1, PORT + 2, PORT + 3):
        try:
            httpd = socketserver.ThreadingTCPServer(("127.0.0.1", candidate), Handler)
        except OSError as e:
            last_err = e
            continue
        with httpd:
            url = f"http://localhost:{candidate}/.ai/dashboard/"
            print(f"AI workflow dashboard: {url}")
            print("Press Ctrl+C to stop.")
            try:
                httpd.serve_forever()
            except KeyboardInterrupt:
                print("\nstopped.")
        return
    raise SystemExit(f"could not bind to any port starting at {PORT}: {last_err}")


if __name__ == "__main__":
    main()
