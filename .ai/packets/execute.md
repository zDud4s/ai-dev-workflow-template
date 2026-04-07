# Execution Packet

<!-- Planner produces this. Executor follows it literally. -->
<!-- If a step is ambiguous, executor STOPS and escalates — do not guess. -->

Task ID: <!-- must match the planning packet -->
Objective: <!-- one sentence -->
Size: <!-- trivial | small | medium | large -->

## Steps
<!-- Ordered. Each step = one atomic change. Max 7 steps per packet. -->
<!-- If >7, the planner should have decomposed into multiple packets. -->
<!-- Format: -->
<!-- 1. [verb] [target file] — [what to change and why] -->

## File Context
<!-- Planner pastes the minimum code snippets the executor needs. -->
<!-- This avoids the executor re-reading entire files to find context. -->
<!-- Format: file:line — snippet -->

## Boundaries
Allowed files: <!-- only these files may be modified -->
Do-not-touch files/dirs: <!-- these must not be changed under any circumstance -->

## Validation
Commands: <!-- exact commands to run after implementation -->
Expected result: <!-- what success looks like -->

## Handoff
<!-- Executor fills this section AFTER completing the work. -->
<!-- This becomes the input for the review packet. -->
Files changed:
Actual commands run:
Deviations from plan: <!-- none, or list each deviation with reason -->
New risks discovered: <!-- none, or list each -->
Memory updates: <!-- operational facts learned, to append to .ai/memory.md -->
