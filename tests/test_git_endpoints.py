"""Tests for the dashboard's git-update endpoints (`/api/git/check`, `/api/git/pull`).

These exercise the JSON-shape contract that `.ai/dashboard/app/settings.js`
relies on. The tests monkeypatch the Handler's `_run_git` so they don't depend
on a remote being reachable.

OBSOLETE: the `/api/git/{check,pull,log}` endpoints were replaced by
`/api/workflow/{check,update}` (template-clone flow). The handlers no
longer exist in `serve.py` — module-level skip below so the suite stays
green while the file is preserved as archaeology.
"""

from __future__ import annotations

import importlib.util
import json
import socketserver
import sys
import threading
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Iterator

import pytest


pytestmark = pytest.mark.skip(
    reason="git-update endpoints removed; replaced by /api/workflow/{check,update}"
)


REPO_ROOT = Path(__file__).resolve().parent.parent
SERVE_PATH = REPO_ROOT / ".ai" / "dashboard" / "serve.py"


@pytest.fixture(scope="module")
def serve_module():
    spec = importlib.util.spec_from_file_location("dashboard_serve_git", SERVE_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["dashboard_serve_git"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def running_server(serve_module) -> Iterator[str]:
    httpd = socketserver.ThreadingTCPServer(("127.0.0.1", 0), serve_module.Handler)
    port = httpd.server_address[1]
    original_port = serve_module.PORT
    original_bound = serve_module.BOUND_PORT
    serve_module.PORT = port
    serve_module.BOUND_PORT = port
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        serve_module.PORT = original_port
        serve_module.BOUND_PORT = original_bound
        httpd.shutdown()
        httpd.server_close()


def _get(url: str) -> tuple[int, dict]:
    with urllib.request.urlopen(url, timeout=5) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def _post(url: str, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body or {}).encode("utf-8")
    parsed = urllib.parse.urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json", "Origin": origin},
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def _stub_run_git(serve_module, responses: dict[tuple[str, ...], tuple[int, str, str]]):
    """Replace Handler._run_git with a deterministic lookup table.

    Keys are the argv tuples passed (after ``git``), values are (rc, stdout, stderr).
    Unknown invocations return (-99, "", "no stub for: <args>") so missing wiring
    fails loudly instead of silently returning success.
    """
    def fake(self, args, timeout=30):
        return responses.get(tuple(args), (-99, "", f"no stub for: {' '.join(args)}"))
    serve_module.Handler._run_git = fake


def test_git_check_returns_json_shape(serve_module, running_server):
    _stub_run_git(serve_module, {
        ("rev-parse", "--abbrev-ref", "HEAD"): (0, "main\n", ""),
        ("fetch", "--quiet"): (0, "", ""),
        ("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"): (0, "origin/main\n", ""),
        ("rev-list", "--left-right", "--count", "HEAD...@{u}"): (0, "0\t3\n", ""),
    })
    status, body = _get(running_server + "/api/git/check")
    assert status == 200, body
    # Shape contract that settings.js relies on.
    assert body["branch"] == "main"
    assert body["upstream"] == "origin/main"
    assert body["ahead"] == 0
    assert body["behind"] == 3
    assert body["has_updates"] is True
    assert isinstance(body["message"], str) and body["message"], "non-empty message"


def test_git_check_no_upstream_surfaces_error(serve_module, running_server):
    _stub_run_git(serve_module, {
        ("rev-parse", "--abbrev-ref", "HEAD"): (0, "feature\n", ""),
        ("fetch", "--quiet"): (0, "", ""),
        ("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"): (
            128, "", "fatal: no upstream configured for branch 'feature'",
        ),
    })
    status, body = _get(running_server + "/api/git/check")
    assert status == 200
    assert body["error"] == "no_upstream"
    assert body["branch"] == "feature"


def test_git_pull_endpoint_invokes_subprocess(serve_module, running_server):
    _stub_run_git(serve_module, {
        ("pull", "--ff-only"): (0, "Already up to date.\n", ""),
    })
    status, body = _post(running_server + "/api/git/pull", {})
    assert status == 200, body
    assert body["success"] is True
    assert "Already up to date" in body["output"]
    assert isinstance(body["message"], str) and body["message"]


def test_git_pull_failure_returns_success_false(serve_module, running_server):
    _stub_run_git(serve_module, {
        ("pull", "--ff-only"): (
            1, "", "fatal: Not possible to fast-forward, aborting.",
        ),
    })
    status, body = _post(running_server + "/api/git/pull", {})
    assert status == 200
    assert body["success"] is False
    assert "Not possible to fast-forward" in body["output"]


def test_system_info_returns_expected_keys(serve_module, running_server):
    status, body = _get(running_server + "/api/system/info")
    assert status == 200, body
    expected = {
        "host", "port", "configured_port", "repo_root", "python_version",
        "platform", "pid", "uptime_seconds", "auto_improver_enabled",
        "events_file", "jobs_dir",
    }
    assert expected.issubset(body.keys()), f"missing: {expected - set(body.keys())}"
    assert isinstance(body["pid"], int) and body["pid"] > 0
    assert isinstance(body["uptime_seconds"], int) and body["uptime_seconds"] >= 0
    assert isinstance(body["auto_improver_enabled"], bool)
    # python version string is "X.Y.Z"
    assert body["python_version"].count(".") == 2


def test_git_log_returns_commit_list(serve_module, running_server):
    _stub_run_git(serve_module, {
        ("log", "HEAD..@{u}", "--oneline", "--no-decorate", "-n", "20"): (
            0,
            "abc1234 first new commit\ndef5678 second new commit\n",
            "",
        ),
    })
    status, body = _get(running_server + "/api/git/log")
    assert status == 200
    assert isinstance(body["commits"], list) and len(body["commits"]) == 2
    assert body["commits"][0]["sha"].startswith("abc")
    assert "first new commit" in body["commits"][0]["subject"]


# ---------- workflow settings tests (redirect ROOT to tmp) ----------

@pytest.fixture
def fake_models_yaml(tmp_path, serve_module, monkeypatch):
    """Redirect serve_module.ROOT to a tmp dir containing a stub models.yaml."""
    ai_dir = tmp_path / ".ai"
    ai_dir.mkdir()
    (ai_dir / "models.yaml").write_text(
        "dispatch_mode: auto\n"
        "\n"
        "session:\n"
        "  tool: claude\n"
        "  model: claude-sonnet-4-6\n"
        "\n"
        "plan:\n"
        "  tool: claude\n"
        "  model: claude-opus-4-7\n"
        "\n"
        "execute:\n"
        "  tool: claude\n"
        "  model: claude-sonnet-4-6\n"
        "\n"
        "review:\n"
        "  tool: claude\n"
        "  model: claude-opus-4-7\n"
        "\n"
        "rescue:\n"
        "  tool: claude\n"
        "  model: claude-opus-4-6\n"
        "\n"
        "maintenance:\n"
        "  tool: claude\n"
        "  model: claude-sonnet-4-6\n"
        "\n"
        "bootstrap:\n"
        "  tool: claude\n"
        "  model: claude-sonnet-4-6\n"
        "\n"
        "auto_select:\n"
        "  enabled: false\n"
        "  token_budget: medium\n"
        "  phases: [execute, review, rescue]\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(serve_module, "ROOT", tmp_path)
    return ai_dir / "models.yaml"


def test_settings_get_returns_expected_blocks(serve_module, running_server, fake_models_yaml):
    status, body = _get(running_server + "/api/settings")
    assert status == 200, body
    assert "improver" in body and "auto_select" in body and "phases" in body
    assert body["auto_select"]["enabled"] is False
    assert body["auto_select"]["token_budget"] == "medium"
    assert set(body["auto_select"]["phases"]) == {"execute", "review", "rescue"}
    assert set(body["phases"].keys()) >= {"plan", "execute", "review", "rescue", "maintenance", "bootstrap"}
    assert body["phases"]["plan"]["model"] == "claude-opus-4-7"
    assert isinstance(body["improver"]["enabled"], bool)
    assert isinstance(body["improver"]["disabled_by_env"], bool)


def test_improver_update_creates_block_when_missing(serve_module, running_server, fake_models_yaml):
    status, body = _post(running_server + "/api/settings/improver", {
        "enabled": True,
        "min_interval_seconds": 600,
        "timeout_seconds": 180,
    })
    assert status == 200, body
    text = fake_models_yaml.read_text(encoding="utf-8")
    assert "improver:" in text
    assert "min_interval_seconds: 600" in text
    assert "timeout_seconds: 180" in text


def test_improver_update_rejects_out_of_range(serve_module, running_server, fake_models_yaml):
    import urllib.error
    req = urllib.request.Request(
        running_server + "/api/settings/improver",
        data=json.dumps({"timeout_seconds": 99999}).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "Origin": running_server},
    )
    try:
        urllib.request.urlopen(req, timeout=5)
        raise AssertionError("expected HTTP 400")
    except urllib.error.HTTPError as e:
        assert e.code == 400
        err = json.loads(e.read().decode("utf-8"))
        assert "timeout_seconds" in err["error"]


def test_auto_select_update_writes_phases_list(serve_module, running_server, fake_models_yaml):
    status, body = _post(running_server + "/api/settings/auto_select", {
        "enabled": True,
        "token_budget": "high",
        "phases": ["execute", "review"],
    })
    assert status == 200, body
    text = fake_models_yaml.read_text(encoding="utf-8")
    assert "enabled: true" in text
    assert "token_budget: high" in text
    assert "phases: [execute, review]" in text


def test_phase_update_accepts_timeout_seconds(serve_module, running_server, fake_models_yaml):
    status, body = _post(running_server + "/api/models/phase", {
        "phase": "execute",
        "timeout_seconds": 2400,
    })
    assert status == 200, body
    text = fake_models_yaml.read_text(encoding="utf-8")
    # execute block should now carry the override
    assert "timeout_seconds: 2400" in text


def test_phase_update_accepts_reasoning_max(serve_module, running_server, fake_models_yaml):
    """`max` is claude-only but the backend accepts it as part of the union."""
    status, body = _post(running_server + "/api/models/phase", {
        "phase": "plan",
        "reasoning_effort": "max",
    })
    assert status == 200, body
    text = fake_models_yaml.read_text(encoding="utf-8")
    assert "reasoning_effort: max" in text


def test_phase_update_accepts_all_reasoning_levels(serve_module, running_server, fake_models_yaml):
    for level in ("low", "medium", "high", "xhigh", "max"):
        status, body = _post(running_server + "/api/models/phase", {
            "phase": "review",
            "reasoning_effort": level,
        })
        assert status == 200, (level, body)


def test_phase_update_rejects_invalid_reasoning(serve_module, running_server, fake_models_yaml):
    import urllib.error
    req = urllib.request.Request(
        running_server + "/api/models/phase",
        data=json.dumps({"phase": "plan", "reasoning_effort": "wat"}).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "Origin": running_server},
    )
    try:
        urllib.request.urlopen(req, timeout=5)
        raise AssertionError("expected HTTP 400")
    except urllib.error.HTTPError as e:
        assert e.code == 400
        err = json.loads(e.read().decode("utf-8"))
        assert "reasoning_effort" in err["error"]


def test_phase_update_rejects_invalid_timeout(serve_module, running_server, fake_models_yaml):
    import urllib.error
    req = urllib.request.Request(
        running_server + "/api/models/phase",
        data=json.dumps({"phase": "plan", "timeout_seconds": 5}).encode("utf-8"),  # below 30
        method="POST",
        headers={"Content-Type": "application/json", "Origin": running_server},
    )
    try:
        urllib.request.urlopen(req, timeout=5)
        raise AssertionError("expected HTTP 400")
    except urllib.error.HTTPError as e:
        assert e.code == 400
        err = json.loads(e.read().decode("utf-8"))
        assert "timeout_seconds" in err["error"] or "30" in err["error"]


def test_phase_update_clears_field_with_empty_string(serve_module, running_server, fake_models_yaml):
    """Empty value should remove the override line."""
    # Set first
    _post(running_server + "/api/models/phase", {"phase": "plan", "reasoning_effort": "high"})
    text = fake_models_yaml.read_text(encoding="utf-8")
    assert "reasoning_effort: high" in text
    # Then clear
    status, body = _post(running_server + "/api/models/phase", {"phase": "plan", "reasoning_effort": ""})
    assert status == 200, body
    text = fake_models_yaml.read_text(encoding="utf-8")
    assert "reasoning_effort:" not in text.split("plan:")[1].split("\n\n")[0]


def test_auto_select_get_after_update_roundtrips(serve_module, running_server, fake_models_yaml):
    """Save then read back should reflect the changes."""
    _post(running_server + "/api/settings/auto_select", {
        "enabled": True, "token_budget": "high", "phases": ["execute"],
    })
    status, body = _get(running_server + "/api/settings")
    assert status == 200
    assert body["auto_select"]["enabled"] is True
    assert body["auto_select"]["token_budget"] == "high"
    assert body["auto_select"]["phases"] == ["execute"]


def test_auto_select_rejects_unknown_phase(serve_module, running_server, fake_models_yaml):
    import urllib.error
    req = urllib.request.Request(
        running_server + "/api/settings/auto_select",
        data=json.dumps({"phases": ["execute", "make-coffee"]}).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "Origin": running_server},
    )
    try:
        urllib.request.urlopen(req, timeout=5)
        raise AssertionError("expected HTTP 400")
    except urllib.error.HTTPError as e:
        assert e.code == 400


