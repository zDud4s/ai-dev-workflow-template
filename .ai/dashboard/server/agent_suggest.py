"""Agent-suggestion pipeline: propose new subagents from recent activity.

Extracted from serve.py. Gathers signal -- editable agent names already on
disk (``_load_editable_agent_names``), recent job tasks (``_recent_job_tasks``),
and a git-log excerpt (``_git_log_excerpt``) -- builds the suggester prompt
(``_build_agent_suggester_prompt``), parses the model's reply
(``_parse_agent_suggestions_output``, reusing ``_parse_improver_output`` from
server.llm_output), and persists an accepted suggestion as a proposal under
``AGENT_PROPOSALS_DIR`` (``_persist_agent_proposal``).

Pure apart from path constants + stdlib; serve.py re-exports every name via a
shim and the ``_handle_agent_suggest`` endpoint drives the flow.
"""
from __future__ import annotations

import datetime as _dt
import json
import re
import subprocess
from pathlib import Path

from server.llm_output import _parse_improver_output
from server.paths import AGENT_PROPOSALS_DIR, JOBS_DIR, ROOT


def _load_editable_agent_names() -> set[str]:
    """Return the set of agent slug names (filename stem) present in either
    the project (``<repo>/.claude/agents``) or user (``~/.claude/agents``)
    scope. Plugin agents are intentionally excluded — they are namespaced
    differently and we will never write into plugin paths anyway."""
    names: set[str] = set()
    for d in (ROOT / ".claude" / "agents",
              Path.home() / ".claude" / "agents"):
        try:
            if not d.is_dir():
                continue
            for f in d.glob("*.md"):
                stem = f.stem.strip().lower()
                if stem:
                    names.add(stem)
        except OSError:
            continue
    return names


