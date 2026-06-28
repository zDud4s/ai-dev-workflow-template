"""Tests for the pipeline YAML schema validator (explicit input/sink nodes)."""
from __future__ import annotations

# conftest.py adds .ai/scripts/ to sys.path; no local insert needed.
from pipeline_schema import validate  # noqa: E402


def _err(errors: list[str], needle: str) -> bool:
    return any(needle in e for e in errors)


# A canonical valid pipeline reused across tests:
#   input -> a -> out(synthesize)
def _valid() -> dict:
    return {
        "description": "ok",
        "nodes": [
            {"id": "input", "kind": "input"},
            {"id": "a", "agent": "code-explorer", "depends_on": ["input"]},
            {"id": "out", "kind": "synthesize", "depends_on": ["a"]},
        ],
    }


# ---------------------------------------------------------------------------
# Positive cases
# ---------------------------------------------------------------------------

def test_minimal_valid_pipeline() -> None:
    ok, errors = validate(_valid())
    assert ok, errors
    assert errors == []


def test_collect_sink_is_valid() -> None:
    p = _valid()
    p["nodes"][2]["kind"] = "collect"
    ok, errors = validate(p)
    assert ok, errors


def test_passthrough_sink_single_input_is_valid() -> None:
    p = _valid()
    p["nodes"][2]["kind"] = "passthrough"  # exactly one depends_on already
    ok, errors = validate(p)
    assert ok, errors


def test_fan_in_synthesize_is_valid() -> None:
    p = {
        "nodes": [
            {"id": "input", "kind": "input"},
            {"id": "a", "agent": "x", "depends_on": ["input"]},
            {"id": "b", "agent": "y", "depends_on": ["input"]},
            {"id": "out", "kind": "synthesize", "depends_on": ["a", "b"]},
        ],
    }
    ok, errors = validate(p)
    assert ok, errors


# ---------------------------------------------------------------------------
# Structural negatives - flow nodes
# ---------------------------------------------------------------------------

def test_missing_nodes_key_is_invalid() -> None:
    ok, errors = validate({})
    assert not ok
    assert _err(errors, "nodes")


def test_empty_nodes_is_invalid() -> None:
    ok, errors = validate({"nodes": []})
    assert not ok
    assert _err(errors, "non-empty")


def test_no_input_node_is_invalid() -> None:
    p = {
        "nodes": [
            {"id": "a", "agent": "x"},
            {"id": "out", "kind": "synthesize", "depends_on": ["a"]},
        ],
    }
    ok, errors = validate(p)
    assert not ok
    assert _err(errors, "exactly one input node (found 0)")


def test_two_input_nodes_is_invalid() -> None:
    p = _valid()
    p["nodes"].append({"id": "input2", "kind": "input"})
    ok, errors = validate(p)
    assert not ok
    assert _err(errors, "exactly one input node (found 2)")


def test_no_sink_node_is_invalid() -> None:
    p = {
        "nodes": [
            {"id": "input", "kind": "input"},
            {"id": "a", "agent": "x", "depends_on": ["input"]},
        ],
    }
    ok, errors = validate(p)
    assert not ok
    assert _err(errors, "exactly one sink node (found 0)")


def test_two_sink_nodes_is_invalid() -> None:
    p = _valid()
    p["nodes"].append({"id": "out2", "kind": "collect", "depends_on": ["a"]})
    ok, errors = validate(p)
    assert not ok
    assert _err(errors, "exactly one sink node (found 2)")


def test_input_with_depends_on_is_invalid() -> None:
    p = _valid()
    p["nodes"][0]["depends_on"] = ["a"]
    ok, errors = validate(p)
    assert not ok
    assert _err(errors, "must not depend on anything")


def test_input_with_no_downstream_is_invalid() -> None:
    # input is orphaned: a depends on nothing, out depends on a
    p = {
        "nodes": [
            {"id": "input", "kind": "input"},
            {"id": "a", "agent": "x"},
            {"id": "out", "kind": "synthesize", "depends_on": ["a"]},
        ],
    }
    ok, errors = validate(p)
    assert not ok
    assert _err(errors, "has no downstream nodes")


def test_sink_with_no_inputs_is_invalid() -> None:
    p = {
        "nodes": [
            {"id": "input", "kind": "input"},
            {"id": "a", "agent": "x", "depends_on": ["input"]},
            {"id": "out", "kind": "synthesize"},
        ],
    }
    ok, errors = validate(p)
    assert not ok
    assert _err(errors, "has no inputs")


