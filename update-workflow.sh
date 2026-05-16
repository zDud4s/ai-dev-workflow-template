#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  bash update-workflow.sh <target-path> [--include-packets]

What it updates by default:
  - .claude/skills/*
  - .ai/workflow/*
  - .ai/dashboard/*   (the local dashboard tool)
  - managed blocks in AGENTS.md and CLAUDE.md
  - ~/.agents/skills/{orchestrate,planner,reviewer,maintenance,rescue,bootstrap,codex,claude}/SKILL.md
    (global mirror so Codex can discover the same skills)

What it preserves by default:
  - .ai/packets/*   (must already exist — run install.sh first on new projects)
  - .ai/models.yaml
  - .ai/project.yaml
  - .ai/memory.md
  - .ai/decisions.md
  - .claude/settings.json   (may contain user hooks — never overwritten)

Options:
  --include-packets   Also update .ai/packets/* (creates them if missing)

Note: this script updates an existing install. For a new project, run install.sh first.
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
mkdir -p "$TARGET_DIR/.ai/dashboard"
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
copy_if_different "$SCRIPT_DIR/.ai/workflow/workflow.md" "$TARGET_DIR/.ai/workflow/workflow.md"
copy_if_different "$SCRIPT_DIR/.ai/workflow/dispatch.md" "$TARGET_DIR/.ai/workflow/dispatch.md"

# Dashboard tool — keep in sync
copy_if_different "$SCRIPT_DIR/.ai/dashboard/serve.py" "$TARGET_DIR/.ai/dashboard/serve.py"
copy_if_different "$SCRIPT_DIR/.ai/dashboard/index.html" "$TARGET_DIR/.ai/dashboard/index.html"
copy_if_different "$SCRIPT_DIR/.ai/dashboard/log_event.py" "$TARGET_DIR/.ai/dashboard/log_event.py"

PACKETS_STATE="kept"   # one of: kept | updated | missing
if [ "$INCLUDE_PACKETS" -eq 1 ]; then
  mkdir -p "$TARGET_DIR/.ai/packets"
  copy_if_different "$SCRIPT_DIR/.ai/packets/plan.md" "$TARGET_DIR/.ai/packets/plan.md"
  copy_if_different "$SCRIPT_DIR/.ai/packets/execute.md" "$TARGET_DIR/.ai/packets/execute.md"
  copy_if_different "$SCRIPT_DIR/.ai/packets/review.md" "$TARGET_DIR/.ai/packets/review.md"
  copy_if_different "$SCRIPT_DIR/.ai/packets/rescue.md" "$TARGET_DIR/.ai/packets/rescue.md"
  PACKETS_STATE="updated"
elif [ ! -d "$TARGET_DIR/.ai/packets" ]; then
  echo "Warning: $TARGET_DIR/.ai/packets/ does not exist. The workflow needs these schema files." >&2
  echo "         Run install.sh first, or re-run with --include-packets to install them now." >&2
  PACKETS_STATE="missing"
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
@.ai/workflow/workflow.md
<!-- <<< AI WORKFLOW MANAGED IMPORT <<< -->"""

def upsert_block(path: Path, start_marker: str, end_marker: str, block_text: str) -> None:
    if path.exists():
        content = path.read_text()
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
    path.write_text(new_content)

agents_path = target_dir / "AGENTS.md"
upsert_block(
    agents_path,
    "# >>> AI WORKFLOW MANAGED BLOCK >>>",
    "# <<< AI WORKFLOW MANAGED BLOCK <<<",
    agents_block,
)

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
PY

# Global skill mirror for Codex.
# Keep ~/.agents/skills/ in sync with project-local skills so Codex can
# orchestrate, plan, review, etc. when run from this project.
AGENTS_SKILLS_HOME="$HOME/.agents/skills"
mkdir -p "$AGENTS_SKILLS_HOME"

mirror_skill_to_home() {
  local src="$1"
  local name="$2"
  local dst_dir="$AGENTS_SKILLS_HOME/$name"
  mkdir -p "$dst_dir"
  copy_if_different "$src" "$dst_dir/SKILL.md"
}

for skill in bootstrap planner reviewer maintenance rescue codex orchestrate; do
  src="$SCRIPT_DIR/.claude/skills/$skill/SKILL.md"
  [ -f "$src" ] || { echo "Warning: missing $src — skipping mirror" >&2; continue; }
  mirror_skill_to_home "$src" "$skill"
done

# Codex-only skill — symmetric of .claude/skills/codex/. Source under .agents/skills/.
mirror_skill_to_home "$SCRIPT_DIR/.agents/skills/claude/SKILL.md" "claude"

echo ""
echo "Done."
echo "Updated shared workflow files in $TARGET_DIR."
case "$PACKETS_STATE" in
  kept)
    echo "Packets were preserved. Use --include-packets if you want to refresh them too."
    ;;
  updated)
    echo "Packets refreshed (--include-packets)."
    ;;
  missing)
    echo "Packets are still missing — re-run with --include-packets (or install.sh) to create them." >&2
    ;;
esac
