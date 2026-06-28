# Execution Packet

<!-- READ-ONLY TEMPLATE. See packets/README.md for the contract. -->

Task ID: <!-- must match the planning packet -->
Objective: <!-- one sentence -->
Size: <!-- trivial | small | medium | large -->

## Steps
<!-- ordered atomic steps, max 7 -->

## File Context
<!-- minimum snippets needed, formatted as file:line - snippet -->

## Boundaries
Allowed files: <!-- only these files may be modified -->
Do-not-touch files/dirs: <!-- these must not be changed under any circumstance -->

## Tests
To add: <!-- same as planning packet `Tests to add` after normalization (trim/lowercase; none/-/empty equivalent) — not byte-identical -->

## Validation
Commands: <!-- exact commands to run after implementation.
  For the test gate, prefer the catalog selector over the full suite:
    trivial/small  -> python .ai/scripts/select_tests.py --gate --run
    medium/large or elevated risk -> full suite (pytest), incl. slow
  The --gate subset is the conservative superset (touched groups + always
  groups); it fails if the catalog is stale. See .ai/tests/README.md. -->
Expected result: <!-- what success looks like -->

## Handoff

## Follow-ups
<!-- optional bullets feeding the TODO scanner; one per line -->

<!-- executor fills this section after completing the work -->
Files changed:
Tests added:
<!-- mirror Tests.To add; planned tests are added or skipped with reason -->
Validation evidence:
<!-- one block per command: `$ <command>`, `exit: <code>`, `tail: <last 5 lines of output, or full output if shorter>` -->
Deviations from plan: <!-- none, or list each deviation with reason -->
New risks discovered: <!-- none, or list each -->
Memory updates: <!-- durable facts learned, or `none` -->
Pending deletions: <!-- required deletions not executed, or `none` -->
