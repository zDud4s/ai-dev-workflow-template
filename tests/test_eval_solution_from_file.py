from __future__ import annotations

import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
EVAL_ROOT = REPO_ROOT / ".ai" / "eval"
SUITE_ROOT = EVAL_ROOT / "suite"
sys.path.insert(0, str(EVAL_ROOT))

from harness.loader import load_manifest  # noqa: E402
from harness.runner import PhaseResult, run_arm_b  # noqa: E402


CORRECT_SOLUTION = "def sum_list(nums):\n    return sum(nums)\n"
GARBAGE_TEXT = "this is a transcript, not python source"


def test_runner_prefers_agent_written_file(tmp_path: Path) -> None:
    task = load_manifest(SUITE_ROOT).tuning()[0]
    workdir = tmp_path / "agent-file"

    def fake_phase_runner(phase_name: str, prompt: str) -> PhaseResult:
        if phase_name == "execute":
            workdir.mkdir(parents=True, exist_ok=True)
            (workdir / task.entrypoint).write_text(CORRECT_SOLUTION, encoding="utf-8")
            return PhaseResult(text=GARBAGE_TEXT, tokens_in=None, tokens_out=None)
        return PhaseResult(text="", tokens_in=None, tokens_out=None)

    result = run_arm_b(task, SUITE_ROOT, fake_phase_runner, workdir)

    assert result.success is True
    assert (workdir / task.entrypoint).read_text(encoding="utf-8") == CORRECT_SOLUTION


def test_runner_falls_back_to_text_when_no_file(tmp_path: Path) -> None:
    task = load_manifest(SUITE_ROOT).tuning()[0]
    workdir = tmp_path / "fallback-text"

    def fake_phase_runner(phase_name: str, prompt: str) -> PhaseResult:
        if phase_name == "execute":
            return PhaseResult(text=CORRECT_SOLUTION, tokens_in=None, tokens_out=None)
        return PhaseResult(text="", tokens_in=None, tokens_out=None)

    result = run_arm_b(task, SUITE_ROOT, fake_phase_runner, workdir)

    assert result.success is True
    assert (workdir / task.entrypoint).read_text(encoding="utf-8") == CORRECT_SOLUTION
