from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import auto_select_scorer
from auto_select_scorer import budget_alignment, score_groups, wilson_lower_bound

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVE_PATH = REPO_ROOT / ".ai" / "dashboard" / "serve.py"

sys.path.insert(0, str(REPO_ROOT / ".ai" / "dashboard"))
import server.analytics as _an  # noqa: E402 — _load_auto_select_ranking reads METRICS_FILE here (follows-the-move)


def _record(
    *,
    ts: str,
    tool: str = "codex",
    model: str = "gpt-5.5",
    effort: str | None = "medium",
    phase: str = "execute",
    size: str = "small",
    risk: str = "low",
    budget: str | None = "medium",
    exit_code: int = 0,
    duration_ms: int = 1000,
    handoff_complete=True,
    review_verdict: str | None = "approve",
) -> dict:
    return {
        "ts": ts,
        "phase": phase,
        "size": size,
        "risk": risk,
        "budget": budget,
        "tool": tool,
        "model": model,
        "reasoning_effort": effort,
        "exit_code": exit_code,
        "duration_ms": duration_ms,
        "handoff_complete": handoff_complete,
        "review_verdict": review_verdict,
    }


def test_score_groups_empty_input_shape():
    result = score_groups([None, {"phase": ""}], min_samples=5)

    assert result == {
        "samples": 0,
        "min_samples": 5,
        "groups": [],
        "dropped_candidates": 0,
        "last_record_ts": None,
    }


def test_budget_alignment_rewards_matching_effort():
    assert budget_alignment("medium", "medium") == 1.0
    assert budget_alignment("xhigh", "high") == 1.0
    assert budget_alignment("max", "high") == 1.0
    assert budget_alignment("low", "medium") == 0.5
    assert budget_alignment("low", "high") == 0.0
    assert budget_alignment(None, "medium") == 0.5
    assert budget_alignment("unknown", "medium") == 0.5


def test_grouping_key_excludes_budget():
    records = [
        _record(ts=f"2026-01-01T00:00:0{i}Z", budget="low")
        for i in range(3)
    ]
    records.extend(
        _record(ts=f"2026-01-01T00:00:0{i}Z", budget="high")
        for i in range(3, 6)
    )

    result = score_groups(records, min_samples=5, effective_budget="medium")

    assert result["samples"] == 6
    assert len(result["groups"]) == 1
    group = result["groups"][0]
    assert group["key"] == {"phase": "execute", "size": "small", "risk": "low"}
    assert group["candidates"][0]["samples"] == 6


def test_guard_rail_flags_low_sr_divergent_top():
    records = [
        _record(
            ts=f"2026-01-01T00:00:0{i}Z",
            model="adaptive",
            effort="high",
            duration_ms=1000,
        )
        for i in range(5)
    ]
    records.extend(
        _record(
            ts=f"2026-01-01T00:00:0{i}Z",
            model="static",
            effort="medium",
            duration_ms=5000,
            exit_code=0 if i < 9 else 1,
        )
        for i in range(5, 10)
    )

    result = score_groups(
        records,
        min_samples=5,
        effective_budget="high",
        static_pick=("codex", "static", "medium"),
    )

    group = result["groups"][0]
    assert group["candidates"][0]["model"] == "adaptive"
    assert group["candidates"][0]["wilson_lower"] < 0.7
    assert group["static_fallback"] is True


def test_correctness_floor_gate_beats_blend():
    records = [
        _record(
            ts=f"2026-01-01T00:00:{i:02d}Z",
            model="correct-slow",
            effort="medium",
            duration_ms=5000,
        )
        for i in range(20)
    ]
    records.extend(
        _record(
            ts=f"2026-01-01T00:01:{i:02d}Z",
            model="fast-flaky",
            effort="medium",
            duration_ms=100,
            exit_code=0 if i < 15 else 1,
        )
        for i in range(20)
    )

    result = score_groups(records, min_samples=5, effective_budget="medium")

    group = result["groups"][0]
    candidates = {candidate["model"]: candidate for candidate in group["candidates"]}
    assert group["candidates"][0]["model"] == "correct-slow"
    assert candidates["fast-flaky"]["wilson_lower"] < 0.7
    assert candidates["correct-slow"]["wilson_lower"] >= 0.7


def test_among_cleared_blend_orders_by_score():
    records = [
        _record(
            ts=f"2026-01-01T00:00:{i:02d}Z",
            model="correct-fast",
            effort="medium",
            duration_ms=100,
        )
        for i in range(20)
    ]
    records.extend(
        _record(
            ts=f"2026-01-01T00:01:{i:02d}Z",
            model="correct-slow",
            effort="medium",
            duration_ms=5000,
        )
        for i in range(20)
    )

    result = score_groups(records, min_samples=5, effective_budget="medium")

    group = result["groups"][0]
    candidates = {candidate["model"]: candidate for candidate in group["candidates"]}
    assert candidates["correct-fast"]["wilson_lower"] >= 0.7
    assert candidates["correct-slow"]["wilson_lower"] >= 0.7
    assert group["candidates"][0]["model"] == "correct-fast"
    assert candidates["correct-fast"]["score"] > candidates["correct-slow"]["score"]


