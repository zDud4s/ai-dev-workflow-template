"""Agent Council orchestrator.

Reads a run spec on stdin, runs the 3-stage council, writes the run record to
<runs_dir>/<id>.json incrementally, and prints one JSON event per line to stdout
for the dashboard's SSE bridge. All seat subprocesses are list-form (no shell).
"""
from __future__ import annotations

import concurrent.futures
import datetime as dt
import json
import os
import random
import re
import subprocess
import sys
import time
from pathlib import Path
from collections.abc import Callable
from typing import Any

import yaml

CLAUDE_TOOL_MODELS: set[str] = set()  # filled per-run from the catalog
DEFAULT_TIMEOUT = 600
DEFAULT_MAX_PARALLEL = 8

Runner = Callable[[list[str], str | None, int], dict[str, Any]]
Emitter = Callable[[dict[str, Any]], None]


def _tool_for_model(model: str, catalog: dict[str, list[str]]) -> str:
    """Return 'claude' or 'codex' for a catalog model id."""
    if model in (catalog.get("codex") or []):
        return "codex"
    return "claude"  # default tool


def build_seat_argv(
    seat: dict[str, Any],
    question: str,
    catalog: dict[str, list[str]],
) -> tuple[list[str], str | None]:
    """Translate a seat into (argv, stdin_text). stdin_text is None for claude."""
    if seat["type"] == "agent":
        model = seat["model"]
        return (["claude", "-p", question, "--agent", seat["ref"], "--model", model], None)
    # model seat
    model = seat.get("model") or seat["ref"]
    if _tool_for_model(model, catalog) == "codex":
        # Mirror serve._build_codex_chat_argv (serve.py:4758-4779) exactly: the
        # trailing "-" makes codex read the prompt from stdin, and the flag is
        # `-m` (not `--model`). Re-deriving these is the #1 source of a hung
        # codex subprocess; keep them identical to the real spawn site.
        return (
            ["codex", "exec", "--json", "--skip-git-repo-check", "-m", model, "-"],
            question + "\n",
        )
    return (["claude", "-p", question, "--model", model], None)


def _all_models(catalog: dict[str, list[str]]) -> set[str]:
    return set(catalog.get("claude") or []) | set(catalog.get("codex") or [])


def validate_seat(
    seat: dict[str, Any],
    catalog: dict[str, list[str]],
    agent_slugs: set[str],
) -> str | None:
    """Return an error string if the seat is invalid, else None."""
    t = seat.get("type")
    if t not in ("model", "agent"):
        return f"seat type must be model|agent, got {t!r}"
    if t == "model":
        model = seat.get("model") or seat.get("ref")
        if model not in _all_models(catalog):
            return f"unknown model ref: {model!r}"
        return None
    # agent
    if seat.get("ref") not in agent_slugs:
        return f"unknown agent ref: {seat.get('ref')!r}"
    model = seat.get("model")
    if model not in (catalog.get("claude") or []):
        return f"agent seats require a claude model (codex has no --agent); got {model!r}"
    return None


def load_council_config(models_yaml_path: str | Path) -> dict[str, Any]:
    """Load the nested council defaults from models.yaml."""
    with Path(models_yaml_path).open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    council = data.get("council") or {}
    return {
        "timeout_seconds": council.get("timeout_seconds", DEFAULT_TIMEOUT),
        "chairman": council.get("chairman") or {},
        "members": council.get("members") or [],
    }


def anonymize(responses: dict[int, str], seed: str) -> dict[str, Any]:
    """Assign shuffled A/B/C labels to seat responses (seeded by run id).

    Returns {anon_map: {label: seat_idx}, for_seat: {seat_idx: {label: text}}}.
    `for_seat[i]` omits seat i's own response (no self-ranking).
    """
    rng = random.Random(seed)
    seat_idxs = sorted(responses)
    labels = [chr(ord("A") + i) for i in range(len(seat_idxs))]
    shuffled = seat_idxs[:]
    rng.shuffle(shuffled)
    anon_map = {label: idx for label, idx in zip(labels, shuffled)}
    for_seat: dict[int, dict[str, str]] = {}
    for viewer in seat_idxs:
        for_seat[viewer] = {
            lbl: responses[idx] for lbl, idx in anon_map.items() if idx != viewer
        }
    return {"anon_map": anon_map, "for_seat": for_seat}


