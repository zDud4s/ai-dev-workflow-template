from __future__ import annotations

import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
EVAL_ROOT = REPO_ROOT / ".ai" / "eval"
SUITE_ROOT = EVAL_ROOT / "suite"
sys.path.insert(0, str(EVAL_ROOT))

from harness import partition as partition_module  # noqa: E402
from harness.loader import Task, load_manifest  # noqa: E402
from harness.partition import PartitionError  # noqa: E402
from harness.run_eval import (  # noqa: E402
    _select_tasks,
    resolve_results_path,
    run_suite,
)
from harness.runner import (  # noqa: E402
    InvokeResult,
    PhaseResult,
    build_phase_command,
    resolve_launch,
)


MODELS_CFG = {
    "plan": {"model": "claude-plan"},
    "execute": {"model": "gpt-execute", "reasoning_effort": "high"},
    "review": {"model": "claude-review"},
}


def test_build_phase_command_plan_is_claude_readonly() -> None:
    for phase, model in (("plan", "claude-plan"), ("review", "claude-review")):
        argv = build_phase_command(phase, "prompt.md", MODELS_CFG)

        assert argv[0] == "claude"
        assert "--model" in argv
        assert argv[argv.index("--model") + 1] == model
        assert "--dangerously-bypass-approvals-and-sandbox" not in argv


def test_build_phase_command_execute_is_codex_writecapable(tmp_path: Path) -> None:
    for phase in ("execute", "fix"):
        argv = build_phase_command(phase, "prompt.md", MODELS_CFG)

        assert argv[:2] == ["codex", "exec"]
        assert "-m" in argv
        assert argv[argv.index("-m") + 1] == "gpt-execute"
        assert "--config" in argv
        assert argv[argv.index("--config") + 1] == "model_reasoning_effort=high"
        assert "--dangerously-bypass-approvals-and-sandbox" in argv
        assert argv[argv.index("-C") + 1] == str(REPO_ROOT)

        cwd_argv = build_phase_command(phase, "prompt.md", MODELS_CFG, cwd=tmp_path)
        assert cwd_argv[cwd_argv.index("-C") + 1] == str(tmp_path)


def test_resolve_launch_nt_prefers_ps1() -> None:
    argv = ["codex", "exec", "-m", "gpt-5.5"]

    resolved = resolve_launch(
        argv,
        os_name="nt",
        which=lambda name: r"C:\x\codex.CMD" if name == "codex" else None,
        exists=lambda path: path == r"C:\x\codex.ps1",
    )

    assert resolved == [
        "powershell",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        r"C:\x\codex.ps1",
        "exec",
        "-m",
        "gpt-5.5",
    ]


def test_resolve_launch_nt_cmd_when_no_ps1() -> None:
    argv = ["codex", "exec", "-m", "gpt-5.5"]

    resolved = resolve_launch(
        argv,
        os_name="nt",
        which=lambda name: r"C:\x\codex.CMD" if name == "codex" else None,
        exists=lambda _path: False,
    )

    assert resolved == ["cmd", "/c", r"C:\x\codex.CMD", "exec", "-m", "gpt-5.5"]


def test_resolve_launch_posix() -> None:
    argv = ["codex", "exec", "-m", "gpt-5.5"]

    resolved = resolve_launch(
        argv,
        os_name="posix",
        which=lambda name: "/usr/bin/codex" if name == "codex" else None,
    )

    assert resolved == ["/usr/bin/codex", "exec", "-m", "gpt-5.5"]


def test_resolve_launch_unresolved() -> None:
    argv = ["codex", "exec", "-m", "gpt-5.5"]

    resolved = resolve_launch(
        argv,
        os_name="nt",
        which=lambda _name: None,
        exists=lambda _path: False,
    )

    assert resolved == ["codex", "exec", "-m", "gpt-5.5"]


