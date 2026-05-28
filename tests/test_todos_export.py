from __future__ import annotations

import datetime
import json
import os
from pathlib import Path

import todos_parser


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _append_rows(repo: Path, rows: list[dict]) -> None:
    path = repo / ".ai" / "ledgers" / "todos.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as f:
        for row in rows:
            f.write(json.dumps(row))
            f.write("\n")


def _todo(
    todo_id: str,
    title: str,
    status: str = "open",
    tags: list[str] | None = None,
    resolution: dict | None = None,
    updated_at: str = "2026-05-26T00:00:00Z",
) -> dict:
    return {
        "id": todo_id,
        "title": title,
        "tags": tags or [],
        "source": "memory.md:1",
        "source_ref": ".ai/memory.md#L1",
        "status": status,
        "created_at": "2026-05-26T00:00:00Z",
        "updated_at": updated_at,
        "captured_by": "maintenance",
        "dedup_hash": todo_id,
        "resolution": resolution,
        "rejected_hashes": [],
    }


def test_regen_markdown_groups_by_tag_and_status(tmp_path):
    repo = tmp_path / "repo"
    recent = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
    old = recent - datetime.timedelta(days=45)
    _append_rows(
        repo,
        [
            _todo("td_2026-05-26_002", "A11y touch target", tags=["a11y"]),
            _todo("td_2026-05-26_001", "Parser fixture coverage", tags=["tests"]),
            _todo(
                "td_2026-05-26_003",
                "Cache cleanup",
                status="resolved-suggested",
                resolution={
                    "by": "commit-match",
                    "evidence": "abcdef123456",
                    "at": recent.isoformat().replace("+00:00", "Z"),
                },
            ),
            _todo(
                "td_2026-05-26_004",
                "Recent decision",
                status="resolved",
                resolution={
                    "by": "manual",
                    "evidence": "manual",
                    "at": recent.isoformat().replace("+00:00", "Z"),
                },
            ),
            _todo(
                "td_2026-04-01_001",
                "Old resolution",
                status="resolved",
                resolution={
                    "by": "manual",
                    "evidence": "manual",
                    "at": old.isoformat().replace("+00:00", "Z"),
                },
            ),
        ],
    )

    result = todos_parser.regen_markdown(repo)

    text = (repo / ".ai" / "TODO.md").read_text(encoding="utf-8")
    assert result["ok"] is True
    assert text.index("### [a11y]") < text.index("### [tests]")
    assert "- [ ] td_2026-05-26_001 - Parser fixture coverage (`memory.md:1`)" in text
    assert "- [?] td_2026-05-26_003 - Cache cleanup (commit `abcdef1`)" in text
    assert "- [x] td_2026-05-26_004 - Recent decision" in text
    assert "Old resolution" not in text


def test_regen_uses_lockfile(tmp_path):
    repo = tmp_path / "repo"
    _append_rows(repo, [_todo("td_2026-05-26_001", "Write export")])

    result = todos_parser.regen_markdown(repo)

    assert result["ok"] is True
    assert (repo / ".ai" / "TODO.md").exists()
    assert not (repo / ".ai" / ".todos.lock").exists()


def test_regen_failure_leaves_jsonl_intact_and_banner_set(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _append_rows(repo, [_todo("td_2026-05-26_001", "Keep source of truth")])
    _write(repo / ".ai" / "TODO.md", "old export\n")
    before = (repo / ".ai" / "ledgers" / "todos.jsonl").read_text(encoding="utf-8")

    monkeypatch.setattr(todos_parser, "_acquire_lock", lambda _path: None)

    result = todos_parser.regen_markdown(repo)

    assert result == {"ok": False, "banner": "TODO.md export stale"}
    assert (repo / ".ai" / "ledgers" / "todos.jsonl").read_text(encoding="utf-8") == before
    assert (repo / ".ai" / "TODO.md").read_text(encoding="utf-8") == "old export\n"
