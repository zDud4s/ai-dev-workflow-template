"""Analytics page aggregation + timeline / auto-select ranking.

Extracted from serve.py. Pure, ``now``-injected aggregation over the six
ledgers (read through the mtime-keyed ``_load_jsonl_cached``), never raising on
null / missing / malformed rows:

  * ``_aggregate_analytics(now, range_key)`` -- the analytics-page rollup
    (jobs, cost, success rate, todos, improver activity) for a time range,
    with current-vs-previous-period deltas. ``_analytics_range_bounds`` /
    ``_analytics_parse_ts`` / ``_analytics_in_range`` are its helpers and
    ``_ANALYTICS_RANGES`` its window table.
  * ``_load_timeline_runs`` -- recent phase-dispatch events shaped for the
    timeline view (task resolved via ``_lookup_session_task``).
  * ``_load_auto_select_ranking`` -- adaptive model rankings from
    ``METRICS_FILE`` via ``auto_select_scorer``.

serve.py re-exports every name via a shim; the analytics endpoints stay in the
Handler. NOTE: these read path constants (METRICS_FILE / EVENTS_FILE / ...) in
THIS module's namespace, so tests that rebind them on ``serve`` must also rebind
``server.analytics.<CONST>`` (follows-the-move).
"""
from __future__ import annotations

import datetime as _dt
import sys as _sys

import auto_select_scorer

from server.paths import (
    EVENTS_FILE,
    IMPROVEMENTS_LEDGER,
    JOBS_PERSIST_FILE,
    METRICS_FILE,
    PRICING_FILE,
    ROOT,
    SKILL_METRICS_FILE,
    TODOS_FILE,
)
from server.storage import _load_jsonl_cached
from server.transcripts import _lookup_session_task
from server.validation import _parse_iso_ts

# Savings helpers live in the eval harness (`.ai/eval/harness/savings.py`), not
# the dashboard package or .ai/scripts. The conftest/serve sys.path setup does
# not cover it, so insert it here. Keep the import optional so analytics fails
# safe (savings -> None, the page still serves) if the harness is unavailable.
_EVAL_HARNESS_DIR = str(ROOT / ".ai" / "eval" / "harness")
if _EVAL_HARNESS_DIR not in _sys.path:
    _sys.path.insert(0, _EVAL_HARNESS_DIR)
try:
    from savings import load_pricing, savings_report  # noqa: E402
    _SAVINGS_IMPORT_ERROR = None
except Exception as _exc:  # pragma: no cover - import failure is environment-specific
    load_pricing = None
    savings_report = None
    _SAVINGS_IMPORT_ERROR = _exc


_ANALYTICS_RANGES = {"7d": 7, "30d": 30, "90d": 90, "all": None}


def _analytics_range_bounds(now, range_key):
    """Return ``(current_period_start, previous_period_start)`` as tz-aware
    datetimes. ``"all"`` returns ``(None, None)``: no lower bound and no
    previous-period delta. Unknown keys fall back to 30d."""
    days = _ANALYTICS_RANGES.get(range_key, 30)
    if days is None:
        return None, None
    cur_start = now - _dt.timedelta(days=days)
    prev_start = now - _dt.timedelta(days=days * 2)
    return cur_start, prev_start


def _analytics_parse_ts(raw):
    """Parse an ISO-8601 ledger timestamp to a tz-aware datetime, or None.
    Accepts a trailing ``Z`` and explicit offsets; never raises."""
    if not raw or not isinstance(raw, str):
        return None
    try:
        d = _dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=_dt.timezone.utc)
    return d


def _analytics_in_range(raw_ts, start, now):
    """True if ``raw_ts`` falls within ``[start, now]``. ``start=None`` means no
    lower bound (the 'all' range). Unparseable / future timestamps are excluded."""
    d = _analytics_parse_ts(raw_ts)
    if d is None:
        return False
    if d > now:
        return False
    if start is not None and d < start:
        return False
    return True


def _analytics_savings(rows):
    """Return the savings report for metric rows, or None on any savings error."""
    try:
        if _SAVINGS_IMPORT_ERROR is not None or load_pricing is None or savings_report is None:
            raise RuntimeError(f"savings import failed: {_SAVINGS_IMPORT_ERROR}")
        return savings_report(rows, load_pricing(PRICING_FILE))
    except Exception as exc:
        print(f"[serve] analytics savings failed: {exc}", flush=True)
        return None


