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
  - ~/.agents/skills/{orchestrate,orchestrate-agents,run-pipeline,synthesizer,planner,reviewer,maintenance,rescue,bootstrap,claude}/SKILL.md
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

# Shared helpers (copy_if_different, dir + mirror functions) and the single
# source of truth for skill lists. See lib/skills.manifest.
# shellcheck source=lib/workflow-lib.sh
source "$SCRIPT_DIR/lib/workflow-lib.sh"
MANIFEST="$SCRIPT_DIR/lib/skills.manifest"
mapfile -t SHARED_SKILLS < <(read_skill_group "$MANIFEST" shared)
mapfile -t BRIDGE_SKILLS < <(read_skill_group "$MANIFEST" codex-bridge)

echo "Updating AI workflow in: $TARGET_DIR"

mkdir -p "$TARGET_DIR/.ai/workflow"
mkdir -p "$TARGET_DIR/.ai/dashboard"
# Per-skill scaffold dirs, driven by the manifest (shared list + claude-only +
# codex-bridge). Same helper install.sh uses.
ensure_skill_dirs "$TARGET_DIR" "${SHARED_SKILLS[@]}"

# Shared workflow skills to refresh in downstream projects. List from the manifest.
for skill in "${SHARED_SKILLS[@]}"; do
  copy_if_different "$SCRIPT_DIR/.claude/skills/$skill/SKILL.md" "$TARGET_DIR/.claude/skills/$skill/SKILL.md"
done

# Claude-only `codex` skill (the runner; no Codex mirror — see manifest).
copy_if_different "$SCRIPT_DIR/.claude/skills/codex/SKILL.md" "$TARGET_DIR/.claude/skills/codex/SKILL.md"

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
for skill in "${BRIDGE_SKILLS[@]}"; do
  copy_if_different "$SCRIPT_DIR/.agents/skills/$skill/SKILL.md" "$TARGET_DIR/.agents/skills/$skill/SKILL.md"
done

# Project-local mirror of shared skills: .claude/skills/<name>/ -> .agents/skills/<name>/.
mirror_shared_skills_project "$TARGET_DIR" "${SHARED_SKILLS[@]}"

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

# Dashboard tool — keep in sync
copy_if_different "$SCRIPT_DIR/.ai/dashboard/serve.py" "$TARGET_DIR/.ai/dashboard/serve.py"
copy_if_different "$SCRIPT_DIR/.ai/dashboard/index.html" "$TARGET_DIR/.ai/dashboard/index.html"
copy_if_different "$SCRIPT_DIR/.ai/dashboard/styles.css" "$TARGET_DIR/.ai/dashboard/styles.css"
mkdir -p "$TARGET_DIR/.ai/scripts"
copy_if_different "$SCRIPT_DIR/.ai/scripts/log_event.py" "$TARGET_DIR/.ai/scripts/log_event.py"
copy_if_different "$SCRIPT_DIR/.ai/scripts/todos_parser.py" "$TARGET_DIR/.ai/scripts/todos_parser.py"
copy_if_different "$SCRIPT_DIR/.ai/scripts/demo.py" "$TARGET_DIR/.ai/scripts/demo.py"
copy_if_different "$SCRIPT_DIR/.ai/scripts/pipeline_schema.py" "$TARGET_DIR/.ai/scripts/pipeline_schema.py"
copy_if_different "$SCRIPT_DIR/.ai/scripts/pipeline_fanout.py" "$TARGET_DIR/.ai/scripts/pipeline_fanout.py"
copy_if_different "$SCRIPT_DIR/.ai/scripts/auto_select_scorer.py" "$TARGET_DIR/.ai/scripts/auto_select_scorer.py"

