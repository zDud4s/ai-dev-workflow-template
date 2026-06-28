from __future__ import annotations

import builtins
import datetime as dt
import importlib.util
import json
import os
import sys
import time
from pathlib import Path

import pytest

from server.improver import _transcript_policy as policy
from server.improver import purge_stale_transcripts as purge_script
import server.improver as _im  # _periodic_transcript_purge_loop resolves these names here (follows-the-move)


REPO_ROOT = Path(__file__).resolve().parent.parent
SERVE_PATH = REPO_ROOT / ".ai" / "dashboard" / "serve.py"


@pytest.fixture(scope="module")
def serve_module():
    spec = importlib.util.spec_from_file_location("dashboard_serve_purge", SERVE_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["dashboard_serve_purge"] = mod
    spec.loader.exec_module(mod)
    return mod


def _iso(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc).isoformat()


def _write_transcript(path: Path, *, skill: str = "demo", ts: float | None = None, assistant: bool = True, improver: bool = True) -> None:
    ts = ts if ts is not None else time.time()
    content = "ordinary user request"
    if improver:
        content = f"OUTPUT FORMAT (STRICT)\nSKILL: {skill}\nReturn JSON only."
    rows = [
        {"type": "user", "timestamp": _iso(ts), "message": {"content": content}},
    ]
    if assistant:
        rows.append({"type": "assistant", "timestamp": _iso(ts + 1), "message": {"content": "{}"}})
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def _age_file(path: Path, days: int) -> None:
    old = time.time() - days * 86400
    os.utime(path, (old, old))


def _ledger_row(skill: str, ts: float, status: str) -> dict:
    return {"ts": _iso(ts), "skill": skill, "status": status}


def test_classifies_orphan(tmp_path):
    path = tmp_path / "orphan.jsonl"
    _write_transcript(path, assistant=False)

    assert policy.classify_transcript(path, [], time.time()) == "orphan"


def test_classifies_resolved_old(tmp_path):
    ts = time.time() - 8 * 86400
    path = tmp_path / "resolved.jsonl"
    _write_transcript(path, ts=ts, assistant=True)
    _age_file(path, 8)

    rows = [_ledger_row("demo", ts, "applied")]
    assert policy.classify_transcript(path, rows, time.time()) == "resolved"


def test_classifies_rolled_back_old(tmp_path):
    ts = time.time() - 8 * 86400
    path = tmp_path / "rolled-back.jsonl"
    _write_transcript(path, ts=ts, assistant=True)
    _age_file(path, 8)

    rows = [_ledger_row("demo", ts, "rolled_back")]
    assert policy.classify_transcript(path, rows, time.time()) == "resolved"


def test_classifies_unmatched_pre_audit(tmp_path):
    ts = time.time() - 8 * 86400
    path = tmp_path / "unmatched.jsonl"
    _write_transcript(path, ts=ts, assistant=True)
    _age_file(path, 8)

    rows = [_ledger_row("demo", ts + 7200, "applied")]
    assert policy.classify_transcript(path, rows, time.time()) == "unmatched_pre_audit"


def test_keeps_pending_recent(tmp_path):
    ts = time.time() - 86400
    path = tmp_path / "recent.jsonl"
    _write_transcript(path, ts=ts, assistant=True)
    _age_file(path, 1)

    rows = [_ledger_row("demo", ts, "pending")]
    assert policy.classify_transcript(path, rows, time.time()) == "keep"


def test_keeps_pending_old(tmp_path):
    ts = time.time() - 8 * 86400
    path = tmp_path / "pending.jsonl"
    _write_transcript(path, ts=ts, assistant=True)
    _age_file(path, 8)

    rows = [_ledger_row("demo", ts, "pending")]
    assert policy.classify_transcript(path, rows, time.time()) == "keep"


def test_keeps_failed_status(tmp_path):
    ts = time.time() - 8 * 86400
    path = tmp_path / "failed.jsonl"
    _write_transcript(path, ts=ts, assistant=True)
    _age_file(path, 8)

    rows = [_ledger_row("demo", ts, "failed")]
    assert policy.classify_transcript(path, rows, time.time()) == "keep"


def test_keeps_non_improver(tmp_path):
    path = tmp_path / "chat.jsonl"
    _write_transcript(path, improver=False, assistant=True)
    _age_file(path, 8)

    assert policy.classify_transcript(path, [], time.time()) == "keep"


def test_dry_run_does_not_unlink(tmp_path):
    path = tmp_path / "orphan.jsonl"
    ledger = tmp_path / "improvements.jsonl"
    ledger.write_text("", encoding="utf-8")
    _write_transcript(path, assistant=False)

    assert purge_script.main(["--project-dir", str(tmp_path), "--ledger", str(ledger)]) == 0
    assert path.exists()

    assert purge_script.main(["--apply", "--project-dir", str(tmp_path), "--ledger", str(ledger)]) == 0
    assert not path.exists()


def test_periodic_sweep_uses_shared_predicate(serve_module, tmp_path, monkeypatch):
    calls = []
    transcript = tmp_path / "one.jsonl"
    _write_transcript(transcript)

    monkeypatch.delenv("AI_WORKFLOW_DISABLE_IMPROVER", raising=False)
    monkeypatch.setattr(_im, "_transcripts_dir_for_cwd", lambda _root: tmp_path)
    monkeypatch.setattr(_im, "load_ledger_rows", lambda _ledger: [])
    monkeypatch.setattr(
        _im,
        "classify_transcript",
        lambda path, rows, now: calls.append((path, rows)) or "keep",
    )

    serve_module._periodic_transcript_purge_loop(run_once=True)

    assert calls == [(transcript, [])]
    assert transcript.exists()


def test_default_project_dir_logs_on_import_error(monkeypatch, capsys):
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "serve":
            raise ImportError("boom")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    result = purge_script._default_project_dir()
    assert result is None
    err = capsys.readouterr().err
    assert "could not import serve helper" in err
    assert "boom" in err


def test_default_project_dir_propagates_non_import_error(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "serve":
            raise RuntimeError("not swallowed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(RuntimeError, match="not swallowed"):
        purge_script._default_project_dir()