def _aggregate_analytics(now, range_key):
    """Pure aggregation: read the six ledgers via the mtime cache, filter to the
    range, and return the chart-ready payload documented in the spec. Never raises
    on null/missing/malformed rows."""
    cur_start, prev_start = _analytics_range_bounds(now, range_key)

    metrics = list(_load_jsonl_cached(METRICS_FILE))

    def in_cur(row):
        return _analytics_in_range(row.get("ts"), cur_start, now)

    def in_prev(row):
        # previous window is [prev_start, cur_start); None bounds => empty
        if prev_start is None or cur_start is None:
            return False
        d = _analytics_parse_ts(row.get("ts"))
        return d is not None and prev_start <= d < cur_start

    cur_metrics = [r for r in metrics if in_cur(r)]
    prev_metrics = [r for r in metrics if in_prev(r)]

    def success_rate(rows):
        rated = [r for r in rows if r.get("exit_code") is not None]
        if not rated:
            return None
        return round(sum(1 for r in rated if r.get("exit_code") == 0) / len(rated), 4)

    def avg_duration(rows):
        ds = [r["duration_ms"] for r in rows if isinstance(r.get("duration_ms"), (int, float))]
        return int(sum(ds) / len(ds)) if ds else None

    payload = {
        "range": range_key if range_key in _ANALYTICS_RANGES else "30d",
        "generated_at": now.isoformat(),
        "kpis": {
            "phase_runs": {"value": len(cur_metrics), "prev": len(prev_metrics)},
            "success_rate": {"value": success_rate(cur_metrics), "prev": success_rate(prev_metrics)},
            "avg_duration": {"value": avg_duration(cur_metrics), "prev": avg_duration(prev_metrics), "unit": "ms"},
            # filled in by the cost / backlog blocks below:
            "total_spend": {"value": 0.0, "prev": 0.0, "unit": "usd"},
            "open_todos": {"value": 0},
            "pending_proposals": {"value": 0},
        },
        "cost": {"spend_over_time": [], "by_model": [], "duration_by_phase": []},
        "health": {"runs_over_time": [], "outcomes": {}, "retries_over_time": [], "review_verdicts": {}},
        "skills": {"top_by_invocations": [], "table": [], "cost_by_skill": []},
        "backlog": {"proposal_status": {}, "todo_burndown": [], "recent_activity": []},
        "savings": _analytics_savings(metrics),
    }

    # ----- Cost & efficiency -------------------------------------------------
    jobs = list(_load_jsonl_cached(JOBS_PERSIST_FILE))
    skill_rows = list(_load_jsonl_cached(SKILL_METRICS_FILE))

    def job_ts(r):
        # jobs rows are stamped with `created_at`, NOT `ts`. Do not reuse the
        # `in_prev` closure (keyed on `ts`) for jobs — use job_ts everywhere.
        return r.get("created_at") or r.get("ts")

    def cost_events(rows, ts_getter, cost_getter):
        out = []
        for r in rows:
            ts = ts_getter(r)
            if not _analytics_in_range(ts, cur_start, now):
                continue
            usd = cost_getter(r)
            if isinstance(usd, (int, float)):
                out.append((ts, r.get("model") or "unknown", float(usd)))
        return out

    cur_costs = (
        cost_events(jobs, job_ts, lambda r: (r.get("cost") or {}).get("cost_usd"))
        + cost_events(skill_rows, lambda r: r.get("ts"), lambda r: r.get("cost_usd"))
    )
    by_model = {}
    for _ts, model, usd in cur_costs:
        by_model[model] = by_model.get(model, 0.0) + usd
    payload["kpis"]["total_spend"]["value"] = round(sum(by_model.values()), 6)
    payload["cost"]["by_model"] = [
        {"model": m, "usd": round(v, 6)} for m, v in sorted(by_model.items(), key=lambda kv: -kv[1])
    ]
    # spend_over_time: bucket by date (from the parsed datetime, robust vs format)
    by_day = {}
    for ts, _m, usd in cur_costs:
        d = _analytics_parse_ts(ts)
        if d is None:
            continue
        day = d.date().isoformat()
        by_day[day] = by_day.get(day, 0.0) + usd
    payload["cost"]["spend_over_time"] = [
        {"date": day, "usd": round(by_day[day], 6)} for day in sorted(by_day)
    ]
    # duration_by_phase from metrics (duration only — no cost source per spec)
    phase_dur, phase_cnt = {}, {}
    for r in cur_metrics:
        ph = r.get("phase") or "unknown"
        if isinstance(r.get("duration_ms"), (int, float)):
            phase_dur[ph] = phase_dur.get(ph, 0) + r["duration_ms"]
            phase_cnt[ph] = phase_cnt.get(ph, 0) + 1
    payload["cost"]["duration_by_phase"] = [
        {"phase": ph, "duration_ms": phase_dur[ph], "runs": phase_cnt[ph]} for ph in sorted(phase_dur)
    ]

    # previous-period spend for the KPI delta
    def in_prev_jobs(r):
        if prev_start is None or cur_start is None:
            return False
        d = _analytics_parse_ts(job_ts(r))     # jobs use created_at, hence job_ts
        return d is not None and prev_start <= d < cur_start

    prev_spend = sum(
        float((r.get("cost") or {}).get("cost_usd") or 0) for r in jobs if in_prev_jobs(r)
    ) + sum(float(r.get("cost_usd") or 0) for r in skill_rows if in_prev(r))
    payload["kpis"]["total_spend"]["prev"] = round(prev_spend, 6)

    # ----- Workflow health ---------------------------------------------------
    # Outcomes: exit_code 0 -> done, non-zero -> failed. No "cancelled" bucket —
    # that is a jobs-level status absent from metrics.jsonl (see spec).
    outcomes = {}
    runs_by_day = {}
    retries_by_day = {}
    verdicts = {}
    for r in cur_metrics:
        code = r.get("exit_code")
        day = None
        d = _analytics_parse_ts(r.get("ts"))
        if d is not None:
            day = d.date().isoformat()
        if code is not None:
            label = "done" if code == 0 else "failed"
            outcomes[label] = outcomes.get(label, 0) + 1
            if day is not None:
                bucket = runs_by_day.setdefault(day, {"date": day, "done": 0, "failed": 0})
                bucket[label] += 1
        retries = r.get("retries")
        if isinstance(retries, (int, float)) and day is not None:
            rb = retries_by_day.setdefault(day, {"date": day, "retries": 0})
            rb["retries"] += retries
        verdict = r.get("review_verdict") or "none"
        verdicts[verdict] = verdicts.get(verdict, 0) + 1
    payload["health"]["outcomes"] = outcomes
    payload["health"]["runs_over_time"] = [runs_by_day[k] for k in sorted(runs_by_day)]
    payload["health"]["retries_over_time"] = [retries_by_day[k] for k in sorted(retries_by_day)]
    payload["health"]["review_verdicts"] = verdicts

    # ----- Skills & agents ---------------------------------------------------
    cur_skill_rows = [r for r in skill_rows if _analytics_in_range(r.get("ts"), cur_start, now)]
    by_skill = {}
    for r in cur_skill_rows:
        sid = r.get("skill") or ""
        if not sid:
            continue
        agg = by_skill.setdefault(sid, {
            "skill": sid, "runs": 0, "successes": 0, "invocations": 0,
            "total_cost_usd": 0.0, "_by_day": {},
        })
        agg["runs"] += 1
        agg["invocations"] += int(r.get("invocations") or 1)
        if r.get("outcome") == "done":
            agg["successes"] += 1
        agg["total_cost_usd"] += float(r.get("cost_usd") or 0.0)
        d = _analytics_parse_ts(r.get("ts"))
        if d is not None:
            day = d.date().isoformat()
            agg["_by_day"][day] = agg["_by_day"].get(day, 0) + 1
    table = []
    for agg in by_skill.values():
        runs = agg["runs"] or 1
        spark = [agg["_by_day"][day] for day in sorted(agg["_by_day"])]
        table.append({
            "skill": agg["skill"],
            "runs": agg["runs"],
            "success_rate": round(agg["successes"] / runs, 4),
            "avg_cost_usd": round(agg["total_cost_usd"] / runs, 6),
            "spark": spark,
        })
    payload["skills"]["table"] = sorted(table, key=lambda t: -t["runs"])
    payload["skills"]["top_by_invocations"] = sorted(
        [{"skill": a["skill"], "invocations": a["invocations"], "success": a["successes"]}
         for a in by_skill.values()],
        key=lambda t: -t["invocations"],
    )
    payload["skills"]["cost_by_skill"] = sorted(
        [{"skill": a["skill"], "usd": round(a["total_cost_usd"], 6)} for a in by_skill.values()],
        key=lambda t: -t["usd"],
    )

    # ----- Improvements & backlog --------------------------------------------
    improvements = list(_load_jsonl_cached(IMPROVEMENTS_LEDGER))
    todos = list(_load_jsonl_cached(TODOS_FILE))
    events = list(_load_jsonl_cached(EVENTS_FILE))

    # proposal_status: 5 explicit buckets so none are silently dropped.
    # `installed` folds into `applied`.
    _PROPOSAL_BUCKETS = {
        "pending": "pending", "applied": "applied", "installed": "applied",
        "rejected": "rejected", "no_change": "no_change", "failed": "failed",
    }
    proposal_status = {"pending": 0, "applied": 0, "rejected": 0, "no_change": 0, "failed": 0}
    for r in improvements:
        if not _analytics_in_range(r.get("ts"), cur_start, now):
            continue
        bucket = _PROPOSAL_BUCKETS.get(r.get("status"))
        if bucket:
            proposal_status[bucket] += 1
    payload["backlog"]["proposal_status"] = proposal_status

    # todo burndown: per-day open vs resolved counts (range-filtered by created_at).
    def todo_ts(r):
        return r.get("created_at") or r.get("updated_at") or r.get("ts")

    burn = {}
    for r in todos:
        d = _analytics_parse_ts(todo_ts(r))
        if d is None or not _analytics_in_range(todo_ts(r), cur_start, now):
            continue
        day = d.date().isoformat()
        b = burn.setdefault(day, {"date": day, "open": 0, "resolved": 0})
        status = (r.get("status") or "").lower()
        if status == "open":
            b["open"] += 1
        elif status.startswith("resolved"):
            b["resolved"] += 1
    payload["backlog"]["todo_burndown"] = [burn[k] for k in sorted(burn)]

    # recent_activity: newest ~15 events. The events ledger has no summary/ref
    # fields, so synthesise a readable summary and use session_id as the ref.
    def event_summary(r):
        parts = [str(r.get("kind") or "event")]
        for key in ("phase", "tool", "model"):
            val = r.get(key)
            if val and val != "unknown":
                parts.append(str(val))
        return " · ".join(parts)

    sorted_events = sorted(events, key=lambda r: r.get("ts") or "", reverse=True)
    payload["backlog"]["recent_activity"] = [
        {"ts": r.get("ts"), "kind": r.get("kind"),
         "summary": event_summary(r), "ref": r.get("session_id") or ""}
        for r in sorted_events[:15]
    ]

    # ----- Current-state KPIs (whole-ledger totals, NOT range-filtered) ------
    payload["kpis"]["open_todos"]["value"] = sum(
        1 for r in todos if (r.get("status") or "").lower() == "open"
    )
    payload["kpis"]["pending_proposals"]["value"] = sum(
        1 for r in improvements if r.get("status") == "pending"
    )

    return payload


