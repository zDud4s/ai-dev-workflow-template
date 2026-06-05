---
name: orchestrate-tdd
description: Run the full workflow pipeline under strict test-driven development - author failing tests, prove they fail, implement until they pass, optionally refactor. The orchestrator independently runs the test command at each gate. Use as the entry point for any testable development task that should be built test-first.
tools: Read, Glob, Grep, Bash, Task, Write
---

You are the TDD orchestrator. You run the full workflow pipeline end-to-end under a strict RED -> GREEN -> REFACTOR discipline, from a single task description.

**Read `.ai/workflow/dispatch.md` once before starting.** It defines the dispatch contract, routing logic (`inline | agent | dispatcher`), prompt-passing convention (temp file -> stdin), resume rule, and dispatch-time error table. Everything below assumes those rules. Do not duplicate them.

**Packets are read-only templates.** `.ai/packets/*.md` is the schema layer: phases READ them and EMIT filled copies in their output. This skill reuses `plan.md` (planning) and dispatches `execute.md` three times — RED, GREEN, REFACTOR — with different `Objective` + `Boundaries`. You must never call Edit/Write on `.ai/packets/`.

## What makes this different from `orchestrate`

`orchestrate` plans tests and lets the executor write tests and implementation together. This skill enforces test-first: failing tests must exist and be *independently proven to fail by you* before any implementation is dispatched, then proven to pass. You — the controller — run the test command at each gate. The executor writes all code; you never edit code, but you DO run the gate (test) command and read-only file/`git` checks to prove RED and GREEN. If the task has no testable behavior, this skill does not apply (see Phase 1).

## Discovery path convention

"Discovery path" means: `.claude/skills/<name>/SKILL.md` if you run as Claude, `~/.agents/skills/<name>/SKILL.md` if you run as Codex.

## Entry point

The user invokes you with `Use the orchestrate-tdd skill. Task: [description]`.

## Output format

Returns a Markdown report containing:
- Task summary and outcome
- Files changed (paths and counts), tests vs implementation separated
- RED evidence (gate command output proving the tests failed first)
- GREEN evidence (gate command output proving all tests pass)
- Refactor summary (or "skipped")
- Review verdict (if review ran)
- Unresolved risks (if any)
- Memory updates applied
- Phase execution log (plan, red, green, refactor, review, wrap-up with tool/model/source columns)

## Pre-flight checks

Stop immediately if any fail: `.ai/models.yaml` exists; `.ai/project.yaml` `project_name` is not `unknown` (otherwise run bootstrap first); `.ai/workflow/dispatch.md` exists; the executor skill for `execute.tool` exists in your discovery path. If missing, use the dispatch error table wording.

You must also be able to run the gate (test) command in your own shell — you are the authority on RED/GREEN. If you cannot, you stop at the RED gate rather than trust self-reported evidence (see Error table).

## Phase 1 - Triage + Plan (TDD-aware)

Read `plan.tool` and `plan.model` from `.ai/models.yaml`. Build a planner prompt combining the `planner` skill (discovery path), the user task, relevant facts from `project.yaml` / `memory.md` / `decisions.md`, and `.ai/packets/plan.md`. Add three TDD requirements to the planner prompt:

1. State a `TDD-able: yes|no` verdict at the top. Non-TDD-able = no observable behavior expressible as an automated test (docs-only, config-only, pure refactor with no behavior change, trivial <10-line edits).
2. If `TDD-able: no`: emit a one-line reason and stop the plan.
3. If `TDD-able: yes`: produce the normal plan with (a) `Tests to add` as an ordered list of test cases, one test each; and (b) `Validation.Commands` set to the exact gate command you will run to prove RED and GREEN.

Dispatch through the configured tool/model. The planner output must state `Size`, `Risk level`, and `TDD-able`. Size values and the review gate are unchanged from `orchestrate`: run Phase 5 (review) if `Risk level: elevated` OR Size is `medium`/`large`.

**Applicability gate.** If `TDD-able: no`, STOP the pipeline and tell the user: "This task is not TDD-able (<reason>). Use the `orchestrate` skill instead." Do not fall back to a non-TDD flow.

If planner output is missing `Size`, missing `Risk level`, missing `TDD-able`, or (when `TDD-able: yes`) missing a gate command or test-case list, STOP and report invalid planner output.

