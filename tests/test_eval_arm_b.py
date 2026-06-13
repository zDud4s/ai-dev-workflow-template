from __future__ import annotations

import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
EVAL_ROOT = REPO_ROOT / ".ai" / "eval"
SUITE_ROOT = EVAL_ROOT / "suite"
sys.path.insert(0, str(EVAL_ROOT))

from harness.loader import load_manifest  # noqa: E402
from harness.runner import PhaseResult, run_arm_b  # noqa: E402


def test_arm_b_success_with_fake_runner(tmp_path: Path) -> None:
    task = load_manifest(SUITE_ROOT).tuning()[0]

    def fake_phase_runner(phase_name: str, prompt: str) -> PhaseResult:
        if phase_name == "execute":
            return PhaseResult(
                text="def sum_list(nums):\n    return sum(nums)\n",
                tokens_in=None,
                tokens_out=None,
            )
        return PhaseResult(text="", tokens_in=None, tokens_out=None)

    result = run_arm_b(task, SUITE_ROOT, fake_phase_runner, tmp_path / "success")

    assert result.success is True
    assert result.arm == "b"
    assert result.duration_ms >= 0


def test_arm_b_failure_with_fake_runner(tmp_path: Path) -> None:
    task = load_manifest(SUITE_ROOT).tuning()[0]

    def fake_phase_runner(phase_name: str, prompt: str) -> PhaseResult:
        if phase_name == "execute":
            return PhaseResult(text="def sum_list(nums):\n    return 0\n", tokens_in=None, tokens_out=None)
        return PhaseResult(text="", tokens_in=None, tokens_out=None)

    result = run_arm_b(task, SUITE_ROOT, fake_phase_runner, tmp_path / "failure")

    assert result.success is False


def test_arm_b_runs_all_three_phases(tmp_path: Path) -> None:
    task = load_manifest(SUITE_ROOT).tuning()[0]
    phases: list[str] = []

    def fake_phase_runner(phase_name: str, prompt: str) -> PhaseResult:
        phases.append(phase_name)
        if phase_name == "execute":
            return PhaseResult(text="def sum_list(nums):\n    return sum(nums)\n", tokens_in=None, tokens_out=None)
        return PhaseResult(text="", tokens_in=None, tokens_out=None)

    run_arm_b(task, SUITE_ROOT, fake_phase_runner, tmp_path / "phases")

    assert phases == ["plan", "execute", "review"]


def test_arm_b_aggregates_tokens(tmp_path: Path) -> None:
    task = load_manifest(SUITE_ROOT).tuning()[0]
    tokens = {
        "plan": (1, 2),
        "execute": (3, 5),
        "review": (8, 13),
    }

    def fake_phase_runner(phase_name: str, prompt: str) -> PhaseResult:
        tokens_in, tokens_out = tokens[phase_name]
        if phase_name == "execute":
            return PhaseResult(
                text="def sum_list(nums):\n    return sum(nums)\n",
                tokens_in=tokens_in,
                tokens_out=tokens_out,
            )
        return PhaseResult(text="", tokens_in=tokens_in, tokens_out=tokens_out)

    result = run_arm_b(task, SUITE_ROOT, fake_phase_runner, tmp_path / "tokens")

    assert result.tokens_in == 12
    assert result.tokens_out == 20
