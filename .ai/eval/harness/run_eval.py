from __future__ import annotations

import argparse
import shlex
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
    run_arm_a,
    run_arm_b,
    run_arm_c,
    write_results,
)


RunnerFactory = Callable[[str], Invoker | PhaseRunner]

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
) -> list[ArmResult]:
    """Run one arm across supplied tasks and append JSONL ArmResults."""
    if arm not in {"a", "b", "c"}:
        raise ValueError(f"unsupported arm: {arm}")

    root = Path(workdir_root)
    root.mkdir(parents=True, exist_ok=True)
    runner = make_runner(arm)
    results: list[ArmResult] = []

    for index, task in enumerate(tasks):
        workdir = Path(
            tempfile.mkdtemp(prefix=f"arm-{arm}-{task.id}-{index}-", dir=root)
        )
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
    try:
        run_suite(
            args.arm,
            tasks,
            _make_live_runner,
            resolve_results_path(REPO_ROOT, args.arm, args.proposal),
            Path(tempfile.mkdtemp(prefix=f"eval-arm-{args.arm}-")),
        )
    finally:
        _ACTIVE_SUITE_ROOT = previous_suite_root
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


def _make_live_runner(arm: str) -> Invoker | PhaseRunner:
    if arm == "a":
        return _live_single_call_invoker
    return cli_phase_runner


def _live_single_call_invoker(prompt: str) -> InvokeResult:
    models_cfg = load_models_config(MODELS_PATH)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle:
        handle.write(prompt)
        prompt_path = Path(handle.name)

    try:
        command = build_phase_command("execute", str(prompt_path), models_cfg)
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
            f"arm a invocation failed with exit {completed.returncode}: "
            f"{completed.stderr.strip()}"
        )
    return InvokeResult(text=completed.stdout, tokens_in=None, tokens_out=None)


if __name__ == "__main__":  # pragma: no cover - exercised by script usage
    raise SystemExit(main())
