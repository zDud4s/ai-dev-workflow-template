from __future__ import annotations

import json
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[3]
RESULTS_DIR = REPO_ROOT / ".ai" / "eval" / "results"


def _empty_summary() -> dict[str, Any]:
    return {"total": 0, "passed": 0, "by_task": {}}


def _summarize(path: Path) -> dict[str, Any]:
    by_task: dict[str, bool] = {}
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if row.get("partition") != "held-out":
                continue
            task_id = row.get("task_id")
            if not task_id:
                continue
            by_task[str(task_id)] = bool(row.get("success"))
    passed = sum(1 for success in by_task.values() if success)
    return {"total": len(by_task), "passed": passed, "by_task": by_task}


def _merge_summaries(paths: list[Path]) -> dict[str, Any]:
    by_task: dict[str, bool] = {}
    for path in paths:
        by_task.update(_summarize(path)["by_task"])
    passed = sum(1 for success in by_task.values() if success)
    return {"total": len(by_task), "passed": passed, "by_task": by_task}


def held_out_summary(results_dir: str | Path = RESULTS_DIR) -> dict[str, Any]:
    root = Path(results_dir)
    if not root.is_dir():
        return _empty_summary()
    paths = sorted(path for path in root.glob("*.jsonl") if path.is_file())
    return _merge_summaries(paths)


def proposal_held_out_summary(
    results_dir: str | Path,
    proposal_id: str,
) -> dict[str, Any] | None:
    path = Path(results_dir) / "proposals" / f"{proposal_id}.jsonl"
    if not path.is_file():
        return None
    return _summarize(path)


def _regresses(baseline: dict[str, Any], candidate: dict[str, Any]) -> bool:
    baseline_by_task = baseline.get("by_task") or {}
    candidate_by_task = candidate.get("by_task") or {}
    for task_id, passed in baseline_by_task.items():
        if passed and not candidate_by_task.get(task_id, False):
            return True
    return int(candidate.get("passed") or 0) < int(baseline.get("passed") or 0)


def evaluate_proposal(
    proposal_id: str,
    results_dir: str | Path = RESULTS_DIR,
) -> dict[str, Any]:
    baseline = held_out_summary(results_dir)
    candidate = proposal_held_out_summary(results_dir, proposal_id)
    if not baseline.get("total") or candidate is None:
        return {
            "decision": "allow",
            "reason": "held-out: not evaluated",
            "baseline": baseline if baseline.get("total") else None,
            "candidate": candidate,
        }
    if _regresses(baseline, candidate):
        return {
            "decision": "block",
            "reason": "held-out: regression detected",
            "baseline": baseline,
            "candidate": candidate,
        }
    return {
        "decision": "allow",
        "reason": "held-out: no regression",
        "baseline": baseline,
        "candidate": candidate,
    }
