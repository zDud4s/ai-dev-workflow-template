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
mkdir -p "$TARGET_DIR/.claude/skills/orchestrate-agents"
mkdir -p "$TARGET_DIR/.claude/skills/orchestrate-tdd"
mkdir -p "$TARGET_DIR/.claude/skills/run-pipeline"
mkdir -p "$TARGET_DIR/.claude/skills/synthesizer"
mkdir -p "$TARGET_DIR/.claude/skills/agent-improver/references"
mkdir -p "$TARGET_DIR/.claude/skills/agent-creator/references"
mkdir -p "$TARGET_DIR/.claude/agents"
mkdir -p "$TARGET_DIR/.agents/skills/bootstrap"
mkdir -p "$TARGET_DIR/.agents/skills/planner"
mkdir -p "$TARGET_DIR/.agents/skills/reviewer"
mkdir -p "$TARGET_DIR/.agents/skills/maintenance"
mkdir -p "$TARGET_DIR/.agents/skills/rescue"
mkdir -p "$TARGET_DIR/.agents/skills/orchestrate"
mkdir -p "$TARGET_DIR/.agents/skills/orchestrate-agents"
mkdir -p "$TARGET_DIR/.agents/skills/orchestrate-tdd"
mkdir -p "$TARGET_DIR/.agents/skills/run-pipeline"
mkdir -p "$TARGET_DIR/.agents/skills/synthesizer"
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

# Agent-orchestration skills: orchestrate-agents drafts a pipeline YAML (workflow.md
# links to it as the non-code entry point), run-pipeline executes a saved pipeline,
# synthesizer folds agent outputs into a Handoff. Like the phase skills these are
# mirrored into .agents/skills/ (project + global) below for Codex discovery.
copy_if_missing "$SCRIPT_DIR/.claude/skills/orchestrate-agents/SKILL.md" "$TARGET_DIR/.claude/skills/orchestrate-agents/SKILL.md"
copy_if_missing "$SCRIPT_DIR/.claude/skills/orchestrate-tdd/SKILL.md" "$TARGET_DIR/.claude/skills/orchestrate-tdd/SKILL.md"
copy_if_missing "$SCRIPT_DIR/.claude/skills/run-pipeline/SKILL.md" "$TARGET_DIR/.claude/skills/run-pipeline/SKILL.md"
copy_if_missing "$SCRIPT_DIR/.claude/skills/synthesizer/SKILL.md" "$TARGET_DIR/.claude/skills/synthesizer/SKILL.md"

# Claude-only `agent-improver` skill (audits .claude/agents/*.md; no Codex counterpart).
# Has bundled reference files alongside SKILL.md.
copy_if_missing "$SCRIPT_DIR/.claude/skills/agent-improver/SKILL.md" "$TARGET_DIR/.claude/skills/agent-improver/SKILL.md"
copy_if_missing "$SCRIPT_DIR/.claude/skills/agent-improver/references/quality-criteria.md" "$TARGET_DIR/.claude/skills/agent-improver/references/quality-criteria.md"
copy_if_missing "$SCRIPT_DIR/.claude/skills/agent-improver/references/agent-template.md" "$TARGET_DIR/.claude/skills/agent-improver/references/agent-template.md"

# Claude-only `agent-creator` skill (creates .claude/agents/*.md after approval).
# Has a bundled reference template alongside SKILL.md.
copy_if_missing "$SCRIPT_DIR/.claude/skills/agent-creator/SKILL.md" "$TARGET_DIR/.claude/skills/agent-creator/SKILL.md"
copy_if_missing "$SCRIPT_DIR/.claude/skills/agent-creator/references/agent-template.md" "$TARGET_DIR/.claude/skills/agent-creator/references/agent-template.md"

# Cross-tool dispatch skill `claude` lives only under .agents/skills/ (no .claude/skills/
# counterpart) — it describes how a non-Claude host (Codex) invokes Claude CLI.
copy_if_missing "$SCRIPT_DIR/.agents/skills/claude/SKILL.md" "$TARGET_DIR/.agents/skills/claude/SKILL.md"

# Project-local mirror of shared skills: .claude/skills/<name>/ -> .agents/skills/<name>/.
# Keeps Codex's view of skills visible in-repo alongside Claude's. Always synced
# from .claude/skills/ — edit there, not here. copy_if_different so customizations
# in .claude/skills/ propagate; direct edits to .agents/skills/<shared>/ are overwritten.
# `codex` is NOT mirrored: codex is the runner, it never invokes a "codex skill"
# to call itself. `claude` is excluded for the symmetric reason and because it
# has no .claude/skills/ counterpart (see copy_if_missing above).
for skill in bootstrap planner reviewer maintenance rescue orchestrate orchestrate-agents orchestrate-tdd run-pipeline synthesizer; do
  src_skill_dir="$TARGET_DIR/.claude/skills/$skill"
  [ -d "$src_skill_dir" ] || continue
  # Mirror EVERY file in the skill dir, not just SKILL.md, so a future
  # multi-file shared skill (references/, etc.) propagates into the .agents
  # mirror — matching sync_skills.py copy_skill()'s rglob behaviour. Mirroring
  # only SKILL.md would silently drop bundled files and the rglob-based
  # `sync_skills.py --check` would then flag the shell-installed copy as drift.
  find "$src_skill_dir" -type f | while IFS= read -r src_file; do
    rel="${src_file#"$src_skill_dir"/}"
    copy_if_different "$src_file" "$TARGET_DIR/.agents/skills/$skill/$rel"
  done
