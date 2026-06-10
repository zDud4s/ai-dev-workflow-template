from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from .loader import Task
from .partition import assert_results_path


@dataclass
class InvokeResult:
    text: str
    tokens_in: int | None
    tokens_out: int | None


@dataclass
class PhaseResult:
    text: str
    tokens_in: int | None
    tokens_out: int | None


@dataclass
class ArmResult:
    arm: str
    task_id: str
    partition: str
    success: bool
    tokens_in: int | None
    tokens_out: int | None
    duration_ms: int


Invoker = Callable[[str], InvokeResult]
PhaseRunner = Callable[[str, str], PhaseResult]
Clock = Callable[[], float]


PHASE_INSTRUCTION = (
    "Execute the attached {phase} phase exactly. Return only the phase result. "
    "If you cannot proceed, emit the Escalation output format and exit non-zero."
)


def run_arm_a(
    task: Task,
    suite_root: str | Path,
    invoke: Invoker,
    workdir: str | Path,
    now: Clock = time.monotonic,
) -> ArmResult:
    root = Path(suite_root)
    task_dir = root / task.path
    prompt = (task_dir / "task.md").read_text(encoding="utf-8")
    output_dir = Path(workdir)
    output_dir.mkdir(parents=True, exist_ok=True)

    start = now()
    invoke_result = invoke(prompt)
    (output_dir / task.entrypoint).write_text(invoke_result.text, encoding="utf-8")
    shutil.copyfile(task_dir / task.check, output_dir / task.check)
    completed = subprocess.run(
        [sys.executable, task.check],
        cwd=output_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    end = now()

    return ArmResult(
        arm="a",
        task_id=task.id,
        partition=task.partition,
        success=completed.returncode == 0,
        tokens_in=invoke_result.tokens_in,
        tokens_out=invoke_result.tokens_out,
        duration_ms=max(0, int((end - start) * 1000)),
    )


def run_arm_b(
    task: Task,
    suite_root: str | Path,
    phase_runner: PhaseRunner,
    workdir: str | Path,
    now: Clock = time.monotonic,
) -> ArmResult:
    root = Path(suite_root)
    task_dir = root / task.path
    prompt = (task_dir / "task.md").read_text(encoding="utf-8")
    output_dir = Path(workdir)
    output_dir.mkdir(parents=True, exist_ok=True)

    start = now()
    plan = phase_runner(
        "plan",
        "Write a short implementation plan for this task.\n\n"
        f"Task:\n{prompt}",
    )
    execute = phase_runner(
        "execute",
        "Write the complete solution file content for this task. "
        "Return only the source code.\n\n"
        f"Task:\n{prompt}\n\nPlan:\n{plan.text}",
    )
    review = phase_runner(
        "review",
        "Review this proposed solution for the task. "
        "This review is advisory only; the deterministic check decides success.\n\n"
        f"Task:\n{prompt}\n\nSolution:\n{execute.text}",
    )

    (output_dir / task.entrypoint).write_text(execute.text, encoding="utf-8")
    shutil.copyfile(task_dir / task.check, output_dir / task.check)
    completed = subprocess.run(
        [sys.executable, task.check],
        cwd=output_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    end = now()

    return ArmResult(
        arm="b",
        task_id=task.id,
        partition=task.partition,
        success=completed.returncode == 0,
        tokens_in=_sum_optional_tokens(plan.tokens_in, execute.tokens_in, review.tokens_in),
        tokens_out=_sum_optional_tokens(
            plan.tokens_out,
            execute.tokens_out,
            review.tokens_out,
        ),
        duration_ms=max(0, int((end - start) * 1000)),
    )


def run_arm_c(
    task: Task,
    suite_root: str | Path,
    phase_runner: PhaseRunner,
    workdir: str | Path,
    now: Clock = time.monotonic,
    max_fixes: int = 2,
) -> ArmResult:
    root = Path(suite_root)
    task_dir = root / task.path
    prompt = (task_dir / "task.md").read_text(encoding="utf-8")
    output_dir = Path(workdir)
    output_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(task_dir / task.check, output_dir / task.check)

    start = now()
    plan = phase_runner(
        "plan",
        "Write a short implementation plan for this task.\n\n"
        f"Task:\n{prompt}",
    )
    execute = phase_runner(
        "execute",
        "Write the complete solution file content for this task. "
        "Return only the source code.\n\n"
        f"Task:\n{prompt}\n\nPlan:\n{plan.text}",
    )

    phase_results = [plan, execute]
    solution = execute.text
    (output_dir / task.entrypoint).write_text(solution, encoding="utf-8")
    completed = _run_check(task, output_dir)
    fixes_done = 0

    while completed.returncode != 0 and fixes_done < max_fixes:
        fix = phase_runner(
            "fix",
            "Fix this solution so it passes the deterministic check. "
            "Return only the complete source code.\n\n"
            f"Task:\n{prompt}\n\nPlan:\n{plan.text}\n\n"
            f"Current solution:\n{solution}\n\nCheck output tail:\n"
            f"{_completed_tail(completed)}",
        )
        phase_results.append(fix)
        solution = fix.text
        (output_dir / task.entrypoint).write_text(solution, encoding="utf-8")
        completed = _run_check(task, output_dir)
        fixes_done += 1

    review = phase_runner(
        "review",
        "Review this final solution for the task. "
        "This review is advisory only; the deterministic check decides success.\n\n"
        f"Task:\n{prompt}\n\nSolution:\n{solution}",
    )
    phase_results.append(review)
    end = now()

    return ArmResult(
        arm="c",
        task_id=task.id,
        partition=task.partition,
        success=completed.returncode == 0,
        tokens_in=_sum_optional_tokens(*(result.tokens_in for result in phase_results)),
        tokens_out=_sum_optional_tokens(*(result.tokens_out for result in phase_results)),
        duration_ms=max(0, int((end - start) * 1000)),
    )


def build_phase_command(phase: str, prompt_path: str, models_cfg: dict[str, Any]) -> list[str]:
    """Build the dispatcher argv for a phase.

    The prompt file is supplied by the caller on stdin; ``prompt_path`` is kept
    in the signature so callers can pair a concrete temp file with the argv
    without the path becoming part of the command.
    """
    del prompt_path

    if phase in {"plan", "review"}:
        model = _phase_model(models_cfg, phase)
        return [
            "claude",
            "-p",
            "--bare",
            "--exclude-dynamic-system-prompt-sections",
            PHASE_INSTRUCTION.format(phase=phase),
            "--model",
            model,
        ]
    if phase in {"execute", "fix"}:
        model = _phase_model(models_cfg, "execute")
        effort = _phase_effort(models_cfg, "execute")
        return [
            "codex",
            "exec",
            "--skip-git-repo-check",
            "-m",
            model,
            "--config",
            f"model_reasoning_effort={effort}",
            "--dangerously-bypass-approvals-and-sandbox",
            "-C",
            str(_repo_root()),
        ]
    raise ValueError(f"unsupported phase: {phase}")


def cli_phase_runner(phase_name: str, prompt: str) -> PhaseResult:
    """Run one live eval phase through the configured headless dispatcher.

    This function writes the phase prompt to a temporary file, pipes that file to
    the subprocess stdin, and captures stdout. It is intentionally live-only and
    is not exercised by unit tests because it may invoke LLM CLIs.
    """
    models_cfg = load_models_config(_repo_root() / ".ai" / "models.yaml")
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle:
        handle.write(prompt)
        prompt_path = Path(handle.name)

    try:
        command = build_phase_command(phase_name, str(prompt_path), models_cfg)
        with prompt_path.open("r", encoding="utf-8") as stdin:
            completed = subprocess.run(
                command,
                stdin=stdin,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
    finally:
        prompt_path.unlink(missing_ok=True)

    if completed.returncode != 0:
        raise RuntimeError(
            f"{phase_name} phase failed with exit {completed.returncode}: "
            f"{completed.stderr.strip()}"
        )
    return PhaseResult(
        text=completed.stdout,
        tokens_in=None,
        tokens_out=_parse_token_total(completed.stdout, completed.stderr),
    )


def load_models_config(path: str | Path) -> dict[str, Any]:
    """Parse the small YAML subset used by .ai/models.yaml without dependencies."""
    data: dict[str, Any] = {}
    current_section: dict[str, Any] | None = None

    for raw_line in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].rstrip()
        if not line.strip():
            continue

        indent = len(line) - len(line.lstrip(" "))
        key, sep, raw_value = line.strip().partition(":")
        if sep != ":":
            continue
        value = raw_value.strip()

        if indent == 0:
            if value:
                data[key] = _parse_yaml_scalar(value)
                current_section = None
            else:
                current_section = {}
                data[key] = current_section
            continue

        if current_section is not None:
            current_section[key] = _parse_yaml_scalar(value)

    return data


def _run_check(task: Task, output_dir: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, task.check],
        cwd=output_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )


def _completed_tail(completed: subprocess.CompletedProcess[str], max_lines: int = 40) -> str:
    output = "\n".join(part for part in (completed.stdout, completed.stderr) if part)
    return "\n".join(output.splitlines()[-max_lines:])


def _sum_optional_tokens(*values: int | None) -> int | None:
    tokens = [value for value in values if value is not None]
    if not tokens:
        return None
    return sum(tokens)


def _phase_model(models_cfg: dict[str, Any], phase: str) -> str:
    phase_cfg = models_cfg.get(phase)
    if not isinstance(phase_cfg, dict):
        raise KeyError(f"missing models config for phase: {phase}")
    model = phase_cfg.get("model")
    if not isinstance(model, str) or not model:
        raise KeyError(f"missing model for phase: {phase}")
    return model


def _phase_effort(models_cfg: dict[str, Any], phase: str) -> str:
    phase_cfg = models_cfg.get(phase)
    if not isinstance(phase_cfg, dict):
        raise KeyError(f"missing models config for phase: {phase}")
    effort = phase_cfg.get("reasoning_effort", "medium")
    if not isinstance(effort, str) or not effort:
        raise KeyError(f"invalid reasoning_effort for phase: {phase}")
    return effort


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _parse_token_total(stdout: str, stderr: str) -> int | None:
    match = re.search(r"tokens used[:\s]+([\d,]+)", f"{stdout}\n{stderr}", re.IGNORECASE)
    if match is None:
        return None
    return int(match.group(1).replace(",", ""))


def _parse_yaml_scalar(value: str) -> Any:
    if value in {"true", "false"}:
        return value == "true"
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [_parse_yaml_scalar(part.strip()) for part in inner.split(",")]
    if value.startswith('"') and value.endswith('"'):
        return value[1:-1]
    try:
        return int(value)
    except ValueError:
        return value


def write_results(results: list[ArmResult], path: str | Path) -> None:
    output_path = Path(path)
    assert_results_path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("a", encoding="utf-8") as handle:
        for result in results:
            handle.write(json.dumps(asdict(result), sort_keys=True) + "\n")
