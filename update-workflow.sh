#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  bash update-workflow.sh <target-path> [--include-packets]

What it updates by default:
  - .claude/skills/*
  - .agents/skills/*  (mirror of .claude/skills/* + the codex-only claude skill)
  - .ai/workflow/*
  - .ai/dashboard/*   (the local dashboard tool)
  - managed blocks in AGENTS.md and CLAUDE.md
  - .ai/memory.md, .ai/decisions.md         (skeleton merge — adds missing ## sections; entries preserved)
  - .ai/project.yaml, .ai/models.yaml       (skeleton merge — adds missing top-level keys; values preserved)
  - ~/.agents/skills/{orchestrate,planner,reviewer,maintenance,rescue,bootstrap,codex,claude}/SKILL.md
    (global mirror so Codex can discover the same skills)

What it preserves by default:
  - .ai/packets/*   (must already exist — run install.sh first on new projects)
  - existing memory entries, decisions, project values, model assignments
    (only the template scaffold around them is merged in)
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
mkdir -p "$TARGET_DIR/.claude/skills/agent-improver/references"
mkdir -p "$TARGET_DIR/.claude/skills/agent-creator/references"
mkdir -p "$TARGET_DIR/.agents/skills/bootstrap"
mkdir -p "$TARGET_DIR/.agents/skills/planner"
mkdir -p "$TARGET_DIR/.agents/skills/reviewer"
mkdir -p "$TARGET_DIR/.agents/skills/maintenance"
mkdir -p "$TARGET_DIR/.agents/skills/rescue"
mkdir -p "$TARGET_DIR/.agents/skills/orchestrate"
mkdir -p "$TARGET_DIR/.agents/skills/claude"

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

# Claude-only `agent-improver` skill (audits .claude/agents/*.md; no Codex counterpart).
# Has bundled reference files alongside SKILL.md.
copy_if_different "$SCRIPT_DIR/.claude/skills/agent-improver/SKILL.md" "$TARGET_DIR/.claude/skills/agent-improver/SKILL.md"
copy_if_different "$SCRIPT_DIR/.claude/skills/agent-improver/references/quality-criteria.md" "$TARGET_DIR/.claude/skills/agent-improver/references/quality-criteria.md"
copy_if_different "$SCRIPT_DIR/.claude/skills/agent-improver/references/agent-template.md" "$TARGET_DIR/.claude/skills/agent-improver/references/agent-template.md"

# Claude-only `agent-creator` skill (creates .claude/agents/*.md after approval).
# Has a bundled reference template alongside SKILL.md.
copy_if_different "$SCRIPT_DIR/.claude/skills/agent-creator/SKILL.md" "$TARGET_DIR/.claude/skills/agent-creator/SKILL.md"
copy_if_different "$SCRIPT_DIR/.claude/skills/agent-creator/references/agent-template.md" "$TARGET_DIR/.claude/skills/agent-creator/references/agent-template.md"

# Codex-only `claude` skill (no Claude counterpart) — source under .agents/skills/.
copy_if_different "$SCRIPT_DIR/.agents/skills/claude/SKILL.md" "$TARGET_DIR/.agents/skills/claude/SKILL.md"

# Project-local mirror of shared skills: .claude/skills/<name>/ -> .agents/skills/<name>/.
# .claude/skills/ is the source of truth; direct edits in .agents/skills/<shared>/ get overwritten.
# `codex` is excluded: Codex does not need a skill describing how to invoke itself.
for skill in bootstrap planner reviewer maintenance rescue orchestrate; do
  copy_if_different "$TARGET_DIR/.claude/skills/$skill/SKILL.md" "$TARGET_DIR/.agents/skills/$skill/SKILL.md"
done

copy_if_different "$SCRIPT_DIR/.ai/workflow/agents-block.md" "$TARGET_DIR/.ai/workflow/agents-block.md"
copy_if_different "$SCRIPT_DIR/.ai/workflow/workflow.md" "$TARGET_DIR/.ai/workflow/workflow.md"
copy_if_different "$SCRIPT_DIR/.ai/workflow/dispatch.md" "$TARGET_DIR/.ai/workflow/dispatch.md"

# Pre-rename leftover from older installs (claude-workflow.md -> workflow.md).
if [ -f "$TARGET_DIR/.ai/workflow/claude-workflow.md" ]; then
  rm -f "$TARGET_DIR/.ai/workflow/claude-workflow.md"
  echo "Removed stale $TARGET_DIR/.ai/workflow/claude-workflow.md (renamed to workflow.md)"
fi

# Dashboard tool — keep in sync
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
import re
import sys

target_dir = Path(sys.argv[1])
script_dir = Path(sys.argv[2])

agents_block = (script_dir / ".ai/workflow/agents-block.md").read_text(encoding="utf-8")
claude_import_block = """<!-- >>> AI WORKFLOW MANAGED IMPORT >>> -->
@.ai/workflow/workflow.md
<!-- <<< AI WORKFLOW MANAGED IMPORT <<< -->"""

def upsert_block(path: Path, start_marker: str, end_marker: str, block_text: str) -> None:
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

# --- Skeleton merge for project state files ---
# Goal: when the template grows a new ## section in memory.md/decisions.md or a
# new top-level key in project.yaml/models.yaml, propagate it to the target
# WITHOUT touching entries/values the user wrote. Strictly additive: missing
# scaffold appended at end; existing content untouched.

MD_SECTION_RE = re.compile(r'(?m)^(## .+)$')
YAML_TOPLEVEL_RE = re.compile(r'^([A-Za-z_][\w-]*):')
DATED_ENTRY_RE = re.compile(r'^- \d{4}-\d{2}-\d{2}\s')

def strip_dated_entries(body):
    # Template ships its own dev memory under sections like "## Entries". When
    # we add that section to a downstream project, the heading/scaffold travels
    # but the dated entries (per-project content) must not. Drop lines matching
    # `- YYYY-MM-DD ...` and collapse the blank-line runs left behind.
    kept = [
        line for line in body.splitlines(keepends=True)
        if not DATED_ENTRY_RE.match(line)
    ]
    return re.sub(r'\n{3,}', '\n\n', "".join(kept))

def md_top_level_sections(text):
    parts = MD_SECTION_RE.split(text)
    sections = []
    for i in range(1, len(parts), 2):
        heading = parts[i]
        body = parts[i + 1] if i + 1 < len(parts) else ""
        sections.append((heading, body))
    return sections

def yaml_top_level_blocks(text):
    # Comments/blank lines immediately preceding a NON-FIRST top-level key are
    # attached to THAT key (its leading doc-comment travels with it on merge).
    # File-leading comments (before the first key) are file preamble and dropped
    # from all blocks — otherwise we'd duplicate the header on merge.
    blocks = []
    current_key = None
    current_lines = []
    pending = []
    seen_any_key = False
    for line in text.splitlines(keepends=True):
        m = YAML_TOPLEVEL_RE.match(line)
        if m:
            if current_key is not None:
                blocks.append((current_key, "".join(current_lines)))
            current_key = m.group(1)
            current_lines = ([line] if not seen_any_key else pending + [line])
            pending = []
            seen_any_key = True
        elif line.strip() == "" or line.lstrip().startswith("#"):
            pending.append(line)
        elif current_key is not None:
            current_lines.extend(pending)
            current_lines.append(line)
            pending = []
        else:
            pending = []
    if current_key is not None:
        current_lines.extend(pending)
        blocks.append((current_key, "".join(current_lines)))
    return blocks

def append_with_blank_line(target_text, addition):
    if not target_text:
        return addition
    out = target_text.rstrip("\n") + "\n\n"
    return out + addition

def merge_md_skeleton(template_path, target_path, label):
    if not target_path.exists():
        return
    template_text = template_path.read_text(encoding="utf-8")
    target_text = target_path.read_text(encoding="utf-8")
    target_headings = {h.strip() for h, _ in md_top_level_sections(target_text)}
    missing = [
        (h, b) for h, b in md_top_level_sections(template_text)
        if h.strip() not in target_headings
    ]
    if not missing:
        print(f"Kept {target_path} ({label}, no missing ## sections)")
        return
    addition = "".join(h + strip_dated_entries(b) for h, b in missing).rstrip("\n") + "\n"
    target_path.write_text(
        append_with_blank_line(target_text, addition),
        encoding="utf-8", newline="\n",
    )
    added = ", ".join(h.strip().lstrip("#").strip() for h, _ in missing)
    print(f"Merged {target_path} ({label}, added sections: {added})")

def merge_yaml_skeleton(template_path, target_path, label):
    if not target_path.exists():
        return
    template_text = template_path.read_text(encoding="utf-8")
    target_text = target_path.read_text(encoding="utf-8")
    target_keys = {k for k, _ in yaml_top_level_blocks(target_text)}
    missing = [
        (k, b) for k, b in yaml_top_level_blocks(template_text)
        if k not in target_keys
    ]
    if not missing:
        print(f"Kept {target_path} ({label}, no missing top-level keys)")
        return
    addition = ""
    for _, b in missing:
        addition += b if b.endswith("\n") else b + "\n"
    target_path.write_text(
        append_with_blank_line(target_text, addition),
        encoding="utf-8", newline="\n",
    )
    added = ", ".join(k for k, _ in missing)
    print(f"Merged {target_path} ({label}, added top-level keys: {added})")

merge_md_skeleton(
    script_dir / ".ai/memory.md",
    target_dir / ".ai/memory.md",
    "memory",
)
merge_md_skeleton(
    script_dir / ".ai/decisions.md",
    target_dir / ".ai/decisions.md",
    "decisions",
)
merge_yaml_skeleton(
    script_dir / ".ai/project.yaml",
    target_dir / ".ai/project.yaml",
    "project state",
)
merge_yaml_skeleton(
    script_dir / ".ai/models.yaml",
    target_dir / ".ai/models.yaml",
    "models config",
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

for skill in bootstrap planner reviewer maintenance rescue orchestrate claude; do
  src="$TARGET_DIR/.agents/skills/$skill/SKILL.md"
  [ -f "$src" ] || { echo "Warning: missing $src — skipping mirror" >&2; continue; }
  mirror_skill_to_home "$src" "$skill"
done

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
