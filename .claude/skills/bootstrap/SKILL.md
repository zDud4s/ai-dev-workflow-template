---
name: bootstrap
description: Initialize workflow scaffolding for a repository. Trigger when onboarding a new repository or when project metadata is incomplete or stale; outputs stack analysis and scaffold configuration.
tools: Read, Glob, Grep, Bash, Edit, Write
---

You are the bootstrap skill.

Goal:
Adapt the workflow scaffold to the current repository without implementing product changes.

## You must

1. Detect the stack, package managers, and likely entrypoints.
2. Identify install, dev, build, test, lint, format, and typecheck commands. If a command cannot be confirmed from repo evidence, mark it as an assumption.
3. Identify major directories and assign rough ownership domains.
4. Identify risky areas, generated files, do-not-touch zones, and security-sensitive areas.
5. Update only:
   - AGENTS.md (only project-specific sections if needed) — edits MUST stay OUTSIDE the `# >>> AI WORKFLOW MANAGED BLOCK >>>` ... `# <<< AI WORKFLOW MANAGED BLOCK <<<` region. Read first; never replace or move the managed block. If the file doesn't exist, leave it for `install.sh` to create.
   - CLAUDE.md (only project-specific sections if needed) — edits MUST stay OUTSIDE the `<!-- >>> AI WORKFLOW MANAGED IMPORT >>> -->` ... `<!-- <<< AI WORKFLOW MANAGED IMPORT <<< -->` region. Same rules: read first, never replace, never duplicate the managed import.
   - `.ai/project.yaml` — write it as **valid YAML** following the rules in "Writing valid `.ai/project.yaml`" below. Ensure the `memory_tuning` block exists with defaults (`consolidation_threshold_lines: 150`, `floor: 50`, `ceiling: 300`, `last_ratios: []`, `last_consolidated_at: ""`). Leave existing values intact if the block is already populated.
   - `.ai/memory.md`
   - `.gitignore` — **only if `.gitignore` already exists at the project root.** Append the managed block below idempotently (skip if the start marker is already present). Never create a new `.gitignore`; if the project doesn't have one, leave it alone. Use `Read` to check for the marker first; use `Edit` (or append via `Write` after reading) — do not blindly overwrite.

     ```
     # >>> AI WORKFLOW INSTALL >>>
     # Scaffold installed by ai-dev-workflow-template install.sh
     .ai/
     .claude/skills/bootstrap/
     .claude/skills/planner/
     .claude/skills/reviewer/
     .claude/skills/maintenance/
     .claude/skills/rescue/
     .claude/skills/codex/
     .claude/skills/orchestrate/
     .claude/skills/agent-creator/
     .claude/skills/agent-improver/
     .agents/skills/bootstrap/
     .agents/skills/planner/
     .agents/skills/reviewer/
     .agents/skills/maintenance/
     .agents/skills/rescue/
     .agents/skills/orchestrate/
     .agents/skills/claude/
     # <<< AI WORKFLOW INSTALL <<<
     ```
6. Preserve the workflow role structure (planner, executor, reviewer). Do not hardcode tool or model names — those are configured in `.ai/models.yaml`.
   - The model has the ability to delegate execution to Codex CLI (via the `codex` skill) or to run the full orchestration pipeline (via the `orchestrate` skill). Mention this in `.ai/memory.md` so future sessions know these options are available.
7. Record uncertainty explicitly in `assumptions` and `unknowns`.
8. After filling `.ai/project.yaml`, prompt the user to review `.ai/models.yaml` and adjust tool/model assignments if the defaults do not match their setup.
9. If the project has clear subdirectories with distinct domains (e.g. `backend/`, `frontend/`, `packages/*`), create local AGENTS.md files for each with domain-specific constraints. Do NOT create subdirectory AGENTS.md files for projects without clear subdomain separation (CLI tools, single-purpose libraries, monoliths with flat structure). Track created files in `subdirectory_agents` in `.ai/project.yaml`.
10. Do not implement features or fixes.
11. Inform the model (via `.ai/memory.md`) that it can invoke the **`codex` skill** to run Codex CLI for implementation tasks, and the **`orchestrate` skill** to run the full plan→execute→review pipeline from a single prompt. These are the primary execution paths for delegating work.

## Writing valid `.ai/project.yaml`

`.ai/project.yaml` is parsed by the dashboard, the test suite, and every later
phase, so it MUST be valid YAML. The failure mode is always the same: free-text
descriptions get written into list fields and break the parser. Follow these rules:

- Every item in a list field (`stack`, `commands.*`, `entrypoints`,
  `important_dirs`, `important_files`, `ownership.*`, `boundaries.*`,
  `conventions.*`, `assumptions`, `unknowns`) is a single **plain string** — never
  a nested mapping.
- A bare `: ` (colon + space) inside an unquoted list item makes YAML parse it as a
  `{key: value}` mapping (or hard-error). If an item must contain a colon,
  **single-quote the whole item**:
  `- 'phase-pinned dispatch: every phase runs in its pinned model'`.
- Never put text after a quoted string on the same line — `- "foo" (bar)` is a hard
  parse error (`bad indentation of a mapping entry`). Quote the whole item instead:
  `- 'foo (bar)'`.
- For annotations use a trailing ` # comment` (with a space before `#`), not a colon.
- `boundaries.generated_files` is the **host project's** generated files (build/dist
  output, codegen, vendored bundles) — discovered per project. Do NOT list the
  workflow's own runtime (`.ai/local/`, `.agents/skills`, `.ai/TODO.md`): the install
  `.gitignore` block already ignores those wholesale. Leave `[]` if the project has none.

### Validate before finishing (mandatory)

After writing `.ai/project.yaml`, prove it parses and that every list entry is a
plain string. Do not declare done until this exits 0:

```bash
python - <<'PY'
import sys, yaml
d = yaml.safe_load(open('.ai/project.yaml', encoding='utf-8'))
bad = []
def check(seq, path):
    if isinstance(seq, list):
        for i, x in enumerate(seq):
            if not isinstance(x, (str, type(None))):
                bad.append(f'{path}[{i}]={x!r}')
for k, v in (d or {}).items():
    check(v, k)
    if isinstance(v, dict):
        for k2, v2 in v.items():
            check(v2, f'{k}.{k2}')
sys.exit('STRAY COLON / non-string list items: ' + '; '.join(bad) if bad else 0)
PY
```

If it errors or reports items, fix the offending line(s) (usually a stray `: `) and
re-run until clean. A `SyntaxError`/parse exception means the YAML is malformed;
non-string items mean a list entry was read as a mapping.

## Token budget

Bootstrap output ≤150 lines.

## Output format

The skill returns a **markdown report** with the following sections:

- Detected stack
- Commands (confirmed vs assumed)
- Important dirs/files
- Risks and boundaries
- Subdirectory AGENTS.md files created (if any)
- Updated files (note explicitly whether `.gitignore` was touched or skipped because it was absent)
- Assumptions
- Unknowns