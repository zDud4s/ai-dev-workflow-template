from __future__ import annotations

import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
EVAL_ROOT = REPO_ROOT / ".ai" / "eval"
SUITE_ROOT = EVAL_ROOT / "suite"
sys.path.insert(0, str(EVAL_ROOT))

from harness.loader import load_manifest  # noqa: E402
from harness.runner import PhaseResult, run_arm_c  # noqa: E402


CORRECT_SOLUTION = "def sum_list(nums):\n    return sum(nums)\n"
WRONG_SOLUTION = "def sum_list(nums):\n    return 0\n"


def test_arm_c_succeeds_first_try(tmp_path: Path) -> None:
    task = load_manifest(SUITE_ROOT).tuning()[0]
    phases: list[str] = []

    def fake_phase_runner(phase_name: str, prompt: str) -> PhaseResult:
        phases.append(phase_name)
        if phase_name == "execute":
            return PhaseResult(text=CORRECT_SOLUTION, tokens_in=None, tokens_out=None)
        return PhaseResult(text="", tokens_in=None, tokens_out=None)

    result = run_arm_c(task, SUITE_ROOT, fake_phase_runner, tmp_path / "first-try")

    assert result.success is True
    assert result.arm == "c"
    assert "fix" not in phases


def test_arm_c_recovers_via_fix_loop(tmp_path: Path) -> None:
    task = load_manifest(SUITE_ROOT).tuning()[0]
    phases: list[str] = []

    def fake_phase_runner(phase_name: str, prompt: str) -> PhaseResult:
        phases.append(phase_name)
        if phase_name == "execute":
            return PhaseResult(text=WRONG_SOLUTION, tokens_in=None, tokens_out=None)
        if phase_name == "fix":
            return PhaseResult(text=CORRECT_SOLUTION, tokens_in=None, tokens_out=None)
        return PhaseResult(text="", tokens_in=None, tokens_out=None)

    result = run_arm_c(task, SUITE_ROOT, fake_phase_runner, tmp_path / "fixes")

    assert result.success is True
    assert phases.count("fix") >= 1


def test_arm_c_gives_up_after_max_fixes(tmp_path: Path) -> None:
    task = load_manifest(SUITE_ROOT).tuning()[0]
    phases: list[str] = []

    def fake_phase_runner(phase_name: str, prompt: str) -> PhaseResult:
        phases.append(phase_name)
        if phase_name in {"execute", "fix"}:
            return PhaseResult(text=WRONG_SOLUTION, tokens_in=None, tokens_out=None)
        return PhaseResult(text="", tokens_in=None, tokens_out=None)

    result = run_arm_c(
        task,
        SUITE_ROOT,
        fake_phase_runner,
        tmp_path / "give-up",
        max_fixes=3,
    )

    assert result.success is False
    assert phases.count("fix") == 3


def test_arm_c_runs_phases_in_order(tmp_path: Path) -> None:
    task = load_manifest(SUITE_ROOT).tuning()[0]
    phases: list[str] = []

    def fake_phase_runner(phase_name: str, prompt: str) -> PhaseResult:
        phases.append(phase_name)
        if phase_name == "execute":
            return PhaseResult(text=WRONG_SOLUTION, tokens_in=None, tokens_out=None)
        if phase_name == "fix":
            return PhaseResult(text=CORRECT_SOLUTION, tokens_in=None, tokens_out=None)
        return PhaseResult(text="", tokens_in=None, tokens_out=None)

    run_arm_c(task, SUITE_ROOT, fake_phase_runner, tmp_path / "order")

    assert phases == ["plan", "execute", "fix", "review"]


def test_arm_c_aggregates_tokens(tmp_path: Path) -> None:
    task = load_manifest(SUITE_ROOT).tuning()[0]
    calls = {
        "plan": [(1, 2)],
        "execute": [(3, 5)],
        "fix": [(8, 13), (21, 34)],
        "review": [(55, 89)],
    }

    def fake_phase_runner(phase_name: str, prompt: str) -> PhaseResult:
        tokens_in, tokens_out = calls[phase_name].pop(0)
        if phase_name in {"execute", "fix"} and tokens_in < 21:
            text = WRONG_SOLUTION
        elif phase_name in {"execute", "fix"}:
            text = CORRECT_SOLUTION
        else:
            text = ""
        return PhaseResult(text=text, tokens_in=tokens_in, tokens_out=tokens_out)

    result = run_arm_c(task, SUITE_ROOT, fake_phase_runner, tmp_path / "tokens")

    assert result.success is True
    assert result.tokens_in == 88
    assert result.tokens_out == 143
