"""The auto-improver audit gate must treat only RECENT failures as a signal.

Regression (2026-06): planner's per-skill telemetry was frozen at demo-seed
rows from 2026-05-17 (5 failed / 9 total). With no recency window,
``_has_audit_signal`` counted those stale failures forever, so the periodic
sweep re-audited planner on every wake and the LLM kept emitting speculative
(often wrong) path edits. Failures older than the recency window must NOT
trigger an audit; recent ones still must.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / ".ai" / "dashboard"))
import serve  # noqa: E402 — the module under test

NOW = "2026-06-02T00:00:00+00:00"


def _not_first_time(monkeypatch):
    # Force past the first-time-audit branch so we exercise the failure window.
    monkeypatch.setattr(serve, "_last_improver_run_ts", lambda _sid: 1.0)


def test_stale_failures_do_not_trigger_audit(monkeypatch):
    _not_first_time(monkeypatch)
    now = serve._iso_to_epoch(NOW)
    # The frozen 2026-05-17 demo seeds: all failures are >2 weeks old.
    recent = [
        {"outcome": "failed", "ts": "2026-05-17T04:06:09+00:00"},
        {"outcome": "failed", "ts": "2026-05-17T04:08:09+00:00"},
        {"outcome": "done", "ts": "2026-05-17T03:05:09+00:00"},
    ]
    should, reason = serve._has_audit_signal("planner", recent, now=now)
    assert should is False, reason


def test_recent_failure_triggers_audit(monkeypatch):
    _not_first_time(monkeypatch)
    now = serve._iso_to_epoch(NOW)
    recent = [
        {"outcome": "failed", "ts": "2026-06-01T12:00:00+00:00"},  # within window
        {"outcome": "done", "ts": "2026-05-17T03:05:09+00:00"},
    ]
    should, reason = serve._has_audit_signal("planner", recent, now=now)
    assert should is True
    assert "recent failure" in reason


def test_failure_with_unparseable_ts_still_counts(monkeypatch):
    # Conservative: a failure we cannot prove is stale is still a signal.
    _not_first_time(monkeypatch)
    now = serve._iso_to_epoch(NOW)
    recent = [{"outcome": "failed", "ts": None}]
    should, _reason = serve._has_audit_signal("planner", recent, now=now)
    assert should is True


def test_first_time_audit_still_fires(monkeypatch):
    monkeypatch.setattr(serve, "_last_improver_run_ts", lambda _sid: 0.0)
    should, reason = serve._has_audit_signal(
        "never-seen", [], now=serve._iso_to_epoch(NOW)
    )
    assert should is True
    assert reason == "first-time audit"
