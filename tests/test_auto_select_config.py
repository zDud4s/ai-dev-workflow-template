"""Config-layer tests for auto-select (spec section: Acceptance PR 1, config bullets)."""
from __future__ import annotations

import pytest


def test_models_has_auto_select_block(models_config):
    """auto_select block exists with required keys and valid value types.

    `enabled` is a bool (default False per spec, but user may flip to True).
    The test verifies schema, not policy.
    """
    block = models_config.get("auto_select")
    assert isinstance(block, dict), "models.yaml missing `auto_select` block"
    assert isinstance(block.get("enabled"), bool), (
        f"auto_select.enabled must be a bool, got {type(block.get('enabled')).__name__}"
    )
    assert block.get("token_budget") in ("low", "medium", "high"), (
        f"auto_select.token_budget must be low|medium|high, got {block.get('token_budget')!r}"
    )
    assert isinstance(block.get("phases"), list), "auto_select.phases must be a list"
    assert set(block["phases"]).issubset({"execute", "review", "rescue"}), (
        "auto_select.phases must be a subset of {execute, review, rescue}"
    )


def test_scope_key_is_commented_out(repo_root):
    """scope remains reserved (per_task activates in a future PR)."""
    import yaml

    text = (repo_root / ".ai" / "models.yaml").read_text(encoding="utf-8")
    assert "# scope:" in text, "scope key must be present as a comment placeholder"
    parsed = yaml.safe_load(text)
    auto = parsed.get("auto_select") or {}
    assert "scope" not in auto, "scope must not be a live key yet"


def test_adaptive_key_is_active(repo_root):
    """PR 3 activates adaptive (default false)."""
    import yaml

    text = (repo_root / ".ai" / "models.yaml").read_text(encoding="utf-8")
    parsed = yaml.safe_load(text)
    auto = parsed.get("auto_select") or {}
    assert "adaptive" in auto, "adaptive must be a live key from PR 3 onward"
    assert auto["adaptive"] is False, "adaptive defaults to False"