def aggregate_rankings(
    rankings: dict[int, list[tuple[str, int]]],
    anon_map: dict[str, int],
) -> list[dict[str, Any]]:
    """Average-rank leaderboard, comparable under partial participation."""
    acc: dict[int, list[int]] = {}
    for _viewer, ranked in rankings.items():
        for label, rank in ranked:
            seat_idx = anon_map[label]
            acc.setdefault(seat_idx, []).append(rank)
    board = [
        {"seat_idx": idx, "avg_rank": sum(rs) / len(rs), "n": len(rs)}
        for idx, rs in acc.items()
    ]
    board.sort(key=lambda r: (r["avg_rank"], r["seat_idx"]))
    return board


def _text_or_empty(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return str(value)


def default_runner(argv: list[str], stdin: str | None, timeout: int) -> dict[str, Any]:
    """Run one council subprocess and return a normalized result dict."""
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            argv,
            input=stdin,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        return {
            "status": "ok" if completed.returncode == 0 else "error",
            "exit_code": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "error": None,
            "ms": int((time.perf_counter() - started) * 1000),
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "status": "timeout",
            "exit_code": None,
            "stdout": _text_or_empty(exc.stdout),
            "stderr": _text_or_empty(exc.stderr),
            "error": str(exc),
            "ms": int((time.perf_counter() - started) * 1000),
        }
    except Exception as exc:
        return {
            "status": "error",
            "exit_code": None,
            "stdout": "",
            "stderr": "",
            "error": str(exc),
            "ms": int((time.perf_counter() - started) * 1000),
        }


def _write_record(runs_dir: str | Path, record: dict[str, Any]) -> None:
    Path(runs_dir).mkdir(parents=True, exist_ok=True)
    path = Path(runs_dir) / f"{record['id']}.json"
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")


def _emit(emit: Emitter, event: dict[str, Any]) -> None:
    emit(event)


def _run_seat(
    seat_idx: int,
    seat: dict[str, Any],
    prompt: str,
    catalog: dict[str, list[str]],
    timeout: int,
    runner: Runner,
) -> tuple[int, dict[str, Any]]:
    argv, stdin = build_seat_argv(seat, prompt, catalog)
    try:
        return seat_idx, runner(argv, stdin, timeout)
    except Exception as exc:
        return seat_idx, {
            "status": "error",
            "stdout": "",
            "stderr": "",
            "error": str(exc),
            "ms": 0,
        }


def _run_parallel(
    jobs: list[tuple[int, dict[str, Any], str]],
    catalog: dict[str, list[str]],
    timeout: int,
    runner: Runner,
) -> dict[int, dict[str, Any]]:
    if not jobs:
        return {}
    results: dict[int, dict[str, Any]] = {}
    max_workers = min(DEFAULT_MAX_PARALLEL, len(jobs))
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_to_idx = {
            pool.submit(_run_seat, idx, seat, prompt, catalog, timeout, runner): idx
            for idx, seat, prompt in jobs
        }
        for future in concurrent.futures.as_completed(future_to_idx):
            idx, result = future.result()
            results[idx] = result
    return results


def _stage1_entry(seat_idx: int, result: dict[str, Any]) -> dict[str, Any]:
    return {
        "seat_idx": seat_idx,
        "status": result.get("status", "error"),
        "response": result.get("stdout", ""),
        "ms": result.get("ms"),
        "error": result.get("error"),
    }


def _stage2_prompt(question: str, anon_responses: dict[str, str]) -> str:
    parts = [
        "Question:",
        question,
        "",
        "Rank the anonymous peer responses by accuracy and insight.",
        "Use lines like `A: 1` where 1 is best.",
        "",
    ]
    for label, text in sorted(anon_responses.items()):
        parts.extend([f"Response {label}:", text, ""])
    return "\n".join(parts).strip() + "\n"


def _parse_rankings(text: str, allowed_labels: set[str] | None = None) -> list[tuple[str, int]]:
    rankings: list[tuple[str, int]] = []
    seen: set[str] = set()
    for match in re.finditer(r"\b(?:Response\s+)?([A-Z])\s*[:.)-]\s*(\d+)\b", text):
        label = match.group(1)
        if allowed_labels is not None and label not in allowed_labels:
            continue
        if label in seen:
            continue
        seen.add(label)
        rankings.append((label, int(match.group(2))))
    return rankings


def _stage2_entry(
    seat_idx: int,
    result: dict[str, Any],
    allowed_labels: set[str],
) -> tuple[dict[str, Any], list[tuple[str, int]]]:
    parsed = []
    if result.get("status") == "ok":
        parsed = _parse_rankings(result.get("stdout", ""), allowed_labels)
    entry = {
        "seat_idx": seat_idx,
        "status": result.get("status", "error"),
        "rankings": [{"anon": label, "rank": rank} for label, rank in parsed],
        "raw": result.get("stdout", ""),
        "ms": result.get("ms"),
        "error": result.get("error"),
    }
    return entry, parsed


def _stage3_prompt(
    question: str,
    responses: dict[int, str],
    leaderboard: list[dict[str, Any]],
) -> str:
    parts = [
        "Question:",
        question,
        "",
        "Valid council responses:",
    ]
    if responses:
        for seat_idx in sorted(responses):
            parts.extend([f"Seat {seat_idx}:", responses[seat_idx], ""])
    else:
        parts.extend(["No seat produced a valid response.", ""])
    parts.extend([
        "Average-rank leaderboard:",
        json.dumps(leaderboard, indent=2),
        "",
        "Synthesize one final chairman answer.",
    ])
    return "\n".join(parts).strip() + "\n"


def _created_timestamp() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def run_council(
    spec: dict[str, Any],
    runner: Runner = default_runner,
    emit: Emitter = lambda e: None,
) -> dict[str, Any]:
    """Run the 3-stage council flow and persist progress after each stage."""
    run_id = spec["id"]
    question = spec["question"]
    seats = spec.get("seats") or []
    chairman = spec["chairman"]
    catalog = spec.get("catalog") or {}
    timeout = int(spec.get("timeout_seconds") or DEFAULT_TIMEOUT)
    runs_dir = spec["runs_dir"]

    record: dict[str, Any] = {
        "id": run_id,
        "created": spec.get("created") or _created_timestamp(),
        "question": question,
        "status": "running",
        "seats": seats,
        "chairman": chairman,
        "stage1": [],
        "anon_seed": run_id,
        "anon_map": {},
        "stage2": [],
        "leaderboard": [],
        "stage3": {},
    }
    _write_record(runs_dir, record)

    stage1_jobs = [(idx, seat, question) for idx, seat in enumerate(seats)]
    for idx, _seat, _prompt in stage1_jobs:
        _emit(emit, {"stage": 1, "seat_idx": idx, "status": "started", "field": "response", "value": None})
    stage1_results = _run_parallel(stage1_jobs, catalog, timeout, runner)

    stage1_slots: dict[int, dict[str, Any]] = {}
    for seat_idx in sorted(stage1_results):
        entry = _stage1_entry(seat_idx, stage1_results[seat_idx])
        stage1_slots[seat_idx] = entry
        record["stage1"] = [stage1_slots[idx] for idx in sorted(stage1_slots)]
        _write_record(runs_dir, record)
        _emit(
            emit,
            {
                "stage": 1,
                "seat_idx": seat_idx,
                "status": entry["status"],
                "field": "response",
                "value": entry["response"],
            },
        )

    responses = {
        entry["seat_idx"]: entry["response"]
        for entry in record["stage1"]
        if entry["status"] == "ok"
    }

    if len(responses) >= 2:
        anon = anonymize(responses, seed=run_id)
        record["anon_map"] = anon["anon_map"]
        _write_record(runs_dir, record)

        stage2_jobs = []
        allowed_by_viewer: dict[int, set[str]] = {}
        for seat_idx in sorted(responses):
            shown = anon["for_seat"][seat_idx]
            allowed_by_viewer[seat_idx] = set(shown)
            stage2_jobs.append((seat_idx, seats[seat_idx], _stage2_prompt(question, shown)))
            _emit(
                emit,
                {"stage": 2, "seat_idx": seat_idx, "status": "started", "field": "rankings", "value": None},
            )

        stage2_results = _run_parallel(stage2_jobs, catalog, timeout, runner)
        stage2_slots: dict[int, dict[str, Any]] = {}
        rankings: dict[int, list[tuple[str, int]]] = {}
        for seat_idx in sorted(stage2_results):
            entry, parsed = _stage2_entry(
                seat_idx,
                stage2_results[seat_idx],
                allowed_by_viewer[seat_idx],
            )
            stage2_slots[seat_idx] = entry
            if entry["status"] == "ok":
                rankings[seat_idx] = parsed
            record["stage2"] = [stage2_slots[idx] for idx in sorted(stage2_slots)]
            record["leaderboard"] = aggregate_rankings(rankings, record["anon_map"]) if rankings else []
            _write_record(runs_dir, record)
            _emit(
                emit,
                {
                    "stage": 2,
                    "seat_idx": seat_idx,
                    "status": entry["status"],
                    "field": "rankings",
                    "value": entry["rankings"],
                },
            )
    else:
        record["stage2"] = []
        record["leaderboard"] = []
        _write_record(runs_dir, record)

    prompt = _stage3_prompt(question, responses, record["leaderboard"])
    _emit(emit, {"stage": 3, "seat_idx": None, "status": "started", "field": "response", "value": None})
    _idx, chair_result = _run_seat(-1, chairman, prompt, catalog, timeout, runner)
    stage3 = {
        "status": chair_result.get("status", "error"),
        "response": chair_result.get("stdout", ""),
        "ms": chair_result.get("ms"),
        "error": chair_result.get("error"),
    }
    record["stage3"] = stage3
    record["status"] = "done" if stage3["status"] == "ok" else "error"
    _write_record(runs_dir, record)
    _emit(
        emit,
        {
            "stage": 3,
            "seat_idx": None,
            "status": stage3["status"],
            "field": "response",
            "value": stage3["response"],
        },
    )
    _emit(emit, {"stage": "run", "status": record["status"]})
    return record


def _fake_runner(argv: list[str], stdin: str | None, timeout: int) -> dict[str, Any]:
    prompt = stdin or ""
    if "-p" in argv:
        try:
            prompt = argv[argv.index("-p") + 1]
        except IndexError:
            prompt = ""
    if "Rank the anonymous peer responses" in prompt:
        stdout = "A: 1\nB: 2\n"
    elif "Synthesize one final chairman answer" in prompt:
        stdout = "Final fake chairman answer.\n"
    else:
        stdout = "Fake council response.\n"
    return {
        "status": "ok",
        "exit_code": 0,
        "stdout": stdout,
        "stderr": "",
        "error": None,
        "ms": 1,
    }


def main() -> int:
    try:
        spec = json.load(sys.stdin)
        runner = _fake_runner if os.environ.get("COUNCIL_FAKE") == "1" else default_runner

        def emit(event: dict[str, Any]) -> None:
            print(json.dumps(event, separators=(",", ":")), flush=True)

        run_council(spec, runner=runner, emit=emit)
    except Exception as exc:
        print(f"council_run error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
