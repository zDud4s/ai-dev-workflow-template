"""Batch-8 regression coverage for serve.py.

What this batch landed:

1. ``pj.write_text`` failure logging at three remaining sites:
     * ``_persist_agent_proposal`` (3607-3614) — used to ``return None``
       on OSError with no operator trace.
     * ``_handle_agent_proposal_decision`` install path (~5338) — used to
       ``except OSError: pass`` silently after the agent .md was created.
     * ``_handle_suggestion_draft`` proposal-triple persistence (~5095) —
       previously unguarded; an OSError would 500 the request without
       context. Now wraps the three writes, logs, and returns a clean
       error response.
     * ``_write_proposal`` (~1516) — was unguarded; raised OSError would
       kill the background improver daemon thread silently. Now logs
       + raises; the caller's wrapper audits the failure into the ledger.

2. ``_run_improver_for_skill`` records a ``"failed"`` audit row when the
   proposal write raises, instead of letting the daemon thread die mid-run.

3. Already-fixed in earlier batches (still validated here as regression
   pins): ``_handle_workflow_update`` non-blocking lock (batch 3), the
   global suggestion semaphore + HTTP timeout cap (batches 3/7), and the
   logging on the apply / reject proposal write paths (batch 3).

Auth/CSRF on state-changing endpoints is intentionally NOT added — that
is documented as accepted residual risk in .ai/memory.md (single trusted
local-user threat model).
"""
from __future__ import annotations

import ast
import importlib.util
import json
import inspect
import re
import sys
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVE_PATH = REPO_ROOT / ".ai" / "dashboard" / "serve.py"
SRC = SERVE_PATH.read_text(encoding="utf-8")

sys.path.insert(0, str(REPO_ROOT / ".ai" / "dashboard"))
import serve  # noqa: E402 — path mangled above
import server.agent_suggest as _ags  # noqa: E402 — _persist_agent_proposal reads AGENT_PROPOSALS_DIR here (follows-the-move)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _function_source(name: str) -> str:
    """Return the source text of one function/method by name.

    Prefers ``inspect.getsource`` of the live attribute (module-level function
    or ``Handler`` method) so functions split out of serve.py into ``server.*``
    modules are still found — they're no longer in ``SRC`` (the serve.py text).
    Falls back to scanning ``SRC`` for anything still defined inline."""
    obj = getattr(serve, name, None) or getattr(serve.Handler, name, None)
    if obj is not None:
        try:
            return inspect.getsource(obj)
        except (OSError, TypeError):
            pass
    needle = f"def {name}("
    idx = SRC.find(needle)
    assert idx >= 0, f"function {name!r} not found in serve.py"
    # Stop at the next blank-line-then-def (any indent) — good-enough scoping
    # for the per-function assertions below.
    tail = SRC[idx:]
    end = re.search(r"\n\n    def |\n\ndef |\n\nclass ", tail)
    return tail[: end.start()] if end else tail


# ---------------------------------------------------------------------------
# 1. write_text failure logging — three previously-silent sites
# ---------------------------------------------------------------------------


def test_persist_agent_proposal_logs_on_oserror():
    """Source-level guard: the OSError branch must log [serve] + the pid,
    not just return None."""
    body = _function_source("_persist_agent_proposal")
    # The except branch is OSError-only (scoped), not bare.
    assert "except OSError" in body
    # And it logs an operator-facing line before returning None.
    assert "[serve] persist_agent_proposal" in body
    assert "return None" in body