def test_non_terminal_sink_is_invalid() -> None:
    # something depends on the sink
    p = {
        "nodes": [
            {"id": "input", "kind": "input"},
            {"id": "a", "agent": "x", "depends_on": ["input"]},
            {"id": "out", "kind": "synthesize", "depends_on": ["a"]},
            {"id": "b", "agent": "y", "depends_on": ["out"]},
        ],
    }
    ok, errors = validate(p)
    assert not ok
    assert _err(errors, "must be terminal")


def test_passthrough_with_two_inputs_is_invalid() -> None:
    p = {
        "nodes": [
            {"id": "input", "kind": "input"},
            {"id": "a", "agent": "x", "depends_on": ["input"]},
            {"id": "b", "agent": "y", "depends_on": ["input"]},
            {"id": "out", "kind": "passthrough", "depends_on": ["a", "b"]},
        ],
    }
    ok, errors = validate(p)
    assert not ok
    assert _err(errors, "passthrough sink") and _err(errors, "exactly one input")


# ---------------------------------------------------------------------------
# Structural negatives - node shape / kind / depends_on / cycles / orphans
# ---------------------------------------------------------------------------

def test_node_with_both_agent_and_kind_is_invalid() -> None:
    p = _valid()
    p["nodes"][1]["kind"] = "input"  # node 'a' now has agent AND kind
    ok, errors = validate(p)
    assert not ok
    assert _err(errors, "must have either 'agent' or 'kind', not both")


def test_node_with_neither_agent_nor_kind_is_invalid() -> None:
    p = _valid()
    del p["nodes"][1]["agent"]  # node 'a' now has neither
    ok, errors = validate(p)
    assert not ok
    assert _err(errors, "must have either 'agent' or 'kind', not both")


def test_unknown_kind_is_invalid() -> None:
    p = _valid()
    p["nodes"][2]["kind"] = "frobnicate"
    ok, errors = validate(p)
    assert not ok
    assert _err(errors, "unknown kind 'frobnicate'")


def test_duplicate_ids_invalid() -> None:
    p = _valid()
    p["nodes"][1]["id"] = "input"  # collide with the input node id
    ok, errors = validate(p)
    assert not ok
    assert _err(errors, "duplicate")


def test_unknown_depends_on_target_invalid() -> None:
    p = _valid()
    p["nodes"][1]["depends_on"] = ["ghost"]
    ok, errors = validate(p)
    assert not ok
    assert _err(errors, "unknown") and _err(errors, "ghost")


def test_cycle_invalid() -> None:
    p = {
        "nodes": [
            {"id": "input", "kind": "input"},
            {"id": "a", "agent": "x", "depends_on": ["input", "c"]},
            {"id": "b", "agent": "y", "depends_on": ["a"]},
            {"id": "c", "agent": "z", "depends_on": ["b"]},
            {"id": "out", "kind": "synthesize", "depends_on": ["c"]},
        ],
    }
    ok, errors = validate(p)
    assert not ok
    assert _err(errors, "cycle")


def test_orphan_agent_node_invalid() -> None:
    # 'orphan' is reachable from input but never reaches the sink
    p = {
        "nodes": [
            {"id": "input", "kind": "input"},
            {"id": "a", "agent": "x", "depends_on": ["input"]},
            {"id": "orphan", "agent": "z", "depends_on": ["input"]},
            {"id": "out", "kind": "synthesize", "depends_on": ["a"]},
        ],
    }
    ok, errors = validate(p)
    assert not ok
    assert _err(errors, "node 'orphan' is not connected between input and sink")


def test_all_messages_carry_prefix() -> None:
    ok, errors = validate({"nodes": []})
    assert not ok
    assert all(e.startswith("pipeline invalid:") for e in errors)


# ---------------------------------------------------------------------------
# Malformed-input robustness (garbage in -> clean verdict, never an exception)
# ---------------------------------------------------------------------------

def test_non_dict_pipeline_is_invalid() -> None:
    for bad in (None, [], "nope", 42):
        ok, errors = validate(bad)
        assert not ok
        assert errors and all(e.startswith("pipeline invalid:") for e in errors)


def test_non_string_depends_on_member_is_clean_error() -> None:
    p = {
        "nodes": [
            {"id": "input", "kind": "input"},
            {"id": "a", "agent": "x", "depends_on": [{"bad": 1}]},
            {"id": "out", "kind": "synthesize", "depends_on": ["a"]},
        ],
    }
    ok, errors = validate(p)  # must not raise
    assert not ok
    assert all(e.startswith("pipeline invalid:") for e in errors)
