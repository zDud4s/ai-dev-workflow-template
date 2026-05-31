"""Tests for the pipeline fanout subprocess helper."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


HELPER_PATH = (
    Path(__file__).resolve().parent.parent
    / ".ai"
    / "dashboard"
    / "scripts"
    / "pipeline_fanout.py"
)


def _invoke_helper(spec: dict) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(HELPER_PATH)],
        input=json.dumps(spec),
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_fanout_runs_all_and_blocks() -> None:
    spec = {
        "max_parallel": 3,
        "nodes": [
            {"id": "one", "cmd": [sys.executable, "-c", "print('hello-1')"]},
            {"id": "two", "cmd": [sys.executable, "-c", "print('hello-2')"]},
            {"id": "three", "cmd": [sys.executable, "-c", "print('hello-3')"]},
        ],
    }

    completed = _invoke_helper(spec)

    assert completed.returncode == 0, completed.stderr
    results = json.loads(completed.stdout)
    assert len(results) == 3
    assert [result["id"] for result in results] == ["one", "two", "three"]
    assert all(result["status"] == "ok" for result in results)
    assert all(result["stdout"] for result in results)


def test_fanout_isolates_node_failure() -> None:
    spec = {
        "max_parallel": 3,
        "nodes": [
            {"id": "first", "cmd": [sys.executable, "-c", "print('ok')"]},
            {"id": "bad", "cmd": [sys.executable, "-c", "import sys; sys.exit(2)"]},
            {"id": "last", "cmd": [sys.executable, "-c", "print('done')"]},
        ],
    }

    completed = _invoke_helper(spec)

    assert completed.returncode == 0, completed.stderr
    results = json.loads(completed.stdout)
    assert len(results) == 3
    by_id = {result["id"]: result for result in results}
    assert by_id["first"]["status"] == "ok"
    assert by_id["last"]["status"] == "ok"
    assert by_id["bad"]["status"] == "error"
    assert by_id["bad"]["exit_code"] == 2


def test_fanout_passes_stdin_as_utf8() -> None:
    """Regression: `subprocess.run(text=True)` on Windows defaults stdin encoding
    to the locale codepage (cp1252), which silently breaks non-ASCII prompts
    that Codex expects in strict UTF-8 — the helper must pin encoding='utf-8'.

    The test child reads/writes raw bytes via sys.stdin.buffer/sys.stdout.buffer
    so the encoding boundary under test is exactly the one in the helper.
    """
    payload = "Verdict: REJECT — emoji \U0001F600 czesc żół"
    spec = {
        "nodes": [
            {
                "id": "echo",
                "cmd": [
                    sys.executable,
                    "-c",
                    "import sys; data = sys.stdin.buffer.read(); sys.stdout.buffer.write(data); sys.stdout.buffer.flush()",
                ],
                "stdin": payload,
            }
        ]
    }

    completed = _invoke_helper(spec)

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)[0]
    assert result["status"] == "ok", result
    assert result["exit_code"] == 0
    assert result["stdout"] == payload, (
        f"UTF-8 payload corrupted in stdin round-trip: {result['stdout']!r} != {payload!r}"
    )


def test_duplicate_node_id_rejected() -> None:
    spec = {
        "nodes": [
            {"id": "a", "cmd": [sys.executable, "-c", "print('1')"]},
            {"id": "a", "cmd": [sys.executable, "-c", "print('2')"]},
        ],
    }

    completed = _invoke_helper(spec)

    assert completed.returncode == 1
    assert "duplicate id" in completed.stderr
