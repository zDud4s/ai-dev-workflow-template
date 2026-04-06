Use the maintenance skill.

Goal:
Refresh the mutable project layer so the workflow stays accurate for the current repository.

Scope:
- re-scan the repository if needed
- refresh commands, entrypoints, important directories, ownership areas, risky areas, and do-not-touch zones
- update:
  - `.ai/project.yaml`
  - `.ai/memory.md`
  - `.ai/decisions.md`
  - local subdirectory `AGENTS.md` files only if structure clearly changed

Constraints:
- do not change the workflow core
- do not rewrite root workflow roles
- do not change escalation policy
- do not rewrite planner / reviewer / rescue / bootstrap behavior
- do not implement product changes
- do not invent commands without marking them as assumptions
- keep updates concise, factual, and operational

Required checks:
1. Are the documented commands still correct?
2. Are important directories and ownership areas still accurate?
3. Are risky areas and do-not-touch zones still valid?
4. Are stale assumptions present and should any be removed?
5. Is any local `AGENTS.md` now misleading or missing?
6. Did any stable architectural decision emerge that belongs in `.ai/decisions.md`?

If evidence is weak:
- stop
- list what is unclear
- list what evidence is missing
- give the smallest safe next step

Required output:
- Scope checked
- Files updated
- Confirmed changes
- Assumptions added
- Assumptions removed
- Unknowns remaining
- Core-change warning (if any)