def test_persist_agent_proposal_logs_runtime(tmp_path, monkeypatch, capsys):
    """Runtime: redirect AGENT_PROPOSALS_DIR to a path that ``mkdir`` can't
    create (a file masquerading as a dir), force OSError, assert the log
    surfaces."""
    fake_parent = tmp_path / "not-a-dir"
    fake_parent.write_text("conflict", encoding="utf-8")  # file blocks mkdir
    monkeypatch.setattr(serve, "AGENT_PROPOSALS_DIR", fake_parent / "kids")
    monkeypatch.setattr(_ags, "AGENT_PROPOSALS_DIR", fake_parent / "kids")  # follows-the-move

    suggestion = {
        "slug": "test-agent",
        "name": "Test Agent",
        "description": "x",
        "trigger_phrasings": ["x"],
        "rationale": "x",
        "tools": "Bash",
        "confidence": 0.9,
        "body": "# Test\n",
    }
    pid = serve._persist_agent_proposal(suggestion, source_signal={"k": "v"})
    captured = capsys.readouterr()
    assert pid is None
    assert "[serve] persist_agent_proposal" in captured.out


def test_handle_agent_proposal_decision_install_logs_on_oserror():
    """The agent-install proposal-write site used to ``except OSError: pass``
    silently. Now it must log."""
    body = _function_source("_handle_agent_proposal_decision")
    # No bare swallow remaining.
    assert not re.search(
        r"except\s+OSError\s*:\s*(?:#[^\n]*\n)?\s*pass\b", body
    ), "agent install proposal-write still silently passes"
    # Logs with the conventional [serve] prefix.
    assert "[serve] failed to write proposal" in body
    assert "agent installed" in body


def test_handle_suggestion_draft_persists_proposal_triple_with_oserror_guard():
    """The three ``write_text`` calls in ``_handle_suggestion_draft`` must
    be inside a try/except OSError block + log + 500 on failure."""
    body = _function_source("_handle_suggestion_draft")
    # The three write sites still exist (we didn't refactor them away).
    assert body.count(".write_text(") >= 3
    # They are now guarded.
    assert "except OSError as e" in body or "except OSError" in body
    # The catch logs + responds with a clean 500 instead of leaking a
    # parser error to the modal.
    assert "[serve] persist draft proposal" in body
    assert "could not persist draft proposal" in body


def test_write_proposal_logs_and_reraises_on_oserror():
    """``_write_proposal`` is called from a daemon thread; an unguarded
    raise would die silently. The wrapper must log + re-raise so the
    caller can audit a ``failed`` row."""
    body = _function_source("_write_proposal")
    assert "except OSError as e" in body
    assert "[serve] _write_proposal" in body
    # Must re-raise so the caller's outer wrapper still audits + returns.
    assert "\n        raise\n" in body


def test_write_proposal_runtime_raises_oserror(tmp_path, monkeypatch, capsys):
    """End-to-end: redirect SKILL_PROPOSALS_DIR to a real dir but patch
    ``Path.write_text`` on one of the children to raise OSError, call
    ``_write_proposal``, assert the helper logs + re-raises so the
    daemon-thread caller can audit the failure.

    Notes:
      * The function mkdir's its target dir at the top, then computes
        ``skill_path.relative_to(ROOT)`` for the payload — both must
        succeed before the writes that we want to force-fail. Keep the
        skill_path under serve.ROOT to satisfy relative_to.
    """
    proposals_dir = tmp_path / "proposals"
    monkeypatch.setattr(serve, "SKILL_PROPOSALS_DIR", proposals_dir)
    # skill_path must be ROOT-relative — use any tracked file under .ai/.
    skill_path = serve.ROOT / ".ai" / "memory.md"
    if not skill_path.is_file():
        pytest.skip("repo missing .ai/memory.md; cannot satisfy relative_to")

    parsed = {"change_summary": "x", "rationale": "y"}
    # Force write_text to fail. Patch the bound method on Path so every
    # call inside the try-block raises.
    real_write_text = serve.Path.write_text

    def _boom(self, *args, **kwargs):
        if "proposals" in str(self):
            raise OSError("simulated disk-full")
        return real_write_text(self, *args, **kwargs)

    monkeypatch.setattr(serve.Path, "write_text", _boom)
    with pytest.raises(OSError):
        serve._write_proposal("fake-skill", skill_path, "old", "new",
                              parsed, diff_lines=2, job_id="job-1")
    captured = capsys.readouterr()
    assert "[serve] _write_proposal" in captured.out


