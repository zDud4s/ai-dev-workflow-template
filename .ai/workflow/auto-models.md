# Auto-model selection — decision table

Lookup table consumed by the `planner` skill when `auto_select.enabled: true` in [.ai/models.yaml](../models.yaml).

Input key:  `(phase, size, risk, effective_budget)`
Output:     `(tool, model, reasoning_effort?)` or `no_match` → orchestrator falls back to `models.yaml`.

See [.ai/specs/2026-05-17-auto-model-selection-design.md](../specs/2026-05-17-auto-model-selection-design.md) for the budget upgrade rule (elevated risk OR large size → effective budget = configured + 1).

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
| review  | small   | low      | *       | claude | claude-sonnet-4-6  | n/a    |
| review  | medium  | low      | low     | claude | claude-sonnet-4-6  | n/a    |
| review  | medium  | low      | medium  | claude | claude-opus-4-6    | n/a    |
| review  | medium  | low      | high    | claude | claude-opus-4-7    | n/a    |
| review  | *       | elevated | *       | claude | claude-opus-4-7    | n/a    |
| review  | large   | *        | *       | claude | claude-opus-4-7    | n/a    |
| rescue  | *       | *        | *       | claude | claude-opus-4-7    | n/a    |

## Notes

- `effort` is consumed only by codex. For claude rows it is `n/a` and the planner MUST omit `reasoning_effort` from the emitted line.
- The `trivial` size never reaches downstream phases — the planner emits `TRIVIAL: ...` and stops. No row needed.
- When no row matches a `(phase, size, risk, budget)` tuple, the planner omits the line for that phase and the orchestrator falls back to `models.yaml`.
