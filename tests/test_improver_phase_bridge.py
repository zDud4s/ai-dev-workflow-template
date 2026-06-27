"""Phase rows from metrics.jsonl must surface as per-skill telemetry.

planner/reviewer/rescue only ever run as *dispatched phases* inside an
orchestrate job, never as standalone dashboard jobs, so they produced no
``skill_metrics.jsonl`` rows and were invisible to the auto-improver and the
dashboard skill cards. Their real outcomes live in ``metrics.jsonl`` keyed by
phase; this bridge re-keys them to the skill that fulfils each phase.
"""
from __future__ import annotations

import json

import pytest

import serve
import server.metrics as _metrics  # _aggregate_skill_metrics/_phase_metric_rows read the ledger consts here (follows-the-move)


def _write_jsonl(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


@pytest.fixture
def ledgers(tmp_path, monkeypatch):
    files = {}
    for const, name in [("METRICS_FILE", "metrics.jsonl"),
                        ("SKILL_METRICS_FILE", "skill_metrics.jsonl")]:
        p = tmp_path / name
        p.write_text("", encoding="utf-8")
        monkeypatch.setattr(serve, const, p)
        monkeypatch.setattr(_metrics, const, p)  # follows-the-move: rollup reads consts in server.metrics
        files[name] = p
    with serve._JSONL_CACHE_LOCK:
        serve._JSONL_CACHE.clear()
    return files


def _refresh(ledgers, rows):
    _write_jsonl(ledgers["metrics.jsonl"], rows)
    with serve._JSONL_CACHE_LOCK:
        serve._JSONL_CACHE.clear()


def test_plan_phase_rows_surface_as_planner(ledgers):
    _refresh(ledgers, [
        {"ts": "2026-06-01T00:00:00+00:00", "phase": "plan", "exit_code": 0,
         "duration_ms": 1000, "model": "claude-opus-4-7", "task_slug": "t1"},
        {"ts": "2026-06-01T01:00:00+00:00", "phase": "plan", "exit_code": 1,
         "duration_ms": 1000, "task_slug": "t2"},
        {"ts": "2026-06-01T02:00:00+00:00", "phase": "review", "exit_code": 0,
         "task_slug": "t3"},
        {"ts": "2026-06-01T03:00:00+00:00", "phase": "rescue", "exit_code": 0,
         "task_slug": "t4"},
    ])
    agg = serve._aggregate_skill_metrics()
    assert "planner" in agg
    assert agg["planner"]["total_jobs"] == 2
    assert agg["planner"]["failures"] == 1
    assert {r["outcome"] for r in agg["planner"]["recent"]} == {"done", "failed"}
    assert "reviewer" in agg
    assert "rescue" in agg


def test_execute_phase_not_bridged(ledgers):
    # No executor SKILL.md exists; an execute row must not invent a skill.
    _refresh(ledgers, [
        {"ts": "2026-06-01T00:00:00+00:00", "phase": "execute", "exit_code": 0,
         "task_slug": "t1"},
    ])
    agg = serve._aggregate_skill_metrics()
    assert "executor" not in agg
    assert "execute" not in agg


def test_null_exit_code_phase_row_skipped(ledgers):
    # An unrecorded outcome must not be fabricated into a failure signal.
    _refresh(ledgers, [
        {"ts": "2026-06-01T00:00:00+00:00", "phase": "plan", "exit_code": None,
         "task_slug": "t1"},
    ])
    agg = serve._aggregate_skill_metrics()
    assert "planner" not in agg


def test_bridged_failure_feeds_audit_gate(ledgers):
    # End-to-end: a recent plan failure should now be a real audit signal.
    _refresh(ledgers, [
        {"ts": "2026-06-01T12:00:00+00:00", "phase": "plan", "exit_code": 1,
         "task_slug": "t1"},
    ])
    recent = serve._aggregate_skill_metrics()["planner"]["recent"]
    now = serve._iso_to_epoch("2026-06-02T00:00:00+00:00")
    should, reason = serve._has_audit_signal("planner", recent, now=now)
    assert should is True
    assert "recent failure" in reason
