from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
EVAL_ROOT = REPO_ROOT / ".ai" / "eval"
PRICING_PATH = REPO_ROOT / ".ai" / "pricing.yaml"
sys.path.insert(0, str(EVAL_ROOT))

from harness.savings import (  # noqa: E402
    _load_jsonl,
    baseline_cost_of,
    cost_of,
    load_pricing,
    savings_report,
)


def test_cached_split_costs_less_than_uncached_pricing() -> None:
    pricing = load_pricing(PRICING_PATH)
    cached_row = _row("cache-task", "execute", "gpt-5.5", tokens_in=1_000_000, cache_read=400_000)
    uncached_row = {**cached_row, "cache_read": 0}

    assert cost_of(cached_row, pricing) < cost_of(uncached_row, pricing)


def test_routing_lever_reflects_real_price_relationship() -> None:
    # Routing saves only when the routed-to model is actually cheaper than the opus
    # baseline. A genuinely cheap model (gpt-5.4-mini, $0.75/$4.5) beats opus...
    pricing = load_pricing(PRICING_PATH)
    cheap = _row("route-cheap", "execute", "gpt-5.4-mini", tokens_in=1_000_000, tokens_out=100_000)
    assert baseline_cost_of(cheap, pricing) > cost_of(cheap, pricing)

    # ...but gpt-5.5 output ($30/Mtok) is dearer than opus-4-8 output ($25/Mtok), so on
    # output-heavy execute rows routing to gpt-5.5 costs MORE than the opus baseline.
    # The engine must report that faithfully, not assume codex always wins.
    pricey = _row("route-pricey", "execute", "gpt-5.5", tokens_in=100_000, tokens_out=1_000_000)
    assert baseline_cost_of(pricey, pricing) < cost_of(pricey, pricing)


def test_missing_review_gets_modeled_gating_baseline() -> None:
    pricing = load_pricing(PRICING_PATH)
    rows = [
        _row("task-with-review", "execute", "gpt-5.5", tokens_in=200_000, tokens_out=10_000),
        _row("task-with-review", "review", "claude-opus-4-8", tokens_in=100_000, tokens_out=20_000),
        _row("task-without-review", "execute", "gpt-5.5", tokens_in=200_000, tokens_out=10_000),
    ]

    report = savings_report(rows, pricing)
    missing = report["tasks"]["task-without-review"]
    modeled_review = baseline_cost_of(rows[1], pricing)

    assert missing["modeled"] is True
    assert missing["breakdown"]["gating"] == pytest.approx(modeled_review)
    assert missing["baseline"] == pytest.approx(baseline_cost_of(rows[2], pricing) + modeled_review)


def test_savings_pct_and_breakdown_sum_to_savings() -> None:
    pricing = load_pricing(PRICING_PATH)
    rows = [
        _row("combined", "execute", "gpt-5.5", tokens_in=1_000_000, tokens_out=100_000, cache_read=250_000),
        _row("combined", "review", "claude-opus-4-8", tokens_in=200_000, tokens_out=20_000),
        _row("gated", "execute", "gpt-5.5", tokens_in=400_000, tokens_out=20_000, cache_read=100_000),
    ]

    report = savings_report(rows, pricing)
    totals = report["totals"]
    saved = totals["baseline"] - totals["real"]
    breakdown_saved = sum(totals["breakdown"].values())

    assert totals["savings_pct"] == pytest.approx(saved / totals["baseline"])
    assert breakdown_saved == pytest.approx(saved, abs=1e-9)


def test_unknown_model_id_raises_explicit_error() -> None:
    pricing = load_pricing(PRICING_PATH)
    row = _row("unknown", "execute", "not-a-real-model")

    with pytest.raises(ValueError, match="unknown model"):
        cost_of(row, pricing)


def test_savings_report_surfaces_unpriced_rows_without_crashing() -> None:
    pricing = load_pricing(PRICING_PATH)
    rows = [
        _row("mixed-pricing", "execute", "gpt-5.5", tokens_in=1_000_000, tokens_out=100_000),
        _row("mixed-pricing", "review", "future-unpriced-model", tokens_in=1_000_000, tokens_out=100_000),
    ]

    report = savings_report(rows, pricing)

    assert report["tasks"]["mixed-pricing"]["real"] == pytest.approx(cost_of(rows[0], pricing))
    assert report["totals"]["unpriced_rows"] >= 1
    assert "future-unpriced-model" in report["totals"]["unpriced_models"]


def test_load_jsonl_skips_blank_and_malformed_lines(tmp_path: Path) -> None:
    pricing = load_pricing(PRICING_PATH)
    valid_row = _row("valid-ledger", "execute", "gpt-5.5", tokens_in=1_000, tokens_out=100)
    joined_objects = (
        f"{json.dumps(_row('malformed-a', 'execute', 'gpt-5.5'))} "
        f"{json.dumps(_row('malformed-b', 'execute', 'gpt-5.5'))}"
    )
    ledger = tmp_path / "metrics.jsonl"
    ledger.write_text(f"{json.dumps(valid_row)}\n\n{joined_objects}\n", encoding="utf-8")

    report = savings_report(_load_jsonl(ledger), pricing)

    assert list(report["tasks"]) == ["valid-ledger"]
    assert report["totals"]["real"] == pytest.approx(cost_of(valid_row, pricing))


def _row(
    task_slug: str,
    phase: str,
    model: str,
    tokens_in: int | None = 100,
    tokens_out: int | None = 10,
    cache_read: int | None = 0,
) -> dict[str, object]:
    return {
        "task_slug": task_slug,
        "phase": phase,
        "model": model,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "cache_read": cache_read,
        "cache_creation": None,
    }
