#!/usr/bin/env bash
set -euo pipefail

TARGET_DIR="${1:-.}"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

echo "Installing AI workflow into: $TARGET_DIR"

mkdir -p "$TARGET_DIR/.ai"
mkdir -p "$TARGET_DIR/.ai/workflow"
mkdir -p "$TARGET_DIR/.ai/packets"
mkdir -p "$TARGET_DIR/.ai/plans"
mkdir -p "$TARGET_DIR/.ai/specs"
mkdir -p "$TARGET_DIR/.ai/dashboard"
mkdir -p "$TARGET_DIR/.claude/skills/bootstrap"
mkdir -p "$TARGET_DIR/.claude/skills/planner"
mkdir -p "$TARGET_DIR/.claude/skills/reviewer"
mkdir -p "$TARGET_DIR/.claude/skills/maintenance"
mkdir -p "$TARGET_DIR/.claude/skills/rescue"
mkdir -p "$TARGET_DIR/.claude/skills/codex"
mkdir -p "$TARGET_DIR/.claude/skills/orchestrate"
mkdir -p "$TARGET_DIR/.claude/skills/agent-improver/references"
mkdir -p "$TARGET_DIR/.claude/skills/agent-creator/references"
mkdir -p "$TARGET_DIR/.agents/skills/bootstrap"
mkdir -p "$TARGET_DIR/.agents/skills/planner"
mkdir -p "$TARGET_DIR/.agents/skills/reviewer"
mkdir -p "$TARGET_DIR/.agents/skills/maintenance"
mkdir -p "$TARGET_DIR/.agents/skills/rescue"
mkdir -p "$TARGET_DIR/.agents/skills/orchestrate"
mkdir -p "$TARGET_DIR/.agents/skills/claude"

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

# Mutable project layer — only create if missing
copy_if_missing "$SCRIPT_DIR/.ai/project.yaml" "$TARGET_DIR/.ai/project.yaml"
copy_if_missing "$SCRIPT_DIR/.ai/memory.md" "$TARGET_DIR/.ai/memory.md"
copy_if_missing "$SCRIPT_DIR/.ai/decisions.md" "$TARGET_DIR/.ai/decisions.md"
copy_if_missing "$SCRIPT_DIR/.ai/models.yaml" "$TARGET_DIR/.ai/models.yaml"

# Skills — only create if missing (user may have customized)
# .claude/skills/ holds the canonical source of truth for shared skills.
copy_if_missing "$SCRIPT_DIR/.claude/skills/bootstrap/SKILL.md" "$TARGET_DIR/.claude/skills/bootstrap/SKILL.md"
copy_if_missing "$SCRIPT_DIR/.claude/skills/planner/SKILL.md" "$TARGET_DIR/.claude/skills/planner/SKILL.md"
copy_if_missing "$SCRIPT_DIR/.claude/skills/reviewer/SKILL.md" "$TARGET_DIR/.claude/skills/reviewer/SKILL.md"
copy_if_missing "$SCRIPT_DIR/.claude/skills/maintenance/SKILL.md" "$TARGET_DIR/.claude/skills/maintenance/SKILL.md"
copy_if_missing "$SCRIPT_DIR/.claude/skills/rescue/SKILL.md" "$TARGET_DIR/.claude/skills/rescue/SKILL.md"
copy_if_missing "$SCRIPT_DIR/.claude/skills/codex/SKILL.md" "$TARGET_DIR/.claude/skills/codex/SKILL.md"
copy_if_missing "$SCRIPT_DIR/.claude/skills/orchestrate/SKILL.md" "$TARGET_DIR/.claude/skills/orchestrate/SKILL.md"

# Claude-only `agent-improver` skill (audits .claude/agents/*.md; no Codex counterpart).
# Has bundled reference files alongside SKILL.md.
copy_if_missing "$SCRIPT_DIR/.claude/skills/agent-improver/SKILL.md" "$TARGET_DIR/.claude/skills/agent-improver/SKILL.md"
copy_if_missing "$SCRIPT_DIR/.claude/skills/agent-improver/references/quality-criteria.md" "$TARGET_DIR/.claude/skills/agent-improver/references/quality-criteria.md"
copy_if_missing "$SCRIPT_DIR/.claude/skills/agent-improver/references/agent-template.md" "$TARGET_DIR/.claude/skills/agent-improver/references/agent-template.md"

