"""Tests for the skill-improver fixes (Bugs 1-5 in the original review).

The auto skill-improver used to do nothing useful because:
  1. The LLM prompt was hard-wired to "be conservative, need clear log
     evidence" — single-job logs rarely contain such evidence, so the
     ledger filled with no_change rows.
  2. The improver only fired for skills that a completed job actually
     used — skills the user never invoked never got audited.
  3. Telemetry signal was reduced to aggregate success_rate; trend
     across recent invocations was invisible to the model.
  4. There was no manual "improve this skill now" trigger — users
     waited for a job-triggered run that might never come for this
     skill.
  5. The per-skill throttle blocked manual triggers too, so even if the
     user fired a manual run twice in a row the second was silently
     dropped.

This file pins the five fixes:
  * _build_improver_prompt now takes ``manual=False`` + ``recent_outcomes``
    and emits the structural-audit role + a "RECENT_OUTCOMES" line.
  * _run_improver_for_skill threads ``manual`` through to audits with
    ``source="manual"`` and returns a result dict so callers can read
    the outcome inline.
  * _periodic_improver_sweep exists, picks oldest skills first, respects
    sweep_batch_max, and uses ``manual=True``.
  * Handler ``_handle_skill_improve_now`` is wired to
    ``POST /api/skills/<name>/improve`` and bypasses the throttle.
  * The "Improve now" button in skills.js POSTs to the new endpoint and
    is gated to project-source skills only.
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import os
import re
import signal
import socket
import socketserver
import sys
import threading
import time
import urllib.error
import urllib.request
from http.client import HTTPResponse
from pathlib import Path
from urllib.parse import urlparse

import pytest

import server.runtime as _runtime  # noqa: E402 — BOUND_PORT + Origin allowlist live here (follows-the-move)


REPO_ROOT = Path(__file__).resolve().parent.parent
SERVE_PATH = REPO_ROOT / ".ai" / "dashboard" / "serve.py"
SKILLS_JS = REPO_ROOT / ".ai" / "dashboard" / "app" / "skills.js"
INDEX_HTML = REPO_ROOT / ".ai" / "dashboard" / "index.html"
SRC = SERVE_PATH.read_text(encoding="utf-8")


# Borrow the same "fresh module instance" trick from test_agent_suggestions.py
# so a per-test monkeypatch of ROOT / SKILL_PROPOSALS_DIR doesn't bleed into
# the cached `import serve` in other test modules.
@pytest.fixture(scope="module")
def serve_module():
    spec = importlib.util.spec_from_file_location("dashboard_serve_improver", SERVE_PATH)
    assert spec and spec.loader, "could not load serve.py"
    mod = importlib.util.module_from_spec(spec)
    sys.modules["dashboard_serve_improver"] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _function_source(name: str) -> str:
    """Return the source text of one function/method by name.

    Follows re-export shims: functions split out of serve.py into
    ``server.*`` modules are no longer in ``SRC`` (the serve.py text), so we
    prefer ``inspect.getsource`` of the live attribute (which resolves to the
    defining module) and fall back to scanning ``SRC`` for anything still
    defined inline in serve.py."""
    _serve = _serve_for_source()
    # Module-level function or a Handler method (handlers were split into
    # server/handlers/*.py mixins; getsource follows them off Handler).
    obj = getattr(_serve, name, None) or getattr(_serve.Handler, name, None)
    if obj is not None:
        try:
            return inspect.getsource(obj)
        except (OSError, TypeError):
            pass
    needle = f"def {name}("
    idx = SRC.find(needle)
    assert idx >= 0, f"function {name!r} not found in serve.py"
    tail = SRC[idx:]
    end = re.search(r"\n\n    def |\n\ndef |\n\nclass ", tail)
    return tail[: end.start()] if end else tail


_SERVE_FOR_SOURCE = None


def _serve_for_source():
    """Lazily import serve once for getsource-based source lookups."""
    global _SERVE_FOR_SOURCE
    if _SERVE_FOR_SOURCE is None:
        spec = importlib.util.spec_from_file_location("dashboard_serve_src", SERVE_PATH)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["dashboard_serve_src"] = mod
        spec.loader.exec_module(mod)
        _SERVE_FOR_SOURCE = mod
    return _SERVE_FOR_SOURCE


def _patch_attr(monkeypatch, serve_module, name, value):
    """setattr ``name``=``value`` on serve_module AND every loaded ``server.*``
    submodule that binds its own copy of ``name``.

    The improver helpers were split out of serve.py into ``server.skills.tree`` /
    ``server.improver_io`` / ``server.improver``; each did ``from server.paths
    import <CONST>`` (or holds a re-exported function), so a function that moved
    out resolves the name in its new module's namespace. Patching only
    ``serve.<name>`` would leave those copies pointing at the real value
    (follows-the-move)."""
    if hasattr(serve_module, name):
        monkeypatch.setattr(serve_module, name, value, raising=False)
    for modname, mod in list(sys.modules.items()):
        if (modname == "server" or modname.startswith("server.")) and mod is not None and hasattr(mod, name):
            monkeypatch.setattr(mod, name, value, raising=False)


def _patch_root(monkeypatch, serve_module, tmp_path):
    """Point ROOT at ``tmp_path`` everywhere (see :func:`_patch_attr`)."""
    _patch_attr(monkeypatch, serve_module, "ROOT", tmp_path)


def _skills_js() -> str:
    return SKILLS_JS.read_text(encoding="utf-8")


def _index_html() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


# ===========================================================================
# Bug 1+3: structural prompt + recent-outcomes telemetry
# ===========================================================================


def test_build_improver_prompt_accepts_manual_kwarg(serve_module):
    """The signature must accept ``manual`` and ``recent_outcomes`` so the
    sweep + manual endpoint can request the structural-audit variant."""
    import inspect
    sig = inspect.signature(serve_module._build_improver_prompt)
    assert "manual" in sig.parameters
    assert "recent_outcomes" in sig.parameters
    # Both must be keyword-only so positional callers don't accidentally
    # toggle the audit mode by argument order.
    assert sig.parameters["manual"].kind == inspect.Parameter.KEYWORD_ONLY
    assert sig.parameters["recent_outcomes"].kind == inspect.Parameter.KEYWORD_ONLY


def test_build_improver_prompt_manual_selects_structural_role(serve_module):
    """With manual=True the role text must invite structural fixes, NOT
    instruct the model to ``be conservative — most invocations need no
    change`` (that line was the root cause of the no-change avalanche)."""
    prompt = serve_module._build_improver_prompt(
        "test-skill", "fake skill body", {}, job_id=None,
        log_excerpt="", manual=True, recent_outcomes=[],
    )
    # Structural rubric is named:
    assert "auditing a project skill STRUCTURALLY" in prompt
    assert "Description quality" in prompt
    assert "Output format declared" in prompt
    assert "Tool allowlist" in prompt
    # And the old "be conservative — most invocations need no change"
    # bias is GONE in this mode:
    assert "Be conservative" not in prompt
    assert "most invocations need no change" not in prompt
    # The mode is announced so the model knows it.
    assert "manual structural audit" in prompt


def test_build_improver_prompt_recent_outcomes_emitted(serve_module):
    """The compact ``done, failed, ...`` line must appear so the model
    can spot a recent failure cluster without grinding through telemetry."""
    recent = [
        {"outcome": "done"}, {"outcome": "failed"}, {"outcome": "failed"},
        {"outcome": "failed"}, {"outcome": "done"},
    ]
    prompt = serve_module._build_improver_prompt(
        "x", "body", {}, "job-1", "log", manual=False, recent_outcomes=recent,
    )
    assert "RECENT_OUTCOMES" in prompt
    # Order preserved (newest-first responsibility belongs to the caller).
    assert "done, failed, failed, failed, done" in prompt


def test_build_improver_prompt_manual_off_keeps_log_based_signal(serve_module):
    """The auto (post-job) variant must still let the model use log
    evidence — we didn't want to break job-triggered runs entirely, just
    let them also lean on the recent-outcomes line."""
    prompt = serve_module._build_improver_prompt(
        "x", "body", {}, "job-1", "ERROR: schema mismatch",
        manual=False, recent_outcomes=[],
    )
    assert "post-job review" in prompt
    # The log excerpt block is intact.
    assert "ERROR: schema mismatch" in prompt


# ===========================================================================
# Bug 2: periodic batch sweep
# ===========================================================================


def test_periodic_improver_sweep_exists(serve_module):
    """The sweep entrypoint is callable from outside the loop (so tests +
    a future on-demand endpoint can drive it without spawning the
    daemon)."""
    assert callable(getattr(serve_module, "_periodic_improver_sweep", None))


def test_periodic_improver_loop_exists_and_is_launched(serve_module):
    """The daemon function is defined AND ``main`` launches a thread
    targeting it. Without the launch the sweep never runs in production
    even if the helper is shippable."""
    assert callable(getattr(serve_module, "_periodic_improver_loop", None))
    main_src = _function_source("main")
    assert "_periodic_improver_loop" in main_src
    assert 'name="improver-sweep"' in main_src
    assert "daemon=True" in main_src


def test_improver_defaults_carry_sweep_fields(serve_module):
    """Two new defaults must exist so config + sweep behaviour are
    deterministic across fresh installs."""
    defaults = serve_module._IMPROVER_DEFAULTS
    assert "sweep_interval_seconds" in defaults
    assert "sweep_batch_max" in defaults
    # 6h default is the documented value.
    assert defaults["sweep_interval_seconds"] == 21600
    assert defaults["sweep_batch_max"] == 4


def test_load_improver_config_parses_sweep_fields(serve_module, tmp_path, monkeypatch):
    """A models.yaml with sweep_interval_seconds + sweep_batch_max must
    flow through ``_load_improver_config`` (it used to silently drop
    unknown fields)."""
    yml = tmp_path / ".ai" / "models.yaml"
    yml.parent.mkdir(parents=True, exist_ok=True)
    yml.write_text(
        "improver:\n"
        "  enabled: true\n"
        "  sweep_interval_seconds: 60\n"
        "  sweep_batch_max: 2\n",
        encoding="utf-8",
    )
    _patch_root(monkeypatch, serve_module, tmp_path)
    # Clear the env var that conftest sets so we actually exercise the
    # YAML overlay path — the env shortcut returns enabled=False without
    # parsing anything else.
    monkeypatch.delenv("AI_WORKFLOW_DISABLE_IMPROVER", raising=False)
    cfg = serve_module._load_improver_config()
    assert cfg["sweep_interval_seconds"] == 60
    assert cfg["sweep_batch_max"] == 2


def test_sweep_visits_oldest_skills_first_and_respects_batch_cap(serve_module, tmp_path, monkeypatch, capsys):
    """Three project skills with staggered last-run timestamps + batch
    cap of 2 → the two OLDEST skills get audited; the youngest is
    skipped with reason ``over-batch-cap``."""

    # Build a fake project skill index.
    skills_root = tmp_path / ".claude" / "skills"
    (skills_root / "alpha").mkdir(parents=True)
    (skills_root / "beta").mkdir(parents=True)
    (skills_root / "gamma").mkdir(parents=True)
    for name in ("alpha", "beta", "gamma"):
        (skills_root / name / "SKILL.md").write_text(
            f"---\nname: {name}\n---\n# body\n", encoding="utf-8")

    _patch_root(monkeypatch, serve_module, tmp_path)
    # Force the project-skill index to return our three.
    proj = {
        "alpha": skills_root / "alpha" / "SKILL.md",
        "beta": skills_root / "beta" / "SKILL.md",
        "gamma": skills_root / "gamma" / "SKILL.md",
    }
    _patch_attr(monkeypatch, serve_module, "_project_skill_index", lambda: proj)

    # Stagger last-run timestamps so we know the expected ordering.
    # alpha = oldest (1000), beta = middle (2000), gamma = newest (3000).
    timestamps = {"alpha": 1000.0, "beta": 2000.0, "gamma": 3000.0}
    _patch_attr(
        monkeypatch, serve_module, "_last_improver_run_ts",
        lambda sid: timestamps.get(sid, 0.0),
    )
    # Throttle disabled so every skill is candidate.
    # Time = 10000 puts all three well past the throttle window.
    monkeypatch.setattr(serve_module.time, "time", lambda: 10000.0)
    # Improver tool is present.
    _patch_attr(monkeypatch, serve_module, "_safe_which", lambda _t: "/fake/bin")

    audited_calls = []

    def fake_run(skill_id, *_args, **kwargs):
        audited_calls.append((skill_id, kwargs.get("manual", False)))
        return {"status": "no_change", "reason": "fake"}

    _patch_attr(monkeypatch, serve_module, "_run_improver_for_skill", fake_run)

    cfg = {"enabled": True, "tool": "claude", "model": "x",
           "min_interval_seconds": 100, "sweep_batch_max": 2}
    out = serve_module._periodic_improver_sweep(cfg)

    audited_names = [c[0] for c in audited_calls]
    assert audited_names == ["alpha", "beta"], (
        f"sweep should audit oldest two, got {audited_names!r}")
    # Every audit was structural (manual=True).
    assert all(c[1] is True for c in audited_calls)
    # The cap-skipped skill is reported in `skipped` so an operator
    # can see partial coverage.
    skipped_names = [s["skill"] for s in out["skipped"]
                     if s["reason"] == "over-batch-cap"]
    assert skipped_names == ["gamma"]


def test_sweep_respects_throttle(serve_module, tmp_path, monkeypatch):
    """A skill whose last improver run is INSIDE the throttle window
    must NOT be audited by the sweep (the operator already saw a recent
    result; sweeping again would just burn an LLM call)."""
    skills_root = tmp_path / ".claude" / "skills"
    (skills_root / "alpha").mkdir(parents=True)
    (skills_root / "alpha" / "SKILL.md").write_text("# body\n", encoding="utf-8")
    _patch_root(monkeypatch, serve_module, tmp_path)
    _patch_attr(
        monkeypatch, serve_module, "_project_skill_index",
        lambda: {"alpha": skills_root / "alpha" / "SKILL.md"},
    )
    # alpha was audited 30s ago. With min_interval_seconds=300 it must
    # be skipped.
    _patch_attr(monkeypatch, serve_module, "_last_improver_run_ts", lambda _sid: 9970.0)
    monkeypatch.setattr(serve_module.time, "time", lambda: 10000.0)
    _patch_attr(monkeypatch, serve_module, "_safe_which", lambda _t: "/fake/bin")

    called = []
    _patch_attr(
        monkeypatch, serve_module, "_run_improver_for_skill",
        lambda *a, **k: called.append(a) or {"status": "no_change"},
    )
    cfg = {"enabled": True, "tool": "claude", "model": "x",
           "min_interval_seconds": 300, "sweep_batch_max": 4}
    out = serve_module._periodic_improver_sweep(cfg)
    assert called == []
    reasons = {s["skill"]: s["reason"] for s in out["skipped"]}
    assert reasons.get("alpha") == "throttled"


# ===========================================================================
# Bug 4+5: manual endpoint + throttle bypass
# ===========================================================================


def test_manual_improve_route_wired(serve_module):
    """The dispatcher's ``do_POST`` must match
    ``/api/skills/<name>/improve`` and call ``_handle_skill_improve_now``."""
    body = _function_source("do_POST")
    assert "/api/skills/" in body
    assert "/improve" in body
    assert "_handle_skill_improve_now" in body


def test_manual_improve_handler_skips_throttle(serve_module):
    """The handler must call ``_run_improver_for_skill`` with
    ``manual=True`` and must NOT consult ``_last_improver_run_ts``
    (that's the throttle gate the manual trigger explicitly bypasses)."""
    body = _function_source("_handle_skill_improve_now")
    assert "_run_improver_for_skill" in body
    assert "manual=True" in body
    assert "_last_improver_run_ts" not in body, (
        "manual improve must NOT honour the per-skill throttle — the "
        "operator is asking explicitly"
    )


def test_manual_improve_handler_uses_semaphore(serve_module):
    """The handler must share the suggestion semaphore so a flood of
    "Improve now" clicks can't exhaust the request thread pool."""
    body = _function_source("_handle_skill_improve_now")
    assert "_SUGGESTION_SEMAPHORE.acquire(blocking=False)" in body
    assert "429" in body
    assert "_SUGGESTION_SEMAPHORE.release()" in body
    # Subprocess wall-clock is capped (same as /draft).
    assert "_SUGGESTION_HTTP_TIMEOUT_MAX" in body


def test_manual_improve_404_when_skill_not_project_scope(serve_module, tmp_path, monkeypatch):
    """Calling the handler with a skill that isn't in the project index
    must return 404 — plugin / user-scope skills are read-only and the
    endpoint refuses to touch them."""
    _patch_attr(monkeypatch, serve_module, "_project_skill_index", lambda: {})

    # Stub out _safe_which so the disabled-tool branch doesn't shadow.
    _patch_attr(monkeypatch, serve_module, "_safe_which", lambda _t: "/fake/bin")
    # Force enabled=True regardless of the conftest env var.
    _patch_attr(
        monkeypatch, serve_module, "_load_improver_config",
        lambda: {"enabled": True, "tool": "claude", "model": "x",
                 "timeout_seconds": 60},
    )

    captured = {}

    class _FakeHandler:
        def _json(self, code, payload):
            captured["code"] = code
            captured["payload"] = payload

    serve_module.Handler._handle_skill_improve_now(
        _FakeHandler(), "ghost-skill",
    )
    assert captured["code"] == 404
    assert "not found" in (captured["payload"].get("error") or "").lower()


# ===========================================================================
# Integration: end-to-end POST /api/skills/<name>/improve
# ===========================================================================


@pytest.fixture
def running_server(serve_module):
    """Start the dashboard HTTP server on an ephemeral port. Same pattern
    as the agent-suggestions tests."""
    httpd = socketserver.ThreadingTCPServer(
        ("127.0.0.1", 0), serve_module.Handler)
    port = httpd.server_address[1]
    original_port = serve_module.PORT
    original_bound = serve_module.BOUND_PORT
    original_runtime_bound = _runtime.BOUND_PORT
    serve_module.PORT = port
    serve_module.BOUND_PORT = port
    # _origin_allowed reads BOUND_PORT from server.runtime's namespace now.
    _runtime.BOUND_PORT = port
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        serve_module.PORT = original_port
        serve_module.BOUND_PORT = original_bound
        _runtime.BOUND_PORT = original_runtime_bound
        httpd.shutdown()
        httpd.server_close()


def _http(method: str, url: str) -> tuple[int, dict]:
    headers = {}
    if method.upper() in {"POST", "PUT", "PATCH", "DELETE"}:
        parsed = urlparse(url)
        headers["Origin"] = f"{parsed.scheme}://{parsed.netloc}"
    req = urllib.request.Request(url, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def test_post_improve_returns_inline_status(
    serve_module, running_server, tmp_path, monkeypatch,
):
    """End-to-end: a project skill exists, POST hits the endpoint,
    monkeypatched subprocess returns a fake "no_change" JSON, response
    has status + 200."""
    # Set up one fake project skill under a tmp ROOT.
    skills_root = tmp_path / ".claude" / "skills" / "fake-skill"
    skills_root.mkdir(parents=True)
    (skills_root / "SKILL.md").write_text(
        "---\nname: fake-skill\n---\n# body\n", encoding="utf-8")
    _patch_root(monkeypatch, serve_module, tmp_path)
    _patch_attr(
        monkeypatch, serve_module, "_project_skill_index",
        lambda: {"fake-skill": skills_root / "SKILL.md"},
    )
    _patch_attr(monkeypatch, serve_module, "_safe_which", lambda _t: "/fake/bin")
    # Bypass the conftest env-var disable so the handler proceeds.
    _patch_attr(
        monkeypatch, serve_module, "_load_improver_config",
        lambda: {
            "enabled": True, "tool": "claude", "model": "x",
            "timeout_seconds": 60, "small_change_max_lines": 6,
            "min_interval_seconds": 300,
        },
    )

    # Stub subprocess.run so no real LLM is invoked.
    class _FakeProc:
        returncode = 0
        stdout = json.dumps({
            "change_summary": "none",
            "rationale": "looks fine",
            "new_content": None,
        })
        stderr = ""

    monkeypatch.setattr(
        serve_module.subprocess, "run",
        lambda *a, **k: _FakeProc(),
    )

    code, payload = _http("POST", f"{running_server}/api/skills/fake-skill/improve")
    assert code == 200, payload
    assert payload["ok"] is True
    assert payload["skill"] == "fake-skill"
    assert payload["status"] == "no_change"
    # No proposal generated for no_change.
    assert payload.get("proposal_id") is None


def test_post_improve_unknown_skill_404(
    serve_module, running_server, tmp_path, monkeypatch,
):
    """Manual improve for a skill that isn't in the project index → 404."""
    _patch_attr(monkeypatch, serve_module, "_project_skill_index", lambda: {})
    _patch_attr(monkeypatch, serve_module, "_safe_which", lambda _t: "/fake/bin")
    _patch_attr(
        monkeypatch, serve_module, "_load_improver_config",
        lambda: {"enabled": True, "tool": "claude", "model": "x",
                 "timeout_seconds": 60},
    )
    code, payload = _http("POST", f"{running_server}/api/skills/ghost/improve")
    assert code == 404
    assert "not found" in payload["error"].lower()


def test_post_improve_generates_proposal_when_diff_non_empty(
    serve_module, running_server, tmp_path, monkeypatch,
):
    """When the model returns a real new SKILL.md, the handler must
    create a proposal AND surface its id inline so the UI can jump
    straight to the diff modal."""
    skill_dir = tmp_path / ".claude" / "skills" / "fake-skill"
    skill_dir.mkdir(parents=True)
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text("---\nname: fake-skill\n---\n# old\n", encoding="utf-8")

    _patch_root(monkeypatch, serve_module, tmp_path)
    _patch_attr(
        monkeypatch, serve_module, "SKILL_PROPOSALS_DIR",
        tmp_path / ".ai" / "dashboard" / "skill_proposals",
    )
    _patch_attr(
        monkeypatch, serve_module, "IMPROVEMENTS_LEDGER",
        tmp_path / ".ai" / "dashboard" / "improvements.jsonl",
    )
    _patch_attr(
        monkeypatch, serve_module, "_project_skill_index",
        lambda: {"fake-skill": skill_md},
    )
    _patch_attr(monkeypatch, serve_module, "_safe_which", lambda _t: "/fake/bin")
    # Manual triggers MUST never auto-apply regardless of small_change_max_lines
    # (the operator clicked Improve so they want to review). Set the threshold
    # high to confirm we don't accidentally rely on it.
    _patch_attr(
        monkeypatch, serve_module, "_load_improver_config",
        lambda: {
            "enabled": True, "tool": "claude", "model": "x",
            "timeout_seconds": 60, "small_change_max_lines": 999,
            "min_interval_seconds": 300,
        },
    )

    new_skill = ("---\nname: fake-skill\n---\n# new\n"
                 "\n## Output format\n\nReturns a JSON object.\n")

    class _FakeProc:
        returncode = 0
        stdout = json.dumps({
            "change_summary": "add output-format section",
            "rationale": "rubric C2 (output format) missing",
            "new_content": new_skill,
        })
        stderr = ""

    monkeypatch.setattr(
        serve_module.subprocess, "run",
        lambda *a, **k: _FakeProc(),
    )

    code, payload = _http("POST", f"{running_server}/api/skills/fake-skill/improve")
    assert code == 200, payload
    assert payload["status"] == "pending"
    assert payload["proposal_id"]
    assert payload["diff_lines"] > 0
    # Proposal file actually exists on disk.
    pj = (tmp_path / ".ai" / "dashboard" / "skill_proposals"
          / f"{payload['proposal_id']}.json")
    assert pj.is_file()


def test_manual_never_auto_applies_even_for_small_diff(
    serve_module, tmp_path, monkeypatch,
):
    """Regression for the UX bug observed in the wild: the user clicked
    "Improve now", the model returned a 4-line delta, and the change was
    silently auto-applied because diff_lines <= small_change_max_lines.
    The operator never got to click Accept. Manual triggers MUST always
    return ``status="pending"`` and leave the SKILL.md untouched until
    the user accepts the proposal."""
    skill_dir = tmp_path / ".claude" / "skills" / "fake-skill"
    skill_dir.mkdir(parents=True)
    skill_md = skill_dir / "SKILL.md"
    original_body = "---\nname: fake-skill\n---\n# old body\n"
    skill_md.write_text(original_body, encoding="utf-8")

    _patch_root(monkeypatch, serve_module, tmp_path)
    _patch_attr(
        monkeypatch, serve_module, "SKILL_PROPOSALS_DIR",
        tmp_path / ".ai" / "dashboard" / "skill_proposals",
    )
    _patch_attr(
        monkeypatch, serve_module, "IMPROVEMENTS_LEDGER",
        tmp_path / ".ai" / "dashboard" / "improvements.jsonl",
    )
    _patch_attr(monkeypatch, serve_module, "_safe_which", lambda _t: "/fake/bin")
    _patch_attr(
        monkeypatch, serve_module, "_aggregate_skill_metrics",
        lambda: {"fake-skill": {"recent": []}},
    )
    # Tiny threshold that the diff WOULD satisfy if manual respected it.
    cfg = {
        "enabled": True, "tool": "claude", "model": "x",
        "timeout_seconds": 60, "small_change_max_lines": 99,
    }
    # 2-line delta — well inside the small_change_max_lines window.
    new_body = "---\nname: fake-skill\n---\n# new body\n## Output format\nReturns JSON\n"

    class _FakeProc:
        returncode = 0
        stdout = json.dumps({
            "change_summary": "add output format",
            "rationale": "rubric miss",
            "new_content": new_body,
        })
        stderr = ""

    monkeypatch.setattr(
        serve_module.subprocess, "run",
        lambda *a, **k: _FakeProc(),
    )

    result = serve_module._run_improver_for_skill(
        "fake-skill", skill_md, job_id=None, log_path=None,
        cfg=cfg, manual=True,
    )

    # Manual must NEVER auto-apply — even when the delta is well under
    # small_change_max_lines.
    assert result["status"] == "pending", (
        f"manual auto-applied a small diff — operator should have to "
        f"click Accept. Got {result!r}"
    )
    # The SKILL.md on disk is UNCHANGED — only the proposal directory got
    # a triple. Until the user clicks Accept the live skill is intact.
    assert skill_md.read_text(encoding="utf-8") == original_body, (
        "Manual improve mutated SKILL.md without user approval"
    )
    # And there's a proposal file the UI can render for review.
    assert result.get("proposal_id")


def test_auto_path_still_auto_applies_for_small_diff(
    serve_module, tmp_path, monkeypatch,
):
    """The auto/job-triggered path keeps its small-diff auto-apply
    shortcut — manual=False is the legacy behaviour and we didn't want
    to break the existing "tiny tweak applied without prompting"
    pipeline."""
    skill_dir = tmp_path / ".claude" / "skills" / "fake-skill"
    skill_dir.mkdir(parents=True)
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text("---\nname: fake-skill\n---\n# v1\n", encoding="utf-8")

    _patch_root(monkeypatch, serve_module, tmp_path)
    _patch_attr(
        monkeypatch, serve_module, "SKILL_PROPOSALS_DIR",
        tmp_path / ".ai" / "dashboard" / "skill_proposals",
    )
    _patch_attr(
        monkeypatch, serve_module, "SKILL_BACKUPS_DIR",
        tmp_path / ".ai" / "dashboard" / "skill_backups",
    )
    _patch_attr(
        monkeypatch, serve_module, "IMPROVEMENTS_LEDGER",
        tmp_path / ".ai" / "dashboard" / "improvements.jsonl",
    )
    _patch_attr(monkeypatch, serve_module, "_safe_which", lambda _t: "/fake/bin")
    _patch_attr(
        monkeypatch, serve_module, "_aggregate_skill_metrics",
        lambda: {"fake-skill": {"recent": []}},
    )
    cfg = {
        "enabled": True, "tool": "claude", "model": "x",
        "timeout_seconds": 60, "small_change_max_lines": 99,
    }
    new_body = "---\nname: fake-skill\n---\n# v2\n"

    class _FakeProc:
        returncode = 0
        stdout = json.dumps({
            "change_summary": "fix",
            "rationale": "x",
            "new_content": new_body,
        })
        stderr = ""

    monkeypatch.setattr(
        serve_module.subprocess, "run",
        lambda *a, **k: _FakeProc(),
    )
    result = serve_module._run_improver_for_skill(
        "fake-skill", skill_md, job_id="j-1", log_path=None,
        cfg=cfg, manual=False,
    )
    assert result["status"] == "applied"
    # File on disk has the new content (the auto-apply path ran).
    assert skill_md.read_text(encoding="utf-8") == new_body


# ===========================================================================
# UX: button gating — hide for healthy skills, show for new / unhealthy
# ===========================================================================


def test_skills_js_button_hidden_for_healthy_skills():
    """100% success rate over a meaningful sample → the button should be
    hidden (running the improver would burn LLM cost for almost-
    guaranteed no_change). New skills with no telemetry still show the
    button so the operator can do a first-pass audit."""
    js = _skills_js()
    # The gate keys on success_rate >= IMPROVE_HEALTHY_RATE AND total_jobs >=
    # IMPROVE_SUFFICIENT_SAMPLE.
    assert "IMPROVE_HEALTHY_RATE" in js
    assert "IMPROVE_SUFFICIENT_SAMPLE" in js
    # The helper consults metrics.total_jobs and metrics.success_rate.
    assert "metricsObj.total_jobs" in js
    assert "metricsObj.success_rate" in js


def test_skills_js_improve_action_helper_exists():
    """The helper that does the gating is named + reused by openSkillDetail
    twice: once in the synchronous prelude (defaults to hidden) and once
    after metrics arrive (final decision). Without this two-phase
    setup the button would flicker visible-then-hidden on every open."""
    js = _skills_js()
    assert "function _setImproveAction(" in js
    # Synchronous prelude call — null for skillName means "no metrics yet".
    assert "_setImproveAction(null, source, null)" in js
    # Post-metrics call.
    assert re.search(r"_setImproveAction\(name,\s*source,\s*metricsObj\)", js)


# ===========================================================================
# Bug 4 (UI): "Improve now" button gated to project scope, POSTs new route
# ===========================================================================


def test_index_html_has_improve_now_footer():
    """The skill-detail modal carries an actions footer with the new
    button. Without this the JS click handler has nothing to bind to."""
    html = _index_html()
    assert 'id="skill-detail-actions"' in html
    assert 'id="skill-detail-improve"' in html
    assert "Improve now" in html


def test_skills_js_has_trigger_improve_now():
    """The button handler POSTs to the new endpoint."""
    js = _skills_js()
    # Function exists.
    assert re.search(r"async function triggerImproveNow\s*\(", js)
    # POSTs to the new route.
    assert "/api/skills/${encodeURIComponent(skillName)}/improve" in js
    assert 'method: "POST"' in js


def test_skills_js_button_gated_to_project_source():
    """Only project-source skills get a wired click handler — for
    plugin / user-global skills the button is hidden (the backend
    refuses to edit them anyway, so a click would 404 and confuse the
    user). The gate lives inside ``_setImproveAction``."""
    js = _skills_js()
    # The helper bails for non-project scope.
    assert 'if (source !== "project")' in js
    # And wires triggerImproveNow as the click handler only past that gate.
    assert "triggerImproveNow(skillName)" in js


def test_skills_js_improve_handler_guards_double_click():
    """A rapid double-click on the button must not fire two concurrent
    audits — the backend caps via semaphore, but a visible 429 flash
    is confusing. Guard with an in-flight Set."""
    js = _skills_js()
    assert "_improveInFlight" in js
    assert "_improveInFlight.add" in js
    assert "_improveInFlight.delete" in js


# ===========================================================================
# Telemetry: _run_improver_for_skill threads `source="manual"` to audit rows
# ===========================================================================


# ===========================================================================
# Cross-tool mirror: .claude/skills/ → .agents/skills/ after apply
# ===========================================================================


def test_mirror_writes_to_agents_tree_after_apply(serve_module, tmp_path, monkeypatch):
    """When ``_apply_improvement`` overwrites
    ``<repo>/.claude/skills/<x>/SKILL.md`` the matching
    ``<repo>/.agents/skills/<x>/SKILL.md`` must end up with the same
    content. Without this, Codex sees a stale skill until the user
    remembers to run .ai/scripts/sync_skills.py by hand."""
    _patch_root(monkeypatch, serve_module, tmp_path)
    _patch_attr(
        monkeypatch, serve_module, "SKILL_PROPOSALS_DIR",
        tmp_path / ".ai" / "dashboard" / "skill_proposals",
    )
    _patch_attr(
        monkeypatch, serve_module, "SKILL_BACKUPS_DIR",
        tmp_path / ".ai" / "dashboard" / "skill_backups",
    )
    _patch_attr(
        monkeypatch, serve_module, "IMPROVEMENTS_LEDGER",
        tmp_path / ".ai" / "dashboard" / "improvements.jsonl",
    )

    claude_skill = tmp_path / ".claude" / "skills" / "planner" / "SKILL.md"
    agents_skill = tmp_path / ".agents" / "skills" / "planner" / "SKILL.md"
    claude_skill.parent.mkdir(parents=True)
    claude_skill.write_text("# old\n", encoding="utf-8")
    agents_skill.parent.mkdir(parents=True)
    agents_skill.write_text("# old\n", encoding="utf-8")

    ok = serve_module._apply_improvement(
        claude_skill, "# new\n",
        source="manual", reason="x", proposal_id=None,
        skill_id="planner", diff_lines=1,
    )
    assert ok is True
    assert claude_skill.read_text(encoding="utf-8") == "# new\n"
    assert agents_skill.read_text(encoding="utf-8") == "# new\n", (
        "agents-side mirror not updated after apply — Codex would keep "
        "seeing the stale SKILL.md"
    )


def test_mirror_skips_when_agents_copy_does_not_exist(serve_module, tmp_path, monkeypatch):
    """A Claude-only skill (no .agents counterpart) must stay Claude-only.
    The improver applying an edit to such a skill must NOT materialise a
    new .agents/skills/<name>/ directory — the operator never asked for
    a Codex mirror, and silently inventing one would surprise them."""
    _patch_root(monkeypatch, serve_module, tmp_path)
    _patch_attr(
        monkeypatch, serve_module, "SKILL_PROPOSALS_DIR",
        tmp_path / ".ai" / "dashboard" / "skill_proposals",
    )
    _patch_attr(
        monkeypatch, serve_module, "SKILL_BACKUPS_DIR",
        tmp_path / ".ai" / "dashboard" / "skill_backups",
    )
    _patch_attr(
        monkeypatch, serve_module, "IMPROVEMENTS_LEDGER",
        tmp_path / ".ai" / "dashboard" / "improvements.jsonl",
    )

    claude_skill = tmp_path / ".claude" / "skills" / "claude-only" / "SKILL.md"
    claude_skill.parent.mkdir(parents=True)
    claude_skill.write_text("# v1\n", encoding="utf-8")
    # .agents root exists but the claude-only skill does NOT live there.
    (tmp_path / ".agents" / "skills").mkdir(parents=True)
    agents_skill = tmp_path / ".agents" / "skills" / "claude-only" / "SKILL.md"

    serve_module._apply_improvement(
        claude_skill, "# v2\n",
        source="manual", reason="x", proposal_id=None,
        skill_id="claude-only", diff_lines=1,
    )
    # .claude side got the update.
    assert claude_skill.read_text(encoding="utf-8") == "# v2\n"
    # .agents side must remain absent — no surprise mirror invented.
    assert not agents_skill.exists(), (
        "Mirror silently created a .agents/skills/<x>/ copy for a "
        "Claude-only skill"
    )


def test_create_skill_in_both_trees_writes_both_sides(serve_module, tmp_path, monkeypatch):
    """The draft-install helper materialises a brand-new skill in BOTH
    .claude/skills/<slug>/ and .agents/skills/<slug>/ when the agents
    tree is present. This is the one path that's allowed to invent a
    fresh .agents mirror — the operator explicitly accepted a draft."""
    _patch_root(monkeypatch, serve_module, tmp_path)
    # .agents/skills exists (typical project layout post-bootstrap).
    (tmp_path / ".agents" / "skills").mkdir(parents=True)
    body = "---\nname: new-skill\n---\n# new\n"
    info = serve_module._create_skill_in_both_trees("new-skill", body)
    claude_md = tmp_path / ".claude" / "skills" / "new-skill" / "SKILL.md"
    agents_md = tmp_path / ".agents" / "skills" / "new-skill" / "SKILL.md"
    assert claude_md.is_file()
    assert agents_md.is_file()
    assert claude_md.read_text(encoding="utf-8") == body
    assert agents_md.read_text(encoding="utf-8") == body
    assert info["claude_path"] == ".claude/skills/new-skill/SKILL.md"
    assert info["agents_path"] == ".agents/skills/new-skill/SKILL.md"
    assert info["agents_skipped_reason"] is None


def test_create_skill_in_both_trees_skips_agents_when_dir_absent(
    serve_module, tmp_path, monkeypatch,
):
    """Claude-only project (no .agents/skills/ tree on disk): create in
    .claude/ only, report the skip reason so the operator knows the
    agents side wasn't done."""
    _patch_root(monkeypatch, serve_module, tmp_path)
    info = serve_module._create_skill_in_both_trees(
        "fresh", "---\nname: fresh\n---\n",
    )
    assert (tmp_path / ".claude" / "skills" / "fresh" / "SKILL.md").is_file()
    assert not (tmp_path / ".agents" / "skills" / "fresh" / "SKILL.md").exists()
    assert info["agents_path"] is None
    assert ".agents/skills" in info["agents_skipped_reason"].lower()


def test_create_skill_in_both_trees_skips_bridge_skills(serve_module, tmp_path, monkeypatch):
    """A new ``codex``-named skill must NOT get mirrored to .agents/
    even at create time — the cross-call bridge convention applies on
    create as well as on update. The agents-side ``codex`` is meant to
    be the 'call claude from codex' bridge, not a copy of the Claude
    'call codex' skill."""
    _patch_root(monkeypatch, serve_module, tmp_path)
    (tmp_path / ".agents" / "skills").mkdir(parents=True)
    info = serve_module._create_skill_in_both_trees(
        "codex", "---\nname: codex\n---\n# call codex\n",
    )
    assert (tmp_path / ".claude" / "skills" / "codex" / "SKILL.md").is_file()
    assert not (tmp_path / ".agents" / "skills" / "codex" / "SKILL.md").exists()
    assert info["agents_path"] is None
    assert "bridge" in info["agents_skipped_reason"].lower()


def test_mirror_skips_bridge_skills(serve_module, tmp_path, monkeypatch):
    """The ``codex`` and ``claude`` skills are cross-tool bridges whose
    .claude/ and .agents/ copies are intentionally different. The mirror
    must NEVER overwrite a bridge skill — the agents-side ``codex``
    file is actually the "call claude" bridge and copying the Claude
    version on top would break the cross-call mechanism."""
    _patch_root(monkeypatch, serve_module, tmp_path)
    _patch_attr(
        monkeypatch, serve_module, "SKILL_PROPOSALS_DIR",
        tmp_path / ".ai" / "dashboard" / "skill_proposals",
    )
    _patch_attr(
        monkeypatch, serve_module, "SKILL_BACKUPS_DIR",
        tmp_path / ".ai" / "dashboard" / "skill_backups",
    )
    _patch_attr(
        monkeypatch, serve_module, "IMPROVEMENTS_LEDGER",
        tmp_path / ".ai" / "dashboard" / "improvements.jsonl",
    )
    claude_codex_skill = tmp_path / ".claude" / "skills" / "codex" / "SKILL.md"
    agents_codex_skill = tmp_path / ".agents" / "skills" / "codex" / "SKILL.md"
    claude_codex_skill.parent.mkdir(parents=True)
    agents_codex_skill.parent.mkdir(parents=True)
    claude_codex_skill.write_text("# call codex\n", encoding="utf-8")
    bridge_body = "# bridge: call claude from codex\n"
    agents_codex_skill.write_text(bridge_body, encoding="utf-8")

    serve_module._apply_improvement(
        claude_codex_skill, "# call codex v2\n",
        source="manual", reason="x", proposal_id=None,
        skill_id="codex", diff_lines=1,
    )
    # .claude got the update; .agents bridge is UNTOUCHED.
    assert claude_codex_skill.read_text(encoding="utf-8") == "# call codex v2\n"
    assert agents_codex_skill.read_text(encoding="utf-8") == bridge_body


def test_mirror_helper_returns_skip_when_agents_dir_absent(serve_module, tmp_path, monkeypatch):
    """When .agents/skills/ doesn't exist at all (project that uses only
    Claude), the mirror must return a clean skip — not a write error."""
    _patch_root(monkeypatch, serve_module, tmp_path)
    claude_skill = tmp_path / ".claude" / "skills" / "x" / "SKILL.md"
    claude_skill.parent.mkdir(parents=True)
    claude_skill.write_text("# body\n", encoding="utf-8")
    ok, msg = serve_module._mirror_claude_skill_to_agents(claude_skill)
    assert ok is False
    assert ".agents/skills" in msg.lower() or "not on disk" in msg.lower()


def test_mirror_helper_skips_non_claude_paths(serve_module, tmp_path, monkeypatch):
    """Calling the mirror on a path that isn't under .claude/skills/ (e.g.
    a user-global skill in ~/.claude/skills/) must NOT touch the
    project's .agents/ tree — only project-scope skills mirror."""
    _patch_root(monkeypatch, serve_module, tmp_path)
    # User-global mock — far outside .claude/skills under ROOT.
    foreign = tmp_path / "elsewhere" / "SKILL.md"
    foreign.parent.mkdir(parents=True)
    foreign.write_text("# body\n", encoding="utf-8")
    ok, msg = serve_module._mirror_claude_skill_to_agents(foreign)
    assert ok is False
    assert "not a .claude/skills path" in msg


# ===========================================================================
# .ai/scripts/sync_skills.py — single-skill mode refuses to invent dst dirs
# ===========================================================================


def test_sync_skills_single_skill_refuses_when_dst_missing(tmp_path, monkeypatch):
    """Running ``sync_skills.py claude agents <name>`` for a skill that
    exists in .claude/skills/ but NOT in .agents/skills/ must refuse to
    create the dst-side dir. Without this guard a routine "sync this
    skill" call quietly invents an Agents mirror the operator never set
    up — same surprise as the in-process mirror used to do."""
    import importlib.util
    script_path = REPO_ROOT / ".ai" / "scripts" / "sync_skills.py"
    spec = importlib.util.spec_from_file_location("sync_skills_mod", script_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    claude_root = tmp_path / ".claude" / "skills"
    agents_root = tmp_path / ".agents" / "skills"
    (claude_root / "fresh").mkdir(parents=True)
    (claude_root / "fresh" / "SKILL.md").write_text("# x\n", encoding="utf-8")
    agents_root.mkdir(parents=True)  # exists, but no "fresh" subdir

    monkeypatch.setattr(mod, "ROOTS", {"claude": claude_root, "agents": agents_root})
    monkeypatch.setattr(mod.sys, "argv",
                        ["sync_skills.py", "claude", "agents", "fresh"])
    rc = mod.main()
    # The script returns 0 (it didn't crash) but printed a refusal and
    # didn't create the dst dir.
    assert rc == 0
    assert not (agents_root / "fresh").exists(), (
        "sync_skills.py invented an agents-side dir for a skill that "
        "wasn't already there"
    )


def test_sync_skills_create_new_flag_allows_dst_creation(tmp_path, monkeypatch):
    """With ``--create-new`` the same call DOES materialise the dst
    side. This is the opt-in path for brand-new skills."""
    import importlib.util
    script_path = REPO_ROOT / ".ai" / "scripts" / "sync_skills.py"
    spec = importlib.util.spec_from_file_location("sync_skills_mod_create", script_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    claude_root = tmp_path / ".claude" / "skills"
    agents_root = tmp_path / ".agents" / "skills"
    (claude_root / "fresh").mkdir(parents=True)
    (claude_root / "fresh" / "SKILL.md").write_text("# x\n", encoding="utf-8")
    agents_root.mkdir(parents=True)

    monkeypatch.setattr(mod, "ROOTS", {"claude": claude_root, "agents": agents_root})
    monkeypatch.setattr(mod.sys, "argv",
                        ["sync_skills.py", "claude", "agents", "fresh", "--create-new"])
    rc = mod.main()
    assert rc == 0
    assert (agents_root / "fresh" / "SKILL.md").is_file()
    assert (agents_root / "fresh" / "SKILL.md").read_text(encoding="utf-8") == "# x\n"


def test_run_improver_for_skill_manual_records_source_manual(
    serve_module, tmp_path, monkeypatch,
):
    """A manual=True invocation that returns "no_change" must write an
    audit row tagged ``source="manual"`` — otherwise the dashboard's
    ``source=auto`` filter would hide manual outcomes."""
    skill = tmp_path / "SKILL.md"
    skill.write_text("# body\n", encoding="utf-8")
    _patch_attr(monkeypatch, serve_module, "_safe_which", lambda _t: "/fake/bin")
    _patch_attr(
        monkeypatch, serve_module, "_aggregate_skill_metrics",
        lambda: {"fake-skill": {"recent": []}},
    )
    captured = {}

    def fake_audit(skill_id, status, reason, pid, backup, dl, source="auto"):
        captured["source"] = source
        captured["status"] = status

    _patch_attr(monkeypatch, serve_module, "_audit_improvement", fake_audit)

    class _FakeProc:
        returncode = 0
        stdout = json.dumps({
            "change_summary": "none", "rationale": "fine",
            "new_content": None,
        })
        stderr = ""

    monkeypatch.setattr(
        serve_module.subprocess, "run",
        lambda *a, **k: _FakeProc(),
    )
    cfg = {"enabled": True, "tool": "claude", "model": "x",
           "timeout_seconds": 60, "small_change_max_lines": 6}
    result = serve_module._run_improver_for_skill(
        "fake-skill", skill, job_id=None, log_path=None, cfg=cfg,
        manual=True,
    )
    assert captured["source"] == "manual"
    assert captured["status"] == "no_change"
    assert result["status"] == "no_change"


def test_tracked_sids_purged_on_atexit(serve_module, monkeypatch):
    """The atexit hook snapshots tracked improver SIDs, clears them, and
    purges each transcript once."""
    calls = []
    with serve_module._IMPROVER_TRACKED_SIDS_LOCK:
        serve_module._IMPROVER_TRACKED_SIDS.clear()
        serve_module._IMPROVER_TRACKED_SIDS.add("fake-sid")

    _patch_attr(monkeypatch, serve_module, "_purge_claude_transcript", lambda sid: calls.append(sid) or True)
    serve_module._purge_all_tracked_improver_sids()

    assert calls == ["fake-sid"]
    with serve_module._IMPROVER_TRACKED_SIDS_LOCK:
        assert not serve_module._IMPROVER_TRACKED_SIDS


def test_tracked_sids_purged_on_sigterm(serve_module, monkeypatch):
    """The installed signal handler purges tracked SIDs, then chains to the
    previous handler."""
    calls = []
    captured = {}
    signum = signal.SIGTERM if hasattr(signal, "SIGTERM") else signal.SIGINT

    def previous_handler(_signum, _frame):
        raise SystemExit(99)

    monkeypatch.setattr(serve_module.atexit, "register", lambda _fn: None)
    monkeypatch.setattr(serve_module.signal, "getsignal", lambda _sig: previous_handler)
    monkeypatch.setattr(serve_module.signal, "signal", lambda sig, handler: captured.setdefault(sig, handler))
    _patch_attr(monkeypatch, serve_module, "_purge_claude_transcript", lambda sid: calls.append(sid) or True)
    _patch_attr(monkeypatch, serve_module, "_IMPROVER_SHUTDOWN_HANDLERS_INSTALLED", False)

    with serve_module._IMPROVER_TRACKED_SIDS_LOCK:
        serve_module._IMPROVER_TRACKED_SIDS.clear()
        serve_module._IMPROVER_TRACKED_SIDS.add("term-sid")

    serve_module._install_improver_shutdown_handlers()
    with pytest.raises(SystemExit) as exc:
        captured[signum](signum, None)

    assert exc.value.code == 99
    assert calls == ["term-sid"]
    with serve_module._IMPROVER_TRACKED_SIDS_LOCK:
        assert not serve_module._IMPROVER_TRACKED_SIDS


def test_shutdown_signal_chains_default_ignored_and_callable(serve_module, monkeypatch):
    signum = signal.SIGTERM if hasattr(signal, "SIGTERM") else signal.SIGINT
    events = []

    _patch_attr(monkeypatch, serve_module, "_purge_all_tracked_improver_sids", lambda: events.append(("purge",)))
    monkeypatch.setattr(
        serve_module.signal,
        "signal",
        lambda sig, handler: events.append(("signal", sig, handler)),
    )

    def fake_kill(pid, sig):
        events.append(("kill", pid, sig))
        raise SystemExit(128 + sig)

    monkeypatch.setattr(serve_module.os, "kill", fake_kill)

    with pytest.raises(SystemExit) as exc:
        serve_module._chain_improver_shutdown_signal(signum, None, signal.SIG_DFL)
    assert exc.value.code == 128 + signum
    assert ("signal", signum, signal.SIG_DFL) in events
    assert ("kill", os.getpid(), signum) in events

    before_ignored = len(events)
    serve_module._chain_improver_shutdown_signal(signum, None, signal.SIG_IGN)
    assert events[before_ignored:] == [("purge",)]

    called = []

    def previous_handler(sig, frame):
        called.append((sig, frame))

    with pytest.raises(SystemExit) as exc:
        serve_module._chain_improver_shutdown_signal(signum, "frame", previous_handler)
    assert called == [(signum, "frame")]
    assert exc.value.code == 128 + signum


def test_purge_retry_on_permission_error(serve_module, tmp_path, monkeypatch, capsys):
    """Locked files on Windows are retried before the purge gives up."""
    sid = "retry-sid"
    transcript = tmp_path / f"{sid}.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    attempts = {"n": 0}
    real_unlink = os.unlink

    def flaky_unlink(path):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise PermissionError("locked")
        real_unlink(path)

    _patch_attr(monkeypatch, serve_module, "_transcripts_dir_for_cwd", lambda _root: tmp_path)
    monkeypatch.setattr(serve_module.os, "unlink", flaky_unlink)

    started = time.monotonic()
    assert serve_module._purge_claude_transcript(sid) is True
    elapsed = time.monotonic() - started

    assert attempts["n"] == 3
    assert elapsed >= 0.09
    assert not transcript.exists()
    assert "after 3 attempts" in capsys.readouterr().out
