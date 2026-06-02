"""Structural guards on the pipeline editor JS after the input/sink rework.

These are text-level assertions (the SVG editor has no JS unit harness); they
lock in that the old output-mode model is gone and the new kind model is wired.
"""
from __future__ import annotations
import pathlib

JS = (pathlib.Path(__file__).resolve().parent.parent
      / ".ai" / "dashboard" / "app" / "pipelines.js").read_text(encoding="utf-8")


def test_no_output_block_in_serializer() -> None:
    assert 'lines.push("output:")' not in JS
    assert "out.mode" not in JS


def test_serializer_emits_kind() -> None:
    assert '"    kind: "' in JS or "kind: " in JS


def test_no_output_mode_select_wiring() -> None:
    assert "pipeline-output-mode" not in JS
    assert "pipeline-output-node-select" not in JS


def test_sink_kinds_defined() -> None:
    assert "SINK_KINDS" in JS


def test_no_editor_output_state_references() -> None:
    # Catches the deleteNodeByRef / updateNodeReferences sites mechanically.
    assert "_editorState.output" not in JS
