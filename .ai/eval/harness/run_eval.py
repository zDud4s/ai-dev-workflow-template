from __future__ import annotations

import argparse
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable, Iterable, cast

if __package__ in {None, ""}:  # pragma: no cover - exercised by script usage
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness.loader import Manifest, Task, load_manifest  # noqa: E402
from harness.runner import (  # noqa: E402
    ArmResult,
    InvokeResult,
    Invoker,
    PhaseRunner,
    build_phase_command,
    cli_phase_runner,
    load_models_config,
    resolve_launch,
    run_arm_a,
    run_arm_b,
    run_arm_c,
    with_project_files_instruction,
    with_solution_file_instruction,
    write_results,
)


TaskRunnerBinder = Callable[[Task, str | Path], Invoker | PhaseRunner]
RunnerFactory = Callable[[str], Invoker | PhaseRunner | TaskRunnerBinder]

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_SUITE_ROOT = REPO_ROOT / ".ai" / "eval" / "suite"
MODELS_PATH = REPO_ROOT / ".ai" / "models.yaml"
_ACTIVE_SUITE_ROOT = DEFAULT_SUITE_ROOT


def run_suite(
    arm: str,
    tasks: Iterable[Task],
    make_runner: RunnerFactory,
    out_path: str | Path,
    workdir_root: str | Path,
    now: Callable[[], float] = time.monotonic,
    trials: int = 1,
) -> list[ArmResult]:
    """Run one arm across supplied tasks and append JSONL ArmResults.

    Each task is run ``trials`` times (default 1). Repeated trials give the
    ablation enough samples to distinguish arms whose per-run outcome is noisy.
    """
    if arm not in {"a", "b", "c"}:
        raise ValueError(f"unsupported arm: {arm}")
    if trials < 1:
        raise ValueError(f"trials must be >= 1, got {trials}")

    root = Path(workdir_root)
    root.mkdir(parents=True, exist_ok=True)
    base_runner = make_runner(arm)
    results: list[ArmResult] = []

    for index, task in enumerate(tasks):
        for trial in range(trials):
            workdir = Path(
                tempfile.mkdtemp(
                    prefix=f"arm-{arm}-{task.id}-{index}-t{trial}-", dir=root
                )
            )
            runner = _bind_runner_to_task(base_runner, task, workdir)
            if arm == "a":
                result = run_arm_a(
                    task,
                    _ACTIVE_SUITE_ROOT,
                    cast(Invoker, runner),
                    workdir,
                    now=now,
                )
            elif arm == "b":
                result = run_arm_b(
                    task,
                    _ACTIVE_SUITE_ROOT,
                    cast(PhaseRunner, runner),
                    workdir,
                    now=now,
                )
            else:
                result = run_arm_c(
                    task,
                    _ACTIVE_SUITE_ROOT,
                    cast(PhaseRunner, runner),
                    workdir,
                    now=now,
                )
            results.append(result)

    write_results(results, out_path)
    return results