def test_run_suite_partition_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    results_dir = _patch_results_dir(tmp_path, monkeypatch)
    manifest = load_manifest(SUITE_ROOT)

    held_out = run_suite(
        "a",
        _select_tasks(manifest, "held-out"),
        _fake_runner_factory([]),
        results_dir / "held-out.jsonl",
        tmp_path / "held-out-work",
    )
    tuning = run_suite(
        "a",
        _select_tasks(manifest, "tuning"),
        _fake_runner_factory([]),
        results_dir / "tuning.jsonl",
        tmp_path / "tuning-work",
    )

    assert [result.task_id for result in held_out] == [
        "reverse-words",
        "roman-to-int",
        "is-balanced",
        "int-to-roman",
        "lru-cache",
    ]
    assert [result.task_id for result in tuning] == [
        "sum-list",
        "compare-version",
        "flatten",
        "json-pointer",
        "kv-store",
        "cli-parser",
    ]


def test_run_suite_repeats_trials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    results_dir = _patch_results_dir(tmp_path, monkeypatch)
    task = load_manifest(SUITE_ROOT).tuning()[0]

    results = run_suite(
        "a",
        [task],
        _fake_runner_factory([]),
        results_dir / "arm-a.jsonl",
        tmp_path / "work",
        trials=3,
    )

    assert len(results) == 3
    assert {result.task_id for result in results} == {task.id}

    with pytest.raises(ValueError):
        run_suite(
            "a",
            [task],
            _fake_runner_factory([]),
            results_dir / "arm-a-bad.jsonl",
            tmp_path / "work-bad",
            trials=0,
        )


def test_run_suite_writes_under_results_and_proposal_routing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    results_dir = _patch_results_dir(tmp_path, monkeypatch)
    task = load_manifest(SUITE_ROOT).tuning()[0]
    out_path = results_dir / "arm-a.jsonl"

    run_suite(
        "a",
        [task],
        _fake_runner_factory([]),
        out_path,
        tmp_path / "work",
    )

    assert out_path.exists()
    assert (
        resolve_results_path(Path("repo"), "b", "candidate-1")
        == Path("repo") / ".ai" / "eval" / "results" / "proposals" / "candidate-1.jsonl"
    )
    with pytest.raises(PartitionError):
        run_suite(
            "a",
            [],
            _fake_runner_factory([]),
            tmp_path / "outside.jsonl",
            tmp_path / "outside-work",
        )


def test_run_suite_dispatches_correct_arm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    results_dir = _patch_results_dir(tmp_path, monkeypatch)
    task = load_manifest(SUITE_ROOT).tuning()[0]
    recorded_arms: list[str] = []

    for arm in ("a", "b", "c"):
        results = run_suite(
            arm,
            [task],
            _fake_runner_factory(recorded_arms),
            results_dir / f"arm-{arm}.jsonl",
            tmp_path / f"work-{arm}",
        )

        assert results[0].arm == arm

    assert recorded_arms == ["a", "b", "c"]


def _patch_results_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    results_dir = tmp_path / "results"
    monkeypatch.setattr(partition_module, "RESULTS_DIR", results_dir)
    return results_dir


def _fake_runner_factory(recorded_arms: list[str]):
    def make_runner(arm: str):
        recorded_arms.append(arm)
        if arm == "a":
            return _fake_invoke
        return _fake_phase_runner

    return make_runner


def _fake_invoke(prompt: str) -> InvokeResult:
    return InvokeResult(text=_solution_for_prompt(prompt), tokens_in=None, tokens_out=None)


def _fake_phase_runner(phase_name: str, prompt: str) -> PhaseResult:
    if phase_name in {"execute", "fix"}:
        return PhaseResult(
            text=_solution_for_prompt(prompt),
            tokens_in=None,
            tokens_out=None,
        )
    return PhaseResult(text="", tokens_in=None, tokens_out=None)


def _solution_for_prompt(prompt: str) -> str:
    if "reverse_words" in prompt:
        return "def reverse_words(s):\n    return ' '.join(reversed(s.split()))\n"
    return "def sum_list(nums):\n    return sum(nums)\n"
