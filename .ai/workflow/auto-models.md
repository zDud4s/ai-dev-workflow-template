# Auto-model selection — decision table

Lookup table consumed by the `planner` skill when `auto_select.enabled: true` in [.ai/models.yaml](../models.yaml).

Input key:  `(phase, size, risk, effective_budget)`
Output:     `(tool, model, reasoning_effort?)` or `no_match` → orchestrator falls back to `models.yaml`. `reasoning_effort` applies to both claude (`--effort`) and codex (`--config model_reasoning_effort`); `max` is claude-only.

Budget upgrade rule: `effective_budget = configured_budget + 1` (one rung: `low→medium→high`, `high` stays) when `Risk level: elevated` OR `Size: large`. Triage matches against `effective_budget`, not the configured one.

## Table

Rows evaluated in order, first match wins. `*` matches any value.

| phase   | size    | risk     | budget  | tool   | model              | effort |
|---------|---------|----------|---------|--------|--------------------|--------|
| execute | small   | low      | low     | codex  | gpt-5.4-mini       | low    |
| execute | small   | low      | medium  | codex  | gpt-5.4            | medium |
| execute | small   | low      | high    | codex  | gpt-5.5            | medium |
| execute | small   | elevated | *       | codex  | gpt-5.5            | high   |
| execute | medium  | low      | low     | codex  | gpt-5.4            | medium |
| execute | medium  | low      | medium  | codex  | gpt-5.5            | medium |
| execute | medium  | low      | high    | codex  | gpt-5.5            | high   |
| execute | medium  | elevated | *       | codex  | gpt-5.5            | high   |
| execute | large   | *        | low     | codex  | gpt-5.5            | medium |
| execute | large   | *        | medium  | codex  | gpt-5.5            | high   |
| execute | large   | *        | high    | codex  | gpt-5.5            | xhigh  |
| review  | small   | low      | *       | claude | claude-sonnet-4-6  | low    |
| review  | medium  | low      | low     | claude | claude-sonnet-4-6  | low    |
| review  | medium  | low      | medium  | claude | claude-opus-4-6    | medium |
| review  | medium  | low      | high    | claude | claude-opus-4-7    | high   |
| review  | *       | elevated | *       | claude | claude-opus-4-7    | high   |
| review  | large   | *        | *       | claude | claude-opus-4-7    | high   |
| rescue  | *       | *        | *       | claude | claude-opus-4-7    | high   |

## Notes

- `effort` is consumed by both claude (`--effort`) and codex (`--config model_reasoning_effort`). Claude accepts `{low, medium, high, xhigh, max}`; codex accepts `{low, medium, high, xhigh}` — `max` is claude-only. Use `n/a` to keep the tool's default; the planner omits `reasoning_effort` from the emitted line in that case.
- The `trivial` size never reaches downstream phases — the planner emits `TRIVIAL: ...` and stops. No row needed.
- When no row matches a `(phase, size, risk, budget)` tuple, the planner omits the line for that phase and the orchestrator falls back to `models.yaml`.
- `plan`, `maintenance`, and `bootstrap` are intentionally NOT listed. They are always served from `.ai/models.yaml` regardless of `auto_select.enabled` — auto-selection is reserved for the per-task phases (`execute`, `review`, `rescue`) whose model choice the planner can vary.
