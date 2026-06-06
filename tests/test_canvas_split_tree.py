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

@requires_node
def test_remove_only_leaf_returns_null():
    assert call("remove", {"leaf": "a"}, "a") is None

@requires_node
def test_remove_collapses_parent_to_sibling():
    t = call("splitLeaf", {"leaf": "a"}, "a", "b", "right")
    assert call("remove", t, "b") == {"leaf": "a"}

@requires_node
def test_remove_deep_keeps_other_branch():
    t = call("splitLeaf", {"leaf": "a"}, "a", "b", "right")
    t = call("splitLeaf", t, "b", "c", "bottom")
    assert call("remove", t, "c") == {"split": "row", "ratios": [0.5, 0.5],
        "children": [{"leaf": "a"}, {"leaf": "b"}]}

@requires_node
def test_keys_in_order():
    t = call("splitLeaf", {"leaf": "a"}, "a", "b", "right")
    assert call("keys", t) == ["a", "b"]

@requires_node
def test_resize_clamps_to_min_ratio():
    t = call("splitLeaf", {"leaf": "a"}, "a", "b", "right")
    out = call("resize", t, [], 0.6)        # would push child0 to 1.1 -> clamp
    assert out["ratios"][0] <= 0.9 + 1e-9 and out["ratios"][1] >= 0.1 - 1e-9

@requires_node
def test_deserialize_rejects_malformed():
    assert call("deserialize", {"split": "row"}) is None     # missing children
    assert call("deserialize", {"leaf": "a"}) == {"leaf": "a"}

@requires_node
def test_resize_invalid_path_is_noop():
    t = {"leaf": "a"}
    assert call("resize", t, [], 0.3) == {"leaf": "a"}   # leaf root -> unchanged

@requires_node
def test_deserialize_rejects_nonpositive_ratios():
    bad = {"split": "row", "ratios": [-0.5, 1.5], "children": [{"leaf": "a"}, {"leaf": "b"}]}
    assert call("deserialize", bad) is None
    bad2 = {"split": "row", "ratios": ["x", 0.5], "children": [{"leaf": "a"}, {"leaf": "b"}]}
    assert call("deserialize", bad2) is None
