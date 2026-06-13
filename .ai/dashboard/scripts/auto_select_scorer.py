"""Score adaptive selector candidates.

Candidates are first gated by WILSON_FLOOR as a correctness floor, then ordered
by the composite score within each tier. A below-floor candidate can never
outrank an above-floor candidate regardless of speed.
"""

from __future__ import annotations

import math
import statistics


_RUNG_INDEX = {
    "low": 0,
    "medium": 1,
    "high": 2,
    "xhigh": 2,
    "max": 2,
}

WILSON_FLOOR = 0.7


def wilson_lower_bound(successes: int, n: int, z: float = 1.96) -> float:
    """Return the Wilson lower bound for a binomial success rate."""
    if n == 0:
        return 0.0
    phat = successes / n
    denom = 1 + z**2 / n
    center = phat + z**2 / (2 * n)
    margin = z * math.sqrt((phat * (1 - phat) + z**2 / (4 * n)) / n)
    return (center - margin) / denom


def budget_alignment(effort: str | None, effective_budget: str | None) -> float:
    """Score how closely reasoning effort matches the effective budget rung."""
    effort_rung = _rung_index(effort)
    budget_rung = _rung_index(effective_budget)
    if effort_rung is None or budget_rung is None:
        return 0.5
    distance = abs(effort_rung - budget_rung)
    if distance == 0:
        return 1.0
    if distance == 1:
        return 0.5
    return 0.0


def is_success(rec: dict) -> bool:
    return (
        rec.get("exit_code") == 0
        and rec.get("handoff_complete") in (True, None)
        and rec.get("review_verdict") in (None, "approve")
    )


def score_groups(
    records,
    *,
    min_samples=5,
    effective_budget=None,
    per_group_tail=200,
    static_pick=None,
) -> dict:
    grouped_records: dict[tuple, list[dict]] = {}
    last_ts: str | None = None

    for rec in records:
        if not isinstance(rec, dict):
            continue
        phase = rec.get("phase")
        if not phase:
            continue
        ts = rec.get("ts")
        if isinstance(ts, str) and (last_ts is None or ts > last_ts):
            last_ts = ts
        group_key = (phase, rec.get("size"), rec.get("risk"))
        grouped_records.setdefault(group_key, []).append(rec)

    out_groups: list[dict] = []
    dropped = 0
    sample_count = 0
    static_key = tuple(static_pick) if static_pick is not None else None
    tail_limit = per_group_tail if isinstance(per_group_tail, int) else 200

    for group_key, group_records in grouped_records.items():
        ordered_records = sorted(group_records, key=_record_ts_sort_key)
        if tail_limit <= 0:
            tail_records = []
        else:
            tail_records = ordered_records[-tail_limit:]
        sample_count += len(tail_records)

        candidates: dict[tuple, list[dict]] = {}
        for rec in tail_records:
            candidate_key = (
                rec.get("tool") or "unknown",
                rec.get("model") or "unknown",
                rec.get("reasoning_effort"),
            )
            candidates.setdefault(candidate_key, []).append(rec)

        scored: list[dict] = []
        for candidate_key, candidate_records in candidates.items():
            n = len(candidate_records)
            if n < min_samples:
                dropped += 1
                continue
            successes = sum(1 for rec in candidate_records if is_success(rec))
            success_rate = successes / n
            wilson = wilson_lower_bound(successes, n)
            durations = [
                rec.get("duration_ms")
                for rec in candidate_records
                if isinstance(rec.get("duration_ms"), int)
            ]
            mean_duration = int(sum(durations) / len(durations)) if durations else 0
            median_duration = _median_duration_ms(durations)
            scored.append(
                {
                    "tool": candidate_key[0],
                    "model": candidate_key[1],
                    "reasoning_effort": candidate_key[2],
                    "samples": n,
                    "success_rate": round(success_rate, 3),
                    "wilson_lower": round(wilson, 3),
                    "median_duration_ms": median_duration,
                    "mean_duration_ms": mean_duration,
                    "_wilson_raw": wilson,
                }
            )

        if not scored:
            continue

        durations = [candidate["median_duration_ms"] for candidate in scored]
        dmin, dmax = min(durations), max(durations)
        spread = (dmax - dmin) or 1
        for candidate in scored:
            norm_median_dur = (candidate["median_duration_ms"] - dmin) / spread
            align = budget_alignment(candidate["reasoning_effort"], effective_budget)
            candidate["score"] = round(
                0.6 * candidate["_wilson_raw"]
                + 0.2 * (1 - norm_median_dur)
                + 0.2 * align,
                3,
            )

        scored.sort(
            key=lambda candidate: (
                candidate["_wilson_raw"] >= WILSON_FLOOR,
                candidate["score"],
            ),
            reverse=True,
        )
        top = scored[0]
        top_key = (top["tool"], top["model"], top["reasoning_effort"])
        static_fallback = bool(
            static_key is not None
            and top_key != static_key
            and top["_wilson_raw"] < WILSON_FLOOR
        )
        for candidate in scored:
            del candidate["_wilson_raw"]

        out_groups.append(
            {
                "key": {
                    "phase": group_key[0],
                    "size": group_key[1],
                    "risk": group_key[2],
                },
                "static_fallback": static_fallback,
                "candidates": scored[:3],
            }
        )

    out_groups.sort(
        key=lambda group: (
            _sort_value(group["key"]["phase"]),
            _sort_value(group["key"]["size"]),
            _sort_value(group["key"]["risk"]),
        )
    )
    return {
        "samples": sample_count,
        "min_samples": min_samples,
        "groups": out_groups,
        "dropped_candidates": dropped,
        "last_record_ts": last_ts,
    }


def _rung_index(value):
    if value is None or not isinstance(value, str):
        return None
    return _RUNG_INDEX.get(value.strip().lower())


def _record_ts_sort_key(rec):
    ts = rec.get("ts")
    if isinstance(ts, str):
        return ts
    return ""


def _sort_value(value):
    if value is None:
        return ""
    return str(value)


def _median_duration_ms(durations):
    if not durations:
        return 0
    median = statistics.median(durations)
    if isinstance(median, float):
        if median.is_integer():
            return int(median)
        return round(median, 3)
    return median
