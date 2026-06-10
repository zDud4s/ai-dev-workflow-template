from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:  # pragma: no cover - exercised by script usage
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_RESULTS_DIR = REPO_ROOT / ".ai" / "eval" / "results"


def compare_results(results_dir: str | Path) -> dict[str, Any]:
    root = Path(results_dir)
    rows_by_arm: dict[str, list[dict[str, Any]]] = {}

    for path in sorted(root.glob("arm-*.jsonl")):
        if not path.is_file():
            continue
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                row = _parse_result_line(line)
                if row is None:
                    continue
                arm = row.get("arm")
                if not isinstance(arm, str) or not arm:
                    continue
                rows_by_arm.setdefault(arm, []).append(row)

    arms = {arm: _summarize_arm(rows) for arm, rows in sorted(rows_by_arm.items())}
    return {
        "results_dir": str(results_dir),
        "arms": arms,
        "arms_present": sorted(arms),
    }


def format_markdown(summary: dict[str, Any]) -> str:
    lines = [
        "# Ablation Comparison",
        "",
        f"Results dir: `{summary.get('results_dir', '')}`",
        "",
        "| arm | n | success_rate | tokens_in | tokens_out | median_ms | total_ms |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]

    arms = summary.get("arms", {})
    for arm in sorted(arms):
        stats = arms[arm]
        lines.append(
            "| {arm} | {n} | {success_rate} | {tokens_in} | {tokens_out} | "
            "{median_ms} | {total_ms} |".format(
                arm=arm,
                n=stats["n"],
                success_rate=stats["success_rate"],
                tokens_in=stats["total_tokens_in"],
                tokens_out=stats["total_tokens_out"],
                median_ms=stats["median_duration_ms"],
                total_ms=stats["total_duration_ms"],
            )
        )

    lines.extend(
        [
            "",
            "## By Partition",
        ]
    )

    for arm in sorted(arms):
        lines.extend(
            [
                "",
                f"### Arm {arm}",
                "",
                "| partition | n | passed | success_rate |",
                "| --- | ---: | ---: | ---: |",
            ]
        )
        for partition in sorted(arms[arm]["by_partition"]):
            stats = arms[arm]["by_partition"][partition]
            lines.append(
                "| {partition} | {n} | {passed} | {success_rate} |".format(
                    partition=partition,
                    n=stats["n"],
                    passed=stats["passed"],
                    success_rate=stats["success_rate"],
                )
            )

    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compare recorded eval arm results.")
    parser.add_argument("--results-dir", default=str(DEFAULT_RESULTS_DIR))
    parser.add_argument("--format", choices=["md", "json"], default="md")
    args = parser.parse_args(argv)

    summary = compare_results(args.results_dir)
    if args.format == "json":
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print(format_markdown(summary), end="")
    return 0


def _parse_result_line(line: str) -> dict[str, Any] | None:
    if not line.strip():
        return None
    try:
        value = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(value, dict):
        return None
    return value


def _summarize_arm(rows: list[dict[str, Any]]) -> dict[str, Any]:
    passed = sum(1 for row in rows if row.get("success") is True)
    durations = [_int_value(row.get("duration_ms")) for row in rows]
    durations = [value for value in durations if value is not None]
    by_partition: dict[str, dict[str, Any]] = {}

    for row in rows:
        partition = row.get("partition")
        if not isinstance(partition, str) or not partition:
            partition = "unknown"
        stats = by_partition.setdefault(partition, {"n": 0, "passed": 0})
        stats["n"] += 1
        if row.get("success") is True:
            stats["passed"] += 1

    for stats in by_partition.values():
        stats["success_rate"] = _success_rate(stats["passed"], stats["n"])

    return {
        "n": len(rows),
        "passed": passed,
        "success_rate": _success_rate(passed, len(rows)),
        "total_tokens_in": _sum_ints(row.get("tokens_in") for row in rows),
        "total_tokens_out": _sum_ints(row.get("tokens_out") for row in rows),
        "median_duration_ms": int(statistics.median(durations)) if durations else 0,
        "total_duration_ms": sum(durations),
        "by_partition": dict(sorted(by_partition.items())),
    }


def _success_rate(passed: int, total: int) -> float:
    if total == 0:
        return 0.0
    return round(passed / total, 3)


def _sum_ints(values: Any) -> int:
    total = 0
    for value in values:
        int_value = _int_value(value)
        if int_value is not None:
            total += int_value
    return total


def _int_value(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    return None


if __name__ == "__main__":  # pragma: no cover - exercised by script usage
    raise SystemExit(main())
