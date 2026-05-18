# Review Packet
<!-- READ-ONLY TEMPLATE. See packets/README.md for the contract. -->
Task ID: <!-- must match planning and execution packets -->
Plan summary: <!-- from the planning packet -->
Risk level: <!-- from the planning packet: low | elevated -->
Files changed: <!-- from executor Handoff -->
Deviations from plan: <!-- from executor Handoff -->
## Hard gates
- [ ] Handoff section present and non-empty
- [ ] `Validation evidence` block exists for EVERY command in `Validation.Commands`
- [ ] Every evidence block shows `exit: 0` or accepted skip
- [ ] `Tests added` accounts for every planned test or concrete skip reason
- [ ] Plan/execute `Tests to add` values match
## Review Checklist
- [ ] Scope respected; no unrelated changes
- [ ] Acceptance criteria met
- [ ] Criteria have tests or exceptions
- [ ] No broken contracts or schemas
- [ ] Edge cases addressed
- [ ] Validation evidence matches acceptance criteria
- [ ] No regressions in adjacent code
- [ ] No security issues introduced
- [ ] Elevated-risk matches got scrutiny
Potential regressions:
Missing tests: <!-- unjustified coverage gaps -->
Simpler alternative: <!-- simpler approach, or `none` -->
Memory updates to apply: <!-- entries to apply, or `none` -->
Verdict: <!-- approve | request-changes | escalate -->
Recommendation: <!-- one sentence next action -->
