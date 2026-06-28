from __future__ import annotations

from pathlib import Path
from typing import Any


TUNING_LEDGER = Path(".ai/local/ledgers/metrics.jsonl")
RESULTS_DIR = Path(".ai/eval/results")


class PartitionError(Exception):
    """Raised when eval output would cross into tuning-ledger storage."""


def assert_results_path(path: str | Path) -> None:
    candidate = Path(path).resolve()
    ledgers_dir = Path(".ai/local/ledgers").resolve()
    try:
        candidate.relative_to(ledgers_dir)
    except ValueError:
        pass
    else:
        raise PartitionError(f"eval results cannot be written under {ledgers_dir}")

    results_dir = RESULTS_DIR.resolve()
    try:
        candidate.relative_to(results_dir)
    except ValueError as exc:
        raise PartitionError(f"eval results must be written under {results_dir}") from exc


def held_out_ids(manifest: Any) -> set[str]:
    return {task.id for task in manifest.held_out()}
