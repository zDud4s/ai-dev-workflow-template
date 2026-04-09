#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  bash update-workflow.sh <target-path> [--include-packets]

What it updates by default:
  - .claude/skills/*
  - .ai/workflow/*
  - managed blocks in AGENTS.md and CLAUDE.md
  - ~/.agents/skills/call-claude/SKILL.md

What it preserves by default:
  - .ai/packets/*
  - .ai/models.yaml
  - .ai/project.yaml
  - .ai/memory.md
  - .ai/decisions.md

Options:
  --include-packets   Also update .ai/packets/*
EOF
}

if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ] || [ $# -eq 0 ]; then
  usage
  exit 0
fi

TARGET_DIR="$1"
shift

INCLUDE_PACKETS=0
for arg in "$@"; do
  case "$arg" in
    --include-packets)
      INCLUDE_PACKETS=1
      ;;
    *)
      echo "Unknown option: $arg" >&2
      usage >&2
      exit 1
      ;;
  esac
done

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
TARGET_DIR="$(cd -- "$TARGET_DIR" && pwd)"

echo "Updating AI workflow in: $TARGET_DIR"

mkdir -p "$TARGET_DIR/.ai/workflow"
mkdir -p "$TARGET_DIR/.claude/skills/bootstrap"
mkdir -p "$TARGET_DIR/.claude/skills/planner"
mkdir -p "$TARGET_DIR/.claude/skills/reviewer"
mkdir -p "$TARGET_DIR/.claude/skills/maintenance"
mkdir -p "$TARGET_DIR/.claude/skills/rescue"
mkdir -p "$TARGET_DIR/.claude/skills/codex"
mkdir -p "$TARGET_DIR/.claude/skills/orchestrate"

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

# Shared workflow files to refresh in downstream projects.
copy_if_different "$SCRIPT_DIR/.claude/skills/bootstrap/SKILL.md" "$TARGET_DIR/.claude/skills/bootstrap/SKILL.md"
copy_if_different "$SCRIPT_DIR/.claude/skills/planner/SKILL.md" "$TARGET_DIR/.claude/skills/planner/SKILL.md"
copy_if_different "$SCRIPT_DIR/.claude/skills/reviewer/SKILL.md" "$TARGET_DIR/.claude/skills/reviewer/SKILL.md"
copy_if_different "$SCRIPT_DIR/.claude/skills/maintenance/SKILL.md" "$TARGET_DIR/.claude/skills/maintenance/SKILL.md"
copy_if_different "$SCRIPT_DIR/.claude/skills/rescue/SKILL.md" "$TARGET_DIR/.claude/skills/rescue/SKILL.md"
copy_if_different "$SCRIPT_DIR/.claude/skills/codex/SKILL.md" "$TARGET_DIR/.claude/skills/codex/SKILL.md"
copy_if_different "$SCRIPT_DIR/.claude/skills/orchestrate/SKILL.md" "$TARGET_DIR/.claude/skills/orchestrate/SKILL.md"
copy_if_different "$SCRIPT_DIR/.ai/workflow/agents-block.md" "$TARGET_DIR/.ai/workflow/agents-block.md"
copy_if_different "$SCRIPT_DIR/.ai/workflow/claude-workflow.md" "$TARGET_DIR/.ai/workflow/claude-workflow.md"

if [ "$INCLUDE_PACKETS" -eq 1 ]; then
  mkdir -p "$TARGET_DIR/.ai/packets"
  copy_if_different "$SCRIPT_DIR/.ai/packets/plan.md" "$TARGET_DIR/.ai/packets/plan.md"
  copy_if_different "$SCRIPT_DIR/.ai/packets/execute.md" "$TARGET_DIR/.ai/packets/execute.md"
  copy_if_different "$SCRIPT_DIR/.ai/packets/review.md" "$TARGET_DIR/.ai/packets/review.md"
  copy_if_different "$SCRIPT_DIR/.ai/packets/rescue.md" "$TARGET_DIR/.ai/packets/rescue.md"
else
  echo "Kept packets unchanged (.ai/packets/*)"
fi

PYTHON_CMD=""
if command -v python3 >/dev/null 2>&1; then
  PYTHON_CMD="python3"
elif command -v python >/dev/null 2>&1; then
  PYTHON_CMD="python"
else
  echo "Error: python3 or python is required but not found." >&2
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

def upsert_block(path: Path, start_marker: str, end_marker: str, block_text: str) -> None:
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

agents_path = target_dir / "AGENTS.md"
upsert_block(
    agents_path,
    "# >>> AI WORKFLOW MANAGED BLOCK >>>",
    "# <<< AI WORKFLOW MANAGED BLOCK <<<",
    agents_block,
)

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

CALL_CLAUDE_DIR="$HOME/.agents/skills/call-claude"
mkdir -p "$CALL_CLAUDE_DIR"
copy_if_different "$SCRIPT_DIR/.agents/skills/call-claude/SKILL.md" "$CALL_CLAUDE_DIR/SKILL.md"

echo ""
echo "Done."
echo "Updated shared workflow files in $TARGET_DIR."
if [ "$INCLUDE_PACKETS" -eq 0 ]; then
  echo "Packets were preserved. Use --include-packets if you want to refresh them too."
fi
