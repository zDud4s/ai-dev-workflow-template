"""Per-skill telemetry rollup for the dashboard skill cards and the auto-improver.

Extracted from serve.py. Two ledger sources feed one rollup: job-scoped rows
from ``SKILL_METRICS_FILE`` (written by serve's ``_record_skill_metrics``) plus
``METRICS_FILE`` phase rows re-keyed through ``PHASE_TO_SKILL`` so phase-only
skills (planner / reviewer / rescue, which never run as standalone dashboard
jobs) accrue per-skill telemetry too. The two sources never overlap on a skill,
so merging cannot double-count. serve.py re-exports these names via a shim.
"""
from __future__ import annotations

from server.paths import METRICS_FILE, SKILL_METRICS_FILE
from server.storage import _load_jsonl_cached
from server.validation import _skill_name_canonical


# A dispatched phase in ``METRICS_FILE`` maps back to the project skill that
# fulfils it, so phase-only skills (planner/reviewer/rescue) accrue per-skill
# telemetry even though they never run as standalone dashboard jobs. ``execute``
# is omitted on purpose: the executor is a configured tool/model, not a
# SKILL.md, so there is nothing for the improver to edit.
PHASE_TO_SKILL = {
    "plan": "planner",
    "review": "reviewer",
    "rescue": "rescue",
}


def _phase_metric_rows() -> list[dict]:
    """Re-key ``METRICS_FILE`` phase rows into ``skill_metrics``-shaped rows.

    One metrics.jsonl row == one dispatched phase. Rows whose phase has no
    project skill (e.g. ``execute``) or no usable ``exit_code`` are dropped so
    we never fabricate a skill or a phantom failure signal. Each survivor is
    shaped exactly like a ``SKILL_METRICS_FILE`` row so ``_aggregate_skill_metrics``
    can fold it into the same rollup."""
    out: list[dict] = []
    for row in _load_jsonl_cached(METRICS_FILE):
        if not isinstance(row, dict):
            continue
        sid = PHASE_TO_SKILL.get(str(row.get("phase") or ""))
        if not sid:
            continue
        ec = row.get("exit_code")
        if ec is None:
            continue  # no outcome recorded — not a signal either way
        out.append({
            "ts": row.get("ts"),
            "skill": sid,
            "name": _skill_name_canonical(sid),
            "job_id": row.get("task_slug"),
            "kind": f"phase:{row.get('phase')}",
            "outcome": "done" if ec == 0 else "failed",
            "exit_code": ec,
            "duration_ms": int(row.get("duration_ms") or 0),
            "cost_usd": 0.0,
            "turns": 0,
            "invocations": 1,
            "session_id": row.get("session_id"),
            "model": row.get("model"),
        })
    return out


def _aggregate_skill_metrics() -> dict[str, dict]:
    """Roll up per-skill telemetry into summaries the dashboard renders in
    skill cards (and the auto-improver gates on).

    Sources: job-scoped rows from ``SKILL_METRICS_FILE`` PLUS phase-scoped rows
    bridged from ``METRICS_FILE`` (see ``_phase_metric_rows``) so skills that
    run only as dispatched phases are represented too. The two sources never
    overlap on a skill (orchestrate has no matching phase; plan/review/rescue
    have no job kind), so merging cannot double-count."""
    by_skill: dict[str, dict] = {}
    for row in [*_load_jsonl_cached(SKILL_METRICS_FILE), *_phase_metric_rows()]:
        sid = row.get("skill") or ""
        if not sid:
            continue
        agg = by_skill.setdefault(sid, {
            "skill": sid,
            "name": row.get("name") or _skill_name_canonical(sid),
            "total_jobs": 0,
            "total_invocations": 0,
            "successes": 0,
            "failures": 0,
            "total_cost_usd": 0.0,
            "total_duration_ms": 0,
            "last_used": None,
            "last_outcome": None,
            "recent": [],
        })
        agg["total_jobs"] += 1
        agg["total_invocations"] += int(row.get("invocations") or 1)
        if row.get("outcome") == "done":
            agg["successes"] += 1
        else:
            agg["failures"] += 1
        agg["total_cost_usd"] += float(row.get("cost_usd") or 0.0)
        agg["total_duration_ms"] += int(row.get("duration_ms") or 0)
        ts = row.get("ts")
        if ts and (agg["last_used"] is None or ts > agg["last_used"]):
            agg["last_used"] = ts
            agg["last_outcome"] = row.get("outcome")
        agg["recent"].append({
            "ts": ts,
            "job_id": row.get("job_id"),
            "kind": row.get("kind"),
            "outcome": row.get("outcome"),
            "cost_usd": row.get("cost_usd"),
            "duration_ms": row.get("duration_ms"),
        })
    for agg in by_skill.values():
        total = agg["total_jobs"] or 1
        agg["success_rate"] = round(agg["successes"] / total, 4)
        agg["avg_cost_usd"] = round(agg["total_cost_usd"] / total, 6)
        agg["avg_duration_ms"] = int(agg["total_duration_ms"] / total)
        agg["recent"] = sorted(agg["recent"], key=lambda r: r.get("ts") or "", reverse=True)[:10]
    return by_skill