def test_wilson_penalizes_small_samples():
    records = [
        _record(ts=f"2026-01-01T00:00:{i:02d}Z", model="small-sample")
        for i in range(5)
    ]
    records.extend(
        _record(ts=f"2026-01-01T00:01:{i:02d}Z", model="large-sample")
        for i in range(50)
    )

    result = score_groups(records, min_samples=5, effective_budget="medium")
    candidates = {c["model"]: c for c in result["groups"][0]["candidates"]}

    assert wilson_lower_bound(5, 5) < wilson_lower_bound(50, 50)
    assert candidates["small-sample"]["success_rate"] == 1.0
    assert candidates["large-sample"]["success_rate"] == 1.0
    assert candidates["small-sample"]["wilson_lower"] < candidates["large-sample"]["wilson_lower"]
    assert result["groups"][0]["candidates"][0]["model"] == "large-sample"


def test_median_duration_and_per_group_tail():
    records = [
        _record(
            ts=f"2026-01-01T00:00:0{i}Z",
            model="stale",
            duration_ms=10,
        )
        for i in range(2)
    ]
    records.extend(
        [
            _record(ts="2026-01-01T00:00:02Z", model="median-fast", duration_ms=100),
            _record(ts="2026-01-01T00:00:03Z", model="median-fast", duration_ms=100),
            _record(ts="2026-01-01T00:00:04Z", model="median-fast", duration_ms=1000),
            _record(ts="2026-01-01T00:00:05Z", model="steady", duration_ms=300),
            _record(ts="2026-01-01T00:00:06Z", model="steady", duration_ms=300),
            _record(ts="2026-01-01T00:00:07Z", model="steady", duration_ms=300),
        ]
    )

    result = score_groups(
        records,
        min_samples=3,
        effective_budget="medium",
        per_group_tail=6,
    )

    group = result["groups"][0]
    candidates = {candidate["model"]: candidate for candidate in group["candidates"]}
    assert result["samples"] == 6
    assert "stale" not in candidates
    assert candidates["median-fast"]["median_duration_ms"] == 100
    assert candidates["median-fast"]["mean_duration_ms"] == 400
    assert group["candidates"][0]["model"] == "median-fast"


def _load_serve_module():
    dashboard_dir = str(SERVE_PATH.parent)
    scripts_dir = str(SERVE_PATH.parent.parent / "scripts")
    for path in (scripts_dir, dashboard_dir):
        if path not in sys.path:
            sys.path.insert(0, path)
    spec = importlib.util.spec_from_file_location("dashboard_serve_autoselect_delegate", SERVE_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["dashboard_serve_autoselect_delegate"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_serve_delegates_to_scorer_helper(tmp_path, monkeypatch):
    rows = [
        _record(ts=f"2026-01-01T00:00:{i:02d}Z", model="gpt-5.5", duration_ms=1000)
        for i in range(5)
    ]
    rows.extend(
        _record(
            ts=f"2026-01-01T00:01:{i:02d}Z",
            model="gpt-5.4",
            effort="high",
            duration_ms=1500,
            exit_code=0 if i < 4 else 1,
        )
        for i in range(5)
    )

    metrics = tmp_path / "metrics.jsonl"
    metrics.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    serve = _load_serve_module()
    monkeypatch.setattr(serve, "METRICS_FILE", metrics)
    monkeypatch.setattr(_an, "METRICS_FILE", metrics)  # follows-the-move
    with serve._JSONL_CACHE_LOCK:
        serve._JSONL_CACHE.pop(str(metrics), None)

    result = serve._load_auto_select_ranking(max_records=200, min_samples=5)
    expected = auto_select_scorer.score_groups(
        rows,
        min_samples=5,
        effective_budget=None,
        per_group_tail=200,
        static_pick=None,
    )

    assert result == expected
    assert set(result) == {
        "samples",
        "min_samples",
        "groups",
        "dropped_candidates",
        "last_record_ts",
    }
    group = result["groups"][0]
    assert set(group) == {"key", "static_fallback", "candidates"}
    assert set(group["key"]) == {"phase", "size", "risk"}
    assert {
        "tool",
        "model",
        "reasoning_effort",
        "samples",
        "success_rate",
        "wilson_lower",
        "median_duration_ms",
        "mean_duration_ms",
        "score",
    }.issubset(group["candidates"][0])


def test_default_min_samples_is_5(tmp_path, monkeypatch):
    records = [
        _record(ts=f"2026-01-01T00:00:{i:02d}Z", model="too-few")
        for i in range(4)
    ]
    records.extend(
        _record(ts=f"2026-01-01T00:01:{i:02d}Z", model="enough")
        for i in range(5)
    )

    scorer_result = auto_select_scorer.score_groups(records)

    metrics = tmp_path / "metrics.jsonl"
    metrics.write_text("\n".join(json.dumps(row) for row in records) + "\n", encoding="utf-8")

    serve = _load_serve_module()
    monkeypatch.setattr(serve, "METRICS_FILE", metrics)
    monkeypatch.setattr(_an, "METRICS_FILE", metrics)  # follows-the-move
    with serve._JSONL_CACHE_LOCK:
        serve._JSONL_CACHE.pop(str(metrics), None)

    serve_result = serve._load_auto_select_ranking(max_records=200)

    for result in (scorer_result, serve_result):
        assert result["min_samples"] == 5
        assert result["dropped_candidates"] == 1
        assert len(result["groups"]) == 1
        candidates = result["groups"][0]["candidates"]
        assert [candidate["model"] for candidate in candidates] == ["enough"]
        assert candidates[0]["samples"] == 5
