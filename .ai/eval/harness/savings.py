from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path
from typing import Any, Iterable


MILLION = 1_000_000.0
REQUIRED_PRICE_KEYS = {"input", "output", "cached_input"}


def load_pricing(path: str | Path) -> dict[str, dict[str, float]]:
    """Parse the small YAML subset used by .ai/pricing.yaml without dependencies."""
    pricing: dict[str, dict[str, float]] = {}

    for raw_line in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue

        model_id, sep, raw_value = line.partition(":")
        if not sep:
            raise ValueError(f"invalid pricing line: {raw_line}")

        value = raw_value.strip()
        if not value.startswith("{") or not value.endswith("}"):
            raise ValueError(f"invalid pricing entry for {model_id.strip()}: {value}")

        entry = _parse_inline_mapping(value)
        missing = REQUIRED_PRICE_KEYS - set(entry)
        if missing:
            raise ValueError(f"pricing entry for {model_id.strip()} missing: {sorted(missing)}")
        pricing[model_id.strip()] = {key: float(entry[key]) for key in REQUIRED_PRICE_KEYS}

    return pricing


def cost_of(row: dict[str, Any], pricing: dict[str, dict[str, float]]) -> float:
    model_id = _model_id(row)
    prices = _prices_for(model_id, pricing)
    tokens_in = _number(row.get("tokens_in"))
    cache_read = min(_number(row.get("cache_read")), tokens_in)
    uncached_input = tokens_in - cache_read
    tokens_out = _number(row.get("tokens_out"))
    return (
        uncached_input * prices["input"]
        + cache_read * prices["cached_input"]
        + tokens_out * prices["output"]
    ) / MILLION


def baseline_cost_of(
    row: dict[str, Any],
    pricing: dict[str, dict[str, float]],
    opus_id: str = "claude-opus-4-8",
) -> float:
    return _uncached_cost_for_model(row, pricing, opus_id)


def savings_report(
    rows: Iterable[dict[str, Any]],
    pricing: dict[str, dict[str, float]],
    opus_id: str = "claude-opus-4-8",
) -> dict[str, Any]:
    ledger_rows = list(rows)
    priced_row_ids: set[int] = set()
    unpriced_models: set[str] = set()
    unpriced_rows = 0

    for row in ledger_rows:
        try:
            _prices_for(_model_id(row), pricing)
        except ValueError:
            unpriced_rows += 1
            unpriced_models.add(_model_id_for_report(row))
            continue
        priced_row_ids.add(id(row))

    priced_rows = [row for row in ledger_rows if id(row) in priced_row_ids]
    review_costs = [
        baseline_cost_of(row, pricing, opus_id)
        for row in priced_rows
        if row.get("phase") == "review"
    ]
    modeled_review_cost = statistics.median(review_costs) if review_costs else 0.0

    by_task: dict[str, list[dict[str, Any]]] = {}
    for row in ledger_rows:
        by_task.setdefault(str(row.get("task_slug") or ""), []).append(row)

    task_reports: dict[str, dict[str, Any]] = {}
    totals = {
        "real": 0.0,
        "baseline": 0.0,
        "breakdown": {"routing": 0.0, "cache": 0.0, "gating": 0.0},
    }

    for task_slug, task_rows in sorted(by_task.items()):
        priced_task_rows = [row for row in task_rows if id(row) in priced_row_ids]
        real = sum(cost_of(row, pricing) for row in priced_task_rows)
        measured_baseline = sum(baseline_cost_of(row, pricing, opus_id) for row in priced_task_rows)
        real_model_uncached = sum(
            _uncached_cost_for_model(row, pricing, _model_id(row)) for row in priced_task_rows
        )
        modeled = not any(row.get("phase") == "review" for row in priced_task_rows)
        gating = modeled_review_cost if modeled else 0.0
        baseline = measured_baseline + gating
        breakdown = {
            "routing": measured_baseline - real_model_uncached,
            "cache": real_model_uncached - real,
            "gating": gating,
        }
        saved = baseline - real

        task_reports[task_slug] = {
            "real": real,
            "baseline": baseline,
            "savings": saved,
            "savings_pct": _savings_pct(baseline, real),
            "modeled": modeled,
            "breakdown": breakdown,
            "breakdown_sources": {
                "routing": "measured",
                "cache": "measured",
                "gating": "modeled" if modeled else "measured",
            },
        }

        totals["real"] += real
        totals["baseline"] += baseline
        for lever, value in breakdown.items():
            totals["breakdown"][lever] += value

    totals["savings"] = totals["baseline"] - totals["real"]
    totals["savings_pct"] = _savings_pct(totals["baseline"], totals["real"])
    totals["unpriced_rows"] = unpriced_rows
    totals["unpriced_models"] = sorted(unpriced_models)

    return {"tasks": task_reports, "totals": totals}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Report modeled cost savings from a metrics ledger.")
    parser.add_argument("--ledger", required=True, help="Path to a JSONL ledger.")
    parser.add_argument(
        "--pricing",
        default=str(Path(__file__).resolve().parents[2] / "pricing.yaml"),
        help="Path to pricing YAML. Defaults to .ai/pricing.yaml.",
    )
    parser.add_argument("--format", choices=["md", "json"], default="md")
    args = parser.parse_args(argv)

    pricing = load_pricing(args.pricing)
    report = savings_report(_load_jsonl(args.ledger), pricing)
    if args.format == "json":
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(format_markdown(report))
    return 0


