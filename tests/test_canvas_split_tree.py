from __future__ import annotations
import json, shutil, subprocess
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parent.parent
RUNNER = Path(__file__).resolve().parent / "_split_tree_runner.js"
NODE = shutil.which("node")
requires_node = pytest.mark.skipif(NODE is None, reason="node not on PATH")

def call(op, *args):
    proc = subprocess.run([NODE, str(RUNNER)], input=json.dumps({"op": op, "args": list(args)}),
                          capture_output=True, text=True, timeout=15)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)["result"]

@requires_node
def test_insert_first_makes_single_leaf():
    assert call("insertFirst", None, "job:a") == {"leaf": "job:a"}