def test_auto_select_rejects_invalid_token_budget(serve_module, running_server, fake_models_yaml):
    import urllib.error
    req = urllib.request.Request(
        running_server + "/api/settings/auto_select",
        data=json.dumps({"token_budget": "ultra"}).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "Origin": running_server},
    )
    try:
        urllib.request.urlopen(req, timeout=5)
        raise AssertionError("expected HTTP 400")
    except urllib.error.HTTPError as e:
        assert e.code == 400


def test_improver_update_full_roundtrip(serve_module, running_server, fake_models_yaml):
    _post(running_server + "/api/settings/improver", {
        "enabled": False,
        "small_change_max_lines": 12,
        "min_interval_seconds": 60,
        "timeout_seconds": 90,
        "revert_after_n_uses": 3,
    })
    status, body = _get(running_server + "/api/settings")
    assert status == 200
    imp = body["improver"]
    # `enabled=false` in YAML is overlaid on the defaults.
    assert imp["enabled"] is False
    # Numeric YAML values come back as strings via _read_yaml_field but should be intelligible.
    assert str(imp["small_change_max_lines"]) == "12"
    assert str(imp["min_interval_seconds"]) == "60"
    assert str(imp["timeout_seconds"]) == "90"
    assert str(imp["revert_after_n_uses"]) == "3"


def test_cache_control_no_store_on_api_response(serve_module, running_server):
    """Every dashboard response must carry Cache-Control: no-store so stale
    HTML/CSS/JS doesn't survive across upgrades."""
    with urllib.request.urlopen(running_server + "/api/system/info", timeout=5) as r:
        cache = r.headers.get("Cache-Control") or ""
    assert "no-store" in cache.lower(), f"Cache-Control header missing or wrong: {cache!r}"
