from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
CANONICAL = REPO_ROOT / ".claude" / "skills" / "maintenance" / "SKILL.md"
MIRROR = REPO_ROOT / ".agents" / "skills" / "maintenance" / "SKILL.md"


def test_pinned_token_present():
    text = CANONICAL.read_text(encoding="utf-8")
    assert "Pinned" in text


def test_governance_topics_named_in_cap_region():
    text = CANONICAL.read_text(encoding="utf-8")
    cap_index = text.index("**Cap.**")
    for topic in ("git", "boundaries", "security", "workflow"):
        assert text.find(topic, cap_index) != -1


def test_maintenance_mirror_matches_canonical_bytes():
    assert MIRROR.read_bytes() == CANONICAL.read_bytes()
