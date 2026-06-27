"""Regression coverage for batch 5 serve.py hardening (broad except logging,
taskkill stderr, SSE wall-clock cap on transcript stream, read_text encoding,
and response-header nosniff).

Loads serve.py via importlib so the conftest path setup is reused. None of
these tests touch the network or the user's running dashboard on :8765 —
they operate purely on the parsed source AST and a few unit helpers.
"""
from __future__ import annotations

import ast
import importlib.util
import inspect
import io
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVE_PATH = REPO_ROOT / ".ai" / "dashboard" / "serve.py"
SRC = SERVE_PATH.read_text(encoding="utf-8")


def _load_serve():
    """Import serve.py via importlib for unit-test access to module-level
    constants. Tests that only need source-level assertions use ``SRC``."""
    spec = importlib.util.spec_from_file_location("serve_under_test", SERVE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# -------- 1. Broad except blocks have logging or scoped exceptions --------

def test_no_bare_except_in_serve():
    """Bare ``except:`` clauses swallow KeyboardInterrupt / SystemExit and
    are forbidden anywhere in serve.py."""
    tree = ast.parse(SRC)
    bare = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler) and node.type is None:
            bare.append(node.lineno)
    assert bare == [], f"bare except: found at lines {bare}"


def test_publish_chunk_no_silent_broad_except():
    """``_publish_chunk`` used to swallow every exception with ``pass``.
    After batch 5 it must either scope to ``_stdqueue.Full`` or log the
    error so a runaway subscriber pattern is visible."""
    # _publish_chunk moved to server/jobs.py; getsource follows the shim.
    src = inspect.getsource(_load_serve()._publish_chunk)
    # No `except Exception:` followed only by `pass`.
    assert not re.search(
        r"except\s+Exception[^:]*:\s*(?:#[^\n]*\n)?\s*pass\b", src
    ), "_publish_chunk still has a silent broad except"
    # Either logs to [serve] or scopes to a queue exception.
    assert "_stdqueue.Full" in src
    assert "[serve] publish_chunk" in src


def test_reaper_poll_failure_logs():
    """The dead-PID reaper used to swallow ``proc.poll()`` failures
    silently. It must now log the OSError so operators can diagnose
    a stuck job that never gets reaped."""
    # The reaper (_reconcile_running_pids) moved to server/jobs_reaper.py;
    # getsource follows the shim to the new module.
    assert "[serve] reaper poll() failed" in inspect.getsource(
        _load_serve()._reconcile_running_pids
    )


def test_pty_ws_write_resize_close_log_on_failure():
    """The PTY WebSocket handler used to have three bare ``except Exception``
    blocks. After batch 5 they are scoped + logged."""
    # The handler is `_handle_pty_ws` — moved to server/pty_handlers.py, so
    # getsource off the Handler follows it.
    body = inspect.getsource(_load_serve().Handler._handle_pty_ws)
    # No `except Exception:` inside the handler.
    # (We allow the surrounding `_pty_spawn` block in `_handle_pty_create`
    # which already exists outside this handler.)
    bare_exc = re.findall(r"except\s+Exception\s*:", body)
    assert bare_exc == [], (
        f"_handle_pty_ws still has bare `except Exception:`: {bare_exc}"
    )
    # And it logs pty_ws failures.
    assert "[serve] pty_ws write" in body
    assert "[serve] pty_ws resize" in body


# -------- 2. taskkill stderr is captured & logged on non-zero exit --------

def test_taskkill_captures_and_logs_stderr():
    """The job-cancel path on Windows shells out to ``taskkill /F /T /PID``.
    A failure (process gone, ACL) returned silently before — now it must
    log ``rc`` + the stderr tail so operators can tell a stuck cancel
    apart from a clean one."""
    # _cancel_job moved to server/jobs.py; getsource follows the shim.
    block = inspect.getsource(_load_serve()._cancel_job)
    assert '"taskkill", "/F", "/T", "/PID"' in block
    assert "capture_output=True" in block
    # Logging on rc != 0.
    assert "[serve] taskkill rc=" in block
    # Timeout protection — taskkill itself can hang if the process is stuck
    # in kernel mode; add a timeout so we never hang the request thread.
    assert "timeout=10" in block


# -------- 3. git log call has a timeout (verifies batch-4 work survived) --

def test_git_log_excerpt_has_timeout():
    """``_git_log_excerpt`` shells out to ``git log`` — a hung git process
    must not pin the suggester thread."""
    # _git_log_excerpt moved to server/agent_suggest.py; getsource follows the shim.
    body = inspect.getsource(_load_serve()._git_log_excerpt)
    assert "timeout=" in body
    # And it must catch TimeoutExpired so the hang doesn't bubble up.
    assert "subprocess.TimeoutExpired" in body


# -------- 4. JOBS_PERSIST_FILE uses the errors-replace cache reader --------

def test_jobs_persist_file_reads_through_cache_with_errors_replace():
    """JOBS_PERSIST_FILE is JSONL — every reader must go through
    ``_load_jsonl_cached`` which uses ``errors='replace'`` and an
    mtime-invalidated cache."""
    # 1. The cache reader opens with errors="replace". The helper now lives in
    #    server/storage.py (re-exported by serve); inspect.getsource follows it.
    import inspect
    assert 'errors="replace"' in inspect.getsource(_load_serve()._load_jsonl_cached)
    # 2. Every JOBS_PERSIST_FILE *read* site uses _load_jsonl_cached, not
    #    a raw read_text(). (Persistence writes go through .open("a").)
    for line in SRC.splitlines():
        if "JOBS_PERSIST_FILE" not in line:
            continue
        if "read_text" in line:
            pytest.fail(
                f"JOBS_PERSIST_FILE read bypassing cached reader: {line.strip()!r}"
            )


