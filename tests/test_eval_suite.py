from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
EVAL_ROOT = REPO_ROOT / ".ai" / "eval"
SUITE_ROOT = EVAL_ROOT / "suite"
sys.path.insert(0, str(EVAL_ROOT))

from harness.loader import ManifestError, load_manifest  # noqa: E402
from harness.partition import PartitionError, assert_results_path, held_out_ids  # noqa: E402
from harness.runner import InvokeResult, run_arm_a  # noqa: E402


def test_manifest_loads_and_partitions() -> None:
    manifest = load_manifest(SUITE_ROOT)

    assert [task.id for task in manifest.all()] == ["sum-list", "reverse-words"]
    assert {task.id for task in manifest.tuning()} == {"sum-list"}
    assert held_out_ids(manifest) == {"reverse-words"}


def test_manifest_rejects_bad_manifest(tmp_path: Path) -> None:
    suite_root = tmp_path / "suite"
    task_dir = suite_root / "tasks" / "sum-list"
    task_dir.mkdir(parents=True)
    (task_dir / "check.py").write_text("import sys\nsys.exit(0)\n", encoding="utf-8")
    (suite_root / "manifest.toml").write_text(
        """
version = 1

[[tasks]]
id = "sum-list"
partition = "tuning"
path = "tasks/sum-list"
entrypoint = "solution.py"
check = "check.py"

[[tasks]]
id = "sum-list"
partition = "held-out"
path = "tasks/sum-list"
entrypoint = "solution.py"
check = "check.py"
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ManifestError):
        load_manifest(suite_root)


def test_partition_guard_blocks_tuning_ledger() -> None:
    with pytest.raises(PartitionError):
        assert_results_path(".ai/ledgers/metrics.jsonl")

    assert_results_path(".ai/eval/results/arm-a.jsonl")


def test_seed_checks_are_well_formed(tmp_path: Path) -> None:
    manifest = load_manifest(SUITE_ROOT)
    solutions = {
        "sum-list": {
            True: "def sum_list(nums):\n    return sum(nums)\n",
            False: "def sum_list(nums):\n    return 0\n",
        },
        "reverse-words": {
            True: "def reverse_words(s):\n    return ' '.join(reversed(s.split()))\n",
            False: "def reverse_words(s):\n    return s\n",
        },
    }

    for task in manifest.all():
        task_dir = SUITE_ROOT / task.path
        for should_pass, source in solutions[task.id].items():
            workdir = tmp_path / task.id / str(should_pass)
            workdir.mkdir(parents=True)
            (workdir / task.entrypoint).write_text(source, encoding="utf-8")
            shutil.copyfile(task_dir / task.check, workdir / task.check)

            completed = subprocess.run(
                [sys.executable, task.check],
                cwd=workdir,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            if should_pass:
                assert completed.returncode == 0
            else:
                assert completed.returncode != 0


def test_run_arm_a_success_and_failure(tmp_path: Path) -> None:
    task = load_manifest(SUITE_ROOT).tuning()[0]

    def correct_invoke(prompt: str) -> InvokeResult:
        assert "sum_list" in prompt
        return InvokeResult(
            text="def sum_list(nums):\n    return sum(nums)\n",
            tokens_in=12,
            tokens_out=8,
        )

    success = run_arm_a(task, SUITE_ROOT, correct_invoke, tmp_path / "success")
    assert success.success is True
    assert success.arm == "a"
    assert success.task_id == "sum-list"
    assert success.partition == "tuning"
    assert success.tokens_in == 12
    assert success.tokens_out == 8
    assert isinstance(success.duration_ms, int)
    assert success.duration_ms >= 0

    def bad_invoke(prompt: str) -> InvokeResult:
        assert "sum_list" in prompt
        return InvokeResult(text="not python", tokens_in=None, tokens_out=None)

    failure = run_arm_a(task, SUITE_ROOT, bad_invoke, tmp_path / "failure")
    assert failure.success is False
    assert failure.duration_ms >= 0
