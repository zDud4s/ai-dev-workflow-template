#!/usr/bin/env bash
set -euo pipefail

TARGET_DIR="${1:-.}"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

echo "Installing AI workflow into: $TARGET_DIR"

mkdir -p "$TARGET_DIR/.ai"
mkdir -p "$TARGET_DIR/.ai/workflow"
mkdir -p "$TARGET_DIR/.ai/packets"
mkdir -p "$TARGET_DIR/.claude/skills/bootstrap"
mkdir -p "$TARGET_DIR/.claude/skills/planner"
mkdir -p "$TARGET_DIR/.claude/skills/reviewer"
mkdir -p "$TARGET_DIR/.claude/skills/maintenance"
mkdir -p "$TARGET_DIR/.claude/skills/rescue"
mkdir -p "$TARGET_DIR/.claude/skills/codex"

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

copy_always() {
  local src="$1"
  local dst="$2"
  mkdir -p "$(dirname "$dst")"
  cp "$src" "$dst"
  echo "Updated $dst"
}

# Mutable project layer — only create if missing
copy_if_missing "$SCRIPT_DIR/.ai/project.yaml" "$TARGET_DIR/.ai/project.yaml"
copy_if_missing "$SCRIPT_DIR/.ai/memory.md" "$TARGET_DIR/.ai/memory.md"
copy_if_missing "$SCRIPT_DIR/.ai/decisions.md" "$TARGET_DIR/.ai/decisions.md"
copy_if_missing "$SCRIPT_DIR/.ai/models.yaml" "$TARGET_DIR/.ai/models.yaml"

# Skills — only create if missing (user may have customized)
copy_if_missing "$SCRIPT_DIR/.claude/skills/bootstrap/SKILL.md" "$TARGET_DIR/.claude/skills/bootstrap/SKILL.md"
copy_if_missing "$SCRIPT_DIR/.claude/skills/planner/SKILL.md" "$TARGET_DIR/.claude/skills/planner/SKILL.md"
copy_if_missing "$SCRIPT_DIR/.claude/skills/reviewer/SKILL.md" "$TARGET_DIR/.claude/skills/reviewer/SKILL.md"
copy_if_missing "$SCRIPT_DIR/.claude/skills/maintenance/SKILL.md" "$TARGET_DIR/.claude/skills/maintenance/SKILL.md"
copy_if_missing "$SCRIPT_DIR/.claude/skills/rescue/SKILL.md" "$TARGET_DIR/.claude/skills/rescue/SKILL.md"
copy_if_missing "$SCRIPT_DIR/.claude/skills/codex/SKILL.md" "$TARGET_DIR/.claude/skills/codex/SKILL.md"

# Workflow core and packets — always update (immutable core)
copy_always "$SCRIPT_DIR/.ai/workflow/agents-block.md" "$TARGET_DIR/.ai/workflow/agents-block.md"
copy_always "$SCRIPT_DIR/.ai/workflow/claude-workflow.md" "$TARGET_DIR/.ai/workflow/claude-workflow.md"
copy_always "$SCRIPT_DIR/.ai/packets/plan.md" "$TARGET_DIR/.ai/packets/plan.md"
copy_always "$SCRIPT_DIR/.ai/packets/execute.md" "$TARGET_DIR/.ai/packets/execute.md"
copy_always "$SCRIPT_DIR/.ai/packets/review.md" "$TARGET_DIR/.ai/packets/review.md"
copy_always "$SCRIPT_DIR/.ai/packets/rescue.md" "$TARGET_DIR/.ai/packets/rescue.md"

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
import sys

target_dir = Path(sys.argv[1])
script_dir = Path(sys.argv[2])

agents_block = (script_dir / ".ai/workflow/agents-block.md").read_text()
claude_import_block = """<!-- >>> AI WORKFLOW MANAGED IMPORT >>> -->
@.ai/workflow/claude-workflow.md
<!-- <<< AI WORKFLOW MANAGED IMPORT <<< -->"""

def upsert_block(path: Path, start_marker: str, end_marker: str, block_text: str):
    if path.exists():
        content = path.read_text()
        if start_marker in content and end_marker in content:
            before = content.split(start_marker)[0].rstrip()
            after = content.split(end_marker, 1)[1].lstrip()
            new_content = before + "\n\n" + block_text.strip() + "\n"
            if after:
                new_content += "\n" + after
        else:
            new_content = content.rstrip() + "\n\n" + block_text.strip() + "\n"
    else:
        new_content = block_text.strip() + "\n"
    path.write_text(new_content)

# AGENTS.md
agents_path = target_dir / "AGENTS.md"
upsert_block(
    agents_path,
    "# >>> AI WORKFLOW MANAGED BLOCK >>>",
    "# <<< AI WORKFLOW MANAGED BLOCK <<<",
    agents_block,
)

# CLAUDE target selection
root_claude = target_dir / "CLAUDE.md"
dot_claude_dir = target_dir / ".claude"
dot_claude = dot_claude_dir / "CLAUDE.md"

if root_claude.exists():
    claude_target = root_claude
elif dot_claude.exists():
    claude_target = dot_claude
else:
    dot_claude_dir.mkdir(parents=True, exist_ok=True)
    claude_target = dot_claude

upsert_block(
    claude_target,
    "<!-- >>> AI WORKFLOW MANAGED IMPORT >>>",
    "<!-- <<< AI WORKFLOW MANAGED IMPORT <<< -->",
    claude_import_block,
)
PY

# Codex→Claude skill — install globally so Codex discovers it at startup
# Codex only scans ~/.agents/skills/ (no project-local discovery)
CALL_CLAUDE_DIR="$HOME/.agents/skills/call-claude"
mkdir -p "$CALL_CLAUDE_DIR"
cp "$SCRIPT_DIR/.agents/skills/call-claude/SKILL.md" "$CALL_CLAUDE_DIR/SKILL.md"
echo "Installed Codex→Claude skill to $CALL_CLAUDE_DIR/SKILL.md"

echo ""
echo "Done."
echo "Next step: open Claude or Sonnet in the repo and run the bootstrap skill."
echo "  Example: 'Use the bootstrap skill. Adapt this repository to the workflow scaffold.'"