def _recent_job_tasks(max_jobs: int = 50) -> list[str]:
    """Most-recent ``task`` strings from ``JOBS_DIR/*.json``, deduped while
    preserving first-seen order. Bad JSON / missing files are skipped
    silently — this signal is best-effort context for the LLM."""
    if not JOBS_DIR.is_dir():
        return []
    try:
        entries = sorted(JOBS_DIR.glob("*.json"),
                         key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for p in entries[:max_jobs * 2]:  # over-fetch in case many are blank
        try:
            obj = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        task = (obj.get("task") or "").strip()
        if not task or task in seen:
            continue
        seen.add(task)
        out.append(task)
        if len(out) >= max_jobs:
            break
    return out


def _git_log_excerpt(max_commits: int = 50) -> str:
    """``git log --oneline -N`` from the repo root, with a hard 10s timeout
    and silent OS-error fallback. Returns "" when git isn't available, the
    repo has no commits, or anything else goes wrong — the suggester prompt
    handles an empty section."""
    try:
        proc = subprocess.run(
            ["git", "log", "--oneline", f"-{max_commits}"],
            cwd=str(ROOT), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=10,
        )
        if proc.returncode != 0:
            return ""
        return proc.stdout or ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _build_agent_suggester_prompt(git_log: str, recent_tasks: list[str],
                                  existing_agent_names: set[str]) -> str:
    """Strict-JSON-output prompt that frames the agent-improver's
    Suggest-new-agents mode. Cap each task at 200 chars so the prompt stays
    small even with many jobs."""
    existing_block = "\n".join(sorted(existing_agent_names)) or "(none)"
    bullets = "\n".join(f"- {t[:200]}" for t in recent_tasks) or "(none)"
    git_block = git_log.strip() or "(none)"
    return (
        "OUTPUT FORMAT (STRICT): Respond with ONE JSON object on a single "
        "line. NO prose, NO commentary, NO markdown fences. If you write "
        "anything else, the output is INVALID.\n\n"
        "Schema:\n"
        '  {"suggestions": [{"name": "<lowercase-slug>", '
        '"description": "<one-sentence trigger description>", '
        '"trigger_phrasings": ["...", "..."], '
        '"rationale": "<why a dedicated agent helps>", '
        '"tools": "<comma-separated tool names, or empty string>", '
        '"confidence": "high|medium|low", '
        '"body": "<full agent file body that goes AFTER the YAML '
        'frontmatter; markdown ok>"}]}\n\n'
        "If there is no meaningful pattern to surface, return: "
        '{"suggestions": []}\n\n'
        "ROLE: You are the agent-improver in Suggest-new-agents mode. Look "
        "at the user's recent git activity and recent dashboard jobs and "
        "propose NEW agents that would capture repeated workflows. "
        "Cross-check against the existing agent catalogue to avoid "
        "duplicates. Be CONSERVATIVE — return [] rather than weak ideas. "
        "At most 3 suggestions.\n\n"
        "Each suggestion's \"body\" MUST start with a short purpose "
        "statement (1-2 sentences) and a short workflow (3-5 bullet "
        "steps). Do NOT include YAML frontmatter in \"body\" — the server "
        "adds the frontmatter from the JSON fields.\n\n"
        "=== Existing agent names (do NOT propose duplicates) ===\n"
        f"<<<EXISTING\n{existing_block}\nEXISTING>>>\n\n"
        "=== Recent git activity (oneline) ===\n"
        f"<<<GIT\n{git_block}\nGIT>>>\n\n"
        "=== Recent dashboard job tasks ===\n"
        f"<<<JOBS\n{bullets}\nJOBS>>>\n\n"
        "Now respond with ONLY the JSON object."
    )


def _parse_agent_suggestions_output(stdout: str) -> list[dict] | None:
    """Extract and validate the suggester's JSON output. Returns a list of
    valid suggestions (possibly empty) or ``None`` if the output is not a
    parseable object with the expected shape. Drops individual items that
    are missing required fields — partial responses still yield the valid
    subset rather than failing the whole call."""
    obj = _parse_improver_output(stdout)
    if obj is None or not isinstance(obj, dict):
        return None
    raw = obj.get("suggestions")
    if not isinstance(raw, list):
        return None
    out: list[dict] = []
    for s in raw:
        if not isinstance(s, dict):
            continue
        name = (s.get("name") or "").strip().lower()
        slug = re.sub(r"[^a-z0-9-]+", "-", name).strip("-")
        if not slug or len(slug) > 80:
            continue
        desc = (s.get("description") or "").strip()
        if not desc:
            continue
        triggers = s.get("trigger_phrasings") or []
        if not isinstance(triggers, list):
            continue
        triggers = [str(t).strip() for t in triggers if str(t).strip()]
        confidence = (s.get("confidence") or "").strip().lower()
        if confidence not in ("high", "medium", "low"):
            confidence = "medium"
        body = s.get("body") or ""
        if not isinstance(body, str) or not body.strip():
            continue
        tools = s.get("tools") or ""
        if not isinstance(tools, str):
            tools = ""
        rationale = (s.get("rationale") or "").strip()
        out.append({
            "name": slug,
            "slug": slug,
            "description": desc,
            "trigger_phrasings": triggers,
            "rationale": rationale,
            "tools": tools.strip(),
            "confidence": confidence,
            "body": body,
        })
    return out


def _persist_agent_proposal(suggestion: dict, *, source_signal: dict) -> str | None:
    """Write the ``{pid}.json`` + ``{pid}.body.md`` pair under
    ``AGENT_PROPOSALS_DIR`` and return the proposal id. ``None`` on any
    OS-level write failure so the caller can skip and continue."""
    slug = suggestion["slug"]
    ts_dt = _dt.datetime.now(_dt.timezone.utc)
    pid = f"_agent-{slug}-{ts_dt.strftime('%Y%m%d-%H%M%S')}"
    payload = {
        "id": pid,
        "kind": "agent-draft",
        "name": suggestion["name"],
        "slug": slug,
        "description": suggestion["description"],
        "trigger_phrasings": suggestion["trigger_phrasings"],
        "rationale": suggestion["rationale"],
        "tools": suggestion["tools"],
        "confidence": suggestion["confidence"],
        "ts": ts_dt.isoformat(timespec="seconds"),
        "source_signal": source_signal,
        "status": "pending",
        "target_path": f".claude/agents/{slug}.md",
        "applied_at": None,
        "installed_path": None,
    }
    try:
        AGENT_PROPOSALS_DIR.mkdir(parents=True, exist_ok=True)
        (AGENT_PROPOSALS_DIR / f"{pid}.json").write_text(
            json.dumps(payload, indent=2), encoding="utf-8")
        (AGENT_PROPOSALS_DIR / f"{pid}.body.md").write_text(
            suggestion["body"], encoding="utf-8")
    except OSError as e:
        # Best-effort persistence; the caller silently skips this proposal
        # when we return None. Log so operators see the underlying cause
        # (disk full, permissions, file locked on Windows) rather than
        # just observing "fewer agent suggestions than expected".
        print(f"[serve] persist_agent_proposal {pid} failed: {e}", flush=True)
        return None
    return pid
