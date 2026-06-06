from __future__ import annotations
import json, shutil, subprocess
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parent.parent
RUNNER = Path(__file__).resolve().parent / "_canvas_bus_runner.js"
NODE = shutil.which("node")
requires_node = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def call(op, *args):
    proc = subprocess.run(
        [NODE, str(RUNNER)],
        input=json.dumps({"op": op, "args": list(args)}),
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)["result"]


@requires_node
def test_normalize_strips_pty_prefix():
    assert call("normalizeKey", "pty:xyz") == "xyz"
    assert call("normalizeKey", "job:a") == "job:a"
    assert call("normalizeKey", "ide:123") == "ide:123"
    assert call("normalizeKey", "abc") == "abc"


@requires_node
def test_queue_buffers_until_ready_then_flushes_in_order():
    assert call("queueFlush", {"pushed": ["open:a", "open:b"], "readyAfter": 2}) == [
        "open:a",
        "open:b",
    ]


@requires_node
def test_queue_after_ready_passes_through_in_order():
    assert call("queueFlush", {"pushed": ["a", "b", "c"], "readyAfter": 1}) == [
        "a",
        "b",
        "c",
    ]


@requires_node
def test_is_stale_after_three_intervals():
    assert call("isStale", {"lastSeen": 0}, 31000, 10000) is True
    assert call("isStale", {"lastSeen": 0}, 20000, 10000) is False
