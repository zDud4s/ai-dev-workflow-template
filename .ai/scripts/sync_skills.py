#!/usr/bin/env python3
"""Sync skill files between `.claude/skills/` and `.agents/skills/`.

Usage:
    python scripts/sync_skills.py <source> <dest> [skill_name]
    python scripts/sync_skills.py <source> <dest> <skill_name> --create-new
    python scripts/sync_skills.py --check

`source` / `dest` are one of: `claude`, `agents`. They map to:
    claude  -> .claude/skills/
    agents  -> .agents/skills/

Default behaviour:
    * Without `skill_name`: sync every skill that exists in BOTH directories,
      except the cross-call bridge pair (codex / claude) whose contents are
      intentionally different.
    * With `skill_name` AND the destination already has it: update the dst
      copy from src.
    * With `skill_name` AND the destination does NOT have it: refuse (exit 1).
      A Claude-only skill should stay Claude-only — running an "update from
      Claude to Agents" call shouldn't quietly invent an Agents mirror the
      operator never asked for.

`--create-new` opts into the "actually create the dst-side directory" path
for the explicit single-skill mode. Use this only when materialising a
brand-new skill that you want available on both sides from the start.

`--check` reports drift without copying anything (exit 1 if any drift).
"""
from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
from pathlib import Path

# Script lives at <repo>/.ai/scripts/sync_skills.py — repo root is two
# parents up. (Was one parent up under the old top-level scripts/ home;
# the move added a layer. Without this fix `__file__.parent.parent`
# resolves to `<repo>/.ai/` and the .claude/.agents lookups fail.)
ROOT = Path(__file__).resolve().parent.parent.parent
ROOTS = {
    "claude": ROOT / ".claude" / "skills",
    "agents": ROOT / ".agents" / "skills",
}
# Skills whose content is intentionally NOT mirrored (call-other-tool bridges).
BRIDGE_SKILLS = {"codex", "claude"}


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


def list_common(src: Path, dst: Path) -> list[str]:
    src_names = {p.name for p in src.iterdir() if p.is_dir()}
    dst_names = {p.name for p in dst.iterdir() if p.is_dir()}
    return sorted((src_names & dst_names) - BRIDGE_SKILLS)


def copy_skill(src_dir: Path, dst_dir: Path) -> list[str]:
    """Mirror src_dir contents into dst_dir; return list of changed files.

    The mirror is a true regeneration: files removed/renamed on the src side
    are pruned from dst so they don't orphan in the .agents mirror.
    """
    changed: list[str] = []
    dst_dir.mkdir(parents=True, exist_ok=True)
    src_rels: set[Path] = set()
    for src_file in src_dir.rglob("*"):
        if not src_file.is_file():
            continue
        rel = src_file.relative_to(src_dir)
        src_rels.add(rel)
        dst_file = dst_dir / rel
        if dst_file.exists() and dst_file.read_bytes() == src_file.read_bytes():
            continue
        dst_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src_file, dst_file)
        changed.append(str(rel))
    # Prune dst files with no src counterpart (left by a src-side delete/
    # rename). Report removals alongside additions.
    for dst_file in sorted(dst_dir.rglob("*")):
        if not dst_file.is_file():
            continue
        if dst_file.relative_to(dst_dir) not in src_rels:
            dst_file.unlink()
            changed.append(f"removed {dst_file.relative_to(dst_dir)}")
    return changed


def check(src: Path, dst: Path) -> int:
    drift = 0
    for name in list_common(src, dst):
        src_skill = src / name
        dst_skill = dst / name
        for src_file in src_skill.rglob("*"):
            if not src_file.is_file():
                continue
            rel = src_file.relative_to(src_skill)
            dst_file = dst_skill / rel
            if not dst_file.exists():
                print(f"MISSING in {dst}: {name}/{rel}")
                drift += 1
            elif dst_file.read_bytes() != src_file.read_bytes():
                print(f"DIFF {name}/{rel}: {sha(src_file)} vs {sha(dst_file)}")
                drift += 1
        # Reverse direction: a file in dst with no src counterpart is an
        # orphan (e.g. left by a src-side rename/delete). One-directional
        # checking would report "in sync" while the mirror carries stale
        # extra files.
        for dst_file in dst_skill.rglob("*"):
            if not dst_file.is_file():
                continue
            rel = dst_file.relative_to(dst_skill)
            if not (src_skill / rel).exists():
                print(f"EXTRA in {dst}: {name}/{rel}")
                drift += 1
    return drift


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("source", nargs="?", choices=ROOTS.keys(),
                        help="source side (claude|agents)")
    parser.add_argument("dest", nargs="?", choices=ROOTS.keys(),
                        help="destination side (claude|agents)")
    parser.add_argument("skill", nargs="?", default=None,
                        help="skill name (default: all common, minus bridges)")
    parser.add_argument("--check", action="store_true",
                        help="report drift without copying (claude vs agents)")
    parser.add_argument("--create-new", action="store_true",
                        help="allow materialising the dst-side dir when it "
                             "doesn't already exist (single-skill mode only). "
                             "Use this for brand-new skills you want on both "
                             "sides from the start; without it, the script "
                             "refuses to invent an unrequested mirror.")
    args = parser.parse_args()

    if args.check:
        drift = check(ROOTS["claude"], ROOTS["agents"])
        print(f"\n{drift} difference(s)")
        return 1 if drift else 0

    if not args.source or not args.dest:
        parser.error("source and dest required (or use --check)")
    if args.source == args.dest:
        parser.error("source and dest must differ")
    if args.create_new and not args.skill:
        parser.error("--create-new requires an explicit skill name "
                     "(the all-common pass never invents new dst dirs)")

    src_root, dst_root = ROOTS[args.source], ROOTS[args.dest]
    skills = [args.skill] if args.skill else list_common(src_root, dst_root)

    total = 0
    for name in skills:
        if name in BRIDGE_SKILLS:
            print(f"skip bridge: {name}")
            continue
        src_dir = src_root / name
        if not src_dir.is_dir():
            print(f"missing in source: {name}")
            continue
        dst_dir = dst_root / name
        # Refuse to create a brand-new skill on the destination side
        # unless the operator explicitly asked for it. This is the only
        # path that can hit this branch — the all-common loop above
        # already filters out skills that aren't in BOTH dirs.
        if not dst_dir.is_dir() and not args.create_new:
            print(f"{name}: dst missing — refusing to create (pass --create-new "
                  f"if you want to materialise {dst_dir})")
            continue
        changed = copy_skill(src_dir, dst_dir)
        if changed:
            print(f"{name}: {len(changed)} file(s) -> {dst_root}")
            for c in changed:
                print(f"  {c}")
            total += len(changed)
        else:
            print(f"{name}: already in sync")
    print(f"\n{total} file(s) copied")
    return 0


if __name__ == "__main__":
    sys.exit(main())
