# backend/AGENTS.md

Backend-specific rules:
- Prefer local fixes over framework-wide refactors.
- Avoid changing API contracts unless the task explicitly calls for it.
- Preserve backward compatibility where feasible.
- Any schema or migration change requires review or escalation.
- Always validate with the backend test command from `.ai/project.yaml`.