def resolve_results_path(repo_root: str | Path, arm: str, proposal: str | None) -> Path:
    results_root = Path(repo_root) / ".ai" / "eval" / "results"
    if proposal:
        return results_root / "proposals" / f"{proposal}.jsonl"
    return results_root / f"arm-{arm}.jsonl"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the objective eval harness.")
    parser.add_argument("--arm", choices=["a", "b", "c"], required=True)
    parser.add_argument(
        "--partition",
        choices=["tuning", "held-out", "all"],
        default="all",
    )
    parser.add_argument("--proposal")
    parser.add_argument("--suite-root", default=str(DEFAULT_SUITE_ROOT))
    parser.add_argument(
        "--trials",
        type=int,
        default=1,
        help="number of times to run each task (default 1)",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    suite_root = Path(args.suite_root).resolve()
    manifest = load_manifest(suite_root)
    tasks = _select_tasks(manifest, args.partition)
    models_cfg = load_models_config(MODELS_PATH)

    if args.dry_run:
        _print_dry_run(args.arm, models_cfg)
        return 0

    global _ACTIVE_SUITE_ROOT
    previous_suite_root = _ACTIVE_SUITE_ROOT
    _ACTIVE_SUITE_ROOT = suite_root
    workdir_root = Path(tempfile.mkdtemp(prefix=f"eval-arm-{args.arm}-"))
    try:
        run_suite(
            args.arm,
            tasks,
            _make_live_runner,
            resolve_results_path(REPO_ROOT, args.arm, args.proposal),
            workdir_root,
            trials=args.trials,
        )
    finally:
        _ACTIVE_SUITE_ROOT = previous_suite_root
        shutil.rmtree(workdir_root, ignore_errors=True)
    return 0


def _select_tasks(manifest: Manifest, partition: str) -> list[Task]:
    if partition == "tuning":
        return manifest.tuning()
    if partition == "held-out":
        return manifest.held_out()
    return manifest.all()


def _print_dry_run(arm: str, models_cfg: dict[str, object]) -> None:
    if arm == "a":
        print("arm a: would issue a single model call")
        return

    phases = ["plan", "execute", "review"]
    if arm == "c":
        phases = ["plan", "execute", "fix", "review"]

    for phase in phases:
        command = build_phase_command(phase, "<stdin>", models_cfg)
        print(f"{phase}: {shlex.join(command)}")


def _make_live_runner(arm: str) -> Invoker | PhaseRunner | TaskRunnerBinder:
    if arm == "a":
        return _live_single_call_invoker_for_task
    return _live_phase_runner_for_task


def _bind_runner_to_task(
    runner: Invoker | PhaseRunner | TaskRunnerBinder,
    task: Task,
    workdir: str | Path,
) -> Invoker | PhaseRunner:
    if getattr(runner, "_needs_eval_workdir", False):
        return cast(TaskRunnerBinder, runner)(task, workdir)
    return cast(Invoker | PhaseRunner, runner)


def _live_phase_runner_for_task(task: Task, workdir: str | Path) -> PhaseRunner:
    project = task.kind == "project"

    def run_phase(phase_name: str, prompt: str) -> PhaseResult:
        if phase_name in {"execute", "fix"}:
            return cli_phase_runner(
                phase_name,
                prompt,
                cwd=workdir,
                entrypoint=task.entrypoint,
                project=project,
            )
        return cli_phase_runner(phase_name, prompt)

    return run_phase


def _live_single_call_invoker_for_task(task: Task, workdir: str | Path) -> Invoker:
    project = task.kind == "project"

    def invoke(prompt: str) -> InvokeResult:
        return _live_single_call_invoker(
            prompt,
            workdir=workdir,
            entrypoint=task.entrypoint,
            project=project,
        )

    return invoke


def _live_single_call_invoker(
    prompt: str,
    workdir: str | Path | None = None,
    entrypoint: str | None = None,
    project: bool = False,
) -> InvokeResult:
    models_cfg = load_models_config(MODELS_PATH)
    if project:
        prompt = with_project_files_instruction(prompt)
    elif entrypoint:
        prompt = with_solution_file_instruction(prompt, entrypoint)

    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle:
        handle.write(prompt)
        prompt_path = Path(handle.name)

    try:
        command = build_phase_command(
            "execute",
            str(prompt_path),
            models_cfg,
            cwd=workdir,
        )
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
            f"arm a invocation failed with exit {completed.returncode}: "
            f"{completed.stderr.strip()}"
        )
    return InvokeResult(text=completed.stdout, tokens_in=None, tokens_out=None)


setattr(_live_phase_runner_for_task, "_needs_eval_workdir", True)
setattr(_live_single_call_invoker_for_task, "_needs_eval_workdir", True)


if __name__ == "__main__":  # pragma: no cover - exercised by script usage
    raise SystemExit(main())
