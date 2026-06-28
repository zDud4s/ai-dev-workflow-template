"""Repo-root-derived path constants for the dashboard server.

Extracted from serve.py. ``ROOT`` is computed independently here (this file is
one directory deeper than serve.py, hence ``parents[3]``). serve.py re-exports
every name, so existing ``serve.ROOT`` / ``serve.METRICS_FILE`` references and
the tests that monkeypatch them by name keep working unchanged.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]  # repo root (server -> dashboard -> .ai -> repo)

# One-line file with the upstream sha that produced the currently-installed
# workflow files. Written by /api/workflow/update after a successful run; read
# by /api/workflow/check to compute "ahead/behind in commits". Absent on
# projects that haven't been updated through the dashboard yet.
WORKFLOW_VERSION_FILE = ROOT / ".ai" / "workflow" / ".version"
JOBS_DIR = ROOT / ".ai" / "local" / "jobs"
# Append-only ledger of job snapshots — every status transition adds one
# JSON line so the dashboard can rebuild the JOBS dict after a server
# restart. Last snapshot per ``id`` wins. Tests override this with a tmp
# path via monkeypatch.
JOBS_PERSIST_FILE = ROOT / ".ai" / "local" / "ledgers" / "jobs.jsonl"
# Append-only telemetry stream written by .ai/scripts/log_event.py
# (a PostToolUse hook). The /api/timeline endpoint aggregates phase_dispatch
# events from this file. Tests override it via monkeypatch.
EVENTS_FILE = ROOT / ".ai" / "local" / "ledgers" / "events.jsonl"
# Append-only metrics stream written by the orchestrate skill, one line per
# dispatched phase. Powers the /api/auto-select ranking. See the orchestrate
# skill "## Metrics logging" section for the schema.
METRICS_FILE = ROOT / ".ai" / "local" / "ledgers" / "metrics.jsonl"
# Filled agent-dispatch packets produced by the agent orchestrator.
AGENT_RUNS_DIR = ROOT / ".ai" / "local" / "agent-runs"
PIPELINES_DIR = ROOT / ".ai" / "local" / "pipelines"
# Append-only ledger of per-(skill, job) invocations. The auto skill-improver
# (Phase 2+) reads this to decide which skills need adapting. One line per
# unique skill invoked in a job; the entry-skill of orchestrate/plan jobs is
# always credited even when the log isn't stream-json.
SKILL_METRICS_FILE = ROOT / ".ai" / "local" / "ledgers" / "skill_metrics.jsonl"
# Todos ledger. `scripts/todos_parser.py` owns the canonical read path for the
# Todos tab; the analytics aggregation reads the same file by this constant so
# all six analytics ledgers are uniform and monkeypatchable by name in tests.
TODOS_FILE = ROOT / ".ai" / "local" / "ledgers" / "todos.jsonl"
# Auto-improver storage. Proposals are dropped here as JSON + .old.md + .new.md
# triples so the dashboard can render a diff and the user can Accept / Reject.
# Backups of overwritten SKILL.md content go to SKILL_BACKUPS_DIR; every
# decision (auto-apply, manual-apply, reject, skip) is appended to the
# ledger for forensic readability.
SKILL_PROPOSALS_DIR = ROOT / ".ai" / "local" / "proposals" / "skills"
SKILL_BACKUPS_DIR = ROOT / ".ai" / "local" / "proposals" / "skill_backups"
IMPROVEMENTS_LEDGER = ROOT / ".ai" / "local" / "ledgers" / "improvements.jsonl"
# Agent suggestions storage. Mirrors the skill-proposal layout but for the
# agent-improver "Suggest-new-agents" mode: one .json payload + one .body.md
# per proposal. Accept writes a real file at .claude/agents/<slug>.md;
# reject just marks status="rejected" and leaves the proposal on disk.
AGENT_PROPOSALS_DIR = ROOT / ".ai" / "local" / "proposals" / "agents"
