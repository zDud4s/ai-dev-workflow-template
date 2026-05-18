# Packet Templates

## What packets are

Packet files in this directory are read-only templates. Workflow phases READ these schemas and EMIT filled copies in their own output; filled packets flow between phases through temporary files as described in `.ai/workflow/dispatch.md`.

Do not edit a packet during a normal task. Only workflow-evolution tasks may change these templates.

The packet templates belong to the workflow Layer model described in `.ai/workflow/workflow.md`.

## Per-file role

`plan.md` defines the planning output schema. The planner uses it for small and larger tasks to capture scope, acceptance criteria, risk, validation, and delegable execution packets.

`execute.md` defines the executor input and handoff schema. The orchestrator sends a filled execution packet to the executor, and the executor returns the completed Handoff in its output.

`review.md` defines the reviewer verdict schema. The reviewer uses it to check validation evidence, scope, tests, risks, and whether the work should be approved, changed, or escalated.

`rescue.md` defines the rescue schema. Use it after repeated failure or drift to identify wrong assumptions, reopen the narrowest scope, and propose one safer experiment.

## Escalation block format

A dispatched phase that cannot proceed MUST emit this block as final output and exit non-zero.

```text
## Escalation
reason: why the phase cannot continue.
needed: the missing decision, file, context, tool, or access.
suggested-next: the smallest next action for the orchestrator or user.
partial-output: any useful work completed before the blocker.
```

Keep each field concrete and short. The block is for orchestration recovery, not general explanation.

## Why fields are short here

Packet schemas stay lean because they are repeatedly loaded into prompts. Ambient prose lives in this README so the schema files carry only field names and short hints.

That keeps prompt-injection surface and token cost lower while preserving the contract in one nearby reference.

Treat this file as the place for workflow context that would otherwise bloat each packet.
