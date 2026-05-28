"""Cross-reference integrity: every path mentioned in a workflow doc resolves.

These tests catch broken references the moment a rename leaves a stale link
behind. They only inspect documents that participate in the workflow contract;
mutable project state (memory.md, decisions.md) is excluded.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from conftest import REPO_ROOT, iter_workflow_markdown


# Repo-relative path mentions. Captures things like:
#   .ai/workflow/dispatch.md
#   .claude/skills/orchestrate/SKILL.md
#   ~/.agents/skills/call-claude/SKILL.md
#
# Excludes:
#   - glob patterns containing `*` or `?`
#   - template placeholders `<...>` or `{...}`
#   - paths inside fenced code blocks (stripped before scanning — see below)
PATH_REF_RE = re.compile(
    r"""
    (?<![\w/])
    (?P<path>
        (?:~/)?\.(?:ai|claude|agents)
        (?:/[A-Za-z0-9_.\-]+)+
    )
    """,
    re.VERBOSE,
)

FENCE_RE = re.compile(r"```.*?```", re.DOTALL)


def strip_fences(text: str) -> str:
    """Remove fenced code blocks; their contents are examples, not refs."""
    return FENCE_RE.sub("", text)


def collect_refs(path: Path) -> list[tuple[str, int]]:
    """Return (ref, line_no) for every path mention outside fenced blocks."""
    text = path.read_text(encoding="utf-8")
    cleaned = strip_fences(text)
    # Line numbers from the original text — search for each match's literal in
    # the original to keep error messages useful.
    refs: list[tuple[str, int]] = []
    seen: set[tuple[str, int]] = set()
    for m in PATH_REF_RE.finditer(cleaned):
        raw = m.group("path").rstrip(".,;:`)\"'")
        if any(ch in raw for ch in "*?<>{}"):
            continue
        # Line number — approximate using the original text.
        try:
            idx = text.index(raw)
            line_no = text.count("\n", 0, idx) + 1
        except ValueError:
            line_no = 0
        key = (raw, line_no)
        if key in seen:
            continue
        seen.add(key)
        refs.append(key)
    return refs


def resolve(ref: str) -> Path:
    """Resolve a repo-relative or ~/-relative ref to an absolute path.

    `~/.agents/skills/<name>/SKILL.md` references the *runtime* global path
    that install.sh populates from one of two repo sources:
      1. `.agents/skills/<name>/SKILL.md` — Codex-only skills (call-claude, claude)
      2. `.claude/skills/<name>/SKILL.md` — shared skills (orchestrate, planner, ...)

    The resolver tries (1) first and falls back to (2). This lets the repo
    carry a single source per skill — install.sh mirrors `.claude/skills/`
    skills globally into `~/.agents/skills/` so Codex can discover them.
    """
    if ref.startswith("~/.agents/skills/"):
        suffix = ref[len("~/.agents/skills/"):]  # e.g. "planner/SKILL.md"
        agents_candidate = REPO_ROOT / ".agents" / "skills" / suffix
        if agents_candidate.exists():
            return agents_candidate
        claude_candidate = REPO_ROOT / ".claude" / "skills" / suffix
        return claude_candidate
    if ref.startswith("~/"):
        return REPO_ROOT / ref[2:]
    return REPO_ROOT / ref


# Build the (file, ref) parameter set at import time so pytest gives one row
# per reference. That makes failures point at the exact stale link.
def _ref_params():
    params = []
    for md in iter_workflow_markdown():
        for ref, line in collect_refs(md):
            params.append(
                pytest.param(
                    md,
                    ref,
                    line,
                    id=f"{md.relative_to(REPO_ROOT).as_posix()}:{line}:{ref}",
                )
            )
    return params


# `~/.claude/plugins/` is the Claude Code plugin install root on the
# operator's home dir — those paths live OUTSIDE the repo by design
# (marketplaces, cache, user-installed plugins). References to them are
# documentation pointers, not in-tree files. Skip resolution for any
# reference under that prefix instead of pretending the repo should
# carry a copy of every plugin install path.
_EXTERNAL_REF_PREFIXES = (
    "~/.claude/plugins",
    "~/.claude/projects",   # IDE Claude transcript dir, populated at runtime
    "~/.codex/sessions",    # codex CLI rollout dir, populated at runtime
)

# Runtime-generated, gitignored paths that workflow docs reference as
# "Allowed writes" (e.g. the maintenance TODO scan step). They live inside
# the repo tree but are produced at runtime, never checked in. Skip
# resolution rather than pretending the repo should ship them.
_RUNTIME_GENERATED_REFS = frozenset({
    ".ai/ledgers/todos.jsonl",
    ".ai/ledgers/todos-archive.jsonl",
    ".ai/ledgers/events.jsonl",
    ".ai/ledgers/metrics.jsonl",
    ".ai/ledgers/jobs.jsonl",
    ".ai/ledgers/skill_metrics.jsonl",
    ".ai/ledgers/improvements.jsonl",
    ".ai/TODO.md",
    ".ai/.todos.lock",
    ".ai/dashboard/.todos-parser.log",
})


@pytest.mark.parametrize("md_file, ref, line", _ref_params())
def test_reference_resolves(md_file: Path, ref: str, line: int):
    if any(ref.startswith(p) for p in _EXTERNAL_REF_PREFIXES):
        pytest.skip(f"external runtime path (not in-tree): {ref}")
    if ref in _RUNTIME_GENERATED_REFS:
        pytest.skip(f"runtime-generated path (gitignored): {ref}")
    target = resolve(ref)
    assert target.exists(), (
        f"{md_file.relative_to(REPO_ROOT).as_posix()}:{line} "
        f"references `{ref}` but {target} does not exist"
    )
