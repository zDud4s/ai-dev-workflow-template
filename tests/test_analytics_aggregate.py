"""Unit tests for the dashboard analytics aggregation (serve._aggregate_analytics
and its helpers). See .ai/specs/2026-06-02-analytics-page-design.md and
.ai/plans/2026-06-02-analytics-page.md."""
from __future__ import annotations

import datetime as dt
import json

import pytest

import serve


def _write_jsonl(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


@pytest.fixture
def ledgers(tmp_path, monkeypatch):
    """Point every analytics ledger constant at a tmp file and clear the cache.

    VERIFIED constant names (note JOBS_PERSIST_FILE / IMPROVEMENTS_LEDGER); no
    raising=False, so a typo fails loudly rather than creating a dead attribute."""
    files = {}
    for const, name in [
        ("METRICS_FILE", "metrics.jsonl"),
        ("SKILL_METRICS_FILE", "skill_metrics.jsonl"),
        ("JOBS_PERSIST_FILE", "jobs.jsonl"),
        ("TODOS_FILE", "todos.jsonl"),
        ("IMPROVEMENTS_LEDGER", "improvements.jsonl"),
        ("EVENTS_FILE", "events.jsonl"),
    ]:
        p = tmp_path / name
        p.write_text("", encoding="utf-8")
        monkeypatch.setattr(serve, const, p)
        files[name] = p
    with serve._JSONL_CACHE_LOCK:
        serve._JSONL_CACHE.clear()
    return files


NOW = dt.datetime(2026, 6, 2, tzinfo=dt.timezone.utc)


def _recent(days):
    return (NOW - dt.timedelta(days=days)).isoformat()


def test_range_bounds_30d():
    now = dt.datetime(2026, 6, 2, tzinfo=dt.timezone.utc)
    cur_start, prev_start = serve._analytics_range_bounds(now, "30d")
    assert cur_start == now - dt.timedelta(days=30)
    assert prev_start == now - dt.timedelta(days=60)


def test_range_bounds_all_has_no_previous():
    now = dt.datetime(2026, 6, 2, tzinfo=dt.timezone.utc)
    cur_start, prev_start = serve._analytics_range_bounds(now, "all")
    assert cur_start is None      # no lower bound
    assert prev_start is None     # no previous-period delta for "all"


def test_range_bounds_defaults_unknown_to_30d():
    now = dt.datetime(2026, 6, 2, tzinfo=dt.timezone.utc)
    assert serve._analytics_range_bounds(now, "bogus") == serve._analytics_range_bounds(now, "30d")


def test_parse_ts_handles_z_and_offset_and_garbage():
    assert serve._analytics_parse_ts("2026-05-18T00:00:00Z") is not None
    assert serve._analytics_parse_ts("2026-05-17T03:05:09+00:00") is not None
    assert serve._analytics_parse_ts(None) is None
    assert serve._analytics_parse_ts("not-a-date") is None


def test_in_range_inclusive_lower_open_upper():
    now = dt.datetime(2026, 6, 2, tzinfo=dt.timezone.utc)
    start = now - dt.timedelta(days=30)
    inside = (now - dt.timedelta(days=1)).isoformat()
    outside = (now - dt.timedelta(days=40)).isoformat()
    assert serve._analytics_in_range(inside, start, now) is True
    assert serve._analytics_in_range(outside, start, now) is False
    # start=None means "no lower bound" (the 'all' range)
    assert serve._analytics_in_range(outside, None, now) is True


def test_kpi_phase_runs_and_success_rate(ledgers):
    _write_jsonl(ledgers["metrics.jsonl"], [
        {"ts": _recent(1), "phase": "plan", "exit_code": 0, "duration_ms": 1000},
        {"ts": _recent(2), "phase": "execute", "exit_code": 0, "duration_ms": 3000},
        {"ts": _recent(3), "phase": "execute", "exit_code": 1, "duration_ms": 2000},
        {"ts": _recent(45), "phase": "plan", "exit_code": 0, "duration_ms": 9000},  # out of 30d range
    ])
    with serve._JSONL_CACHE_LOCK:
        serve._JSONL_CACHE.clear()
    out = serve._aggregate_analytics(NOW, "30d")
    assert out["range"] == "30d"
    assert out["kpis"]["phase_runs"]["value"] == 3
    assert out["kpis"]["success_rate"]["value"] == pytest.approx(2 / 3, abs=1e-3)


def test_cost_by_model_and_total_spend(ledgers):
    _write_jsonl(ledgers["jobs.jsonl"], [
        {"created_at": _recent(2), "model": "claude-opus-4-7", "cost": {"cost_usd": 1.50}},
        {"created_at": _recent(3), "model": "claude-sonnet-4-6", "cost": {"cost_usd": 0.50}},
    ])
    _write_jsonl(ledgers["skill_metrics.jsonl"], [
        {"ts": _recent(1), "model": "claude-opus-4-7", "cost_usd": 0.25},
        {"ts": _recent(1), "model": None, "cost_usd": 0.10},  # null model -> "unknown"
    ])
    with serve._JSONL_CACHE_LOCK:
        serve._JSONL_CACHE.clear()
    out = serve._aggregate_analytics(NOW, "30d")
    assert out["kpis"]["total_spend"]["value"] == pytest.approx(2.35, abs=1e-6)
    by_model = {d["model"]: d["usd"] for d in out["cost"]["by_model"]}
    assert by_model["claude-opus-4-7"] == pytest.approx(1.75, abs=1e-6)
    assert by_model["unknown"] == pytest.approx(0.10, abs=1e-6)


def test_health_outcomes_and_verdicts(ledgers):
    _write_jsonl(ledgers["metrics.jsonl"], [
        {"ts": _recent(1), "phase": "execute", "exit_code": 0, "duration_ms": 1,
         "retries": 0, "review_verdict": "approve"},
        {"ts": _recent(1), "phase": "execute", "exit_code": 1, "duration_ms": 1,
         "retries": 2, "review_verdict": "request-changes"},
        {"ts": _recent(2), "phase": "review", "exit_code": 0, "duration_ms": 1,
         "retries": 0, "review_verdict": None},
    ])
    with serve._JSONL_CACHE_LOCK:
        serve._JSONL_CACHE.clear()
    out = serve._aggregate_analytics(NOW, "30d")
    assert out["health"]["outcomes"] == {"done": 2, "failed": 1}
    assert out["health"]["review_verdicts"]["approve"] == 1
    assert out["health"]["review_verdicts"]["request-changes"] == 1
    assert out["health"]["review_verdicts"]["none"] == 1   # null -> "none"


def test_skills_table_and_top(ledgers):
    _write_jsonl(ledgers["skill_metrics.jsonl"], [
        {"ts": _recent(1), "skill": "planner", "outcome": "done", "cost_usd": 0.01, "invocations": 1},
        {"ts": _recent(1), "skill": "planner", "outcome": "error", "cost_usd": 0.02, "invocations": 1},
        {"ts": _recent(2), "skill": "reviewer", "outcome": "done", "cost_usd": 0.00, "invocations": 1},
    ])
    with serve._JSONL_CACHE_LOCK:
        serve._JSONL_CACHE.clear()
    out = serve._aggregate_analytics(NOW, "30d")
    top = {d["skill"]: d for d in out["skills"]["top_by_invocations"]}
    assert top["planner"]["invocations"] == 2
    tbl = {d["skill"]: d for d in out["skills"]["table"]}
    assert tbl["planner"]["runs"] == 2
    assert tbl["planner"]["success_rate"] == pytest.approx(0.5, abs=1e-3)


def test_backlog_proposal_buckets_and_todo_kpi(ledgers):
    _write_jsonl(ledgers["improvements.jsonl"], [
        {"ts": _recent(1), "status": "pending"},
        {"ts": _recent(1), "status": "installed"},   # folds into applied
        {"ts": _recent(1), "status": "applied"},
        {"ts": _recent(1), "status": "no_change"},
    ])
    _write_jsonl(ledgers["todos.jsonl"], [
        {"created_at": _recent(1), "status": "open"},
        {"created_at": _recent(1), "status": "resolved"},
    ])
    with serve._JSONL_CACHE_LOCK:
        serve._JSONL_CACHE.clear()
    out = serve._aggregate_analytics(NOW, "30d")
    ps = out["backlog"]["proposal_status"]
    assert ps["pending"] == 1 and ps["applied"] == 2 and ps["no_change"] == 1
    # open_todos / pending_proposals are current-state totals (not range-filtered)
    assert out["kpis"]["open_todos"]["value"] == 1
    assert out["kpis"]["pending_proposals"]["value"] == 1


def test_empty_ledgers_return_safe_shape(ledgers):
    out = serve._aggregate_analytics(NOW, "30d")
    assert out["kpis"]["success_rate"]["value"] is None      # no rows -> None, not crash
    assert out["cost"]["by_model"] == []
    assert out["health"]["outcomes"] == {}


def test_malformed_jsonl_line_is_skipped(ledgers):
    ledgers["metrics.jsonl"].write_text(
        '{"ts":"%s","exit_code":0,"duration_ms":5}\nNOT JSON\n' % _recent(1),
        encoding="utf-8",
    )
    with serve._JSONL_CACHE_LOCK:
        serve._JSONL_CACHE.clear()
    out = serve._aggregate_analytics(NOW, "30d")
    assert out["kpis"]["phase_runs"]["value"] == 1


def test_all_range_has_null_previous_deltas(ledgers):
    _write_jsonl(ledgers["metrics.jsonl"], [{"ts": _recent(100), "exit_code": 0, "duration_ms": 1}])
    with serve._JSONL_CACHE_LOCK:
        serve._JSONL_CACHE.clear()
    out = serve._aggregate_analytics(NOW, "all")
    assert out["kpis"]["phase_runs"]["value"] == 1
    assert out["kpis"]["phase_runs"]["prev"] == 0   # 'all' => no previous window