done

# Workflow core and packets — always update (immutable core)
copy_if_different "$SCRIPT_DIR/.ai/workflow/agents-block.md" "$TARGET_DIR/.ai/workflow/agents-block.md"
copy_if_different "$SCRIPT_DIR/.ai/workflow/workflow.md" "$TARGET_DIR/.ai/workflow/workflow.md"
copy_if_different "$SCRIPT_DIR/.ai/workflow/dispatch.md" "$TARGET_DIR/.ai/workflow/dispatch.md"
# Auto-select decision table — the planner requires it whenever models.yaml has
# auto_select.enabled: true, and README documents it as part of .ai/workflow/*.
copy_if_different "$SCRIPT_DIR/.ai/workflow/auto-models.md" "$TARGET_DIR/.ai/workflow/auto-models.md"

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
mkdir -p "$TARGET_DIR/.ai/dashboard/scripts"
copy_if_different "$SCRIPT_DIR/.ai/dashboard/scripts/log_event.py" "$TARGET_DIR/.ai/dashboard/scripts/log_event.py"
copy_if_different "$SCRIPT_DIR/.ai/dashboard/scripts/pty_session.py" "$TARGET_DIR/.ai/dashboard/scripts/pty_session.py"
copy_if_different "$SCRIPT_DIR/.ai/dashboard/scripts/todos_parser.py" "$TARGET_DIR/.ai/dashboard/scripts/todos_parser.py"
copy_if_different "$SCRIPT_DIR/.ai/dashboard/scripts/demo.py" "$TARGET_DIR/.ai/dashboard/scripts/demo.py"
copy_if_different "$SCRIPT_DIR/.ai/dashboard/scripts/_improver_transcript_policy.py" "$TARGET_DIR/.ai/dashboard/scripts/_improver_transcript_policy.py"
copy_if_different "$SCRIPT_DIR/.ai/dashboard/scripts/pipeline_schema.py" "$TARGET_DIR/.ai/dashboard/scripts/pipeline_schema.py"
copy_if_different "$SCRIPT_DIR/.ai/dashboard/scripts/pipeline_fanout.py" "$TARGET_DIR/.ai/dashboard/scripts/pipeline_fanout.py"
copy_if_different "$SCRIPT_DIR/.ai/dashboard/scripts/purge_stale_improver_transcripts.py" "$TARGET_DIR/.ai/dashboard/scripts/purge_stale_improver_transcripts.py"
copy_if_different "$SCRIPT_DIR/.ai/dashboard/scripts/session_registry.py" "$TARGET_DIR/.ai/dashboard/scripts/session_registry.py"
# Glob every app/*.js so new modules (settings.js, auto-select.js, future ones)
# propagate without an explicit list to maintain. index.html references files
# by name — if any are missing, the dashboard silently 404s and dependent
# wirings (e.g. the workflow-check button) never bind.
for js_src in "$SCRIPT_DIR/.ai/dashboard/app/"*.js; do
  [ -f "$js_src" ] || continue
  copy_if_different "$js_src" "$TARGET_DIR/.ai/dashboard/app/$(basename "$js_src")"
done

# Vendored third-party assets live in app/vendor/ (e.g. chart.umd.js, which the
# Analytics tab needs). The app/*.js glob above is top-level only and skips this
# subdirectory — without an explicit copy the Analytics charts 404 on Chart.js.
mkdir -p "$TARGET_DIR/.ai/dashboard/app/vendor"
for vendor_src in "$SCRIPT_DIR/.ai/dashboard/app/vendor/"*; do
  [ -f "$vendor_src" ] || continue
  copy_if_different "$vendor_src" "$TARGET_DIR/.ai/dashboard/app/vendor/$(basename "$vendor_src")"
done

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
    "<!-- >>> AI WORKFLOW MANAGED IMPORT >>> -->",
    "<!-- <<< AI WORKFLOW MANAGED IMPORT <<< -->",
    claude_import_block,
)

