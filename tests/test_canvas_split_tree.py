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

@requires_node
def test_split_right_makes_row():
    t = call("insertFirst", None, "a")
    assert call("splitLeaf", t, "a", "b", "right") == {
        "split": "row", "ratios": [0.5, 0.5],
        "children": [{"leaf": "a"}, {"leaf": "b"}]}

@requires_node
def test_split_left_puts_new_first():
    t = call("insertFirst", None, "a")
    assert call("splitLeaf", t, "a", "b", "left")["children"] == [{"leaf": "b"}, {"leaf": "a"}]

@requires_node
def test_split_bottom_makes_col():
    t = call("insertFirst", None, "a")
    out = call("splitLeaf", t, "a", "b", "bottom")
    assert out["split"] == "col" and out["children"] == [{"leaf": "a"}, {"leaf": "b"}]

@requires_node
def test_split_nested_leaf_only_touches_target():
    t = call("splitLeaf", call("insertFirst", None, "a"), "a", "b", "right")
    out = call("splitLeaf", t, "b", "c", "bottom")
    assert out["children"][0] == {"leaf": "a"}
    assert out["children"][1] == {"split": "col", "ratios": [0.5, 0.5],
                                  "children": [{"leaf": "b"}, {"leaf": "c"}]}
