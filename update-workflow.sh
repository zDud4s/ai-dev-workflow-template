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
  - .claude/settings.json                   (merge — adds missing workflow permissions/hooks; user entries preserved)
  - ~/.agents/skills/{orchestrate,planner,reviewer,maintenance,rescue,bootstrap,claude}/SKILL.md
    (global mirror so Codex can discover the same skills; no `codex` skill — codex is the runner)

What it preserves by default:
  - .ai/packets/*   (must already exist — run install.sh first on new projects)
  - existing memory entries, decisions, project values, model assignments,
    custom Claude Code permissions/hooks (only the workflow scaffold is merged in)

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
mkdir -p "$TARGET_DIR/.claude/agents"
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

# Cross-tool dispatch skill `claude` lives only under .agents/skills/ (no .claude/skills/
# counterpart) — it describes how a non-Claude host (Codex) invokes Claude CLI.
copy_if_different "$SCRIPT_DIR/.agents/skills/claude/SKILL.md" "$TARGET_DIR/.agents/skills/claude/SKILL.md"

# Project-local mirror of shared skills: .claude/skills/<name>/ -> .agents/skills/<name>/.
# .claude/skills/ is the source of truth; direct edits in .agents/skills/<shared>/ get overwritten.
# `codex` is NOT mirrored: codex is the runner, it never invokes a "codex skill" to call
# itself. `claude` is excluded for the symmetric reason and because it has no
# .claude/skills/ counterpart (see copy_if_different above).
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
mkdir -p "$TARGET_DIR/.ai/dashboard/scripts"
copy_if_different "$SCRIPT_DIR/.ai/dashboard/scripts/log_event.py" "$TARGET_DIR/.ai/dashboard/scripts/log_event.py"
copy_if_different "$SCRIPT_DIR/.ai/dashboard/scripts/pty_session.py" "$TARGET_DIR/.ai/dashboard/scripts/pty_session.py"
copy_if_different "$SCRIPT_DIR/.ai/dashboard/scripts/todos_parser.py" "$TARGET_DIR/.ai/dashboard/scripts/todos_parser.py"
copy_if_different "$SCRIPT_DIR/.ai/dashboard/scripts/demo.py" "$TARGET_DIR/.ai/dashboard/scripts/demo.py"
# Clean up old top-level locations from pre-scripts/ layouts so old + new
# don't co-exist in upgraded projects.
rm -f "$TARGET_DIR/.ai/dashboard/log_event.py" \
      "$TARGET_DIR/.ai/dashboard/pty_session.py" \
      "$TARGET_DIR/.ai/dashboard/todos_parser.py" \
      "$TARGET_DIR/.ai/dashboard/demo.py"
# Glob every app/*.js so new modules (settings.js, auto-select.js, future ones)
# propagate without an explicit list to maintain. index.html references files
# by name — if any are missing, the dashboard silently 404s and dependent
# wirings (e.g. the workflow-check button) never bind.
for js_src in "$SCRIPT_DIR/.ai/dashboard/app/"*.js; do
  [ -f "$js_src" ] || continue
  copy_if_different "$js_src" "$TARGET_DIR/.ai/dashboard/app/$(basename "$js_src")"
done

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
from datetime import date
import json
import re
import sys
import unicodedata

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

MEMORY_HEADER_START = "<!-- >>> WORKFLOW MANAGED MEMORY HEADER >>> -->"
MEMORY_HEADER_END = "<!-- <<< WORKFLOW MANAGED MEMORY HEADER <<< -->"

# Common Portuguese -> English topic-tag mapping for slugifying section headings
# when migrating bullets. Unknown words pass through as-is.
PT_EN_TOPIC_MAP = {
    "comandos": "commands", "comando": "commands",
    "ambiente": "env", "encoding": "encoding",
    "estrutura": "apps", "apps": "apps",
    "convencoes": "conventions",
    "anomalias": "anomalies",
    "factos": "facts", "fatos": "facts",
    "anotacoes": "notes", "notas": "notes",
}
PT_STOPWORDS = {
    "de", "do", "da", "das", "dos", "o", "a", "os", "as",
    "um", "uma", "uns", "umas", "e", "para", "em", "no", "na",
    "nas", "nos", "com", "por", "ao",
}

def heading_to_topic_slug(heading):
    # "## Comandos confirmados" -> "commands"; first significant word, stripped
    # of diacritics, mapped through PT_EN_TOPIC_MAP when known.
    text = re.sub(r"^#+\s*", "", heading).strip()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    words = re.findall(r"[a-z]+", text.lower())
    words = [w for w in words if w not in PT_STOPWORDS]
    if not words:
        return "misc"
    first = words[0]
    return PT_EN_TOPIC_MAP.get(first, first)

def upsert_block_at_top(path, start_marker, end_marker, block_text):
    # Like upsert_block but places the block at the TOP of the file. When
    # markers don't exist (legacy file), discards the legacy preamble — i.e.
    # everything before the first `## ` heading — since the managed block
    # replaces it. If the file has no `## ` heading at all, the entire legacy
    # content is treated as preamble and replaced.
    block_text = block_text.strip()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(block_text + "\n", encoding="utf-8", newline="\n")
        return "created"
    content = path.read_text(encoding="utf-8")
    if start_marker in content and end_marker in content:
        before = content.split(start_marker)[0].rstrip()
        after = content.split(end_marker, 1)[1].lstrip()
        had_markers = True
    else:
        before = ""
        m = re.search(r"(?m)^## ", content)
        after = content[m.start():] if m else ""
        had_markers = False
    pieces = []
    if before:
        pieces.append(before)
    pieces.append(block_text)
    if after:
        pieces.append(after)
    new_content = "\n\n".join(pieces).rstrip() + "\n"
    if new_content == content:
        return "unchanged"
    path.write_text(new_content, encoding="utf-8", newline="\n")
    return "refreshed" if had_markers else "injected"

DATED_BULLET_RE = re.compile(r"^- (\d{4}-\d{2}-\d{2})\s+(\[[^\]]+\]\s+)?(.*)$")
BULLET_RE = re.compile(r"^- ")

def migrate_bullets_to_entries(target_path):
    # Move bullet lines living under non-Entries sections into the canonical
    # `## Entries` section. Dated bullets keep their date/topic; undated ones
    # get today's date and a [<section_slug>] tag derived from their original
    # section heading. Empty headings (no remaining bullets, no prose) are
    # removed. Headings inside the managed marker region are preserved as-is.
    if not target_path.exists():
        return
    content = target_path.read_text(encoding="utf-8")
    # Split off the managed header so we don't touch it during migration.
    if MEMORY_HEADER_START in content and MEMORY_HEADER_END in content:
        head_end_pos = content.find(MEMORY_HEADER_END) + len(MEMORY_HEADER_END)
        header = content[:head_end_pos]
        body = content[head_end_pos:]
    else:
        header = ""
        body = content
    # Walk body into sections keyed by their `## ` heading. The pre-section
    # area (text before first ##) is preserved as-is.
    lines = body.splitlines(keepends=True)
    sections = []  # [(heading or None, [content lines])]
    current = (None, [])
    for line in lines:
        if line.startswith("## "):
            sections.append(current)
            current = (line, [])
        else:
            current[1].append(line)
    sections.append(current)
    today = date.today().isoformat()
    migrated = []  # list of (target_topic, line_text) to append to ## Entries
    drained_sections = []  # headings whose bullets were moved
    removed_sections = []  # headings dropped entirely (now empty)
    new_sections = []
    entries_idx = None
    for i, (heading, body_lines) in enumerate(sections):
        if heading is None:
            new_sections.append((heading, body_lines))
            continue
        if heading.strip().lower() == "## entries":
            entries_idx = len(new_sections)
            new_sections.append((heading, body_lines))
            continue
        slug = heading_to_topic_slug(heading)
        kept_lines = []
        moved_count = 0
        for line in body_lines:
            if BULLET_RE.match(line):
                m = DATED_BULLET_RE.match(line.rstrip("\n"))
                if m:
                    # Already dated — keep its date/topic verbatim.
                    migrated.append(line if line.endswith("\n") else line + "\n")
                else:
                    rest = line[2:].rstrip("\n")
                    migrated.append(f"- {today} [{slug}] {rest}\n")
                moved_count += 1
            else:
                kept_lines.append(line)
        if moved_count > 0:
            drained_sections.append((heading.strip(), moved_count))
        non_blank = [l for l in kept_lines if l.strip()]
        if not non_blank:
            removed_sections.append(heading.strip())
            continue
        new_sections.append((heading, kept_lines))
    if not migrated and not removed_sections:
        return  # nothing to do
    # If target had no ## Entries, create one (placed where the markers' close
    # already sits — i.e., immediately after the header / first content gap).
    if entries_idx is None:
        new_sections.insert(0, ("## Entries\n", ["\n"]))
        entries_idx = 0
    # Append migrated bullets to ## Entries body (after existing entries).
    heading, body_lines = new_sections[entries_idx]
    # Ensure body ends with a blank line before appending if it has content.
    has_content = any(l.strip() for l in body_lines)
    if has_content and body_lines and body_lines[-1].strip():
        body_lines.append("\n")
    if not has_content and not body_lines:
        body_lines.append("\n")
    body_lines.extend(migrated)
    new_sections[entries_idx] = (heading, body_lines)
    # Rebuild body text.
    rebuilt = []
    for heading, body_lines in new_sections:
        if heading is not None:
            rebuilt.append(heading)
        rebuilt.extend(body_lines)
    new_body = "".join(rebuilt)
    new_content = (header + "\n\n" + new_body.lstrip("\n")).rstrip("\n") + "\n"
    target_path.write_text(new_content, encoding="utf-8", newline="\n")
    drained_desc = ", ".join(f"{h.lstrip('#').strip()} ({n})" for h, n in drained_sections)
    print(f"Migrated {len(migrated)} bullet(s) to ## Entries from: {drained_desc}")
    if removed_sections:
        removed_desc = ", ".join(h.lstrip("#").strip() for h in removed_sections)
        print(f"Removed empty heading(s): {removed_desc}")

def refresh_memory_header(template_path, target_path):
    # Refresh the managed memory.md header (preamble + `## Entries`) and then
    # migrate any stray bullets into `## Entries` so the structure matches the
    # template contract: bullets live ONLY under `## Entries`.
    if not template_path.exists():
        return
    template_text = template_path.read_text(encoding="utf-8")
    if MEMORY_HEADER_START not in template_text or MEMORY_HEADER_END not in template_text:
        # Template doesn't define markers — fall back to the additive merge.
        merge_md_skeleton(template_path, target_path, "memory")
        return
    block = template_text.split(MEMORY_HEADER_START, 1)[1]
    block = block.split(MEMORY_HEADER_END, 1)[0]
    block = f"{MEMORY_HEADER_START}{block}{MEMORY_HEADER_END}"
    result = upsert_block_at_top(target_path, MEMORY_HEADER_START, MEMORY_HEADER_END, block)
    if result == "created":
        print(f"Created {target_path} (workflow memory header)")
    elif result == "injected":
        print(f"Injected {target_path} managed header (no markers found before)")
    elif result == "refreshed":
        print(f"Refreshed {target_path} managed header")
    # else: unchanged — print nothing
    migrate_bullets_to_entries(target_path)

refresh_memory_header(
    script_dir / ".ai/memory.md",
    target_dir / ".ai/memory.md",
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
    # permissions.allow: union, preserve target order, append new ones.
    template_allow = (template_data.get("permissions") or {}).get("allow") or []
    target_perms = target_data.setdefault("permissions", {})
    target_allow = target_perms.setdefault("allow", [])
    target_allow_set = set(target_allow)
    for perm in template_allow:
        if perm not in target_allow_set:
            target_allow.append(perm)
            target_allow_set.add(perm)
            added_perms.append(perm)
    # hooks.<event>: per-matcher merge, per-command dedup.
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
