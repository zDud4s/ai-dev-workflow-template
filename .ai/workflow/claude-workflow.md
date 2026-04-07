# AI workflow shared instructions

## Pipeline

1. **Triage**: planner classifies task size (trivial / small / medium / large)
2. **Plan**: planner produces execution packet(s) using `.ai/packets/` schemas
3. **Execute**: executor follows packet steps literally, fills Handoff section when done
4. **Review**: reviewer checks Handoff output (skip for trivial; optional for small unless risky)
5. **Maintain**: update `.ai/memory.md` and `.ai/decisions.md` with discoveries

## Roles

Role assignments are configured in `.ai/models.yaml`.
Default: plan=claude/sonnet-4-6, execute=codex/o4-mini, review=claude/opus-4-6

## Rules

1. If `project_name` in `.ai/project.yaml` is `unknown`, run bootstrap first.
2. Preserve existing repository instructions.
3. Do not rewrite the workflow core during bootstrap.
4. Do not implement product changes during bootstrap.
5. Use `.ai/project.yaml`, `.ai/memory.md`, and `.ai/decisions.md` as the mutable project layer.
6. Executor must fill the Handoff section of the execution packet before declaring done.
7. Prefer the smallest correct change.
8. Do not broaden scope silently.
