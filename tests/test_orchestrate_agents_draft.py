"""Static-lint over orchestrate-agents SKILL.md (merged: drafts pipeline YAML inline)."""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CANONICAL = REPO_ROOT / ".claude" / "skills" / "orchestrate-agents" / "SKILL.md"
MIRROR = REPO_ROOT / ".agents" / "skills" / "orchestrate-agents" / "SKILL.md"
SKILL = CANONICAL


def test_skill_mentions_pipelines_dir() -> None:
    text = SKILL.read_text(encoding="utf-8")
    assert ".ai/local/pipelines/" in text


def test_skill_mentions_three_save_options() -> None:
    text = SKILL.read_text(encoding="utf-8").lower()
    for token in ("save & run", "save only", "discard"):
        assert token.lower() in text


def test_skill_does_not_dispatch_tasks() -> None:
    """Draft helper must NOT dispatch agents via Task tool — that's run-pipeline's job."""
    text = SKILL.read_text(encoding="utf-8")
    assert "Phase 2 - Dispatch" not in text
    assert "in-session Task" not in text


def test_skill_does_not_mention_synthesizer_call() -> None:
    """The draft helper does not call synthesizer; that moved to run-pipeline."""
    text = SKILL.read_text(encoding="utf-8")
    assert "Phase 3 - Synthesis" not in text


def test_skill_mentions_pipeline_draft_metric() -> None:
    text = SKILL.read_text(encoding="utf-8")
    assert "pipeline_draft" in text


def test_skill_does_not_reference_removed_agent_dispatch_packet() -> None:
    text = SKILL.read_text(encoding="utf-8")
    assert "agent-dispatch.md" not in text
    assert "packets/agent-dispatch" not in text


def test_skill_embeds_pipeline_yaml_schema() -> None:
    """After merging agent-planner inline, the merged skill must carry the
    schema rules itself (description / output / nodes; output modes; subagent_type
    convention; fenced yaml example).
    """
    text = SKILL.read_text(encoding="utf-8")
    for key in ("description", "output", "nodes"):
        assert key in text, f"schema key '{key}' must be referenced"
    for mode in ("synthesize", "passthrough", "per-agent"):
        assert mode in text, f"output mode '{mode}' must be documented"
    assert "subagent_type" in text, "agent-field rule (subagent_type) must be documented"
    assert "```yaml" in text, "schema example must be a fenced yaml block"


def test_skill_does_not_dispatch_to_external_planner() -> None:
    """The planner now runs inline. The skill must not reference dispatching to
    a separate `agent-planner` skill nor a `orchestrate_agents.plan` config key.
    """
    text = SKILL.read_text(encoding="utf-8")
    assert "agent-planner" not in text
    assert "orchestrate_agents.plan" not in text


def test_canonical_drops_codex_runtime_note() -> None:
    text = CANONICAL.read_text(encoding="utf-8")
    assert "Codex runtime note" not in text
    assert "Save & run** is unavailable" not in text


def test_orchestrate_agents_mirror_byte_identical_to_canonical() -> None:
    assert CANONICAL.read_bytes() == MIRROR.read_bytes()
