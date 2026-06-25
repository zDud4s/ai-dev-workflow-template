#!/usr/bin/env bash
# workflow-lib.sh — shared Bash helpers for install.sh and update-workflow.sh.
#
# Both scripts `source` this file. It carries the de-duplicated copy helpers,
# the project-local skill mirror loop, the global (~/.agents/skills/) mirror
# helper, the directory-creation block, and a tiny manifest reader so the skill
# lists live in exactly one place (lib/skills.manifest).
#
# Behaviour is preserved verbatim from the pre-refactor inline copies.

# --- copy helpers ------------------------------------------------------------

copy_if_missing() {
  local src="$1"
  local dst="$2"
  if [ ! -f "$dst" ]; then
    cp "$src" "$dst"
    echo "Created $dst"
  else
    echo "Kept existing $dst"
  fi
}

copy_if_different() {
  local src="$1"
  local dst="$2"
  mkdir -p "$(dirname "$dst")"
  if [ ! -f "$dst" ]; then
    cp "$src" "$dst"
    echo "Created $dst"
    return
  fi
  if cmp -s "$src" "$dst"; then
    echo "Kept $dst (already up to date)"
    return
  fi
  cp "$src" "$dst"
  echo "Updated $dst"
}

# --- manifest reader ---------------------------------------------------------

# read_skill_group <manifest-path> <group> -> array on stdout (one name/line)
#
# Reads the named group (`shared`, `claude-only`, `codex-bridge`) from
# lib/skills.manifest. Group headers look like `# === <group> ===`; blank lines
# and other `#` comment lines are ignored. Callers collect the output into an
# array, e.g.:
#     mapfile -t SHARED_SKILLS < <(read_skill_group "$MANIFEST" shared)
read_skill_group() {
  local manifest="$1"
  local want="$2"
  local current=""
  local line trimmed
  while IFS= read -r line || [ -n "$line" ]; do
    # Group header: `# === <group> ===`
    if [[ "$line" =~ ^#[[:space:]]*===[[:space:]]*([A-Za-z0-9_-]+)[[:space:]]*===[[:space:]]*$ ]]; then
      current="${BASH_REMATCH[1]}"
      continue
    fi
    # Skip blank lines and ordinary comments.
    trimmed="${line#"${line%%[![:space:]]*}"}"   # strip leading whitespace
    trimmed="${trimmed%"${trimmed##*[![:space:]]}"}"  # strip trailing whitespace
    [ -z "$trimmed" ] && continue
    case "$trimmed" in
      \#*) continue ;;
    esac
    if [ "$current" = "$want" ]; then
      echo "$trimmed"
    fi
  done < "$manifest"
}

# --- directory-creation block ------------------------------------------------

# ensure_skill_dirs <target-dir> <shared-skill...>
#
# Creates the per-skill scaffold dirs in the target: the mirrored shared-skill
# dirs (.claude/skills/<s> and .agents/skills/<s>) driven by the list passed in
# (from the manifest), plus the claude-only multi-file skills (agent-improver/,
# agent-creator/) and the codex-bridge (.agents/skills/claude/) which are not
# part of the shared mirror loop. Shared by install.sh and update-workflow.sh.
ensure_skill_dirs() {
  local target_dir="$1"
  shift
  local s
  for s in "$@"; do
    mkdir -p "$target_dir/.claude/skills/$s"
    mkdir -p "$target_dir/.agents/skills/$s"
  done

  # codex ships to .claude/skills/ only (claude-only, no mirror).
  mkdir -p "$target_dir/.claude/skills/codex"
  # agent-improver / agent-creator are claude-only multi-file skills.
  mkdir -p "$target_dir/.claude/skills/agent-improver/references"
  mkdir -p "$target_dir/.claude/skills/agent-creator/references"
  mkdir -p "$target_dir/.claude/agents"
  # codex-bridge: .agents/skills/claude/ is its own source of truth.
  mkdir -p "$target_dir/.agents/skills/claude"
}

# ensure_workflow_dirs <target-dir> <shared-skill...>
#
# install.sh scaffold: the full .ai/ tree (packets/plans/specs included) plus
# the per-skill dirs via ensure_skill_dirs.
ensure_workflow_dirs() {
  local target_dir="$1"
  shift

  mkdir -p "$target_dir/.ai"
  mkdir -p "$target_dir/.ai/workflow"
  mkdir -p "$target_dir/.ai/packets"
  mkdir -p "$target_dir/.ai/plans"
  mkdir -p "$target_dir/.ai/specs"
  mkdir -p "$target_dir/.ai/dashboard"

  ensure_skill_dirs "$target_dir" "$@"
}

# --- project-local skill mirror ----------------------------------------------

# mirror_shared_skills_project <target-dir> <shared-skill...>
#
# Project-local mirror of shared skills: .claude/skills/<name>/ -> .agents/skills/<name>/.
# Keeps Codex's view of skills visible in-repo alongside Claude's. Always synced
# from .claude/skills/ — edit there, not here. copy_if_different so customizations
# in .claude/skills/ propagate; direct edits to .agents/skills/<shared>/ are overwritten.
# `codex` is NOT mirrored: codex is the runner, it never invokes a "codex skill"
# to call itself. `claude` is excluded for the symmetric reason and because it
# has no .claude/skills/ counterpart.
mirror_shared_skills_project() {
  local target_dir="$1"
  shift
  local skill
  for skill in "$@"; do
    local src_skill_dir="$target_dir/.claude/skills/$skill"
    [ -d "$src_skill_dir" ] || continue
    # Mirror EVERY file in the skill dir, not just SKILL.md, so a future
    # multi-file shared skill (references/, etc.) propagates into the .agents
    # mirror — matching sync_skills.py copy_skill()'s rglob behaviour. Mirroring
    # only SKILL.md would silently drop bundled files and the rglob-based
    # `sync_skills.py --check` would then flag the shell-installed copy as drift.
    find "$src_skill_dir" -type f | while IFS= read -r src_file; do
      local rel="${src_file#"$src_skill_dir"/}"
      copy_if_different "$src_file" "$target_dir/.agents/skills/$skill/$rel"
    done
  done
}

# --- global (~/.agents/skills/) mirror for Codex -----------------------------

# mirror_skills_to_home <target-dir> <skill...>
#
# Global skill mirror for Codex. Codex scans ~/.agents/skills/ exclusively (no
# project-local discovery), so every workflow skill must be mirrored there.
# Source for the mirror is the project's own .agents/skills/ (which itself was
# synced from .claude/skills/ for shared phase skills, plus the cross-tool
# dispatch skill which is its own source of truth), so user customizations in
# the project propagate to the global discovery path.
mirror_skills_to_home() {
  local target_dir="$1"
  shift
  local agents_skills_home="$HOME/.agents/skills"
  mkdir -p "$agents_skills_home"

  local skill
  for skill in "$@"; do
    local src="$target_dir/.agents/skills/$skill/SKILL.md"
    [ -f "$src" ] || { echo "Warning: missing $src — skipping mirror" >&2; continue; }
    mirror_skill_to_home "$src" "$skill"
  done
}

# mirror_skill_to_home <src-SKILL.md> <skill-name>
# Copies a single skill's SKILL.md into ~/.agents/skills/<name>/SKILL.md.
mirror_skill_to_home() {
  local src="$1"
  local name="$2"
  local dst_dir="$HOME/.agents/skills/$name"
  mkdir -p "$dst_dir"
  copy_if_different "$src" "$dst_dir/SKILL.md"
}
