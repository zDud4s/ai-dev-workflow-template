# infra/AGENTS.md

Infra-specific rules:
- Treat secrets, deployment config, auth, billing, and production resources as sensitive areas.
- Avoid broad environment changes unless explicitly requested.
- Any destructive or security-sensitive change requires review or escalation.
- Validate infra changes with the documented commands or checks from `.ai/project.yaml`.
