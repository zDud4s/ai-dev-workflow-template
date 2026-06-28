"""Job lifecycle, streaming, and the session-resume engine for the dashboard.

Extracted from serve.py. This module owns everything that turns an operator
request into a running subprocess and pumps its output back out:

  * Spawning  -- ``_spawn_job`` dispatches by kind (orchestrate/plan one-shot,
    ``chat`` long-lived claude stream-json, ``chat-codex`` per-turn codex);
    ``_start_subprocess_job`` is the underlying primitive: it launches the
    child on a worker thread, pumps stdout to the per-job log + SSE subscribers
    (``_publish_chunk``, with the ``_DROP_*`` slow-subscriber backpressure
    counters), tracks cost, and drives the session callbacks.
  * Stdin     -- ``_send_to_stdin`` / ``_send_chat_blocks`` /
    ``_interrupt_chat_turn`` write user turns / interrupts to a running chat.
  * Cancel    -- ``_cancel_job`` terminates a job (taskkill /T on Windows).
  * Sessions  -- the resume-engine layer (``_ResumeEngineAdapter``,
    ``_session_engine_factory``, ``SESSION_REGISTRY`` / ``SESSION_LOCK``,
    ``ForeignWriteWatcher`` / ``_watcher_loop``, the ``_maybe_*`` baton
    callbacks, ``_copy_transcript_with_new_sid``, ``_tail_chat_catchup``) lives
    here too: a resumed session IS a ``chat`` job, the runner calls back into
    it, and it calls back into the job primitives -- one cohesive domain rather
    than a clean import boundary. Folding them avoids a circular dependency.

serve.py re-exports every public name here via a shim, so ``serve._spawn_job``,
``serve.SESSION_REGISTRY``, the ``_handle_*`` request handlers, and the
``main()`` thread wiring keep working unchanged.

The one seam back to serve is ``record_skill_metrics_hook``: serve installs its
``_record_skill_metrics`` there at import time so the job runner can record
skill-usage metrics on completion without this module importing serve (which
would be circular -- serve imports this module). It stays None in standalone /
unit-test use.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import queue as _stdqueue
import subprocess
import threading
import time
import uuid
from pathlib import Path

from server import session_lock, session_registry
from server.config import _read_yaml_field
from server.jobs_persistence import _persist_job, _update_job_cost
from server.jobs_state import JOB_KINDS, JOBS, JOBS_LOCK
from server.paths import JOBS_DIR, ROOT
from server.transcript_paths import _transcripts_dir_for_cwd
from server.validation import _safe_which

# Seam back to serve.py, installed at import time by serve: the job runner
# calls this on completion to record skill-usage metrics. Kept as an injected
# hook rather than ``import serve`` because serve imports THIS module via a
# re-export shim, so importing back would be circular. None when unused.
record_skill_metrics_hook = None


# Job-stream SSE backpressure counters (keyed by job_id), used by
# ``_publish_chunk`` and the job-stream SSE handler in serve.py (which reads
# them via the shim). A slow subscriber whose bounded queue fills has chunks
# dropped for it; after ``_DROP_THRESHOLD`` sustained drops it is asked to
# reconnect and catch up.
_DROP_THRESHOLD = 64
_DROP_COUNTS: dict[str, dict[int, int]] = {}
_DROP_COUNTS_LOCK = threading.Lock()


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
            # Mark fork jobs so the stdout pump knows to overwrite session_id
            # with the new (forked) id claude mints, rather than keeping source.
            if fork_session_id:
                JOBS[job_id]["forked_from"] = fork_session_id
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
                "last_stdout_ts": 0.0,
            }
        sid_for_path = JOBS[job_id].get("session_id")

    # For chat jobs with a known session_id we route ``log_path`` to
    # claude's own transcript file (which claude writes anyway via
    # ``--session-id``). This avoids duplicating storage in
    # ``.ai/local/jobs/`` — the transcript is the single source of
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
                    with JOBS_LOCK:
                        JOBS[job_id]["last_stdout_ts"] = time.monotonic()
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
                                _maybe_mark_session_turn_done(job_id, obj)
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
                            # A forked chat job (POST /api/jobs with
                            # fork_session_id): claude mints a new session id and
                            # reports it in its init event. Capture it so the job
                            # row exposes the forked sid to the caller.
                            _maybe_capture_forked_sid(job_id, kind, obj)

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
            hook = record_skill_metrics_hook
            if hook is not None:
                try:
                    hook(job_id)
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

# Seconds after the last stdout chunk within which the engine is considered
# "recently active" for the concurrency heuristic in the registry. Kept at
# >= 3x the watch interval so scheduler jitter or a slow tick cannot make the
# engine mis-cede the trailing bytes of its own reply as a foreign write.
STDOUT_CORROBORATION_WINDOW_S = 3.0
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

    def submit(self, turn: dict) -> bool:
        """Write a user turn to the stdin of the resume subprocess.

        turn is a dict such as {"text": "..."} (and eventually images/files;
        for now only text is handled). Returns True if the write succeeded,
        False if the subprocess is gone or its stdin is closed — the registry
        uses this to fail safe instead of leaving a turn wedged in-flight.
        """
        text = turn.get("text") or ""
        # Reuses _send_to_stdin which already handles JSON-wrapping for kind=chat.
        ok, _err = _send_to_stdin(self._job_id, text)
        return ok

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

    def recently_active(self) -> bool:
        """True when the engine produced stdout within STDOUT_CORROBORATION_WINDOW_S seconds.

        Used by the session registry as a corroboration signal to attribute .jsonl
        growth to our engine rather than the IDE.
        """
        with JOBS_LOCK:
            j = JOBS.get(self._job_id)
            if not j:
                return False
            ts = j.get("last_stdout_ts", 0.0)
        if ts <= 0.0:
            return False
        return (time.monotonic() - ts) < STDOUT_CORROBORATION_WINDOW_S


def _session_engine_factory(sid: str, model: str) -> _ResumeEngineAdapter:
    """Build an EngineProtocol adapter for session ``sid``.

    Generates a fresh job_id, registers the job in JOBS with session_id=sid so
    that _start_subprocess_job resolves the correct transcript file, and
    starts the ``claude --resume <sid>`` subprocess in the background.
    """
    job_id = str(uuid.uuid4())
    # Resume an existing transcript; if the .jsonl does not exist yet this is a
    # brand-new session, so create it with --session-id instead of --resume
    # (a `claude --resume <unknown>` would fail). This lets dashboard-started
    # chats run through the same baton state machine as resumed IDE chats.
    tdir = _transcripts_dir_for_cwd(ROOT)
    transcript_exists = bool(tdir and (tdir / f"{sid}.jsonl").exists())
    argv = _build_chat_argv(model=model, session_id=sid, resume=transcript_exists)

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
            "last_stdout_ts": 0.0,
        }

    _start_subprocess_job(
        job_id=job_id,
        kind="chat",
        task=f"session-resume:{sid}",
        argv=argv,
    )

    adapter = _ResumeEngineAdapter(job_id)

    # The subprocess is launched on a worker thread, so it is almost never
    # alive at the instant submit_turn() checks engine.is_ready(). Without a
    # follow-up trigger the session would dwell in ACQUIRING forever and the
    # buffered first turn would never reach the engine. Poll for readiness in
    # the background and promote ACQUIRING -> ENGINE (which flushes the
    # buffered turn into the engine stdin) as soon as the process is up.
    def _await_engine_ready() -> None:
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            if adapter.is_ready():
                try:
                    SESSION_REGISTRY.mark_engine_ready(sid)
                except KeyError:
                    # Session was released/removed before the engine came up —
                    # nothing left to promote.
                    print(f"[serve] engine-ready: session {sid} gone before promotion", flush=True)
                return
            time.sleep(0.05)
        print(f"[serve] engine for session {sid} never reported ready (spawn failed?)", flush=True)

    threading.Thread(
        target=_await_engine_ready,
        daemon=True,
        name=f"engine-ready-{sid[:8]}",
    ).start()

    return adapter


# Cross-process file lock: prevents two dashboard processes from running an
# engine on the same session simultaneously. Lock files live under sessions/.
SESSION_LOCK = session_lock.SessionLock(ROOT / ".ai" / "dashboard" / "sessions")

SESSION_REGISTRY = session_registry.SessionRegistry(
    engine_factory=_session_engine_factory,
    lock_acquire=lambda sid, owner: SESSION_LOCK.try_acquire(sid, owner or "dashboard"),
    lock_release=SESSION_LOCK.release,
    lock_heartbeat=SESSION_LOCK.heartbeat,
)

# Poll interval for the background foreign-write watcher (seconds).
WATCH_INTERVAL_S = 1.0


class ForeignWriteWatcher:
    """Background watcher that detects file growth or disappearance for every
    registered session.  poll_once() is driven by _watcher_loop() at a regular
    interval; tests drive it directly to avoid real sleeps.
    """

    def poll_once(self) -> None:
        """Snapshot the session table and stat each .jsonl file once."""
        with SESSION_REGISTRY._lock:
            items = list(SESSION_REGISTRY._sessions.items())
        for sid, s in items:
            try:
                st = os.stat(s.jsonl_path)
                SESSION_REGISTRY.note_jsonl_growth(sid, st.st_size, st.st_mtime)
                SESSION_REGISTRY.tick(sid)
                # Keep the file lock alive for sessions that currently own an engine.
                if s.state.value in ("engine", "acquiring"):
                    SESSION_LOCK.heartbeat(sid)
            except OSError:
                # File gone or rotated; reconcile the session back to MIRROR.
                SESSION_REGISTRY.note_jsonl_gone(sid)


def _watcher_loop() -> None:
    """Loop that calls ForeignWriteWatcher.poll_once() every WATCH_INTERVAL_S.

    One bad tick must not kill the loop, so the body is wrapped and any
    unexpected exception is logged.  The OSError path inside poll_once() does
    real work (note_jsonl_gone) and is not silenced here.
    """
    watcher = ForeignWriteWatcher()
    while True:
        try:
            watcher.poll_once()
        except Exception as e:  # noqa: BLE001 — log and continue so the loop survives
            print("[serve] watcher: %r" % e, flush=True)
        time.sleep(WATCH_INTERVAL_S)


def _maybe_capture_forked_sid(job_id: str, kind: str, obj: dict) -> None:
    """Record the new session id minted by a ``--fork-session`` chat job.

    A forked chat job is spawned with ``--resume <src> --fork-session`` (via
    ``POST /api/jobs`` with ``fork_session_id``); claude keeps the history but
    writes new turns under a freshly-generated session id, reported in its
    ``system``/init event. We overwrite JOBS[job_id]["session_id"] with it so the
    job row exposes the forked sid. Only acts on chat jobs flagged
    ``forked_from``; a plain resume keeps its sid.
    """
    if kind != "chat" or obj.get("type") != "system":
        return
    new_sid = obj.get("session_id")
    if not new_sid:
        return
    with JOBS_LOCK:
        j = JOBS.get(job_id)
        if j is None or not j.get("forked_from"):
            return
        if new_sid != j.get("session_id"):
            j["session_id"] = new_sid


def _copy_transcript_with_new_sid(src_path: Path, dst_path: Path, new_sid: str) -> int:
    """Copy a Claude transcript to ``dst_path`` while rewriting each record's
    ``sessionId`` to ``new_sid``; return the number of records written.

    This is how a session is branched: the new transcript carries the full
    history under a fresh id, so resuming it (``claude --resume <new_sid>``)
    continues the conversation independently of the source. Per-record
    ``uuid``/``parentUuid`` links are internal to the transcript and stay
    self-consistent, so only ``sessionId`` is rewritten.

    The write is atomic (temp file + ``os.replace``) so a crash mid-copy can
    never leave a partial ``<new_sid>.jsonl`` that a later resume would read.
    Non-JSON lines (none expected in a well-formed transcript) are preserved
    verbatim so an unexpected line never corrupts the copy.
    """
    count = 0
    tmp_path = dst_path.with_name(dst_path.name + ".tmp")
    with src_path.open("r", encoding="utf-8", errors="replace") as fin, \
            tmp_path.open("w", encoding="utf-8", newline="\n") as fout:
        for line in fin:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                rec = json.loads(stripped)
            except (json.JSONDecodeError, ValueError):
                fout.write(stripped + "\n")
                count += 1
                continue
            if isinstance(rec, dict):
                rec["sessionId"] = new_sid
                fout.write(json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n")
            else:
                fout.write(stripped + "\n")
            count += 1
    os.replace(str(tmp_path), str(dst_path))
    return count


def _maybe_mark_session_turn_done(job_id: str, obj: dict) -> None:
    """Advance the session-registry baton when a ``type=result`` event arrives.

    Called from the stdout pump whenever a complete JSON line is parsed.
    Only acts when:
      - obj["type"] == "result"
      - the job's task starts with "session-resume:"
      - the session id extracted from the job is registered in SESSION_REGISTRY

    This is the production linchpin that drives mark_turn_done so the baton
    advances and a queued pending turn (if any) is dispatched to the engine.
    """
    if obj.get("type") != "result":
        return
    with JOBS_LOCK:
        j = JOBS.get(job_id)
        if not j:
            return
        task = j.get("task", "")
        if not task.startswith("session-resume:"):
            return
        sid = j.get("session_id")
    if not sid:
        return
    if sid not in SESSION_REGISTRY._sessions:
        return
    try:
        SESSION_REGISTRY.mark_turn_done(sid)
    except Exception as exc:
        print(f"[serve] mark_turn_done failed for session {sid}: {exc}", flush=True)


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
