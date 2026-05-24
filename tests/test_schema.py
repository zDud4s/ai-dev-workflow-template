"""Schema invariants for the configuration and dispatch-contract documents.

These tests guard the structural contract independent of specific values:
- models.yaml must have every phase declared with `tool` + `model`
- project.yaml must keep its top-level layout
- dispatch.md must keep the contract vocabulary (routing modes, escalation
  format, both tool invocation examples)
- orchestrate skill must keep its pipeline structure
"""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import (
    ALLOWED_TOOLS,
    CLAUDE_SKILLS_DIR,
    PHASES,
    WORKFLOW_DIR,
)


# --- models.yaml -------------------------------------------------------------


def test_models_dispatch_mode_valid(models_config):
    mode = models_config.get("dispatch_mode")
    assert mode in ("auto", "manual"), f"dispatch_mode must be auto|manual, got {mode!r}"


def test_models_session_block_present_for_auto(models_config):
    if models_config.get("dispatch_mode") != "auto":
        pytest.skip("dispatch_mode != auto")
    session = models_config.get("session")
    assert isinstance(session, dict), "auto dispatch needs a `session` block"
    assert session.get("tool") in ALLOWED_TOOLS, (
        f"session.tool must be one of {ALLOWED_TOOLS}, got {session.get('tool')!r}"
    )
    assert session.get("model"), "session.model is required when dispatch_mode=auto"


@pytest.mark.parametrize("phase", PHASES)
def test_models_phase_has_tool_and_model(models_config, phase):
    block = models_config.get(phase)
    assert isinstance(block, dict), f"models.yaml missing block for `{phase}`"
    assert block.get("tool") in ALLOWED_TOOLS, (
        f"{phase}.tool must be one of {ALLOWED_TOOLS}, got {block.get('tool')!r}"
    )
    assert block.get("model"), f"{phase}.model is required"


# --- project.yaml ------------------------------------------------------------


def test_project_top_level_keys(project_config):
    required = {
        "project_name",
        "commands",
        "boundaries",
        "memory_tuning",
        "definition_of_done",
    }
    missing = required - set(project_config.keys())
    assert not missing, f"project.yaml missing required keys: {sorted(missing)}"


def test_project_commands_layout(project_config):
    cmds = project_config.get("commands") or {}
    expected = {"install", "dev", "build", "test", "lint", "format", "typecheck"}
    missing = expected - set(cmds.keys())
    assert not missing, f"project.yaml `commands` missing: {sorted(missing)}"


def test_project_boundaries_layout(project_config):
    b = project_config.get("boundaries") or {}
    expected = {
        "risky_areas",
        "do_not_touch",
        "generated_files",
        "migration_sensitive",
        "security_sensitive",
    }
    missing = expected - set(b.keys())
    assert not missing, f"project.yaml `boundaries` missing: {sorted(missing)}"


def test_project_memory_tuning_threshold_is_int(project_config):
    mt = project_config.get("memory_tuning") or {}
    threshold = mt.get("consolidation_threshold_lines")
    assert isinstance(threshold, int) and threshold > 0, (
        f"memory_tuning.consolidation_threshold_lines must be a positive int, got {threshold!r}"
    )


# --- dispatch.md contract ----------------------------------------------------


@pytest.fixture(scope="module")
def dispatch_text() -> str:
    return (WORKFLOW_DIR / "dispatch.md").read_text(encoding="utf-8")


def test_dispatch_mentions_all_phases(dispatch_text):
    for phase in PHASES:
        assert phase in dispatch_text, f"dispatch.md missing phase `{phase}`"


@pytest.mark.parametrize("mode", ["inline", "agent", "dispatcher"])
def test_dispatch_routing_modes_documented(dispatch_text, mode):
    assert mode in dispatch_text, f"dispatch.md missing routing mode `{mode}`"


def test_dispatch_escalation_format_documented(dispatch_text):
    # The escalation block has four required fields.
    for field in ("reason:", "needed:", "suggested-next:", "partial-output:"):
        assert field in dispatch_text, (
            f"dispatch.md missing escalation field `{field}`"
        )


def test_dispatch_both_tool_invocations_present(dispatch_text):
    """The dispatch contract must show how to call each supported tool, so the
    refactor preserves symmetry between Claude and Codex as executors."""
    assert "claude -p" in dispatch_text, "dispatch.md missing `claude -p` example"
    assert "codex exec" in dispatch_text, "dispatch.md missing `codex exec` example"


def test_dispatch_timeout_convention_present(dispatch_text):
    assert "timeout" in dispatch_text.lower(), (
        "dispatch.md should document the timeout convention"
    )


# --- orchestrate skill -------------------------------------------------------


@pytest.fixture(scope="module")
def orchestrate_text() -> str:
    return (CLAUDE_SKILLS_DIR / "orchestrate" / "SKILL.md").read_text(encoding="utf-8")


def test_orchestrate_references_dispatch_doc(orchestrate_text):
    assert "dispatch.md" in orchestrate_text, (
        "orchestrate must reference dispatch.md (the dispatch contract)"
    )


@pytest.mark.parametrize(
    "section",
    [
        "Pre-flight checks",
        "Phase 1",
        "Phase 2",
        "Phase 3",
        "Phase 4",
    ],
)
def test_orchestrate_has_section(orchestrate_text, section):
    assert section in orchestrate_text, f"orchestrate skill missing `{section}`"


def test_orchestrate_no_in_context_execution_rule(orchestrate_text):
    """Hard rule: the orchestrator must not silently substitute itself for the
    configured executor. We assert the rule text is still there in some form."""
    lowered = orchestrate_text.lower()
    assert "no in-context execution" in lowered or "no-in-context-execution" in lowered, (
        "orchestrate skill must keep the 'no in-context execution' hard rule"
    )
