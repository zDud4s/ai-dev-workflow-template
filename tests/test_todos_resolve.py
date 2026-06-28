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


def _empty_commit(repo: Path, message: str) -> str:
    _run(repo, ["git", "commit", "--allow-empty", "-m", message])
    return _run(repo, ["git", "rev-parse", "HEAD"]).stdout.strip()


def _write_jsonl(repo: Path, rows: list[dict]) -> None:
    path = repo / ".ai" / "local" / "ledgers" / "todos.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for row in rows:
            f.write(json.dumps(row))
            f.write("\n")


def _todo(title: str, rejected_hashes: list[str] | None = None) -> dict:
    return {
        "id": "td_2026-05-26_001",
        "title": title,
        "tags": ["tests"],
        "source": "memory.md:1",
        "source_ref": ".ai/memory.md#L1",
        "status": "open",
        "created_at": "2026-05-26T00:00:00Z",
        "updated_at": "2026-05-26T00:00:00Z",
        "captured_by": "maintenance",
        "dedup_hash": "hash",
        "resolution": None,
        "rejected_hashes": rejected_hashes or [],
    }


def _latest(repo: Path) -> dict:
    rows = todos_parser._load_jsonl(repo / ".ai" / "local" / "ledgers" / "todos.jsonl")
    return rows[-1]


def test_commit_match_requires_three_distinct_keywords_with_one_long(tmp_path):
    repo = _init_repo(tmp_path)
    _write(repo / "README.md", "base\n")
    _write_jsonl(repo, [_todo("Refactor dashboard cache cleanup pipeline")])
    base = _commit_all(repo, "base")

    _empty_commit(repo, "dashboard cache landed")
    first = todos_parser.auto_resolve(repo, last_sha=base)
    assert first["suggested"] == 0
    assert _latest(repo)["status"] == "open"

    _empty_commit(repo, "dashboard cache cleanup pipeline retired")
    second = todos_parser.auto_resolve(repo, last_sha=base)

    assert second["suggested"] == 1
    latest = _latest(repo)
    assert latest["status"] == "resolved-suggested"
    assert latest["resolution"]["by"] == "commit-match"


def test_decision_match_uses_new_entries_only(tmp_path):
    repo = _init_repo(tmp_path)
    _write(
        repo / ".ai" / "decisions.md",
        "# Decisions\n\n- Retire obsolete endpoint cleanup\n",
    )
    _write_jsonl(repo, [_todo("Retire obsolete endpoint cleanup")])
    base = _commit_all(repo, "base")

    _write(
        repo / ".ai" / "decisions.md",
        "# Decisions\n\n- Retire obsolete endpoint cleanup\n- unrelated note\n",
    )
    _commit_all(repo, "change notes")
    first = todos_parser.auto_resolve(repo, last_sha=base)
    assert first["suggested"] == 0

    _write(
        repo / ".ai" / "decisions.md",
        "# Decisions\n\n"
        "- Retire obsolete endpoint cleanup\n"
        "- unrelated note\n"
        "- obsolete endpoint cleanup retired in dashboard pipeline\n",
    )
    _commit_all(repo, "more notes")
    second = todos_parser.auto_resolve(repo, last_sha=base)

    assert second["suggested"] == 1
    latest = _latest(repo)
    assert latest["resolution"]["by"] == "decision-match"
    assert latest["resolution"]["evidence"].startswith("decisions.md:")


def test_rejected_hashes_block_re_suggestion(tmp_path):
    repo = _init_repo(tmp_path)
    _write(repo / "README.md", "base\n")
    base = _commit_all(repo, "base")
    sha = _empty_commit(repo, "dashboard cache cleanup fixed")
    _write_jsonl(repo, [_todo("Dashboard cache cleanup", rejected_hashes=[sha])])

    result = todos_parser.auto_resolve(repo, last_sha=base)

    assert result["suggested"] == 0
    assert _latest(repo)["status"] == "open"
