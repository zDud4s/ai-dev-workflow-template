"""Tests for the pipeline YAML schema validator."""
from __future__ import annotations

# conftest.py adds .ai/dashboard/scripts/ to sys.path; no local insert needed.
from pipeline_schema import validate, is_linear  # noqa: E402


# ---------------------------------------------------------------------------
# Positive cases
# ---------------------------------------------------------------------------

def test_minimal_linear_pipeline_is_valid() -> None:
    pipeline = {
        "output": {"mode": "passthrough", "node": "b"},
        "nodes": [
            {"id": "a", "agent": "code-explorer"},
            {"id": "b", "agent": "code-architect"},
        ],
    }
    ok, errors = validate(pipeline)
    assert ok, errors
    assert errors == []


def test_dag_with_explicit_depends_on_is_valid() -> None:
    pipeline = {
        "output": {"mode": "synthesize"},
        "nodes": [
            {"id": "s1", "agent": "code-explorer"},
            {"id": "s2", "agent": "code-explorer"},
            {"id": "s3", "agent": "code-architect", "depends_on": ["s1", "s2"]},
        ],
    }
    ok, errors = validate(pipeline)
    assert ok, errors


def test_per_agent_output_mode_is_valid() -> None:
    pipeline = {
        "output": {"mode": "per-agent"},
        "nodes": [{"id": "x", "agent": "code-explorer"}],
    }
    ok, errors = validate(pipeline)
    assert ok, errors


def test_is_linear_returns_true_when_no_depends_on() -> None:
    pipeline = {
        "output": {"mode": "passthrough", "node": "b"},
        "nodes": [{"id": "a", "agent": "x"}, {"id": "b", "agent": "y"}],
    }
    assert is_linear(pipeline) is True


def test_is_linear_returns_false_when_any_depends_on() -> None:
    pipeline = {
        "output": {"mode": "synthesize"},
        "nodes": [
            {"id": "a", "agent": "x"},
            {"id": "b", "agent": "y", "depends_on": ["a"]},
        ],
    }
    assert is_linear(pipeline) is False


# ---------------------------------------------------------------------------
# Negative cases
# ---------------------------------------------------------------------------

def test_missing_output_key_is_invalid() -> None:
    pipeline = {"nodes": [{"id": "a", "agent": "x"}]}
    ok, errors = validate(pipeline)
    assert not ok
    assert any("output" in e for e in errors)


def test_missing_nodes_key_is_invalid() -> None:
    pipeline = {"output": {"mode": "synthesize"}}
    ok, errors = validate(pipeline)
    assert not ok
    assert any("nodes" in e for e in errors)


def test_empty_nodes_is_invalid() -> None:
    pipeline = {"output": {"mode": "synthesize"}, "nodes": []}
    ok, errors = validate(pipeline)
    assert not ok
    assert any("non-empty" in e for e in errors)


def test_duplicate_ids_invalid() -> None:
    pipeline = {
        "output": {"mode": "synthesize"},
        "nodes": [
            {"id": "x", "agent": "a"},
            {"id": "x", "agent": "b"},
        ],
    }
    ok, errors = validate(pipeline)
    assert not ok
    assert any("duplicate" in e.lower() for e in errors)


def test_unknown_depends_on_target_invalid() -> None:
    pipeline = {
        "output": {"mode": "synthesize"},
        "nodes": [
            {"id": "a", "agent": "x"},
            {"id": "b", "agent": "y", "depends_on": ["ghost"]},
        ],
    }
    ok, errors = validate(pipeline)
    assert not ok
    assert any("unknown" in e.lower() and "ghost" in e for e in errors)


def test_cycle_invalid() -> None:
    pipeline = {
        "output": {"mode": "synthesize"},
        "nodes": [
            {"id": "a", "agent": "x", "depends_on": ["c"]},
            {"id": "b", "agent": "y", "depends_on": ["a"]},
            {"id": "c", "agent": "z", "depends_on": ["b"]},
        ],
    }
    ok, errors = validate(pipeline)
    assert not ok
    assert any("cycle" in e.lower() for e in errors)


def test_invalid_output_mode_invalid() -> None:
    pipeline = {
        "output": {"mode": "wrong"},
        "nodes": [{"id": "a", "agent": "x"}],
    }
    ok, errors = validate(pipeline)
    assert not ok
    assert any("output.mode" in e for e in errors)


def test_passthrough_mode_without_node_invalid() -> None:
    pipeline = {
        "output": {"mode": "passthrough"},
        "nodes": [{"id": "a", "agent": "x"}],
    }
    ok, errors = validate(pipeline)
    assert not ok
    assert any("passthrough" in e.lower() for e in errors)


def test_passthrough_mode_with_unknown_node_invalid() -> None:
    pipeline = {
        "output": {"mode": "passthrough", "node": "ghost"},
        "nodes": [{"id": "a", "agent": "x"}],
    }
    ok, errors = validate(pipeline)
    assert not ok
    assert any("passthrough" in e.lower() and "ghost" in e for e in errors)


def test_node_missing_id_invalid() -> None:
    pipeline = {
        "output": {"mode": "synthesize"},
        "nodes": [{"agent": "x"}],
    }
    ok, errors = validate(pipeline)
    assert not ok


def test_node_missing_agent_invalid() -> None:
    pipeline = {
        "output": {"mode": "synthesize"},
        "nodes": [{"id": "a"}],
    }
    ok, errors = validate(pipeline)
    assert not ok
