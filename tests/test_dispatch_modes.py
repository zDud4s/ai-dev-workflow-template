"""Tests for dispatch mode documentation: agent (in-process), dispatcher (subprocess), cache-stable layout."""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (REPO_ROOT / rel).read_text(encoding="utf-8")


def test_agent_mode_uses_task_tool() -> None:
    """Agent mode section mentions Task tool + in-process; no timeout/temp-file."""
    text = _read(".ai/workflow/dispatch.md")
    agent_heading = "### Mode: agent (in-process)"
    dispatcher_heading = "### Mode: dispatcher (subprocess)"
    assert agent_heading in text, "dispatch.md missing agent-mode heading"
    assert dispatcher_heading in text, "dispatch.md missing dispatcher-mode heading"

    agent_start = text.index(agent_heading)
    agent_end = text.index(dispatcher_heading)
    agent_section = text[agent_start:agent_end]

    assert "Task tool" in agent_section, "agent section must mention Task tool"
    assert "in-process" in agent_section, "agent section must mention in-process"
    # Agent mode runs no subprocess, so there must be no actual `timeout N` wrapper
    # command. The section MAY mention the word "timeout" to explicitly say
    # "no timeout wrapper" — we check for a real wrapper invocation instead.
    import re as _re
    assert not _re.search(r"timeout\s+<?\d", agent_section.lower()) \
        and not _re.search(r"timeout\s+<t>", agent_section.lower()), (
        "agent section must not contain a timeout wrapper command"
    )
    # No real temp-file path / cat-into-stdin pattern either.
    assert "/tmp/phase-" not in agent_section.lower(), (
        "agent section must not reference temp-file paths"
    )


def test_cache_stable_prompt_order() -> None:
    """Stable prefix (skill body, packet schema, project.yaml) before volatile suffix."""
    dispatch = _read(".ai/workflow/dispatch.md")
    assert "## Cache-stable prompt layout" in dispatch

    layout_start = dispatch.index("## Cache-stable prompt layout")
    layout_end = dispatch.index("## ", layout_start + 1)
    layout = dispatch[layout_start:layout_end]

    skill_pos = layout.index("skill body")
    packet_pos = layout.index("Packet schema")
    project_pos = layout.index("project.yaml")
    task_pos = layout.index("Task")
    memory_pos = layout.index("Memory slice")

    assert skill_pos < packet_pos < project_pos < task_pos < memory_pos, (
        "cache-stable layout order must be: skill body → packet schema → project.yaml → task → memory slice"
    )

    orch = _read(".claude/skills/orchestrate/SKILL.md")
    section_marker = "Dispatched-phase prompt contents"
    assert section_marker in orch
    sec_start = orch.index(section_marker)
    sec_end_candidates = [
        i for i in [orch.find("\n## ", sec_start + 1), orch.find("\n### ", sec_start + 1)]
        if i != -1
    ]
    sec_end = min(sec_end_candidates) if sec_end_candidates else len(orch)
    sec = orch[sec_start:sec_end]

    o_skill = sec.index("skill body")
    o_packet = sec.index("packet schema")
    o_project = sec.index("project.yaml")
    o_task = sec.index("user task")
    o_memory = sec.index("memory.md")

    assert o_skill < o_packet < o_project < o_task < o_memory, (
        "orchestrate SKILL.md prompt order must be: skill body → packet schema → project.yaml → user task → memory"
    )


def test_dispatcher_codex_unchanged() -> None:
    """Dispatcher section still shows codex exec wrapped in timeout."""
    text = _read(".ai/workflow/dispatch.md")
    dispatcher_heading = "### Mode: dispatcher (subprocess)"
    assert dispatcher_heading in text

    disp_start = text.index(dispatcher_heading)
    next_h2 = text.find("\n## ", disp_start + 1)
    dispatcher_section = text[disp_start:next_h2] if next_h2 != -1 else text[disp_start:]

    assert "codex exec" in dispatcher_section, (
        "dispatcher section must contain codex exec command"
    )
    assert "-m <phase.model>" in dispatcher_section, (
        "dispatcher section must reference <phase.model>"
    )
    assert "timeout <T>s" in dispatcher_section, (
        "dispatcher section must wrap commands with timeout"
    )