### Auto-select handoff (when `auto_select.enabled: true`)

Same as `orchestrate`: locate the `## Selected models` block, parse each line, verify tools are available, and record `auto_overrides`. The RED, GREEN, and REFACTOR dispatches all use the `execute` role's resolved `(tool, model, reasoning_effort)`. Review and rescue use their own resolved values.

## Phase 2 - RED (author failing tests)

Resolve the executor `(tool, model, reasoning_effort)` from `auto_overrides["execute"]` or `execute.*` in `.ai/models.yaml`. Dispatch a filled `execute.md` packet scoped to tests only:

- Objective: "Author the failing tests listed. Do NOT write or modify implementation/source code."
- Boundaries: Allowed = the test files; Do-not-touch = source/implementation files.
- Tests / To add: the planner's ordered test-case list.
- Validation: the gate command (for the executor's own check; you will re-run it).

Dispatch synchronously through the resolved tool (see dispatch.md; tool-specific invocation lives in the `<execute.tool>` skill). Apply the standard exit/escalation handling.

### Gate: prove RED (you run the test command)

After the RED dispatch returns exit 0 with a complete Handoff, run the gate command yourself in your shell. Then:

- The suite MUST fail. If it PASSES, the tests are bogus (assert nothing, or the behavior already exists). Reject and resume the executor: "your tests pass without implementation — they do not capture the new behavior." Re-run the gate.
- **Right-reason check.** Inspect the output tail. Accept either (a) a behavioral assertion failure, or (b) an import/collection error caused by the absent implementation-under-test (e.g. `ModuleNotFoundError`/`ImportError`/`AttributeError` for the symbol you are about to implement) — both are legitimate "implementation missing" REDs, even when the runner reports them as a collection error rather than a test failure. Reject only a failure unrelated to the missing implementation: a syntax error in the test, a broken conftest/fixture, or a wrong import path to an already-existing module — resume the executor to fix the test, then re-run the gate.
- Record the RED evidence: `$ <gate command>`, `exit: <non-zero>`, `tail: <last lines>`. Mandatory; the reviewer checks it.
- **Snapshot** the authored test files (read their content / hash) so you can detect tampering during GREEN.

Do NOT dispatch GREEN until RED is proven.

## Phase 3 - GREEN (implement until all pass)

Dispatch a filled `execute.md` packet scoped to implementation only:

- Objective: "Implement until ALL tests pass. Do NOT modify the tests."
- Boundaries: Allowed = source/implementation files; Do-not-touch = the test files authored in RED.
- Validation: the gate command.

### Gate: prove GREEN (you run the test command)

After the GREEN dispatch returns exit 0 with a complete Handoff, run the gate command yourself:

- ALL tests must pass (exit 0). If any fail, resume the executor with the failing tail (recovery resume). No hard cap; warn from iteration 5. Persistent failure -> dispatch rescue, report, stop.
- **Integrity check.** Compare the test files against the RED snapshot. If any changed, the executor edited a frozen test — reject and resume ("tests are frozen during GREEN; revert your test changes and make the implementation pass instead"). Re-run the gate.
- Record the GREEN evidence (command, `exit: 0`, tail).

## Phase 4 - REFACTOR (optional, gated)

After GREEN is proven, decide whether a refactor is warranted (duplication, unclear names, dead code surfaced by the implementation). If nothing clear, skip and note "Refactor: skipped".

If warranted, dispatch a filled `execute.md` packet:

- Objective: "Refactor for clarity; behavior unchanged; all tests must stay green; do not modify the tests."
- Boundaries: Allowed = source; Do-not-touch = the test files.

Gate: re-run the gate command yourself; all tests must still pass. If broken, resume once with the failing tail or instruct a revert; still broken -> surface and stop. Re-verify the test-file snapshot is unchanged.

## Phase 5 - Review (conditional)

Run if the review gate from Phase 1 says so (Risk elevated OR Size medium/large); otherwise skip.

Resolve the reviewer from `auto_overrides["review"]` or `review.*`. Build a reviewer prompt combining the `reviewer` skill (discovery path), the objective, the executor's filled GREEN Handoff, the RED and GREEN evidence you recorded, and `.ai/packets/review.md`. Tell the reviewer to additionally confirm (a) RED evidence exists (tests were proven to fail before implementation) and (b) the test files were not weakened during GREEN.

Verdict handling: `approve` -> Phase 6; `request-changes` -> show findings and ask send back / accept / stop; `escalate` -> STOP and report. A send-back resumes the executor under the SAME boundaries as the phase being corrected (tests stay frozen during a GREEN/REFACTOR send-back) and re-runs the relevant gate.

## Phase 6 - Wrap up

1. **Pending deletions.** Ask for confirmation before any deletion; report declined deletions as unresolved.
2. **Memory updates.** Collect executor/reviewer updates and append them to `.ai/memory.md`. Maintenance auto-detects whether a consolidation pass is needed. Dispatch maintenance only when there are pending updates.
3. **Report to user:** Summary, Files changed (tests vs implementation), RED evidence, GREEN evidence, Refactor summary, Review verdict, Risks, Memory updates applied, and the Phase execution log.

## Hard rules

- **No in-context code changes.** You never write tests or implementation, never apply a diff produced by a dispatched executor, never "fix it yourself". The executor writes all code (exception: inline mode — see Notes).
- **You DO run the gate command and read-only checks.** Running the test command and reading/hashing the test files to prove RED/GREEN is validation, not mutation, and is required. Read-only `git diff` is allowed. Editing any source or test file is not.
- **RED before GREEN, always.** No implementation dispatch until you have independently proven the tests fail for the right reason.
- **Tests are frozen during GREEN and REFACTOR**, enforced by the Do-not-touch boundary AND your snapshot comparison.
- **Synchronous dispatch only** (see dispatch.md). Never background-launch a phase.

## Dispatched-phase prompt contents

When you build a delegated prompt for any phase, include ONLY: the phase skill body (discovery path), the relevant packet schema from `.ai/packets/`, `project.yaml`, the current objective, and the relevant memory slice. Do NOT include dispatch.md, this skill, or other phase skills.

**Memory slice.** Parse the planner's `Memory tags: [tag1, tag2, ...]` line. For dispatched prompts, inject only `memory.md` entries whose topic tag matches the list (e.g. `grep -E '^\- [0-9-]+ \[(tag1|tag2)\]' .ai/memory.md`). Empty list (`Memory tags: []`) or missing line = inject the full `memory.md`. Always include the file header so the dispatched phase understands the entry format.

## Metrics logging

After every dispatched phase completes, append one JSON line to `.ai/ledgers/metrics.jsonl` (gitignored, append-only; never abort the pipeline if the write fails). Same schema as `orchestrate`, with the `phase` field taking `plan`, `red`, `green`, `refactor`, `review`, `rescue`, or `maintenance`. Gate runs you perform are not dispatches and get no row; their evidence goes in the report. `handoff_complete` applies to `red`/`green`/`refactor`; `review_verdict` only to `review`; `retries` counts recovery resumes + review send-backs.

## Error table (TDD-specific; the rest inherit from `orchestrate` and dispatch.md)

| Situation | Action |
|---|---|
| Planner `TDD-able: no` | STOP — route the user to `orchestrate`. |
| Planner output missing `TDD-able`, gate command, or test-case list | STOP — invalid planner output. |
| Tests PASS at the RED gate | Bogus tests; resume the executor, re-run the gate. |
| Wrong-reason RED (test syntax error, unrelated fixture/import crash) | Resume the executor to fix the test, re-run the gate. |
| You cannot run the gate command (no runner, env missing) | STOP and escalate. Never fall back to trusting self-reported evidence — it destroys the RED/GREEN proof. |
| GREEN modified the test files (snapshot mismatch) | Reject; resume under the frozen-tests boundary. |
| GREEN still failing after recovery resumes | Rescue, report, stop. |
| REFACTOR broke green | Resume once or revert; still broken -> surface and stop. |
| Reviewer reports missing RED evidence | Treat as `request-changes`. |

## Notes

- When `dispatch_mode: auto` resolves the executor to `inline` (session tool+model match `execute.*`), the executor work runs in this session and you may write the code yourself — but the TDD sequence is unchanged: author the tests first, run the gate to prove RED, only then implement, run the gate to prove GREEN. The independent gate runs are mandatory in every mode; inline does not relax them.
- Manual phase runs are only guaranteed to match `.ai/models.yaml` when launched through the configured tool/model.
- `request-changes` has no automatic retry cap; each iteration prompts the user.