# Claude-only `agent-creator` skill (creates .claude/agents/*.md after approval).
# Has a bundled reference template alongside SKILL.md.
copy_if_missing "$SCRIPT_DIR/.claude/skills/agent-creator/SKILL.md" "$TARGET_DIR/.claude/skills/agent-creator/SKILL.md"
copy_if_missing "$SCRIPT_DIR/.claude/skills/agent-creator/references/agent-template.md" "$TARGET_DIR/.claude/skills/agent-creator/references/agent-template.md"

# Codex-only `claude` skill (no Claude counterpart) — source under .agents/skills/.
copy_if_missing "$SCRIPT_DIR/.agents/skills/claude/SKILL.md" "$TARGET_DIR/.agents/skills/claude/SKILL.md"

# Project-local mirror of shared skills: .claude/skills/<name>/ -> .agents/skills/<name>/.
# Keeps Codex's view of skills visible in-repo alongside Claude's. Always synced
# from .claude/skills/ — edit there, not here. copy_if_different so customizations
# in .claude/skills/ propagate; direct edits to .agents/skills/<shared>/ are overwritten.
# `codex` is excluded: Codex does not need a skill describing how to invoke itself
# (symmetric to Claude not having a `claude` skill).
for skill in bootstrap planner reviewer maintenance rescue orchestrate; do
  copy_if_different "$TARGET_DIR/.claude/skills/$skill/SKILL.md" "$TARGET_DIR/.agents/skills/$skill/SKILL.md"
done

# Workflow core and packets — always update (immutable core)
copy_if_different "$SCRIPT_DIR/.ai/workflow/agents-block.md" "$TARGET_DIR/.ai/workflow/agents-block.md"
copy_if_different "$SCRIPT_DIR/.ai/workflow/workflow.md" "$TARGET_DIR/.ai/workflow/workflow.md"
copy_if_different "$SCRIPT_DIR/.ai/workflow/dispatch.md" "$TARGET_DIR/.ai/workflow/dispatch.md"

# Pre-rename leftover from older installs (claude-workflow.md -> workflow.md).
if [ -f "$TARGET_DIR/.ai/workflow/claude-workflow.md" ]; then
  rm -f "$TARGET_DIR/.ai/workflow/claude-workflow.md"
  echo "Removed stale $TARGET_DIR/.ai/workflow/claude-workflow.md (renamed to workflow.md)"
fi
copy_if_different "$SCRIPT_DIR/.ai/packets/plan.md" "$TARGET_DIR/.ai/packets/plan.md"
copy_if_different "$SCRIPT_DIR/.ai/packets/execute.md" "$TARGET_DIR/.ai/packets/execute.md"
copy_if_different "$SCRIPT_DIR/.ai/packets/review.md" "$TARGET_DIR/.ai/packets/review.md"
copy_if_different "$SCRIPT_DIR/.ai/packets/rescue.md" "$TARGET_DIR/.ai/packets/rescue.md"

# Local dashboard — always update (it's a small standalone tool)
copy_if_different "$SCRIPT_DIR/.ai/dashboard/serve.py" "$TARGET_DIR/.ai/dashboard/serve.py"
copy_if_different "$SCRIPT_DIR/.ai/dashboard/index.html" "$TARGET_DIR/.ai/dashboard/index.html"
copy_if_different "$SCRIPT_DIR/.ai/dashboard/styles.css" "$TARGET_DIR/.ai/dashboard/styles.css"
copy_if_different "$SCRIPT_DIR/.ai/dashboard/log_event.py" "$TARGET_DIR/.ai/dashboard/log_event.py"
copy_if_different "$SCRIPT_DIR/.ai/dashboard/app/core.js" "$TARGET_DIR/.ai/dashboard/app/core.js"
copy_if_different "$SCRIPT_DIR/.ai/dashboard/app/skills.js" "$TARGET_DIR/.ai/dashboard/app/skills.js"
copy_if_different "$SCRIPT_DIR/.ai/dashboard/app/agents.js" "$TARGET_DIR/.ai/dashboard/app/agents.js"
copy_if_different "$SCRIPT_DIR/.ai/dashboard/app/jobs.js" "$TARGET_DIR/.ai/dashboard/app/jobs.js"
copy_if_different "$SCRIPT_DIR/.ai/dashboard/app/terminals.js" "$TARGET_DIR/.ai/dashboard/app/terminals.js"
copy_if_different "$SCRIPT_DIR/.ai/dashboard/app/main.js" "$TARGET_DIR/.ai/dashboard/app/main.js"