def test_run_improver_audits_failed_when_write_proposal_raises():
    """``_run_improver_for_skill`` body must catch the OSError from
    ``_write_proposal`` and call ``_audit_improvement(..., "failed", ...)``
    so the daemon thread doesn't die uncaught + the failure shows up in
    the per-skill audit ledger."""
    body = _function_source("_run_improver_for_skill")
    # The new try-block around _write_proposal exists.
    assert "_write_proposal(" in body
    assert "proposal write error" in body, (
        "expected an audit row referencing the write failure"
    )
    # Scoped catch, not bare.
    assert re.search(
        r"except\s+OSError\s+as\s+e\s*:\s*\n\s*#",
        body,
    ) is not None or "except OSError as e:" in body


# ---------------------------------------------------------------------------
# 2. _handle_workflow_update keeps its non-blocking lock (batch-3 pin)
# ---------------------------------------------------------------------------


def test_handle_workflow_update_acquires_non_blocking_lock():
    """Two concurrent /api/workflow/update requests must not both spawn
    update-workflow.sh against the same tree. The lock acquire is
    non-blocking: second caller gets 409. Batch 3 added it; this batch
    pins it as a regression guard."""
    body = _function_source("_handle_workflow_update")
    # The lock object lives at module scope; the method references it.
    assert "_WORKFLOW_UPDATE_LOCK" in body
    assert ".acquire(blocking=False)" in body
    # The refusal path returns 409 with a clear error.
    assert "409" in body
    assert "workflow update already in progress" in body
    # Release happens in a top-level finally so a mid-body exception
    # can't strand the lock.
    assert ".release()" in body


def test_workflow_update_lock_is_module_level():
    """Module-level lock instance; not per-request."""
    assert hasattr(serve, "_WORKFLOW_UPDATE_LOCK")
    # The lock must already be acquired by one holder for the test (we
    # acquire then release to prove it's a real Lock).
    assert serve._WORKFLOW_UPDATE_LOCK.acquire(blocking=False)
    serve._WORKFLOW_UPDATE_LOCK.release()


# ---------------------------------------------------------------------------
# 3. Suggestion subprocess slot + timeout cap (batches 3/7 pins)
# ---------------------------------------------------------------------------


def test_suggestion_endpoints_share_semaphore_and_timeout_cap():
    """Both /api/suggestions/<id>/draft and /api/agents/suggest must
    acquire ``_SUGGESTION_SEMAPHORE`` (non-blocking, 429 on saturation)
    AND cap subprocess wall-clock at ``_SUGGESTION_HTTP_TIMEOUT_MAX``
    so a long ``cfg['timeout_seconds']`` can't pin a request thread."""
    draft = _function_source("_handle_suggestion_draft")
    suggest = _function_source("_handle_agent_suggest")

    for body, label in ((draft, "draft"), (suggest, "agent suggest")):
        assert "_SUGGESTION_SEMAPHORE.acquire(blocking=False)" in body, (
            f"{label} missing semaphore acquire"
        )
        assert "429" in body, f"{label} doesn't 429 on saturation"
        assert "_SUGGESTION_HTTP_TIMEOUT_MAX" in body, (
            f"{label} doesn't cap subprocess timeout"
        )
        assert "_SUGGESTION_SEMAPHORE.release()" in body, (
            f"{label} doesn't release the slot"
        )


def test_suggestion_http_timeout_cap_is_module_level():
    """The cap constant exists and is bounded (60s)."""
    assert hasattr(serve, "_SUGGESTION_HTTP_TIMEOUT_MAX")
    assert serve._SUGGESTION_HTTP_TIMEOUT_MAX == 60


# ---------------------------------------------------------------------------
# 4. Apply / reject proposal write paths still log on OSError (batch-3 pins)
# ---------------------------------------------------------------------------


