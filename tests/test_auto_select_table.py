"""Decision-table tests (spec section: Phase 1 - static decision table)."""
from __future__ import annotations

import pytest

from conftest import REPO_ROOT

TABLE = REPO_ROOT / ".ai" / "workflow" / "auto-models.md"

PHASES = ("execute", "review", "rescue")
SIZES = ("small", "medium", "large")
RISKS = ("low", "elevated")
BUDGETS = ("low", "medium", "high")
TOOLS = ("codex", "claude")
EFFORTS = ("low", "medium", "high", "xhigh", "max", "n/a")  # `max` is claude-only


@pytest.fixture(scope="module")
def table_rows():
    """Parse the pipe-table rows into list of dicts."""
    text = TABLE.read_text(encoding="utf-8")
    lines = [ln for ln in text.splitlines() if ln.startswith("|")]
    assert len(lines) >= 3, "table must have header, separator, and at least one row"
    header = [c.strip() for c in lines[0].strip("|").split("|")]
    expected = ["phase", "size", "risk", "budget", "tool", "model", "effort"]
    assert header == expected, f"unexpected header: {header}"
    rows = []
    for ln in lines[2:]:
        cells = [c.strip() for c in ln.strip("|").split("|")]
        if len(cells) != len(expected):
            continue
        rows.append(dict(zip(expected, cells)))
    return rows


def test_table_file_exists():
    assert TABLE.is_file(), ".ai/workflow/auto-models.md missing"


def test_table_has_rows(table_rows):
    assert len(table_rows) > 0, "table must contain at least one row"


def test_every_phase_covered(table_rows):
    phases_in_table = {r["phase"] for r in table_rows}
    for phase in PHASES:
        assert phase in phases_in_table, f"phase {phase!r} has no rows"


def test_row_values_are_valid(table_rows):
    # claude `--effort` accepts {low, medium, high, xhigh, max}; codex
    # `model_reasoning_effort` accepts {low, medium, high, xhigh} (no `max`).
    # `n/a` means "use the tool's default" (planner omits the field).
    CLAUDE_EFFORTS = {"low", "medium", "high", "xhigh", "max", "n/a"}
    CODEX_EFFORTS  = {"low", "medium", "high", "xhigh", "n/a"}
    for r in table_rows:
        assert r["phase"] in PHASES, f"unknown phase: {r['phase']}"
        assert r["size"] in SIZES + ("*",), f"unknown size: {r['size']}"
        assert r["risk"] in RISKS + ("*",), f"unknown risk: {r['risk']}"
        assert r["budget"] in BUDGETS + ("*",), f"unknown budget: {r['budget']}"
        assert r["tool"] in TOOLS, f"unknown tool: {r['tool']}"
        assert r["effort"] in EFFORTS, f"unknown effort: {r['effort']}"
        if r["tool"] == "claude":
            assert r["effort"] in CLAUDE_EFFORTS, (
                f"claude row has invalid effort {r['effort']}; allowed: {sorted(CLAUDE_EFFORTS)}"
            )
        if r["tool"] == "codex":
            assert r["effort"] in CODEX_EFFORTS, (
                f"codex row has invalid effort {r['effort']}; `max` is claude-only. allowed: {sorted(CODEX_EFFORTS)}"
            )


def test_rescue_always_high_quality(table_rows):
    """Rescue rows must use claude-opus-4-7 (spec Phase 1 static decision table)."""
    rescue_rows = [r for r in table_rows if r["phase"] == "rescue"]
    assert rescue_rows, "rescue must have at least one row"
    for r in rescue_rows:
        assert r["model"] == "claude-opus-4-7", (
            f"rescue must use claude-opus-4-7, got {r['model']}"
        )