# Pre-split monolithic app.js lingers from older installs — remove it so the
# new index.html (which loads app/*.js) doesn't share a directory with dead code.
if [ -f "$TARGET_DIR/.ai/dashboard/app.js" ]; then
  rm -f "$TARGET_DIR/.ai/dashboard/app.js"
  echo "Removed stale $TARGET_DIR/.ai/dashboard/app.js (now split into app/*.js)"
fi

PYTHON_CMD=""
if command -v python3 &>/dev/null; then
  PYTHON_CMD="python3"
elif command -v python &>/dev/null; then
  PYTHON_CMD="python"
else
  echo "Error: python3 or python is required but not found."
  exit 1
fi

$PYTHON_CMD - "$TARGET_DIR" "$SCRIPT_DIR" <<'PY'
from pathlib import Path
import json
import sys

target_dir = Path(sys.argv[1])
script_dir = Path(sys.argv[2])

agents_block = (script_dir / ".ai/workflow/agents-block.md").read_text(encoding="utf-8")
claude_import_block = """<!-- >>> AI WORKFLOW MANAGED IMPORT >>> -->
@.ai/workflow/workflow.md
<!-- <<< AI WORKFLOW MANAGED IMPORT <<< -->"""

def upsert_block(path: Path, start_marker: str, end_marker: str, block_text: str):
    if path.exists():
        content = path.read_text(encoding="utf-8")
        if start_marker in content and end_marker in content:
            before = content.split(start_marker)[0].rstrip()
            after = content.split(end_marker, 1)[1].lstrip()
        else:
            before = content.rstrip()
            after = ""
    else:
        before = ""
        after = ""
    new_content = ""
    if before:
        new_content += before + "\n\n"
    new_content += block_text.strip() + "\n"
    if after:
        new_content += "\n" + after
    # encoding="utf-8" so we don't crash on PT/UTF-8 content on Windows (default cp1252).
    # newline="\n" so the file stays LF on Windows (matches .gitattributes eol=lf).
    path.write_text(new_content, encoding="utf-8", newline="\n")

# AGENTS.md
agents_path = target_dir / "AGENTS.md"
upsert_block(
    agents_path,
    "# >>> AI WORKFLOW MANAGED BLOCK >>>",
    "# <<< AI WORKFLOW MANAGED BLOCK <<<",
    agents_block,
)

# CLAUDE target selection — prefer root (aligned with AGENTS.md);
# legacy .claude/CLAUDE.md is respected when present but not created.
root_claude = target_dir / "CLAUDE.md"
dot_claude = target_dir / ".claude" / "CLAUDE.md"

if dot_claude.exists() and not root_claude.exists():
    claude_target = dot_claude
else:
    claude_target = root_claude

upsert_block(
    claude_target,
    "<!-- >>> AI WORKFLOW MANAGED IMPORT >>>",
    "<!-- <<< AI WORKFLOW MANAGED IMPORT <<< -->",
    claude_import_block,
)