# Ship every server/**/*.py so the serve.py decomposition package — split into
# domain sub-packages (jobs/, pty/, sessions/, improver/, transcripts/,
# analytics/, pipelines/, skills/, agent_suggest/) alongside flat foundation
# modules (paths, storage, validation, runtime, ws, llm_output, ...) —
# propagates without naming each file. serve.py does `from server.X import ...`
# and `from server.<domain>.Y import ...` on boot; a missing module hard-crashes
# the dashboard on launch. find recurses so sub-package files keep their relative
# path (copy_if_different mkdir -p's each nested destination).
mkdir -p "$TARGET_DIR/.ai/dashboard/server"
while IFS= read -r py_src; do
  rel="${py_src#"$SCRIPT_DIR/.ai/dashboard/server/"}"
  copy_if_different "$py_src" "$TARGET_DIR/.ai/dashboard/server/$rel"
done < <(find "$SCRIPT_DIR/.ai/dashboard/server" -type f -name '*.py')

# Clean up old top-level locations from pre-scripts/ layouts so old + new
# don't co-exist in upgraded projects.
rm -f "$TARGET_DIR/.ai/dashboard/log_event.py" \
      "$TARGET_DIR/.ai/dashboard/pty_session.py" \
      "$TARGET_DIR/.ai/dashboard/todos_parser.py" \
      "$TARGET_DIR/.ai/dashboard/demo.py"

# The .ai/dashboard/scripts/ helper folder was dissolved: server-only modules
# moved into .ai/dashboard/server/ and the shared workflow scripts moved into
# .ai/scripts/. Remove the stale copies from older installs so they can't
# shadow the new locations on sys.path.
for stale in pty_session session_registry session_lock _improver_transcript_policy \
             purge_stale_improver_transcripts log_event todos_parser demo \
             pipeline_schema pipeline_fanout auto_select_scorer; do
  rm -f "$TARGET_DIR/.ai/dashboard/scripts/$stale.py"
done
rmdir "$TARGET_DIR/.ai/dashboard/scripts" 2>/dev/null || true

# Glob every app/*.js so new modules (settings.js, auto-select.js, future ones)
# propagate without an explicit list to maintain. index.html references files
# by name — if any are missing, the dashboard silently 404s and dependent
# wirings (e.g. the workflow-check button) never bind.
for js_src in "$SCRIPT_DIR/.ai/dashboard/app/"*.js; do
  [ -f "$js_src" ] || continue
  copy_if_different "$js_src" "$TARGET_DIR/.ai/dashboard/app/$(basename "$js_src")"
done

# Glob every styles/*.css so split-out stylesheets (a sibling refactor splits the
# monolithic styles.css into .ai/dashboard/styles/*.css) propagate without naming
# each file. index.html @imports them by name — a missing file 404s silently.
mkdir -p "$TARGET_DIR/.ai/dashboard/styles"
for css_src in "$SCRIPT_DIR/.ai/dashboard/styles/"*.css; do
  [ -f "$css_src" ] || continue
  copy_if_different "$css_src" "$TARGET_DIR/.ai/dashboard/styles/$(basename "$css_src")"
done

# canvas.html is the standalone multi-pane canvas window (loaded in its own
# browser window, not by index.html). It lives under app/ but the *.js glob
# above is JS-only, so it needs an explicit copy or downstream installs 404 the
# canvas (the Terminals tab's send-to-canvas opens app/canvas.html).
copy_if_different "$SCRIPT_DIR/.ai/dashboard/app/canvas.html" "$TARGET_DIR/.ai/dashboard/app/canvas.html"

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

# Managed blocks, memory header + bullet migration, skeleton merges, settings
# merge and pre-refactor ledger/proposal migrations all live in
# lib/install_common.py (shared with install.sh).
"$PYTHON_CMD" "$SCRIPT_DIR/lib/install_common.py" update "$TARGET_DIR" "$SCRIPT_DIR"

# Global skill mirror for Codex: ~/.agents/skills/. Keep it in sync with the
# project-local skills so Codex can orchestrate, plan, review, etc. Lists from
# the manifest.
mirror_skills_to_home "$TARGET_DIR" "${SHARED_SKILLS[@]}" "${BRIDGE_SKILLS[@]}"

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
