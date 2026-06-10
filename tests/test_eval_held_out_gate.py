from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
EVAL_ROOT = REPO_ROOT / ".ai" / "eval"
sys.path.insert(0, str(EVAL_ROOT))

from harness import gate  # noqa: E402

sys.path.insert(0, str(REPO_ROOT / ".ai" / "dashboard"))
import serve  # noqa: E402


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def _result(task_id: str, success: bool, partition: str = "held-out") -> dict:
    return {
        "arm": "a",
        "task_id": task_id,
        "partition": partition,
        "success": success,
        "tokens_in": 1,
        "tokens_out": 1,
        "duration_ms": 1,
    }


def test_gate_blocks_held_out_regression(tmp_path: Path) -> None:
    results_dir = tmp_path / "results"
    _write_jsonl(results_dir / "baseline.jsonl", [_result("task-x", True)])
    _write_jsonl(
        results_dir / "proposals" / "proposal-1.jsonl",
        [_result("task-x", False)],
    )

    verdict = gate.evaluate_proposal("proposal-1", results_dir=results_dir)

    assert verdict["decision"] == "block"
    assert verdict["baseline"]["passed"] == 1
    assert verdict["candidate"]["passed"] == 0


def test_gate_allows_when_no_regression(tmp_path: Path) -> None:
    results_dir = tmp_path / "results"
    _write_jsonl(
        results_dir / "baseline.jsonl",
        [_result("task-x", True), _result("task-y", False)],
    )
    _write_jsonl(
        results_dir / "proposals" / "proposal-1.jsonl",
        [_result("task-x", True), _result("task-y", True)],
    )

    verdict = gate.evaluate_proposal("proposal-1", results_dir=results_dir)

    assert verdict["decision"] == "allow"
    assert verdict["reason"] == "held-out: no regression"
    assert verdict["candidate"]["passed"] >= verdict["baseline"]["passed"]


def test_gate_permissive_without_results(tmp_path: Path) -> None:
    results_dir = tmp_path / "results"

    no_baseline = gate.evaluate_proposal("proposal-1", results_dir=results_dir)

    _write_jsonl(results_dir / "baseline.jsonl", [_result("task-x", True)])
    no_candidate = gate.evaluate_proposal("proposal-1", results_dir=results_dir)

    assert no_baseline["decision"] == "allow"
    assert "not evaluated" in no_baseline["reason"]
    assert no_candidate["decision"] == "allow"
    assert "not evaluated" in no_candidate["reason"]


def test_gate_failsafe_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class BrokenGate:
        @staticmethod
        def evaluate_proposal(proposal_id: str) -> dict:
            raise RuntimeError(f"boom {proposal_id}")

    monkeypatch.setattr(serve.importlib, "import_module", lambda name: BrokenGate)

    verdict = serve._check_held_out_gate("proposal-1")

    assert verdict["decision"] == "allow"
    assert "gate error: boom proposal-1" == verdict["reason"]


def test_proposal_accept_blocked_on_regression_returns_409_and_no_apply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proposals_dir = tmp_path / ".ai" / "dashboard" / "proposals" / "skills"
    skill_path = tmp_path / "skills" / "foo" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text("old content\n", encoding="utf-8")
    proposals_dir.mkdir(parents=True)
    proposal_id = "proposal-1"
    proposal = {
        "id": proposal_id,
        "skill": "foo",
        "skill_path": "skills/foo/SKILL.md",
        "ts": "2026-06-09T00:00:00+00:00",
        "change_summary": "change",
        "rationale": "test",
        "diff_lines": 1,
        "status": "pending",
        "kind": "improve",
    }
    (proposals_dir / f"{proposal_id}.json").write_text(
        json.dumps(proposal),
        encoding="utf-8",
    )
    (proposals_dir / f"{proposal_id}.new.md").write_text(
        "new content\n",
        encoding="utf-8",
    )
    results_dir = tmp_path / ".ai" / "eval" / "results"
    _write_jsonl(results_dir / "baseline.jsonl", [_result("task-x", True)])
    _write_jsonl(
        results_dir / "proposals" / f"{proposal_id}.jsonl",
        [_result("task-x", False)],
    )
    verdict = gate.evaluate_proposal(proposal_id, results_dir=results_dir)

    monkeypatch.setattr(serve, "ROOT", tmp_path)
    monkeypatch.setattr(serve, "SKILL_PROPOSALS_DIR", proposals_dir)
    monkeypatch.setattr(serve, "_check_held_out_gate", lambda pid: verdict)
    monkeypatch.setattr(
        serve,
        "_apply_improvement",
        lambda *args, **kwargs: pytest.fail("blocked proposal was applied"),
    )

    captured: dict[str, object] = {}

    class FakeHandler:
        def _json(self, status: int, payload: dict) -> None:
            captured["status"] = status
            captured["payload"] = payload

    serve.Handler._handle_proposal_decision(FakeHandler(), proposal_id, "accept")

    saved = json.loads((proposals_dir / f"{proposal_id}.json").read_text(encoding="utf-8"))
    assert captured["status"] == 409
    assert captured["payload"] == {
        "error": "proposal regresses the held-out set",
        "held_out": verdict,
    }
    assert saved["held_out"] == verdict
    assert skill_path.read_text(encoding="utf-8") == "old content\n"
