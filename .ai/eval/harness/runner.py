from __future__ import annotations

import json
import os
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
    _prepare_seed(task, task_dir, output_dir)

    start = now()
    invoke_result = invoke(prompt)
    if task.kind == "single":
        solution_path = output_dir / task.entrypoint
        _materialize_solution_file(solution_path, invoke_result.text, None)
    shutil.copyfile(task_dir / task.check, output_dir / task.check)
    completed = subprocess.run(
        [sys.executable, task.check],
        cwd=output_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
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
    _prepare_seed(task, task_dir, output_dir)

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
    solution = _solution_for_review(task, output_dir, execute.text)
    review = phase_runner(
        "review",
        "Review this proposed solution for the task. "
        "This review is advisory only; the deterministic check decides success.\n\n"
        f"Task:\n{prompt}\n\nSolution:\n{solution}",
    )

    shutil.copyfile(task_dir / task.check, output_dir / task.check)
    completed = subprocess.run(
        [sys.executable, task.check],
        cwd=output_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
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
    _prepare_seed(task, task_dir, output_dir)
    shutil.copyfile(task_dir / task.check, output_dir / task.check)

    is_single = task.kind == "single"
    solution_path = output_dir / task.entrypoint if is_single else None

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
    if is_single:
        solution = _materialize_solution_file(solution_path, execute.text, None)
    else:
        solution = execute.text
    completed = _run_check(task, output_dir)
    fixes_done = 0

    while completed.returncode != 0 and fixes_done < max_fixes:
        before_solution = _solution_snapshot(solution_path) if is_single else None
        fix = phase_runner(
            "fix",
            "Fix this solution so it passes the deterministic check. "
            "Return only the complete source code.\n\n"
            f"Task:\n{prompt}\n\nPlan:\n{plan.text}\n\n"
            f"Current solution:\n{solution}\n\nCheck output tail:\n"
            f"{_completed_tail(completed)}",
        )
        phase_results.append(fix)
        if is_single:
            solution = _materialize_solution_file(solution_path, fix.text, before_solution)
        else:
            solution = fix.text
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


def build_phase_command(
    phase: str,
    prompt_path: str,
    models_cfg: dict[str, Any],
    cwd: str | Path | None = None,
) -> list[str]:
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
            # NOTE: no --bare here. --bare skips the keychain, so OAuth/claude.ai
            # logins resolve as "Not logged in" for these plan/review calls. The
            # eval runs read-only claude phases with normal keychain auth.
            "--exclude-dynamic-system-prompt-sections",
            PHASE_INSTRUCTION.format(phase=phase),
            "--model",
            model,
        ]
    if phase in {"execute", "fix"}:
        model = _phase_model(models_cfg, "execute")
        effort = _phase_effort(models_cfg, "execute")
        command_cwd = Path(cwd) if cwd is not None else _repo_root()
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
            str(command_cwd),
        ]
    raise ValueError(f"unsupported phase: {phase}")


def resolve_launch(
    argv: list[str],
    os_name: str = os.name,
    which: Callable[[str], str | None] = shutil.which,
    exists: Callable[[str], bool] | None = None,
) -> list[str]:
    """Resolve a CLI argv to a CreateProcess-safe launch argv."""
    if not argv:
        raise ValueError("argv must not be empty")

    exists = exists or os.path.exists
    base = which(argv[0]) or argv[0]
    lower_base = base.lower()

    if os_name == "nt":
        ps1 = base if lower_base.endswith(".ps1") else os.path.splitext(base)[0] + ".ps1"
        if exists(ps1):
            return [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                ps1,
                *argv[1:],
            ]
        if lower_base.endswith((".cmd", ".bat")):
            return ["cmd", "/c", base, *argv[1:]]

    return [base, *argv[1:]]


def cli_phase_runner(
    phase_name: str,
    prompt: str,
    cwd: str | Path | None = None,
    entrypoint: str | None = None,
    project: bool = False,
) -> PhaseResult:
    """Run one live eval phase through the configured headless dispatcher.

    This function writes the phase prompt to a temporary file, pipes that file to
    the subprocess stdin, and captures stdout. It is intentionally live-only and
    is not exercised by unit tests because it may invoke LLM CLIs.
    """
    models_cfg = load_models_config(_repo_root() / ".ai" / "models.yaml")
    if phase_name in {"execute", "fix"}:
        if project:
            prompt = with_project_files_instruction(prompt)
        elif entrypoint is not None:
            prompt = with_solution_file_instruction(prompt, entrypoint)

    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle:
        handle.write(prompt)
        prompt_path = Path(handle.name)

    try:
        command = build_phase_command(phase_name, str(prompt_path), models_cfg, cwd=cwd)
        launch_command = resolve_launch(command)
        with prompt_path.open("r", encoding="utf-8") as stdin:
            completed = subprocess.run(
                launch_command,
                stdin=stdin,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
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


def with_solution_file_instruction(prompt: str, entrypoint: str) -> str:
    return (
        f"{prompt}\n\n"
        "Write the candidate solution to a file in the current directory.\n"
        f"File path: {entrypoint}\n"
        "The file must contain only the complete solution source code. "
        "Do not rely on stdout as the solution; stdout may be a progress "
        "transcript."
    )


def with_project_files_instruction(prompt: str) -> str:
    return (
        f"{prompt}\n\n"
        "Create or modify whatever files are needed in the current directory to "
        "satisfy the task. Any starter files already present are part of the "
        "project; preserve their existing behavior unless the task says to change "
        "it. Do not rely on stdout as the solution; write real files."
    )


def _prepare_seed(task: Task, task_dir: Path, output_dir: Path) -> None:
    """Copy a project task's seed/ tree into the workdir before the agent runs."""
    if task.seed is None:
        return
    seed_dir = task_dir / task.seed
    for src in seed_dir.rglob("*"):
        rel = src.relative_to(seed_dir)
        dest = output_dir / rel
        if src.is_dir():
            dest.mkdir(parents=True, exist_ok=True)
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest)


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


def _solution_snapshot(path: Path) -> tuple[int, int] | None:
    if not path.is_file():
        return None
    stat = path.stat()
    return (stat.st_size, stat.st_mtime_ns)


def _materialize_solution_file(
    path: Path,
    fallback_text: str,
    before_solution: tuple[int, int] | None,
) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file() and path.stat().st_size > 0:
        after_solution = _solution_snapshot(path)
        if before_solution is None or after_solution != before_solution:
            return path.read_text(encoding="utf-8")

    path.write_text(fallback_text, encoding="utf-8")
    return fallback_text


def _solution_for_review(task: Task, output_dir: Path, execute_text: str) -> str:
    """Resolve the solution text shown to the advisory review phase.

    For single-file tasks this materializes the entrypoint (file precedence over
    transcript) and returns its content. For project tasks there is no single
    file, so the executor's own output is passed through as advisory context.
    """
    if task.kind == "single":
        return _materialize_solution_file(
            output_dir / task.entrypoint,
            execute_text,
            before_solution=None,
        )
    return execute_text


def _run_check(task: Task, output_dir: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, task.check],
        cwd=output_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
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
