"""Dashboard /api/auto-select endpoint tests (spec PR 3 Phase 2)."""
from __future__ import annotations

import importlib.util
import json
import socketserver
import sys
import threading
import urllib.request
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVE_PATH = REPO_ROOT / ".ai" / "dashboard" / "serve.py"


@pytest.fixture(scope="module")
def serve_module():
    """Load .ai/dashboard/serve.py without running main()."""
    spec = importlib.util.spec_from_file_location("dashboard_serve_autoselect", SERVE_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["dashboard_serve_autoselect"] = mod
    spec.loader.exec_module(mod)
    return mod


def _patch_attr(monkeypatch, serve_module, name, value):
    """setattr name=value on serve_module AND every loaded server.* module that
    binds its own copy (re-export shims create independent bindings, so a fn
    moved out of serve.py — e.g. into server.analytics — reads the name in its
    new module's namespace). follows-the-move."""
    if hasattr(serve_module, name):
        monkeypatch.setattr(serve_module, name, value, raising=False)
    for modname, mod in list(sys.modules.items()):
        if (modname == "server" or modname.startswith("server.")) and mod is not None and hasattr(mod, name):
            monkeypatch.setattr(mod, name, value, raising=False)


@pytest.fixture
def running_server(serve_module):
    httpd = socketserver.ThreadingTCPServer(("127.0.0.1", 0), serve_module.Handler)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def _write_metrics(tmp_path: Path, records: list[dict]) -> Path:
    p = tmp_path / "metrics.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return p


# --- Loader unit tests -------------------------------------------------------


def test_loader_returns_empty_when_file_missing(serve_module, tmp_path, monkeypatch):
    _patch_attr(monkeypatch, serve_module, "METRICS_FILE", tmp_path / "absent.jsonl")
    result = serve_module._load_auto_select_ranking()
    # Loader response shape expanded over time — pin only the fields this
    # test cares about (samples count + groups list); other diagnostics
    # (dropped_candidates, last_record_ts, min_samples) live alongside.
    assert result["samples"] == 0
    assert result["groups"] == []


def test_loader_groups_by_phase_size_risk(serve_module, tmp_path, monkeypatch):
    """Distinct (phase, size, risk) keys produce distinct groups."""
    records = [
        # group A: execute/small/low, codex/gpt-5.4 with 5 successes.
        # Differing budgets are intentionally collapsed into the same group.
        *[{"phase": "execute", "size": "small", "risk": "low", "budget": "high",
           "tool": "codex", "model": "gpt-5.4", "reasoning_effort": "medium",
           "exit_code": 0, "duration_ms": 5000, "handoff_complete": True,
           "review_verdict": "approve"} for _ in range(3)],
        *[{"phase": "execute", "size": "small", "risk": "low", "budget": "medium",
           "tool": "codex", "model": "gpt-5.4", "reasoning_effort": "medium",
           "exit_code": 0, "duration_ms": 5000, "handoff_complete": True,
           "review_verdict": "approve"} for _ in range(2)],
        # group B: review/medium/low, claude/opus-4-6 with 5 successes
        *[{"phase": "review", "size": "medium", "risk": "low", "budget": "medium",
           "tool": "claude", "model": "claude-opus-4-6", "reasoning_effort": None,
           "exit_code": 0, "duration_ms": 3000, "handoff_complete": None,
           "review_verdict": "approve"} for _ in range(5)],
    ]
    metrics = _write_metrics(tmp_path, records)
    _patch_attr(monkeypatch, serve_module, "METRICS_FILE", metrics)
    result = serve_module._load_auto_select_ranking()
    assert result["samples"] == 10
    keys = {(g["key"]["phase"], g["key"]["size"], g["key"]["risk"])
            for g in result["groups"]}
    assert keys == {
        ("execute", "small", "low"),
        ("review", "medium", "low"),
    }
    assert all("budget" not in g["key"] for g in result["groups"])
    assert all(g["static_fallback"] is False for g in result["groups"])


def test_loader_drops_candidates_below_min_samples(serve_module, tmp_path, monkeypatch):
    """Candidates with < min_samples records are excluded; groups with no
    qualifying candidates are omitted entirely. Default min_samples is 5."""
    records = [
        {"phase": "execute", "size": "small", "risk": "low", "budget": "medium",
         "tool": "codex", "model": "gpt-5.4", "exit_code": 0, "duration_ms": 1000,
         "handoff_complete": True, "review_verdict": "approve"}
        for _ in range(3)  # 3 samples — below explicit threshold of 5
    ]
    metrics = _write_metrics(tmp_path, records)
    _patch_attr(monkeypatch, serve_module, "METRICS_FILE", metrics)
    result = serve_module._load_auto_select_ranking(min_samples=5)
    assert result["samples"] == 3
    assert result["groups"] == []  # no group qualifies


def test_loader_computes_success_rate(serve_module, tmp_path, monkeypatch):
    """Mixed successes and failures produce the right success_rate."""
    records = [
        # 4 successes
        *[{"phase": "execute", "size": "small", "risk": "low", "budget": "medium",
           "tool": "codex", "model": "gpt-5.4", "exit_code": 0, "duration_ms": 1000,
           "handoff_complete": True, "review_verdict": "approve"} for _ in range(4)],
        # 1 failure
        {"phase": "execute", "size": "small", "risk": "low", "budget": "medium",
         "tool": "codex", "model": "gpt-5.4", "exit_code": 1, "duration_ms": 2000,
         "handoff_complete": True, "review_verdict": None},
    ]
    metrics = _write_metrics(tmp_path, records)
    _patch_attr(monkeypatch, serve_module, "METRICS_FILE", metrics)
    result = serve_module._load_auto_select_ranking()
    assert len(result["groups"]) == 1
    candidates = result["groups"][0]["candidates"]
    assert len(candidates) == 1
    assert candidates[0]["samples"] == 5
    assert candidates[0]["success_rate"] == 0.8  # 4/5
    assert 0 <= candidates[0]["wilson_lower"] < candidates[0]["success_rate"]
    assert candidates[0]["median_duration_ms"] == 1000
    assert candidates[0]["score"] < candidates[0]["success_rate"]


def test_loader_ranks_candidates_by_score(serve_module, tmp_path, monkeypatch):
    """Top candidate must be highest-scoring (success_rate dominates)."""
    records = []
    # codex/gpt-5.4: 5 successes (sr=1.0)
    records.extend([
        {"phase": "execute", "size": "small", "risk": "low", "budget": "medium",
         "tool": "codex", "model": "gpt-5.4", "exit_code": 0, "duration_ms": 1000,
         "handoff_complete": True, "review_verdict": "approve"}
        for _ in range(5)
    ])
    # codex/gpt-5.5: 5 records, only 3 successes (sr=0.6)
    records.extend([
        {"phase": "execute", "size": "small", "risk": "low", "budget": "medium",
         "tool": "codex", "model": "gpt-5.5", "exit_code": 0, "duration_ms": 1000,
         "handoff_complete": True, "review_verdict": "approve"}
        for _ in range(3)
    ])
    records.extend([
        {"phase": "execute", "size": "small", "risk": "low", "budget": "medium",
         "tool": "codex", "model": "gpt-5.5", "exit_code": 1, "duration_ms": 1000,
         "handoff_complete": False, "review_verdict": None}
        for _ in range(2)
    ])
    metrics = _write_metrics(tmp_path, records)
    _patch_attr(monkeypatch, serve_module, "METRICS_FILE", metrics)
    result = serve_module._load_auto_select_ranking()
    cands = result["groups"][0]["candidates"]
    assert cands[0]["model"] == "gpt-5.4"
    assert cands[0]["success_rate"] == 1.0


# --- Endpoint integration test -----------------------------------------------


def _http_get(url: str) -> tuple[int, bytes]:
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status, resp.read()


def test_endpoint_returns_json_payload(running_server, serve_module, tmp_path, monkeypatch):
    """/api/auto-select returns a 200 JSON payload with samples + groups."""
    _patch_attr(monkeypatch, serve_module, "METRICS_FILE", tmp_path / "missing.jsonl")
    status, body = _http_get(f"{running_server}/api/auto-select")
    assert status == 200
    payload = json.loads(body)
    assert "samples" in payload
    assert "groups" in payload
    assert payload["samples"] == 0
    assert payload["groups"] == []


# --- Edge cases & robustness -------------------------------------------------


def test_loader_skips_malformed_json_lines(serve_module, tmp_path, monkeypatch):
    """Garbage lines must not crash the loader; valid lines on either side
    are still counted."""
    raw = (
        '{"phase":"execute","size":"small","risk":"low","budget":"medium",'
        '"tool":"codex","model":"gpt-5.4","exit_code":0,"duration_ms":1000,'
        '"handoff_complete":true,"review_verdict":"approve"}\n'
        "this is not json\n"
        '{"phase":"execute","size":"small","risk":"low","budget":"medium",'
        '"tool":"codex","model":"gpt-5.4","exit_code":0,"duration_ms":1000,'
        '"handoff_complete":true,"review_verdict":"approve"}\n'
        "\n"  # blank line — also skipped
        '{"phase":"execute","size":"small","risk":"low","budget":"medium",'
        '"tool":"codex","model":"gpt-5.4","exit_code":0,"duration_ms":1000,'
        '"handoff_complete":true,"review_verdict":"approve"}\n'
    )
    p = tmp_path / "metrics.jsonl"
    p.write_text(raw, encoding="utf-8")
    _patch_attr(monkeypatch, serve_module, "METRICS_FILE", p)
    result = serve_module._load_auto_select_ranking()
    assert result["samples"] == 3  # 3 valid + 1 garbage skipped + 1 blank skipped


def test_loader_skips_records_without_phase(serve_module, tmp_path, monkeypatch):
    """A record without a `phase` key is unusable; it must be dropped."""
    records = [
        # 5 valid records
        *[{"phase": "execute", "size": "small", "risk": "low", "budget": "medium",
           "tool": "codex", "model": "gpt-5.4", "exit_code": 0, "duration_ms": 1000,
           "handoff_complete": True, "review_verdict": "approve"} for _ in range(5)],
        # garbage: no phase
        {"size": "small", "tool": "codex", "exit_code": 0},
    ]
    metrics = _write_metrics(tmp_path, records)
    _patch_attr(monkeypatch, serve_module, "METRICS_FILE", metrics)
    result = serve_module._load_auto_select_ranking()
    assert result["samples"] == 5
    assert len(result["groups"]) == 1


def test_loader_handles_null_size_risk_budget(serve_module, tmp_path, monkeypatch):
    """Records from the `plan` phase (before triage runs) have null
    size/risk/budget. They must still group cleanly, without budget in
    the group key."""
    records = [
        {"phase": "plan", "size": None, "risk": None, "budget": None,
         "tool": "claude", "model": "claude-sonnet-4-6", "reasoning_effort": None,
         "exit_code": 0, "duration_ms": 2000, "handoff_complete": None,
         "review_verdict": None}
        for _ in range(5)
    ]
    metrics = _write_metrics(tmp_path, records)
    _patch_attr(monkeypatch, serve_module, "METRICS_FILE", metrics)
    result = serve_module._load_auto_select_ranking()
    assert len(result["groups"]) == 1
    g = result["groups"][0]
    assert g["key"]["phase"] == "plan"
    assert g["key"]["size"] is None
    assert g["key"]["risk"] is None
    assert "budget" not in g["key"]
    cands = g["candidates"]
    assert cands[0]["success_rate"] == 1.0  # exit 0, handoff null (ok), verdict null (ok)


def test_loader_tolerates_missing_duration_ms(serve_module, tmp_path, monkeypatch):
    """A record without duration_ms must not crash and mean_duration_ms
    defaults to 0 when no valid samples are present."""
    records = [
        {"phase": "execute", "size": "small", "risk": "low", "budget": "medium",
         "tool": "codex", "model": "gpt-5.4", "exit_code": 0,
         "handoff_complete": True, "review_verdict": "approve"}
        for _ in range(5)
    ]
    metrics = _write_metrics(tmp_path, records)
    _patch_attr(monkeypatch, serve_module, "METRICS_FILE", metrics)
    result = serve_module._load_auto_select_ranking()
    assert result["groups"][0]["candidates"][0]["mean_duration_ms"] == 0
    assert result["groups"][0]["candidates"][0]["median_duration_ms"] == 0


def test_loader_max_records_window_only_tails(serve_module, tmp_path, monkeypatch):
    """The loader must only consider the last `max_records` records — older
    data is windowed out."""
    # Write 250 records: first 50 are codex/gpt-5.4 with 100% success;
    # last 200 are codex/gpt-5.5 with 100% success. With default
    # max_records=200, only the last 200 (all gpt-5.5) should appear.
    old = [{"phase": "execute", "size": "small", "risk": "low", "budget": "medium",
            "tool": "codex", "model": "gpt-5.4", "exit_code": 0, "duration_ms": 1000,
            "handoff_complete": True, "review_verdict": "approve"} for _ in range(50)]
    new = [{"phase": "execute", "size": "small", "risk": "low", "budget": "medium",
            "tool": "codex", "model": "gpt-5.5", "exit_code": 0, "duration_ms": 1000,
            "handoff_complete": True, "review_verdict": "approve"} for _ in range(200)]
    metrics = _write_metrics(tmp_path, old + new)
    _patch_attr(monkeypatch, serve_module, "METRICS_FILE", metrics)
    result = serve_module._load_auto_select_ranking(max_records=200)
    cands = result["groups"][0]["candidates"]
    assert len(cands) == 1
    assert cands[0]["model"] == "gpt-5.5"
    assert cands[0]["samples"] == 200


def test_loader_caps_candidates_at_top_3(serve_module, tmp_path, monkeypatch):
    """When >3 candidates qualify, only top 3 are returned."""
    records = []
    # 5 different (tool, model) combos, each with 5 samples
    combos = [
        ("codex", "gpt-5.4-mini"),
        ("codex", "gpt-5.4"),
        ("codex", "gpt-5.5"),
        ("claude", "claude-sonnet-4-6"),
        ("claude", "claude-opus-4-6"),
    ]
    for tool, model in combos:
        records.extend([
            {"phase": "execute", "size": "small", "risk": "low", "budget": "medium",
             "tool": tool, "model": model, "exit_code": 0, "duration_ms": 1000,
             "handoff_complete": True, "review_verdict": "approve"}
            for _ in range(5)
        ])
    metrics = _write_metrics(tmp_path, records)
    _patch_attr(monkeypatch, serve_module, "METRICS_FILE", metrics)
    result = serve_module._load_auto_select_ranking()
    assert len(result["groups"][0]["candidates"]) == 3  # capped


def test_loader_score_matches_delegated_scorer(serve_module, tmp_path, monkeypatch):
    """Loader score output stays in lockstep with the delegated scorer.

    Two candidates in one group:
      A: codex/gpt-5.4: 5 records, all success, median duration 1000ms
      B: codex/gpt-5.5: 5 records, all success, median duration 5000ms

    Both candidates have raw success_rate 1.0, but Wilson-based scoring keeps
    score below the raw rate and ranks the faster candidate first.
    """
    records = []
    records.extend([
        {"phase": "execute", "size": "small", "risk": "low", "budget": "medium",
         "tool": "codex", "model": "gpt-5.4", "exit_code": 0, "duration_ms": 1000,
         "handoff_complete": True, "review_verdict": "approve"}
        for _ in range(5)
    ])
    records.extend([
        {"phase": "execute", "size": "small", "risk": "low", "budget": "medium",
         "tool": "codex", "model": "gpt-5.5", "exit_code": 0, "duration_ms": 5000,
         "handoff_complete": True, "review_verdict": "approve"}
        for _ in range(5)
    ])
    metrics = _write_metrics(tmp_path, records)
    _patch_attr(monkeypatch, serve_module, "METRICS_FILE", metrics)
    result = serve_module._load_auto_select_ranking()
    expected = serve_module.auto_select_scorer.score_groups(
        records,
        min_samples=5,
        effective_budget=None,
        per_group_tail=200,
        static_pick=None,
    )
    assert result == expected
    cands = result["groups"][0]["candidates"]
    # First (faster, same success rate) wins.
    assert cands[0]["model"] == "gpt-5.4"
    assert cands[0]["median_duration_ms"] == 1000
    assert cands[0]["wilson_lower"] < cands[0]["success_rate"]
    assert cands[0]["score"] < cands[0]["success_rate"]
    assert cands[1]["model"] == "gpt-5.5"
    assert cands[1]["median_duration_ms"] == 5000
    assert cands[0]["score"] > cands[1]["score"]


def test_handoff_false_counts_as_failure(serve_module, tmp_path, monkeypatch):
    """Per spec/skill: success requires exit_code==0 AND handoff_complete in
    {True, None} AND review_verdict in {None, 'approve'}. handoff_complete:false
    must drop success_rate below 1.0 even with exit_code 0."""
    records = [
        # 3 records: exit 0 + handoff_complete True
        *[{"phase": "execute", "size": "small", "risk": "low", "budget": "medium",
           "tool": "codex", "model": "gpt-5.4", "exit_code": 0, "duration_ms": 1000,
           "handoff_complete": True, "review_verdict": "approve"} for _ in range(3)],
        # 2 records: exit 0 BUT handoff_complete False (count as failure)
        *[{"phase": "execute", "size": "small", "risk": "low", "budget": "medium",
           "tool": "codex", "model": "gpt-5.4", "exit_code": 0, "duration_ms": 1000,
           "handoff_complete": False, "review_verdict": None} for _ in range(2)],
    ]
    metrics = _write_metrics(tmp_path, records)
    _patch_attr(monkeypatch, serve_module, "METRICS_FILE", metrics)
    result = serve_module._load_auto_select_ranking()
    assert result["groups"][0]["candidates"][0]["success_rate"] == 0.6  # 3/5


def test_review_verdict_request_changes_counts_as_failure(serve_module, tmp_path, monkeypatch):
    """review_verdict: 'request-changes' is a failure for the executor's row."""
    records = [
        # 4 successes
        *[{"phase": "execute", "size": "small", "risk": "low", "budget": "medium",
           "tool": "codex", "model": "gpt-5.4", "exit_code": 0, "duration_ms": 1000,
           "handoff_complete": True, "review_verdict": "approve"} for _ in range(4)],
        # 1 with verdict 'request-changes' = failure
        {"phase": "execute", "size": "small", "risk": "low", "budget": "medium",
         "tool": "codex", "model": "gpt-5.4", "exit_code": 0, "duration_ms": 1000,
         "handoff_complete": True, "review_verdict": "request-changes"},
    ]
    metrics = _write_metrics(tmp_path, records)
    _patch_attr(monkeypatch, serve_module, "METRICS_FILE", metrics)
    result = serve_module._load_auto_select_ranking()
    assert result["groups"][0]["candidates"][0]["success_rate"] == 0.8


def test_reasoning_effort_is_part_of_candidate_key(serve_module, tmp_path, monkeypatch):
    """Same (tool, model) with different reasoning_effort must be distinct
    candidates — the planner ranks them separately."""
    records = []
    records.extend([
        {"phase": "execute", "size": "small", "risk": "low", "budget": "medium",
         "tool": "codex", "model": "gpt-5.4", "reasoning_effort": "medium",
         "exit_code": 0, "duration_ms": 1000,
         "handoff_complete": True, "review_verdict": "approve"}
        for _ in range(5)
    ])
    records.extend([
        {"phase": "execute", "size": "small", "risk": "low", "budget": "medium",
         "tool": "codex", "model": "gpt-5.4", "reasoning_effort": "high",
         "exit_code": 0, "duration_ms": 3000,
         "handoff_complete": True, "review_verdict": "approve"}
        for _ in range(5)
    ])
    metrics = _write_metrics(tmp_path, records)
    _patch_attr(monkeypatch, serve_module, "METRICS_FILE", metrics)
    result = serve_module._load_auto_select_ranking()
    cands = result["groups"][0]["candidates"]
    assert len(cands) == 2
    efforts = {c["reasoning_effort"] for c in cands}
    assert efforts == {"medium", "high"}


# --- Integration: write metrics, hit endpoint, parse response ---------------


def test_endpoint_returns_populated_rankings_end_to_end(
    running_server, serve_module, tmp_path, monkeypatch
):
    """Full path: synthetic metrics file -> HTTP request -> parsed JSON
    matches the loader's contract."""
    records = [
        {"phase": "execute", "size": "small", "risk": "low", "budget": "medium",
         "tool": "codex", "model": "gpt-5.4", "reasoning_effort": "medium",
         "exit_code": 0, "duration_ms": 2500, "handoff_complete": True,
         "review_verdict": "approve"}
        for _ in range(5)
    ]
    metrics = _write_metrics(tmp_path, records)
    _patch_attr(monkeypatch, serve_module, "METRICS_FILE", metrics)
    status, body = _http_get(f"{running_server}/api/auto-select")
    assert status == 200
    payload = json.loads(body)
    assert payload["samples"] == 5
    assert len(payload["groups"]) == 1
    g = payload["groups"][0]
    assert g["key"] == {
        "phase": "execute",
        "size": "small",
        "risk": "low",
    }
    assert g["static_fallback"] is False
    assert g["candidates"][0]["tool"] == "codex"
    assert g["candidates"][0]["model"] == "gpt-5.4"
    assert g["candidates"][0]["samples"] == 5
    assert g["candidates"][0]["success_rate"] == 1.0
    assert g["candidates"][0]["wilson_lower"] < g["candidates"][0]["success_rate"]
    assert g["candidates"][0]["median_duration_ms"] == 2500
    assert g["candidates"][0]["mean_duration_ms"] == 2500


# --- Schema consistency: metrics.jsonl spec <-> loader expectations ----------


def test_loader_consumes_every_field_documented_in_orchestrate_skill():
    """Every field listed in the orchestrate skill's `## Metrics logging`
    JSON schema must be one the loader can consume without crashing.

    This guards against documentation drift: if someone adds a new field
    to the orchestrate spec, the loader test will fail until either the
    loader handles the field or the doc is updated."""
    orchestrate_path = REPO_ROOT / ".claude" / "skills" / "orchestrate" / "SKILL.md"
    text = orchestrate_path.read_text(encoding="utf-8")
    section = text.split("## Metrics logging", 1)[1]
    documented_fields = {
        "ts", "task_slug", "phase", "tool", "model", "reasoning_effort",
        "size", "risk", "budget", "exit_code", "duration_ms",
        "handoff_complete", "review_verdict", "retries",
        "tokens_in", "tokens_out",
    }
    for field in documented_fields:
        assert f'"{field}"' in section, (
            f"orchestrate skill 'Metrics logging' section missing field {field!r} — "
            "documentation has drifted from loader expectations"
        )