def merge_claude_settings(template_path, target_path):
    # .claude/settings.json carries permissions + hooks the workflow needs to
    # orchestrate (dashboard event hook, codex exec permission, etc.). Merge
    # required entries in without overwriting user-added permissions or hooks.
    # Create the file if missing.
    if not template_path.exists():
        return
    template_data = json.loads(template_path.read_text(encoding="utf-8"))
    if not target_path.exists():
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(
            json.dumps(template_data, indent=2) + "\n",
            encoding="utf-8", newline="\n",
        )
        print(f"Created {target_path} (workflow settings)")
        return
    target_data = json.loads(target_path.read_text(encoding="utf-8"))
    added_perms = []
    added_hooks = []
    template_allow = (template_data.get("permissions") or {}).get("allow") or []
    target_perms = target_data.setdefault("permissions", {})
    target_allow = target_perms.setdefault("allow", [])
    target_allow_set = set(target_allow)
    for perm in template_allow:
        if perm not in target_allow_set:
            target_allow.append(perm)
            target_allow_set.add(perm)
            added_perms.append(perm)
    template_hooks = template_data.get("hooks") or {}
    target_hooks = target_data.setdefault("hooks", {})
    for event, template_entries in template_hooks.items():
        target_entries = target_hooks.setdefault(event, [])
        for template_entry in template_entries:
            matcher = template_entry.get("matcher", "")
            target_entry = next(
                (te for te in target_entries if te.get("matcher") == matcher),
                None,
            )
            if target_entry is None:
                target_entry = {"matcher": matcher, "hooks": []}
                target_entries.append(target_entry)
            existing_cmds = {h.get("command") for h in target_entry.get("hooks") or []}
            for template_hook in template_entry.get("hooks") or []:
                cmd = template_hook.get("command")
                if cmd and cmd not in existing_cmds:
                    target_entry.setdefault("hooks", []).append(template_hook)
                    existing_cmds.add(cmd)
                    added_hooks.append(f"{event}/{matcher}")
    if not (added_perms or added_hooks):
        print(f"Kept {target_path} (workflow settings already present)")
        return
    target_path.write_text(
        json.dumps(target_data, indent=2) + "\n",
        encoding="utf-8", newline="\n",
    )
    parts = []
    if added_perms:
        parts.append(f"+{len(added_perms)} permission(s)")
    if added_hooks:
        parts.append(f"+{len(added_hooks)} hook(s)")
    print(f"Merged {target_path} ({', '.join(parts)})")

merge_claude_settings(
    script_dir / ".claude/settings.json",
    target_dir / ".claude/settings.json",
)
PY

# Global skill mirror for Codex.
# Codex only scans ~/.agents/skills/ (no project-local discovery), so every
# workflow skill must be mirrored there. Source for the mirror is the project's
# own .agents/skills/ (which itself was synced from .claude/skills/ above), so
# user customizations in the project propagate to the global discovery path.
AGENTS_SKILLS_HOME="$HOME/.agents/skills"
mkdir -p "$AGENTS_SKILLS_HOME"

mirror_skill_to_home() {
  local src="$1"
  local name="$2"
  local dst_dir="$AGENTS_SKILLS_HOME/$name"
  mkdir -p "$dst_dir"
  cp "$src" "$dst_dir/SKILL.md"
  echo "Mirrored skill '$name' to $dst_dir/SKILL.md"
}

for skill in bootstrap planner reviewer maintenance rescue orchestrate claude; do
  src="$TARGET_DIR/.agents/skills/$skill/SKILL.md"
  [ -f "$src" ] || { echo "Warning: missing $src — skipping mirror" >&2; continue; }
  mirror_skill_to_home "$src" "$skill"
done

# Codex global config — ensure approval_policy allows --full-auto to work
# approval_policy = "on-request" means Codex auto-approves all actions when --full-auto is passed
# Only adds the setting if not already present — never overwrites a user's existing policy
CODEX_CONFIG="$HOME/.codex/config.toml"
if [ -f "$CODEX_CONFIG" ]; then
  if ! grep -q "approval_policy" "$CODEX_CONFIG"; then
    echo "" >> "$CODEX_CONFIG"
    echo 'approval_policy = "on-request"' >> "$CODEX_CONFIG"
    echo "Added approval_policy = \"on-request\" to $CODEX_CONFIG"
  else
    echo "Kept existing approval_policy in $CODEX_CONFIG"
  fi
else
  mkdir -p "$HOME/.codex"
  echo 'approval_policy = "on-request"' > "$CODEX_CONFIG"
  echo "Created $CODEX_CONFIG with approval_policy = \"on-request\""
fi

echo ""
echo "Done."
echo "Next step: open Claude or Sonnet in the repo and run the bootstrap skill."
echo "  Example: 'Use the bootstrap skill. Adapt this repository to the workflow scaffold.'"