MEMORY_HEADER_START = "<!-- >>> WORKFLOW MANAGED MEMORY HEADER >>> -->"
MEMORY_HEADER_END = "<!-- <<< WORKFLOW MANAGED MEMORY HEADER <<< -->"

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
    if not target_path.exists():
        return
    content = target_path.read_text(encoding="utf-8")
    if MEMORY_HEADER_START in content and MEMORY_HEADER_END in content:
        head_end_pos = content.find(MEMORY_HEADER_END) + len(MEMORY_HEADER_END)
        header = content[:head_end_pos]
        body = content[head_end_pos:]
    else:
        header = ""
        body = content
    lines = body.splitlines(keepends=True)
    sections = []
    current = (None, [])
    for line in lines:
        if line.startswith("## "):
            sections.append(current)
            current = (line, [])
        else:
            current[1].append(line)
    sections.append(current)
    today = date.today().isoformat()
    migrated = []
    drained_sections = []
    removed_sections = []
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
        return
    if entries_idx is None:
        new_sections.insert(0, ("## Entries\n", ["\n"]))
        entries_idx = 0
    heading, body_lines = new_sections[entries_idx]
    has_content = any(l.strip() for l in body_lines)
    if has_content and body_lines and body_lines[-1].strip():
        body_lines.append("\n")
    if not has_content and not body_lines:
        body_lines.append("\n")
    body_lines.extend(migrated)
    new_sections[entries_idx] = (heading, body_lines)
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
    if not template_path.exists():
        return
    template_text = template_path.read_text(encoding="utf-8")
    if MEMORY_HEADER_START not in template_text or MEMORY_HEADER_END not in template_text:
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
    migrate_bullets_to_entries(target_path)

refresh_memory_header(
    script_dir / ".ai/memory.md",
    target_dir / ".ai/memory.md",
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
    try:
        target_data = json.loads(target_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, ValueError) as exc:
        # A malformed/empty user settings.json must not abort the whole
        # install under `set -euo pipefail` after files were already copied.
        # Warn and skip the merge so the operator can fix the file and re-run.
        print(f"WARNING: {target_path} is not valid JSON ({exc}); "
              f"skipping workflow settings merge — fix the file and re-run.")
        return
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

def upsert_gitignore(target_path):
    # The workflow footprint (.ai/, .claude/, .agents/) is machinery for THIS
    # developer's local agent runs — it should not land in the host project's
    # history. Upsert a managed block into the target's .gitignore so every
    # install keeps the workflow folders out of git. Idempotent: re-running
    # rewrites only the managed block and preserves the rest of the file.
    block = """# >>> AI WORKFLOW MANAGED >>>
# Installed by ai-dev-workflow-template install.sh — keeps the workflow
# footprint out of this project's git history.
.ai/
.claude/
.agents/
# <<< AI WORKFLOW MANAGED <<<"""
    existed = target_path.exists()
    had_block = False
    if existed:
        content = target_path.read_text(encoding="utf-8")
        had_block = (
            "# >>> AI WORKFLOW MANAGED >>>" in content
            and "# <<< AI WORKFLOW MANAGED <<<" in content
        )
    upsert_block(
        target_path,
        "# >>> AI WORKFLOW MANAGED >>>",
        "# <<< AI WORKFLOW MANAGED <<<",
        block,
    )
    if not existed:
        print(f"Created {target_path} (workflow footprint ignore block)")
    elif had_block:
        print(f"Refreshed {target_path} (workflow footprint ignore block)")
    else:
        print(f"Updated {target_path} (added workflow footprint ignore block)")

upsert_gitignore(target_dir / ".gitignore")
PY

# Global skill mirror for Codex.
# Codex scans ~/.agents/skills/ exclusively (no project-local discovery), so every
# workflow skill must be mirrored there. Source for the mirror is the project's own
# .agents/skills/ (which itself was synced from .claude/skills/ above for shared
# phase skills, plus the cross-tool dispatch skills which are their own source of
# truth), so user customizations in the project propagate to the global discovery
# path.
AGENTS_SKILLS_HOME="$HOME/.agents/skills"
mkdir -p "$AGENTS_SKILLS_HOME"

mirror_skill_to_home() {
  local src="$1"
  local name="$2"
  local dst_dir="$AGENTS_SKILLS_HOME/$name"
  mkdir -p "$dst_dir"
  copy_if_different "$src" "$dst_dir/SKILL.md"
}

for skill in bootstrap planner reviewer maintenance rescue orchestrate orchestrate-agents orchestrate-tdd run-pipeline synthesizer claude; do
  src="$TARGET_DIR/.agents/skills/$skill/SKILL.md"
  [ -f "$src" ] || { echo "Warning: missing $src — skipping mirror" >&2; continue; }
  mirror_skill_to_home "$src" "$skill"
done

# Codex global config — ensure approval_policy allows --full-auto to work
# approval_policy = "on-request" means Codex auto-approves all actions when --full-auto is passed
# Only adds the setting if not already present — never overwrites a user's existing policy
CODEX_CONFIG="$HOME/.codex/config.toml"
if [ -f "$CODEX_CONFIG" ]; then
  if ! grep -Eq '^[[:space:]]*approval_policy[[:space:]]*=' "$CODEX_CONFIG"; then
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