def test_apply_improvement_logs_on_proposal_write_failure():
    """``_apply_improvement`` writes back to ``pj`` after a successful
    SKILL.md replace; an OSError there must NOT silently abandon the
    proposal-status drift — log it."""
    body = _function_source("_apply_improvement")
    assert "pj.write_text" in body
    assert "[serve] failed to write proposal" in body
    assert "apply" in body  # the log context tag


def test_handle_proposal_decision_reject_logs_on_oserror():
    """The reject path must surface OSError as both a 500 response AND
    a [serve] log line."""
    body = _function_source("_handle_proposal_decision")
    # The reject branch is the first ``pj.write_text`` inside the function.
    assert "[serve] failed to write proposal" in body
    assert "reject" in body  # appears in the log/contextual messages


def test_auto_revert_skill_logs_on_proposal_write_failure():
    """The auto-revert path writes a status="rolled_back" back to ``pj``
    after restoring SKILL.md from .bak; an OSError there leaves the
    proposal stale on disk. Must log."""
    body = _function_source("_auto_revert_skill")
    assert "[serve] failed to write proposal" in body
    assert "rollback" in body


# ---------------------------------------------------------------------------
# 5. No bare except: anywhere in serve.py (batch-5 invariant pin)
# ---------------------------------------------------------------------------


def test_no_bare_except_in_serve():
    """Bare ``except:`` swallows KeyboardInterrupt / SystemExit; forbidden."""
    tree = ast.parse(SRC)
    bare = [
        node.lineno for node in ast.walk(tree)
        if isinstance(node, ast.ExceptHandler) and node.type is None
    ]
    assert bare == [], f"bare except: found at lines {bare}"


# ---------------------------------------------------------------------------
# 6. All write_text sites in serve.py either use try/except OR are inside
#    one (helper-level safety net) — batch-8 invariant pin
# ---------------------------------------------------------------------------


def test_all_write_text_sites_have_oserror_handler_in_scope():
    """Every ``write_text`` call in serve.py must either:
      * be inside a try/except OSError block (function-local), OR
      * be the inner call of ``_write_text_lf`` which is itself wrapped at
        each call site (the LF helper has no business swallowing).

    Method: walk the AST, find every ``Call`` whose func.attr is
    ``write_text``, and assert that one of its ancestors in the function
    body is a ``Try`` node whose handlers include OSError (directly or as
    part of a tuple) or Exception (logged elsewhere by separate tests).
    """
    tree = ast.parse(SRC)

    # Map node id -> parent for ancestor walks.
    parents: dict[int, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[id(child)] = node

    def _ancestors(node):
        cur = parents.get(id(node))
        while cur is not None:
            yield cur
            cur = parents.get(id(cur))

    def _is_oserror_handler(handler: ast.ExceptHandler) -> bool:
        t = handler.type
        if t is None:
            return False
        if isinstance(t, ast.Name) and t.id in {"OSError", "Exception"}:
            return True
        if isinstance(t, ast.Tuple):
            return any(
                isinstance(elt, ast.Name) and elt.id in {"OSError", "Exception"}
                for elt in t.elts
            )
        return False

    unguarded: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "write_text"):
            continue
        guarded = False
        for anc in _ancestors(node):
            if isinstance(anc, ast.Try) and any(
                _is_oserror_handler(h) for h in anc.handlers
            ):
                guarded = True
                break
            # _write_text_lf is itself called from inside guarded blocks
            # at every site; if we hit a FunctionDef boundary first, the
            # caller is responsible.
            if isinstance(anc, ast.FunctionDef) and anc.name == "_write_text_lf":
                guarded = True
                break
        if not guarded:
            unguarded.append(node.lineno)

    assert not unguarded, (
        f"unguarded write_text sites at lines {unguarded} — wrap in "
        f"try/except OSError + log on failure"
    )
