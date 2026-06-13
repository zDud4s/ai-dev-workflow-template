from __future__ import annotations

import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
EVAL_ROOT = REPO_ROOT / ".ai" / "eval"
sys.path.insert(0, str(EVAL_ROOT))

from harness.compare import compare_results, format_markdown  # noqa: E402


def test_compare_aggregates_per_arm(tmp_path: Path) -> None:
    results_dir = tmp_path / "results"
    _write_jsonl(
        results_dir / "arm-a.jsonl",
        [
            _row("a", "sum-list", "tuning", True, 10, 4, 100),
            _row("a", "reverse-words", "held-out", False, 20, 6, 300),
            _row("a", "join-lines", "tuning", True, 30, 8, 200),
        ],
    )
    _write_jsonl(
        results_dir / "arm-b.jsonl",
        [
            _row("b", "sum-list", "tuning", True, 3, 2, 50),
            _row("b", "reverse-words", "held-out", True, 7, 3, 70),
        ],
    )
    _write_jsonl(
        results_dir / "arm-c.jsonl",
        [
            _row("c", "sum-list", "tuning", False, 5, 9, 500),
        ],
    )

    summary = compare_results(results_dir)

    assert summary["arms_present"] == ["a", "b", "c"]
    assert summary["arms"]["a"]["n"] == 3
    assert summary["arms"]["a"]["passed"] == 2
    assert summary["arms"]["a"]["success_rate"] == 0.667
    assert summary["arms"]["a"]["total_tokens_in"] == 60
    assert summary["arms"]["a"]["total_tokens_out"] == 18
    assert summary["arms"]["a"]["median_duration_ms"] == 200
    assert summary["arms"]["a"]["total_duration_ms"] == 600
    assert summary["arms"]["b"]["success_rate"] == 1.0
    assert summary["arms"]["b"]["median_duration_ms"] == 60
    assert summary["arms"]["c"]["passed"] == 0


def test_compare_excludes_proposals_subdir(tmp_path: Path) -> None:
    results_dir = tmp_path / "results"
    _write_jsonl(results_dir / "arm-a.jsonl", [_row("a", "sum-list", "tuning", True)])
    _write_jsonl(
        results_dir / "proposals" / "candidate-1.jsonl",
        [_row("proposal", "reverse-words", "held-out", True)],
    )

    summary = compare_results(results_dir)

    assert summary["arms_present"] == ["a"]
    assert "proposal" not in summary["arms"]
    assert summary["arms"]["a"]["n"] == 1


def test_compare_handles_none_tokens_and_blank_lines(tmp_path: Path) -> None:
    results_dir = tmp_path / "results"
    path = results_dir / "arm-a.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                json.dumps(_row("a", "sum-list", "tuning", True, None, 4, 100)),
                "",
                "not json",
                json.dumps(_row("a", "reverse-words", "held-out", False, 5, None, None)),
                "",
            ]
        ),
        encoding="utf-8",
    )

    summary = compare_results(results_dir)

    assert summary["arms"]["a"]["n"] == 2
    assert summary["arms"]["a"]["total_tokens_in"] == 5
    assert summary["arms"]["a"]["total_tokens_out"] == 4
    assert summary["arms"]["a"]["median_duration_ms"] == 100
    assert summary["arms"]["a"]["total_duration_ms"] == 100


def test_format_markdown_contains_each_arm(tmp_path: Path) -> None:
    results_dir = tmp_path / "results"
    _write_jsonl(results_dir / "arm-a.jsonl", [_row("a", "sum-list", "tuning", True)])
    _write_jsonl(results_dir / "arm-b.jsonl", [_row("b", "sum-list", "tuning", False)])

    markdown = format_markdown(compare_results(results_dir))

    assert "| a | 1 | 1.0 |" in markdown
    assert "| b | 1 | 0.0 |" in markdown
    assert "## By Partition" in markdown


def test_compare_by_partition_breakdown(tmp_path: Path) -> None:
    results_dir = tmp_path / "results"
    _write_jsonl(
        results_dir / "arm-a.jsonl",
        [
            _row("a", "sum-list", "tuning", True),
            _row("a", "join-lines", "tuning", False),
            _row("a", "reverse-words", "held-out", True),
        ],
    )

    by_partition = compare_results(results_dir)["arms"]["a"]["by_partition"]

    assert by_partition["tuning"] == {"n": 2, "passed": 1, "success_rate": 0.5}
    assert by_partition["held-out"] == {"n": 1, "passed": 1, "success_rate": 1.0}


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def _row(
    arm: str,
    task_id: str,
    partition: str,
    success: bool,
    tokens_in: int | None = 1,
    tokens_out: int | None = 1,
    duration_ms: int | None = 1,
) -> dict[str, object]:
    return {
        "arm": arm,
        "task_id": task_id,
        "partition": partition,
        "success": success,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "duration_ms": duration_ms,
    }