# -------- 5. _run_subprocess uses list args (no shell=True) ----------------

def test_run_subprocess_uses_list_args_no_shell_true():
    """``_run_subprocess`` must take a ``list[str]`` so Windows path
    quoting is handled by the OS, not by Python string concatenation."""
    # _run_subprocess moved to server/workflow_handlers.py; getsource off the
    # Handler follows it (indentation preserved by the move).
    body = inspect.getsource(_load_serve().Handler._run_subprocess)
    assert "args: list[str]" in body
    assert "shell=True" not in body
    # And `subprocess.run(args, ...)` — the first positional is the list.
    assert "subprocess.run(\n                args," in body


def test_no_shell_true_anywhere():
    """Defense in depth: no ``shell=True`` anywhere in serve.py."""
    assert "shell=True" not in SRC


# -------- 6. SSE wall-clock cap on the transcript stream too ---------------

def test_transcript_stream_has_max_session_cap():
    """``_handle_transcript_stream`` previously only bailed on idle ticks.
    Batch 5 adds a hard ``MAX_SSE_SESSION_S`` wall-clock cap so a chatty
    transcript can't pin the request thread forever."""
    # _handle_transcript_stream moved to server/transcripts_handlers.py;
    # getsource off the Handler follows it.
    body = inspect.getsource(_load_serve().Handler._handle_transcript_stream)
    assert "MAX_SSE_SESSION_S" in body
    assert "session_start" in body
    assert '"reason":"max_session"' in body


def test_max_sse_session_s_constant_exists():
    mod = _load_serve()
    assert hasattr(mod, "MAX_SSE_SESSION_S")
    assert mod.MAX_SSE_SESSION_S >= 60  # sane lower bound — anything less is a bug
    assert mod.MAX_SSE_SESSION_S <= 3600  # sane upper bound — never longer than an hour


# -------- 7. Suggestion semaphore wraps both claude/codex endpoints --------

def test_suggestion_semaphore_present_and_used():
    """Both /api/suggestions/<id>/draft and /api/agents/suggest must
    acquire ``_SUGGESTION_SEMAPHORE`` non-blocking and 429 on contention."""
    mod = _load_serve()
    assert hasattr(mod, "_SUGGESTION_SEMAPHORE")
    # The acquire/release sites moved with their handlers into the proposals +
    # agent-suggest mixin modules; scan those module sources (not serve.py).
    import server.agent_suggest_handlers as _agh
    import server.proposals_handlers as _ph
    src = inspect.getsource(_ph) + inspect.getsource(_agh)
    acquires = src.count("_SUGGESTION_SEMAPHORE.acquire(blocking=False)")
    releases = src.count("_SUGGESTION_SEMAPHORE.release()")
    assert acquires >= 2, f"expected ≥2 semaphore acquires, found {acquires}"
    assert releases >= 2, f"expected ≥2 semaphore releases, found {releases}"
    # And each acquire site returns 429 with Retry-After.
    assert src.count('send_response(429)') >= 2
    assert src.count('"Retry-After"') >= 2


# -------- 8. read_text on user-visible files passes errors='replace' -------

@pytest.mark.parametrize(
    "needle",
    [
        # memory.md / decisions.md append handlers — user-content appended.
        '".ai" / "memory.md"',
        '".ai" / "decisions.md"',
        # models.yaml patch endpoints — config file edited from the UI.
        '".ai" / "models.yaml"',
        # workflow version file.
        "WORKFLOW_VERSION_FILE",
        # Skill apply path.
        "skill_path.read_text",
    ],
)
def test_user_visible_read_text_uses_errors_replace(needle):
    """For each user-facing file, every ``read_text(encoding="utf-8")`` call
    in the surrounding context must use ``errors="replace"`` so a stray
    non-UTF-8 byte never 500s a request."""
    if needle not in SRC:
        pytest.skip(f"needle {needle!r} not present in current serve.py")
    # Locate every occurrence and check the next read_text in that block.
    for m in re.finditer(re.escape(needle), SRC):
        window = SRC[m.start() : m.start() + 600]
        m2 = re.search(r"read_text\(encoding=\"utf-8\"([^)]*)\)", window)
        if not m2:
            continue
        suffix = m2.group(1)
        assert "errors=" in suffix and "replace" in suffix, (
            f"read_text near {needle!r} missing errors='replace': {m2.group(0)!r}"
        )


# -------- 9. _json sets X-Content-Type-Options: nosniff --------------------

def test_json_response_has_nosniff_header():
    """API responses are never HTML — set ``X-Content-Type-Options: nosniff``
    so a misconfigured proxy can't MIME-sniff a JSON error message into a
    rendered HTML payload."""
    idx = SRC.find("def _json(self, status: int, payload: dict)")
    assert idx >= 0
    body = SRC[idx : idx + 800]
    assert 'X-Content-Type-Options' in body
    assert 'nosniff' in body


# -------- 10. Module imports cleanly -------------------------------------

def test_serve_module_imports():
    """Sanity: the patched serve.py imports without ImportError /
    SyntaxError. Catches accidentally-introduced typos."""
    mod = _load_serve()
    assert hasattr(mod, "JOBS_PERSIST_FILE")
    assert hasattr(mod, "MAX_JSON_BODY")
