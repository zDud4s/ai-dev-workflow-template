from __future__ import annotations

import json
import subprocess
from pathlib import Path

import todos_parser


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _run(repo: Path, args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=repo, check=True, capture_output=True, text=True)


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(repo, ["git", "init"])
    _run(repo, ["git", "config", "user.email", "todo-tests@example.invalid"])
    _run(repo, ["git", "config", "user.name", "Todo Tests"])
    return repo


def _commit_all(repo: Path, message: str) -> str:
    _run(repo, ["git", "add", "."])
    _run(repo, ["git", "commit", "-m", message])
    return _run(repo, ["git", "rev-parse", "HEAD"]).stdout.strip()


def _latest(repo: Path) -> dict[str, dict]:
    rows = todos_parser._load_jsonl(repo / ".ai" / "todos.jsonl")
    return {row["id"]: row for row in rows}


def test_parses_followup_entries_from_memory(tmp_path):
    repo = _init_repo(tmp_path)
    _write(
        repo / ".ai" / "memory.md",
        "# Project Memory\n\n"
        "- 2026-05-25 [tests] ordinary memory entry\n"
        "- 2026-05-26 [followup] Sweep parser fixtures #tests\n",
    )
    base = _commit_all(repo, "base")

    result = todos_parser.scan_and_append(repo, last_sha=base)

    assert result["added"] == 1
    latest = list(_latest(repo).values())
    assert latest[0]["title"] == "Sweep parser fixtures #tests"
    assert latest[0]["source"] == "memory.md:4"
    assert latest[0]["tags"] == ["tests"]


def test_parses_follow_ups_block_from_handoff(tmp_path):
    repo = _init_repo(tmp_path)
    _write(repo / ".ai" / "memory.md", "# Project Memory\n")
    _write(
        repo / ".ai" / "plans" / "2026-05-25-old.md",
        "## Handoff\n\n## Follow-ups\n- Ignore older plan\n",
    )
    _write(
        repo / ".ai" / "plans" / "2026-05-26-new.md",
        "## Handoff\nFiles changed:\n\n"
        "## Follow-ups\n"
        "- [tests] Add export coverage\n"
        "- none\n"
        "\n## Validation\n",
    )
    base = _commit_all(repo, "base")

    result = todos_parser.scan_and_append(repo, last_sha=base)

    assert result["added"] == 1
    todo = next(iter(_latest(repo).values()))
    assert todo["title"] == "[tests] Add export coverage"
    assert todo["source"].startswith("plans/2026-05-26-new.md:")
    assert todo["tags"] == ["tests"]


def test_parses_todo_fixme_xxx_from_diff(tmp_path):
    repo = _init_repo(tmp_path)
    _write(repo / "README.md", "base\n")
    base = _commit_all(repo, "base")
    _write(
        repo / "src" / "app.py",
        "# TODO: wire parser\n"
        "# FIXME clean export\n"
        "# XXX revisit resolution\n",
    )
    _write(repo / "node_modules" / "pkg.js", "// TODO: ignore vendored code\n")
    _commit_all(repo, "add markers")

    result = todos_parser.scan_and_append(repo, last_sha=base)

    assert result["added"] == 3
    titles = {row["title"] for row in _latest(repo).values()}
    assert titles == {"wire parser", "clean export", "revisit resolution"}
    assert "ignore vendored code" not in titles


def test_dedup_hash_is_idempotent(tmp_path):
    repo = _init_repo(tmp_path)
    _write(repo / ".ai" / "memory.md", "- 2026-05-26 [followup] Normalize this title!\n")
    base = _commit_all(repo, "base")

    first = todos_parser.scan_and_append(repo, last_sha=base)
    second = todos_parser.scan_and_append(repo, last_sha=base)

    rows = todos_parser._load_jsonl(repo / ".ai" / "todos.jsonl")
    assert first["added"] == 1
    assert second["updated"] == 1
    assert len({row["id"] for row in rows}) == 1
    assert todos_parser._dedup_hash("x", "Normalize this title!") == todos_parser._dedup_hash(
        "x", "normalize this title"
    )


def test_one_source_failure_does_not_abort_others(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    _write(repo / ".ai" / "memory.md", "- 2026-05-26 [followup] Should be skipped by failure\n")
    _write(
        repo / ".ai" / "plans" / "2026-05-26-plan.md",
        "## Handoff\n\n## Follow-ups\n- Capture from surviving source\n",
    )
    base = _commit_all(repo, "base")

    def _raise(_repo):
        raise RuntimeError("memory exploded")

    monkeypatch.setattr(todos_parser, "_capture_memory_followups", _raise)

    result = todos_parser.scan_and_append(repo, last_sha=base)

    assert result["ok"] is False
    assert result["added"] == 1
    todo = next(iter(_latest(repo).values()))
    assert todo["title"] == "Capture from surviving source"
    assert "memory exploded" in (repo / ".ai" / "dashboard" / ".todos-parser.log").read_text(
        encoding="utf-8"
    )