def format_markdown(report: dict[str, Any]) -> str:
    lines = [
        "| task_slug | real_usd | baseline_usd | savings_% | modeled |",
        "| --- | ---: | ---: | ---: | :---: |",
    ]
    for task_slug, task in report["tasks"].items():
        lines.append(
            "| {task_slug} | {real:.6f} | {baseline:.6f} | {savings_pct:.2%} | {modeled} |".format(
                task_slug=task_slug,
                real=task["real"],
                baseline=task["baseline"],
                savings_pct=task["savings_pct"],
                modeled="yes" if task["modeled"] else "no",
            )
        )

    totals = report["totals"]
    lines.append(
        "| TOTAL | {real:.6f} | {baseline:.6f} | {savings_pct:.2%} |  |".format(
            real=totals["real"],
            baseline=totals["baseline"],
            savings_pct=totals["savings_pct"],
        )
    )
    return "\n".join(lines)


def _uncached_cost_for_model(
    row: dict[str, Any],
    pricing: dict[str, dict[str, float]],
    model_id: str,
) -> float:
    prices = _prices_for(model_id, pricing)
    tokens_in = _number(row.get("tokens_in"))
    tokens_out = _number(row.get("tokens_out"))
    return (tokens_in * prices["input"] + tokens_out * prices["output"]) / MILLION


def _prices_for(model_id: str, pricing: dict[str, dict[str, float]]) -> dict[str, float]:
    try:
        return pricing[model_id]
    except KeyError as exc:
        raise ValueError(f"unknown model id: {model_id}") from exc


def _model_id(row: dict[str, Any]) -> str:
    model_id = row.get("model")
    if model_id is None or model_id == "":
        raise ValueError("unknown model id: <missing>")
    return str(model_id)


def _model_id_for_report(row: dict[str, Any]) -> str:
    try:
        return _model_id(row)
    except ValueError:
        return "<missing>"


def _number(value: Any) -> float:
    if value is None or value == "":
        return 0.0
    return float(value)


def _savings_pct(baseline: float, real: float) -> float:
    if baseline == 0:
        return 0.0
    return (baseline - real) / baseline


def _parse_inline_mapping(value: str) -> dict[str, Any]:
    inner = value[1:-1].strip()
    if not inner:
        return {}
    result: dict[str, Any] = {}
    for part in inner.split(","):
        key, sep, raw_value = part.partition(":")
        if not sep:
            raise ValueError(f"invalid inline mapping part: {part}")
        result[key.strip()] = _parse_yaml_scalar(raw_value.strip())
    return result


def _parse_yaml_scalar(value: str) -> Any:
    if value in {"true", "false"}:
        return value == "true"
    if value in {"null", "None", "~"}:
        return None
    if value.startswith('"') and value.endswith('"'):
        return value[1:-1]
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


def _load_jsonl(path: str | Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


if __name__ == "__main__":
    raise SystemExit(main())
