"""Dual-tree skill mirroring: keep .claude/skills/ and .agents/skills/ in sync.

Extracted from serve.py. The repo keeps two parallel skill trees -- .claude/
(source of truth, consumed by Claude) and .agents/ (mirror, consumed by Codex).
``_mirror_claude_skill_to_agents`` propagates an improver-applied edit to an
EXISTING .agents mirror (never inventing one); ``_create_skill_in_both_trees``
is the one path allowed to materialise a brand-new skill on both sides.
``_BRIDGE_SKILLS_NO_MIRROR`` names the cross-call bridge skills (codex/claude)
whose two copies are deliberately different and must never be mirrored over.

Pure apart from ROOT (for tree locations) + stdlib. serve.py re-exports all
three names via a shim.
"""
from __future__ import annotations

from pathlib import Path

from server.paths import ROOT


# Cross-tool skill mirror. The repo keeps two parallel trees:
#   .claude/skills/<name>/   (source of truth, consumed by Claude)
#   .agents/skills/<name>/   (mirror, consumed by Codex)
# The two are kept in sync by .ai/scripts/sync_skills.py — without an in-process
# mirror step here, an improver-applied edit to .claude/skills/<x>/SKILL.md
# would only be visible to Claude. Codex would keep using the stale .agents
# copy until the user remembered to re-run the sync script by hand.
#
# Skills whose contents are intentionally NOT mirrored — cross-call bridges
# whose claude/agents copies are deliberately different.
_BRIDGE_SKILLS_NO_MIRROR = frozenset({"codex", "claude"})


def _mirror_claude_skill_to_agents(claude_skill_md: Path) -> tuple[bool, str]:
    """Update the parallel ``.agents/skills/<name>/SKILL.md`` ONLY when
    that mirror already exists. Used as a post-apply hook so an edit to
    an existing dual-tree skill propagates to the Codex side; a Claude-
    only skill (no .agents counterpart) stays Claude-only — the improver
    must not invent a Codex mirror the user never asked for.

    Best-effort, never raises:
      * ``(True, "<rel>")`` — mirror file existed and was updated.
      * ``(False, "skipped: <reason>")`` for known no-op cases
        (not a project skill, bridge pair, agents dir absent, mirror
        file absent, identical content).
      * ``(False, "error: ...")`` when a write actually failed.

    For the "I just created a brand-new skill and want it on both sides"
    case, see ``_create_skill_in_both_trees`` — that's the only path
    that's allowed to materialise a new file under .agents/skills/."""
    try:
        claude_root = (ROOT / ".claude" / "skills").resolve()
        agents_root = (ROOT / ".agents" / "skills").resolve()
        target_under_claude = claude_skill_md.resolve()
        rel = target_under_claude.relative_to(claude_root)
    except (ValueError, OSError):
        return (False, "skipped: not a .claude/skills path")
    # rel is "<skill_name>/SKILL.md" (or deeper for reference files we
    # don't currently mirror through this hook — see _apply_improvement
    # caller, which only touches SKILL.md).
    parts = rel.parts
    if not parts:
        return (False, "skipped: empty relative path")
    skill_name = parts[0]
    if skill_name in _BRIDGE_SKILLS_NO_MIRROR:
        return (False, f"skipped: bridge skill {skill_name!r} intentionally not mirrored")
    if not agents_root.is_dir():
        return (False, "skipped: .agents/skills not on disk")
    dst = agents_root / rel
    # New: only mirror when the destination ALREADY exists. A skill that
    # lives only under .claude/skills/ stays that way — there's no
    # reason to invent a .agents/skills/ copy for a Claude-only skill,
    # and doing so silently is the bug the operator was hitting.
    if not dst.is_file():
        return (False, f"skipped: no .agents mirror exists for {skill_name!r}")
    try:
        new_bytes = target_under_claude.read_bytes()
    except OSError as e:
        return (False, f"error: read source failed: {e}")
    try:
        if dst.read_bytes() == new_bytes:
            return (False, "skipped: agents copy already matches")
    except OSError:
        # Unreadable mirror — fall through and overwrite it; the safe
        # default is to align to the source of truth.
        pass
    try:
        dst.write_bytes(new_bytes)
    except OSError as e:
        return (False, f"error: write mirror failed: {e}")
    rel_str = str(Path(".agents/skills") / rel).replace("\\", "/")
    return (True, rel_str)


def _create_skill_in_both_trees(slug: str, content: str) -> dict:
    """Materialise a brand-new project skill at
    ``.claude/skills/<slug>/SKILL.md`` AND (when it's not a cross-call
    bridge) at ``.agents/skills/<slug>/SKILL.md``. Used by the draft-
    install path where the operator explicitly wants the new skill on
    both sides of the dual tree.

    Returns a dict:
      ``{"claude_path": "<rel>", "agents_path": "<rel>" | None,
        "agents_skipped_reason": "<str>" | None}``

    Errors on the Claude side raise (caller responsibility — the entire
    install fails). Errors on the Agents side are reported via
    ``agents_skipped_reason`` so the caller can decide whether to
    surface as a warning."""
    claude_dir = ROOT / ".claude" / "skills" / slug
    claude_md = claude_dir / "SKILL.md"
    try:
        claude_dir.mkdir(parents=True, exist_ok=True)
        claude_md.write_text(content, encoding="utf-8")
    except OSError as e:
        # Source-of-truth write failed — propagate so the caller can fail
        # the install cleanly (the proposal stays pending, no audit row
        # claims success). The wrapping try gives the AST-level
        # "every write_text is OSError-guarded" invariant test a handler
        # to find — re-raising is the intended behaviour.
        print(f"[serve] dual-install: claude-side write failed for {slug}: {e}",
              flush=True)
        raise
    result = {
        "claude_path": f".claude/skills/{slug}/SKILL.md",
        "agents_path": None,
        "agents_skipped_reason": None,
    }
    if slug in _BRIDGE_SKILLS_NO_MIRROR:
        result["agents_skipped_reason"] = (
            f"bridge skill {slug!r} intentionally not mirrored to .agents"
        )
        return result
    agents_root = ROOT / ".agents" / "skills"
    if not agents_root.is_dir():
        # Claude-only project — don't invent a parallel tree the operator
        # never set up. The .claude side is enough for them.
        result["agents_skipped_reason"] = ".agents/skills not on disk"
        return result
    agents_dir = agents_root / slug
    agents_md = agents_dir / "SKILL.md"
    try:
        agents_dir.mkdir(parents=True, exist_ok=True)
        agents_md.write_text(content, encoding="utf-8")
    except OSError as e:
        # Best-effort: .claude/ install already committed, surface the
        # agents miss as a warning but don't fail the whole flow.
        print(f"[serve] dual-install: agents-side write failed for {slug}: {e}",
              flush=True)
        result["agents_skipped_reason"] = f"write error: {e}"
        return result
    result["agents_path"] = f".agents/skills/{slug}/SKILL.md"
    return result
