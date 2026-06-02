"""Structural guards on the run-pipeline skill after the input/sink rework."""
from __future__ import annotations
import pathlib

SKILL = (pathlib.Path(__file__).resolve().parent.parent
         / ".claude" / "skills" / "run-pipeline" / "SKILL.md").read_text(encoding="utf-8")


def test_no_output_mode_references() -> None:
    assert "output.mode" not in SKILL


def test_sink_kind_vocabulary_present() -> None:
    assert "sink" in SKILL.lower()
    assert "kind" in SKILL


def test_validator_path_is_scripts() -> None:
    # The import path must point at the real location.
    assert "scripts/pipeline_schema.py" in SKILL
    assert "in .ai/dashboard/pipeline_schema.py" not in SKILL


def test_flow_nodes_skipped_in_dispatch() -> None:
    low = SKILL.lower()
    # Avoid operator-precedence traps: assert the phrase AND the skip intent.
    assert "flow node" in low
    assert "not dispatched" in low or "never dispatched" in low


def test_mirror_is_byte_identical() -> None:
    import pathlib as _p
    root = _p.Path(__file__).resolve().parent.parent
    canonical = root / ".claude" / "skills" / "run-pipeline" / "SKILL.md"
    mirror = root / ".agents" / "skills" / "run-pipeline" / "SKILL.md"
    assert mirror.read_bytes() == canonical.read_bytes()