def _load_auto_select_ranking(max_records: int = 200, min_samples: int = 5) -> dict:
    """Aggregate METRICS_FILE into adaptive auto-select rankings."""
    empty = {
        "samples": 0,
        "min_samples": min_samples,
        "groups": [],
        "dropped_candidates": 0,
        "last_record_ts": None,
    }
    # Route through the mtime-keyed JSONL cache so repeated /api/auto-select
    # polls don't re-parse the whole ledger. The helper preserves the prior
    # ``errors="replace"`` invariant (so a half-written concurrent append
    # from the orchestrate skill can't surface as UnicodeDecodeError) and
    # silently skips malformed rows — mirroring this function's previous
    # hand-rolled behaviour.
    rows = _load_jsonl_cached(METRICS_FILE)
    if not rows:
        return empty
    return auto_select_scorer.score_groups(
        rows,
        min_samples=min_samples,
        effective_budget=None,
        per_group_tail=max_records,
        static_pick=None,
    )


def _load_timeline_runs(max_events: int = 500) -> list[dict]:
    """Aggregate `phase_dispatch` events from EVENTS_FILE into per-session runs.

    Each returned run has the shape::

        {
          "session_id": "<id or 'unknown'>",
          "task": "<first user message from transcript, or None>",
          "tag":  "<primary 'tool/model' for the run, or 'mixed'>",
          "started_at":       "<ISO ts of first phase>",
          "ended_at":         "<ISO ts of last phase>",
          "total_duration_ms": <int>,   # ended_at - started_at
          "phases": [
            {
              "phase": "plan" | "execute" | ... | "unknown",
              "tool":  "claude" | "codex",
              "model": "<str>",
              "exit_code": 0 | <int> | None,
              "end_ts": "<ISO ts>",
              "duration_ms": <int>,   # ts diff vs previous phase in same session; 0 for the first
              "status": "success" | "failure" | "pending",
            },
            ...
          ],
        }

    Runs are sorted by `ended_at` descending (newest first). Only the most
    recent `max_events` lines of the file are scanned so the endpoint stays
    cheap regardless of historical volume.
    """
    # Route through the mtime-keyed JSONL cache so repeated /api/timeline
    # polls don't re-parse the whole ledger. The helper preserves the prior
    # ``errors="replace"`` invariant (so a half-written concurrent append
    # from log_event.py — writes are not atomic at the byte level — can't
    # surface as UnicodeDecodeError) and silently skips malformed rows.
    rows = _load_jsonl_cached(EVENTS_FILE)
    if not rows:
        return []
    tail = rows[-max_events:] if len(rows) > max_events else rows

    by_session: dict[str, list[dict]] = {}
    for ev in tail:
        if not isinstance(ev, dict):
            continue
        if ev.get("kind") != "phase_dispatch":
            continue
        sid = ev.get("session_id") or "unknown"
        by_session.setdefault(sid, []).append(dict(ev))

    runs: list[dict] = []
    for sid, events in by_session.items():
        for ev in events:
            ev["_dt"] = _parse_iso_ts(ev.get("ts"))
        events.sort(key=lambda e: e["_dt"] or _dt.datetime.min.replace(tzinfo=_dt.timezone.utc))
        phases: list[dict] = []
        prev_dt: _dt.datetime | None = None
        tag_counter: dict[str, int] = {}
        for ev in events:
            ts = ev.get("ts") or ""
            cur_dt = ev["_dt"]
            if cur_dt is None or prev_dt is None:
                duration_ms = 0
            else:
                duration_ms = max(0, int((cur_dt - prev_dt).total_seconds() * 1000))
            exit_code = ev.get("exit_code")
            if exit_code is None:
                status = "pending"
            elif exit_code == 0:
                status = "success"
            else:
                status = "failure"
            tool = ev.get("tool") or "unknown"
            model = ev.get("model") or "unknown"
            tag_counter[f"{tool}/{model}"] = tag_counter.get(f"{tool}/{model}", 0) + 1
            phases.append({
                "phase": ev.get("phase") or "unknown",
                "tool": tool,
                "model": model,
                "exit_code": exit_code,
                "end_ts": ts,
                "duration_ms": duration_ms,
                "status": status,
            })
            if cur_dt is not None:
                prev_dt = cur_dt
        if not phases:
            continue

        start_dt = events[0]["_dt"]
        end_dt = events[-1]["_dt"]
        if start_dt and end_dt:
            total_duration_ms = max(0, int((end_dt - start_dt).total_seconds() * 1000))
        else:
            total_duration_ms = 0

        if len(tag_counter) == 1:
            tag = next(iter(tag_counter))
        elif tag_counter:
            tag = "mixed"
        else:
            tag = "unknown"

        runs.append({
            "session_id": sid,
            "task": _lookup_session_task(sid),
            "tag": tag,
            "started_at": phases[0]["end_ts"],
            "ended_at": phases[-1]["end_ts"],
            "total_duration_ms": total_duration_ms,
            "phases": phases,
        })

    runs.sort(key=lambda r: r["ended_at"] or "", reverse=True)
    return runs
