# Project Memory — Archive

Resolved snapshots and point-in-time facts moved out of `memory.md` to keep
the active memory layer lean. Phases do NOT load this file by default.

Entries follow the same format as `memory.md`:
`- YYYY-MM-DD [topic] fact (archived: YYYY-MM-DD reason)`

## Entries

- 2026-05-17 [tests-known-failures] 3 pre-existing pytest failures unrelated to current work: tests/test_dashboard_jobs.py::test_jobs_list_endpoint_runs_reconcile_before_returning (zombie-pid reconcile); tests/test_mirror.py::test_skill_mirrored_to_home_agents_skills[codex] and test_shared_skill_mirror_matches_source[codex] (codex skill removed in commit b23a080 but mirror tests still expect it). (archived: 2026-05-17 stale snapshot — superseded by next entry)
- 2026-05-17 [tests] full suite at 214/214 green after removing stale `codex` parametrize entries from tests/test_mirror.py (REQUIRED_GLOBAL_SKILLS + test_shared_skill_mirror_matches_source) — these matched install.sh's pre-b23a080 mirror loop and were obsolete. (archived: 2026-05-17 point-in-time fix narrative)
