"""Local dashboard server for the AI workflow.

Run from repo root:
    python .ai/dashboard/serve.py

Then open http://localhost:8765/.ai/dashboard/

Serves the whole repo as static files (read-only) plus a small JSON API:

    GET  /api/list?path=.ai/plans              ->  {"entries": [...]}
    POST /api/memory       {topic, fact}        ->  appends to .ai/memory.md
    POST /api/decisions    {date, decision,
                            why, consequence,
                            revisit}            ->  appends to .ai/decisions.md
    POST /api/events/clear                      ->  truncates .ai/events.jsonl
    POST /api/models/dispatch_mode {mode}       ->  flips dispatch_mode in
                                                    .ai/models.yaml

    POST /api/jobs            {kind, task}      ->  spawn an orchestrate /
                                                    planner subprocess
    GET  /api/jobs                              ->  list recent jobs
    GET  /api/jobs/<id>?tail=N                  ->  job details + last N log lines
    GET  /api/jobs/<id>/stream                  ->  SSE: streams new log bytes
                                                    as the subprocess writes them
    POST /api/jobs/<id>/input  {text}           ->  write a line to the
                                                    subprocess stdin
    POST /api/jobs/<id>/cancel                  ->  terminate the subprocess
"""
from __future__ import annotations

import datetime as _dt
import http.server
import json
import os
import re
import shutil
import socketserver
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
from collections import deque
from pathlib import Path

PORT = int(os.environ.get("DASHBOARD_PORT", "8765"))
ROOT = Path(__file__).resolve().parents[2]  # repo root
JOBS_DIR = ROOT / ".ai" / "dashboard" / "jobs"
# Append-only ledger of job snapshots — every status transition adds one
# JSON line so the dashboard can rebuild the JOBS dict after a server
# restart. Last snapshot per ``id`` wins. Tests override this with a tmp
# path via monkeypatch.
JOBS_PERSIST_FILE = ROOT / ".ai" / "dashboard" / "jobs.jsonl"
# Append-only telemetry stream written by .ai/dashboard/log_event.py (a
# PostToolUse hook). The /api/timeline endpoint aggregates phase_dispatch
# events from this file. Tests override it via monkeypatch.
EVENTS_FILE = ROOT / ".ai" / "events.jsonl"
# Append-only ledger of per-(skill, job) invocations. The auto skill-improver
# (Phase 2+) reads this to decide which skills need adapting. One line per
# unique skill invoked in a job; the entry-skill of orchestrate/plan jobs is
# always credited even when the log isn't stream-json.
SKILL_METRICS_FILE = ROOT / ".ai" / "dashboard" / "skill_metrics.jsonl"
# Auto-improver storage. Proposals are dropped here as JSON + .old.md + .new.md
# triples so the dashboard can render a diff and the user can Accept / Reject.
# Backups of overwritten SKILL.md content go to SKILL_BACKUPS_DIR; every
# decision (auto-apply, manual-apply, reject, skip) is appended to the
# ledger for forensic readability.
SKILL_PROPOSALS_DIR  = ROOT / ".ai" / "dashboard" / "skill_proposals"
SKILL_BACKUPS_DIR    = ROOT / ".ai" / "dashboard" / "skill_backups"
IMPROVEMENTS_LEDGER  = ROOT / ".ai" / "dashboard" / "improvements.jsonl"
# Defaults used when models.yaml has no `improver:` block. The improver
# only edits skills under PROJECT (.claude/skills/) — global skills are
# never modified.
_IMPROVER_DEFAULTS = {
    "enabled": True,
    "tool": "claude",
    "model": "claude-haiku-4-5",
    "small_change_max_lines": 6,    # auto-apply threshold (added+removed lines)
    "min_interval_seconds": 300,    # per-skill throttle
    "timeout_seconds": 120,         # subprocess wall-clock cap
    # Auto-revert safety net: if a skill that received an `applied` proposal
    # later shows success-rate regression by >= ``revert_margin`` over the
    # next ``revert_after_n_uses`` invocations, restore the .bak silently.
    "revert_after_n_uses": 5,
    "revert_margin": 0.2,
}

# Fields that exist only at runtime inside the JOBS dict and must NOT be
# serialised to disk (they are either not JSON-encodable or meaningless
# after the subprocess dies).
_JOB_RUNTIME_FIELDS = frozenset({"proc", "subscribers", "stdin_lock"})

# Terminal job statuses — used to know when scanned log-file cost can be
# memoised back onto the job entry (cost can't change once the subprocess
# is dead).
_TERMINAL_JOB_STATUSES = frozenset({"done", "failed", "cancelled", "interrupted"})


def _write_text_lf(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` with LF line endings, regardless of platform.

    Python's ``Path.write_text`` defaults to ``newline=None`` which translates
    ``\\n`` to the OS line terminator (``\\r\\n`` on Windows). The repo's
    ``.gitattributes`` pins ``*.yaml`` / ``*.md`` to ``eol=lf``, so writing
    those files through the dashboard previously produced spurious
    ``"CRLF will be replaced by LF"`` git warnings on Windows."""
    path.write_text(text, encoding="utf-8", newline="\n")


def _persist_job(job_id: str) -> None:
    """Append the current snapshot of ``JOBS[job_id]`` to the persistence
    ledger. Idempotent across calls — restoring on boot just replays the
    last snapshot per id."""
    with JOBS_LOCK:
        j = JOBS.get(job_id)
        if not j:
            return
        snapshot = {k: v for k, v in j.items() if k not in _JOB_RUNTIME_FIELDS}
    try:
        JOBS_PERSIST_FILE.parent.mkdir(parents=True, exist_ok=True)
        with JOBS_PERSIST_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(snapshot, default=str) + "\n")
    except OSError:
        # Persistence is best-effort; never break the live pipeline.
        pass


def _update_job_cost(job_id: str, result_obj: dict) -> None:
    """Accumulate cost / duration / turns from a single ``type=result``
    event onto the live ``JOBS[job_id]["cost"]`` summary."""
    usd_raw = result_obj.get("total_cost_usd")
    if usd_raw is None:
        usd_raw = result_obj.get("cost_usd")
    try:
        usd = float(usd_raw) if usd_raw is not None else 0.0
    except (TypeError, ValueError):
        usd = 0.0
    try:
        dur = int(result_obj.get("duration_ms") or 0)
    except (TypeError, ValueError):
        dur = 0
    with JOBS_LOCK:
        j = JOBS.get(job_id)
        if not j:
            return
        cost = j.get("cost")
        if not isinstance(cost, dict):
            cost = {"turns": 0, "cost_usd": 0.0, "duration_ms": 0}
            j["cost"] = cost
        cost["turns"] = int(cost.get("turns", 0)) + 1
        cost["cost_usd"] = round(float(cost.get("cost_usd", 0.0)) + usd, 6)
        cost["duration_ms"] = int(cost.get("duration_ms", 0)) + dur


def _prune_old_logs(jobs_dir: Path, max_age_days: int = 14, keep_newest: int = 200) -> int:
    """Remove ``.log`` files in ``jobs_dir`` that are older than
    ``max_age_days`` OR beyond the ``keep_newest`` cap. Returns the
    number of files deleted. Best-effort: tolerates missing dir and
    individual unlink failures."""
    try:
        if not jobs_dir.is_dir():
            return 0
        entries = []
        for p in jobs_dir.glob("*.log"):
            try:
                entries.append((p.stat().st_mtime, p))
            except OSError:
                continue
    except OSError:
        return 0

    cutoff = time.time() - (max_age_days * 86400)
    deleted = 0
    # Sort newest first so the "keep newest N" rule is easy to apply.
    entries.sort(key=lambda x: x[0], reverse=True)
    for idx, (mtime, p) in enumerate(entries):
        too_old = mtime < cutoff
        over_cap = idx >= keep_newest
        if too_old or over_cap:
            try:
                p.unlink()
                deleted += 1
            except OSError:
                pass
    return deleted


def _extract_cost_from_log(log_path: Path) -> dict | None:
    """Scan a chat-mode log for ``{"type":"result", ...}`` events and
    aggregate cost / duration / turn count. Returns None if the file does
    not exist; an empty summary (turns=0) for files with no result events.
    """
    try:
        if not Path(log_path).is_file():
            return None
    except OSError:
        return None
    cost = 0.0
    duration = 0
    turns = 0
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    obj = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if obj.get("type") != "result":
                    continue
                usd = obj.get("total_cost_usd")
                if usd is None:
                    usd = obj.get("cost_usd")
                if usd is not None:
                    try:
                        cost += float(usd)
                    except (TypeError, ValueError):
                        pass
                dur = obj.get("duration_ms")
                if dur is not None:
                    try:
                        duration += int(dur)
                    except (TypeError, ValueError):
                        pass
                turns += 1
    except OSError:
        return None
    if turns == 0 and cost == 0.0 and duration == 0:
        return {"turns": 0, "cost_usd": 0.0, "duration_ms": 0}
    return {"turns": turns, "cost_usd": round(cost, 6), "duration_ms": duration}


def _load_persisted_jobs() -> None:
    """Replay the persistence ledger at server startup and seed ``JOBS``.

    Jobs serialised in a non-terminal state (queued/running/cancelling)
    are flagged as ``interrupted`` since their subprocess is dead — we
    cannot honestly call them running after a restart.
    """
    try:
        if not JOBS_PERSIST_FILE.exists():
            return
        seen: dict[str, dict] = {}
        with JOBS_PERSIST_FILE.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                jid = obj.get("id")
                if jid:
                    seen[jid] = obj
    except OSError:
        return

    with JOBS_LOCK:
        for obj in seen.values():
            if obj.get("status") in {"queued", "running", "cancelling"}:
                obj["status"] = "interrupted"
                obj.setdefault("error", "server restart")
            JOBS[obj["id"]] = obj

# IDE transcript mirror: Claude Code (the VSCode/Cursor extension) writes a
# JSONL transcript of every session to ~/.claude/projects/<encoded-cwd>/<sid>.jsonl
# so the dashboard can tail those files and surface ANY ongoing IDE chat as
# a read-only terminal pane. Tests override this with a tmp tree.
_CLAUDE_PROJECTS_ROOT_OVERRIDE: Path | None = None


def _claude_projects_root() -> Path | None:
    if _CLAUDE_PROJECTS_ROOT_OVERRIDE is not None:
        return _CLAUDE_PROJECTS_ROOT_OVERRIDE
    home = Path.home()
    candidate = home / ".claude" / "projects"
    return candidate if candidate.is_dir() else None


def _transcripts_dir_for_cwd(cwd: Path) -> Path | None:
    """Pick the ``~/.claude/projects/<slug>`` directory matching ``cwd``.

    Claude Code's slug rule (observed): replace ``:``, ``/``, ``\\`` and
    spaces with ``-``. We try a few common variants because case-folding
    of the drive letter has been seen both ways across machines."""
    root = _claude_projects_root()
    if root is None:
        return None
    s = str(cwd)
    slug_lower = (s[0].lower() + s[1:]).replace(":", "-").replace("\\", "-").replace("/", "-").replace(" ", "-")
    slug_upper = (s[0].upper() + s[1:]).replace(":", "-").replace("\\", "-").replace("/", "-").replace(" ", "-")
    for slug in (slug_lower, slug_upper, slug_lower.lower()):
        p = root / slug
        if p.is_dir():
            return p
    # Last-ditch: scan all subdirs and check if any transcript records this cwd.
    target = str(cwd).lower()
    for sub in root.iterdir():
        if not sub.is_dir():
            continue
        # Peek the first non-empty line of any jsonl file looking for a cwd match.
        for f in sub.glob("*.jsonl"):
            try:
                with f.open("r", encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            obj = json.loads(line)
                        except Exception:
                            continue
                        if str(obj.get("cwd") or "").lower() == target:
                            return sub
                        break  # only peek first record per file
            except OSError:
                continue
    return None


# Codex stores per-session rollouts here. Tests override via
# ``_CODEX_SESSIONS_ROOT_OVERRIDE``.
_CODEX_SESSIONS_ROOT_OVERRIDE: Path | None = None


def _codex_sessions_root() -> Path | None:
    if _CODEX_SESSIONS_ROOT_OVERRIDE is not None:
        return _CODEX_SESSIONS_ROOT_OVERRIDE
    p = Path.home() / ".codex" / "sessions"
    return p if p.is_dir() else None


def _parse_iso_ts(s):
    """Return a timezone-aware UTC datetime, or None on failure."""
    if not isinstance(s, str) or not s:
        return None
    raw = s[:-1] + "+00:00" if s.endswith("Z") else s
    try:
        dt = _dt.datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    return dt.astimezone(_dt.timezone.utc)


def _normalise_path_for_match(s: str) -> str:
    return (s or "").lower().replace("\\", "/").rstrip("/")


# Cached aggregator result (re-read after _USAGE_TTL_SECONDS) so the overview
# can refresh without re-scanning ~/.codex/sessions on every reload.
_USAGE_CACHE: dict = {"at": 0.0, "data": None}
_USAGE_TTL_SECONDS = 30.0


def _aggregate_claude_usage(repo_root: Path, now: _dt.datetime) -> dict:
    """Per-model + windowed token totals from this repo's Claude transcripts.

    Reads ``message.usage`` plus ``message.model`` and ``timestamp`` from each
    line in ``~/.claude/projects/<slug>/*.jsonl`` and accumulates totals by
    model for three windows: ``all``, last 5 hours, last 7 days. Returns
    zeros if there is no transcripts directory for this repo."""
    cutoff_5h = now - _dt.timedelta(hours=5)
    cutoff_7d = now - _dt.timedelta(days=7)

    def empty():
        return {
            "total": 0, "input": 0, "output": 0,
            "cache_creation": 0, "cache_read": 0,
            "messages": 0, "by_model": {},
        }

    out = {"all": empty(), "5h": empty(), "7d": empty(), "transcripts": 0}

    def add(win, model, usage):
        inp = int(usage.get("input_tokens") or 0)
        outp = int(usage.get("output_tokens") or 0)
        cc = int(usage.get("cache_creation_input_tokens") or 0)
        cr = int(usage.get("cache_read_input_tokens") or 0)
        tot = inp + outp + cc + cr
        win["total"] += tot
        win["input"] += inp
        win["output"] += outp
        win["cache_creation"] += cc
        win["cache_read"] += cr
        win["messages"] += 1
        bm = win["by_model"].setdefault(model, {"total": 0, "messages": 0})
        bm["total"] += tot
        bm["messages"] += 1

    tdir = _transcripts_dir_for_cwd(repo_root)
    if tdir is None:
        return out
    try:
        files = list(tdir.glob("*.jsonl"))
    except OSError:
        return out
    for p in files:
        out["transcripts"] += 1
        try:
            with p.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line.startswith("{"):
                        continue
                    try:
                        obj = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    msg = obj.get("message")
                    if not isinstance(msg, dict):
                        continue
                    usage = msg.get("usage")
                    if not isinstance(usage, dict):
                        continue
                    model = msg.get("model") or "unknown"
                    ts = _parse_iso_ts(obj.get("timestamp"))
                    try:
                        add(out["all"], model, usage)
                        if ts is not None and ts >= cutoff_5h:
                            add(out["5h"], model, usage)
                        if ts is not None and ts >= cutoff_7d:
                            add(out["7d"], model, usage)
                    except (TypeError, ValueError):
                        continue
        except OSError:
            continue
    _fill_percent_shares(out)
    return out


def _aggregate_codex_usage(repo_root: Path, now: _dt.datetime) -> dict:
    """Per-model + windowed token totals from Codex session rollouts for this
    repo, plus the most recent account-wide ``rate_limits`` block.

    Codex rollouts at ``~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`` carry
    ``turn_context`` events (which expose ``payload.model`` for each turn) and
    ``event_msg`` events with ``payload.type == "token_count"`` that bundle
    both ``info.last_token_usage`` (per-turn) and ``rate_limits`` (5h primary
    + weekly secondary, account-wide). Per-turn totals are filtered by
    matching ``session_meta.payload.cwd`` to ``repo_root`` so the overview
    only counts tokens spent on this project. Rate-limit percentages are
    account-wide (Codex does not expose per-project quotas)."""
    cutoff_5h = now - _dt.timedelta(hours=5)
    cutoff_7d = now - _dt.timedelta(days=7)

    def empty():
        return {
            "total": 0, "input": 0, "cached_input": 0,
            "output": 0, "reasoning_output": 0,
            "turns": 0, "by_model": {},
        }

    out = {
        "all": empty(), "5h": empty(), "7d": empty(),
        "sessions": 0, "rate_limits": None, "matched_sessions": 0,
    }

    root = _codex_sessions_root()
    if root is None:
        return out

    target = _normalise_path_for_match(str(repo_root))
    latest_rl: tuple[_dt.datetime, dict] | None = None  # account-wide

    try:
        files = list(root.rglob("rollout-*.jsonl"))
    except OSError:
        return out

    def add(win, model, last):
        inp = int(last.get("input_tokens") or 0)
        cinp = int(last.get("cached_input_tokens") or 0)
        outp = int(last.get("output_tokens") or 0)
        rout = int(last.get("reasoning_output_tokens") or 0)
        tot = int(last.get("total_tokens") or 0) or (inp + outp + rout)
        win["total"] += tot
        win["input"] += inp
        win["cached_input"] += cinp
        win["output"] += outp
        win["reasoning_output"] += rout
        win["turns"] += 1
        bm = win["by_model"].setdefault(model, {"total": 0, "turns": 0})
        bm["total"] += tot
        bm["turns"] += 1

    for p in files:
        out["sessions"] += 1
        cwd_matches = False
        current_model = "unknown"
        try:
            with p.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line.startswith("{"):
                        continue
                    try:
                        obj = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    t = obj.get("type")
                    payload = obj.get("payload") or {}
                    if t == "session_meta":
                        cwd = _normalise_path_for_match(payload.get("cwd") or "")
                        if cwd == target:
                            cwd_matches = True
                    elif t == "turn_context":
                        m = payload.get("model")
                        if isinstance(m, str) and m:
                            current_model = m
                        cwd = _normalise_path_for_match(payload.get("cwd") or "")
                        if cwd and cwd == target:
                            cwd_matches = True
                    elif t == "event_msg" and payload.get("type") == "token_count":
                        ts = _parse_iso_ts(obj.get("timestamp"))
                        rl = payload.get("rate_limits")
                        if isinstance(rl, dict) and ts is not None:
                            if latest_rl is None or ts > latest_rl[0]:
                                latest_rl = (ts, rl)
                        if not cwd_matches:
                            continue
                        info = payload.get("info") or {}
                        last = info.get("last_token_usage")
                        if not isinstance(last, dict):
                            continue
                        try:
                            add(out["all"], current_model, last)
                            if ts is not None and ts >= cutoff_5h:
                                add(out["5h"], current_model, last)
                            if ts is not None and ts >= cutoff_7d:
                                add(out["7d"], current_model, last)
                        except (TypeError, ValueError):
                            continue
        except OSError:
            continue
        if cwd_matches:
            out["matched_sessions"] += 1

    if latest_rl is not None:
        ts, rl = latest_rl
        out["rate_limits"] = {
            "primary": rl.get("primary"),
            "secondary": rl.get("secondary"),
            "plan_type": rl.get("plan_type"),
            "last_event_at": ts.isoformat(),
        }
    _fill_percent_shares(out)
    return out


def _fill_percent_shares(out: dict) -> None:
    """Annotate each ``by_model[m]`` entry with a ``percent`` share of the
    window total (rounded to one decimal). Mutates in place."""
    for w in ("all", "5h", "7d"):
        win = out.get(w)
        if not isinstance(win, dict):
            continue
        total = win.get("total") or 0
        bm = win.get("by_model") or {}
        if total > 0:
            for info in bm.values():
                info["percent"] = round(100.0 * info["total"] / total, 1)
        else:
            for info in bm.values():
                info["percent"] = 0.0


# Claude Code stores its OAuth token here when the user is signed into a
# subscription plan. Tests override via ``_CLAUDE_CREDENTIALS_PATH_OVERRIDE``.
_CLAUDE_CREDENTIALS_PATH_OVERRIDE: Path | None = None
_CLAUDE_USAGE_CACHE: dict = {"at": 0.0, "data": None}
_CLAUDE_USAGE_TTL_SECONDS = 60.0
_CLAUDE_OAUTH_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"


def _read_claude_oauth_token() -> tuple[str | None, str | None]:
    """Read the local Claude Code OAuth access token plus subscription tier.

    Returns ``(token, tier)`` or ``(None, None)`` when the credentials file
    is missing, malformed, or the token has expired. ``tier`` is the
    ``rateLimitTier`` (e.g. ``default_claude_max_5x``); useful for UI hints
    but not required for the API call itself."""
    path = _CLAUDE_CREDENTIALS_PATH_OVERRIDE or (Path.home() / ".claude" / ".credentials.json")
    try:
        creds = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None, None
    oauth = creds.get("claudeAiOauth")
    if not isinstance(oauth, dict):
        return None, None
    tok = oauth.get("accessToken")
    if not isinstance(tok, str) or not tok:
        return None, None
    exp = oauth.get("expiresAt")
    # expiresAt is milliseconds since epoch in this file.
    if isinstance(exp, (int, float)) and exp <= time.time() * 1000:
        return None, oauth.get("rateLimitTier")
    return tok, oauth.get("rateLimitTier")


def _fetch_claude_oauth_usage() -> dict:
    """Fetch real Claude session/weekly utilization from the OAuth usage
    endpoint Claude Code itself uses for ``/usage``. Caches the response for
    ``_CLAUDE_USAGE_TTL_SECONDS`` so a busy overview reload doesn't hammer
    the API. The token never leaves this process.

    Returns ``{"available": bool, "data"?: {...}, "error"?: str, "tier"?: str}``.
    Network errors are swallowed and surfaced as ``available=False``."""
    import urllib.error
    import urllib.request

    now_mono = time.monotonic()
    cached = _CLAUDE_USAGE_CACHE["data"]
    if cached is not None and (now_mono - _CLAUDE_USAGE_CACHE["at"]) < _CLAUDE_USAGE_TTL_SECONDS:
        return cached

    tok, tier = _read_claude_oauth_token()
    if not tok:
        result = {
            "available": False,
            "error": "no claude oauth token (~/.claude/.credentials.json missing or expired)",
            "tier": tier,
        }
    else:
        req = urllib.request.Request(
            _CLAUDE_OAUTH_USAGE_URL,
            headers={
                "Authorization": f"Bearer {tok}",
                "User-Agent": "ai-workflow-dashboard/0.1 (claude-code-compatible)",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=8) as r:  # nosec: B310 - constant trusted URL
                body = r.read().decode("utf-8")
                data = json.loads(body)
            result = {"available": True, "data": data, "tier": tier}
        except urllib.error.HTTPError as e:
            result = {"available": False, "error": f"http {e.code}", "tier": tier}
        except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError) as e:
            result = {"available": False, "error": str(e)[:160], "tier": tier}

    _CLAUDE_USAGE_CACHE["data"] = result
    _CLAUDE_USAGE_CACHE["at"] = now_mono
    return result


def _aggregate_project_token_usage() -> dict:
    """Combined Claude + Codex token usage for this repo, with per-model and
    per-window breakdowns. Cached for ``_USAGE_TTL_SECONDS`` to keep the
    overview reload cheap (Codex scans can read ~150MB across all rollouts).
    Claude's real session-vs-quota utilization comes from the OAuth usage
    endpoint and is cached separately (see ``_fetch_claude_oauth_usage``)."""
    now_mono = time.monotonic()
    if _USAGE_CACHE["data"] is not None and (now_mono - _USAGE_CACHE["at"]) < _USAGE_TTL_SECONDS:
        cached = dict(_USAGE_CACHE["data"])
        # Always re-evaluate Claude OAuth usage on its own (shorter) TTL.
        cached["claude"] = dict(cached["claude"], rate_limits=_fetch_claude_oauth_usage())
        return cached
    now = _dt.datetime.now(_dt.timezone.utc)
    claude = _aggregate_claude_usage(ROOT, now)
    codex = _aggregate_codex_usage(ROOT, now)
    claude["rate_limits"] = _fetch_claude_oauth_usage()
    combined_all = (
        claude["all"]["total"] + codex["all"]["total"]
    )
    data = {
        "generated_at": now.isoformat(timespec="seconds"),
        "windows": {
            "5h": {"hours": 5},
            "7d": {"days": 7},
        },
        "claude": claude,
        "codex": codex,
        "combined": {"total": combined_all},
        # Backwards-compat with the first iteration of the API.
        "input": claude["all"]["input"],
        "output": claude["all"]["output"],
        "cache_creation": claude["all"]["cache_creation"],
        "cache_read": claude["all"]["cache_read"],
        "messages": claude["all"]["messages"],
        "transcripts": claude["transcripts"],
        "total": claude["all"]["total"],
    }
    _USAGE_CACHE["data"] = data
    _USAGE_CACHE["at"] = now_mono
    return data


# Allowed job kinds.
#   orchestrate / plan : one-shot `claude -p <skill prompt>` runs.
#   chat               : long-lived interactive `claude` session driven by
#                        JSON messages on stdin and JSON events on stdout
#                        (--input-format stream-json / --output-format stream-json).
#   chat-codex         : one-turn `codex exec --json` run per user message.
#                        Resumed via `codex exec resume <session_id>`.
JOB_KINDS = {
    "orchestrate": "orchestrate",
    "plan": "planner",
    "chat": None,
    "chat-codex": None,
}

# In-memory job registry. State is lost on server restart by design;
# log files survive on disk for forensic reading.
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()
JOBS_MAX = 50  # cap memory; oldest finished entries get evicted


def _load_improver_config() -> dict:
    """Read the optional ``improver:`` block from ``.ai/models.yaml`` and
    overlay it on ``_IMPROVER_DEFAULTS``. Always returns a fully populated
    config dict, even if the YAML block is missing or malformed.

    Honours ``AI_WORKFLOW_DISABLE_IMPROVER``: when that env var is truthy
    the config is returned with ``enabled=False`` regardless of YAML.
    Used by the pytest suite to stop the auto-improver from spawning real
    ``claude -p`` subprocesses (each one creates a new chat session in
    Claude Code's history)."""
    cfg = dict(_IMPROVER_DEFAULTS)
    if str(os.environ.get("AI_WORKFLOW_DISABLE_IMPROVER", "")).strip().lower() in {"1", "true", "yes", "on"}:
        cfg["enabled"] = False
        return cfg
    fields = _read_yaml_field(ROOT / ".ai" / "models.yaml", "improver")
    if not fields:
        return cfg
    for k in ("tool", "model"):
        v = fields.get(k)
        if v:
            cfg[k] = v
    for k in ("small_change_max_lines", "min_interval_seconds",
              "timeout_seconds", "revert_after_n_uses"):
        v = fields.get(k)
        if v is None or v == "":
            continue
        try:
            cfg[k] = int(v)
        except (TypeError, ValueError):
            continue
    v = fields.get("revert_margin")
    if v not in (None, ""):
        try:
            cfg["revert_margin"] = max(0.0, min(1.0, float(v)))
        except (TypeError, ValueError):
            pass
    if "enabled" in fields:
        cfg["enabled"] = str(fields["enabled"]).strip().lower() in {"true", "1", "yes", "on"}
    return cfg


def _last_improver_run_ts(skill_id: str) -> float:
    """Look at the audit ledger for the most recent improvement attempt
    against this skill, return epoch seconds (or 0 if never)."""
    try:
        if not IMPROVEMENTS_LEDGER.is_file():
            return 0.0
    except OSError:
        return 0.0
    last = 0.0
    try:
        with IMPROVEMENTS_LEDGER.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    o = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if o.get("skill") != skill_id:
                    continue
                ts = _iso_to_epoch(o.get("ts") or "")
                if ts > last:
                    last = ts
    except OSError:
        return 0.0
    return last


def _project_skill_index() -> dict[str, Path]:
    """Map canonical skill name -> SKILL.md path for every project skill
    under ``.claude/skills/``. The improver only edits skills in this map."""
    out: dict[str, Path] = {}
    for e in _scan_skills_dir(ROOT / ".claude" / "skills"):
        # The on-disk dir name is the canonical id; frontmatter ``name`` is
        # for display only. Use the dir name to avoid collisions.
        try:
            p = (ROOT / e["path"]).resolve()
            p.relative_to(ROOT.resolve())
        except (ValueError, OSError):
            continue
        out[p.parent.name] = p
    return out


def _build_improver_prompt(skill_id: str, skill_content: str,
                           metrics: dict, job_id: str,
                           log_excerpt: str) -> str:
    """Craft the one-shot prompt sent to the model.

    The schema and ``no change`` example are intentionally front-loaded so
    smaller models (Haiku) don't drift into prose. The skill content and
    log are delimited with ``<<<...>>>`` markers (not triple-backticks)
    because SKILL.md itself often contains fenced code blocks."""
    rate = round((metrics.get("success_rate") or 0.0) * 100) if metrics else None
    summary = (
        f"success_rate={rate}% over {metrics.get('total_jobs',0)} jobs"
        f", avg_cost=${metrics.get('avg_cost_usd',0):.4f}"
        f", avg_duration={int((metrics.get('avg_duration_ms') or 0)/1000)}s"
    ) if metrics and metrics.get("total_jobs") else "no telemetry yet"
    return (
        "OUTPUT FORMAT (STRICT): Respond with ONE JSON object. NO prose, "
        "NO commentary, NO markdown fences. If you write anything other "
        "than a JSON object, the output is INVALID.\n\n"
        "Schema:\n"
        '  {"change_summary": "<short str>", "rationale": "<short str>", '
        '"new_content": <full new SKILL.md as string OR null>}\n\n'
        'When no change is warranted: '
        '{"change_summary":"none","rationale":"<why>","new_content":null}\n\n'
        "ROLE: You are reviewing a project skill after one of its "
        "invocations. Be conservative — most invocations need no change. "
        "Only propose a refinement if there is CLEAR evidence in the log "
        "excerpt that the skill caused ambiguity, repeated failure, or "
        "missing guardrails. Keep edits small (≤ ~6 line delta) and keep "
        "frontmatter name/description intact.\n\n"
        f"SKILL: {skill_id}\n"
        f"TELEMETRY: {summary}\n"
        f"JOB: {job_id}\n\n"
        "=== Current SKILL.md (between markers) ===\n"
        f"<<<SKILL\n{skill_content}\nSKILL>>>\n\n"
        "=== Job log excerpt (between markers, may be empty) ===\n"
        f"<<<LOG\n{log_excerpt}\nLOG>>>\n\n"
        "Now respond with ONLY the JSON object."
    )


def _read_log_excerpt(log_path: str | None, max_bytes: int = 6144) -> str:
    """Tail the job's log/transcript so the improver has recent evidence.
    Returns an empty string if the path is missing or unreadable."""
    if not log_path:
        return ""
    try:
        p = Path(log_path)
        if not p.is_file():
            return ""
        size = p.stat().st_size
        with p.open("rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
            data = f.read()
        return data.decode("utf-8", errors="replace")
    except OSError:
        return ""


def _parse_improver_output(stdout: str) -> dict | None:
    """Robustly extract one JSON object from the model's free-form output.

    Handles three common shapes:
      1. ``stdout`` IS JSON (no prose around it)
      2. Fenced block: ```` ```json ... ``` ````
      3. JSON embedded in prose — scanned with a brace counter that
         respects strings + backslash escapes, so ``{`` characters inside
         JSON string values don't confuse the search.

    Returns ``None`` if no parseable object is found."""
    if not stdout:
        return None
    s = stdout.strip()

    # 1. Whole output IS JSON.
    try:
        obj = json.loads(s)
        if isinstance(obj, dict):
            return obj
    except (json.JSONDecodeError, ValueError):
        pass

    # 2. Fenced block from a chatty model.
    fence = re.search(r"```(?:json)?\s*\n?(.+?)\n?```", s, re.DOTALL)
    if fence:
        try:
            obj = json.loads(fence.group(1).strip())
            if isinstance(obj, dict):
                return obj
        except (json.JSONDecodeError, ValueError):
            pass

    # 3. String-aware brace counter; tries each top-level ``{...}`` slice.
    n = len(s)
    i = 0
    while i < n:
        if s[i] != "{":
            i += 1
            continue
        depth = 0
        j = i
        in_str = False
        escape = False
        while j < n:
            c = s[j]
            if escape:
                escape = False
            elif c == "\\" and in_str:
                escape = True
            elif c == '"':
                in_str = not in_str
            elif not in_str:
                if c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            obj = json.loads(s[i:j + 1])
                            if isinstance(obj, dict):
                                return obj
                        except (json.JSONDecodeError, ValueError):
                            pass
                        break
            j += 1
        i += 1
    return None


def _diff_line_count(a: str, b: str) -> int:
    """Count net edited lines (whitespace-only changes don't count) so the
    auto-apply heuristic ignores no-op reformatting."""
    import difflib
    al = [ln.rstrip() for ln in (a or "").splitlines()]
    bl = [ln.rstrip() for ln in (b or "").splitlines()]
    count = 0
    for line in difflib.unified_diff(al, bl, lineterm=""):
        if line.startswith(("+++", "---", "@@")):
            continue
        if line.startswith("+") or line.startswith("-"):
            count += 1
    return count


def _audit_improvement(skill_id: str, status: str, reason: str,
                       proposal_id: str | None, backup_path: str | None,
                       diff_lines: int, source: str = "auto") -> None:
    """Append one row to ``IMPROVEMENTS_LEDGER``. ``status`` is one of:
    applied, pending, rejected, no_change, failed, skipped."""
    row = {
        "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "skill": skill_id,
        "status": status,
        "source": source,
        "reason": reason or "",
        "diff_lines": diff_lines,
        "proposal_id": proposal_id,
        "backup": backup_path,
    }
    try:
        IMPROVEMENTS_LEDGER.parent.mkdir(parents=True, exist_ok=True)
        with IMPROVEMENTS_LEDGER.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, default=str) + "\n")
    except OSError:
        pass


def _write_proposal(skill_id: str, skill_path: Path, old: str, new: str,
                    parsed: dict, diff_lines: int, job_id: str) -> dict:
    """Persist a (proposal.json, .old.md, .new.md) triple under
    ``SKILL_PROPOSALS_DIR`` and return the proposal summary dict."""
    SKILL_PROPOSALS_DIR.mkdir(parents=True, exist_ok=True)
    ts_dt = _dt.datetime.now(_dt.timezone.utc)
    slug = re.sub(r"[^a-z0-9]+", "-", skill_id.lower()).strip("-") or "skill"
    pid = f"{slug}-{ts_dt.strftime('%Y%m%d-%H%M%S')}"
    payload = {
        "id": pid,
        "skill": skill_id,
        "skill_path": str(skill_path.relative_to(ROOT)).replace("\\", "/"),
        "ts": ts_dt.isoformat(timespec="seconds"),
        "job_id": job_id,
        "change_summary": parsed.get("change_summary", "") or "",
        "rationale": parsed.get("rationale", "") or "",
        "diff_lines": diff_lines,
        "status": "pending",
        "applied_at": None,
        "applied_via": None,
        "backup_path": None,
        "kind": "improve",
    }
    (SKILL_PROPOSALS_DIR / f"{pid}.json").write_text(
        json.dumps(payload, indent=2), encoding="utf-8")
    (SKILL_PROPOSALS_DIR / f"{pid}.old.md").write_text(old, encoding="utf-8")
    (SKILL_PROPOSALS_DIR / f"{pid}.new.md").write_text(new, encoding="utf-8")
    return payload


def _apply_improvement(skill_path: Path, new_content: str, source: str,
                       reason: str, proposal_id: str | None,
                       skill_id: str, diff_lines: int) -> bool:
    """Backup -> overwrite -> audit. Returns True on success. Skill files
    are git-tracked so a `git diff` is always available as a second safety
    net beyond the on-disk .bak."""
    SKILL_BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    ts = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
    slug = re.sub(r"[^a-z0-9]+", "-", skill_id.lower()).strip("-") or "skill"
    backup_path = SKILL_BACKUPS_DIR / f"{slug}-{ts}.md.bak"
    try:
        original = skill_path.read_text(encoding="utf-8")
        backup_path.write_text(original, encoding="utf-8")
        skill_path.write_text(new_content, encoding="utf-8")
    except OSError as e:
        _audit_improvement(skill_id, "failed", f"write error: {e}",
                           proposal_id, None, diff_lines, source=source)
        return False
    _audit_improvement(skill_id, "applied", reason, proposal_id,
                       str(backup_path), diff_lines, source=source)
    if proposal_id:
        pj = SKILL_PROPOSALS_DIR / f"{proposal_id}.json"
        if pj.is_file():
            try:
                obj = json.loads(pj.read_text(encoding="utf-8"))
                obj["status"] = "applied"
                obj["applied_at"] = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
                obj["applied_via"] = source
                obj["backup_path"] = str(backup_path.relative_to(ROOT)).replace("\\", "/")
                pj.write_text(json.dumps(obj, indent=2), encoding="utf-8")
            except (OSError, json.JSONDecodeError):
                pass
    return True


def _check_skill_regression(skill_id: str, cfg: dict) -> dict | None:
    """Decide whether the last ``applied`` improvement to this skill
    regressed enough to warrant auto-revert.

    Rules:
      * Find the most recent ``applied`` audit row for the skill.
      * Skip if there's already a later ``rolled_back`` / ``revert_failed``
        row for the same proposal (don't loop on the same revert).
      * Partition ``SKILL_METRICS_FILE`` rows into pre- and post-apply.
      * Need at least ``revert_after_n_uses`` post rows AND at least 1 pre
        row (so we have a baseline to compare against).
      * If pre_rate - post_rate >= ``revert_margin``, return the decision
        dict; else None.

    Returns ``{skill, proposal_id, backup_path, pre_rate, post_rate, n_pre,
    n_post}`` when a revert should fire, otherwise ``None``."""
    try:
        if not IMPROVEMENTS_LEDGER.is_file():
            return None
    except OSError:
        return None
    rows: list[dict] = []
    try:
        with IMPROVEMENTS_LEDGER.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    o = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if o.get("skill") == skill_id:
                    rows.append(o)
    except OSError:
        return None
    if not rows:
        return None

    last_applied = None
    for r in reversed(rows):
        if r.get("status") == "applied":
            last_applied = r
            break
    if not last_applied:
        return None
    apply_ts = _iso_to_epoch(last_applied.get("ts") or "")
    proposal_id = last_applied.get("proposal_id")
    backup = last_applied.get("backup")
    if not backup:
        return None
    # Already rolled back / revert tried for this proposal?
    for r in rows:
        if r.get("proposal_id") != proposal_id:
            continue
        if r.get("status") in ("rolled_back", "revert_failed") \
                and _iso_to_epoch(r.get("ts") or "") > apply_ts:
            return None

    try:
        if not SKILL_METRICS_FILE.is_file():
            return None
    except OSError:
        return None
    pre: list[dict] = []
    post: list[dict] = []
    try:
        with SKILL_METRICS_FILE.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    m = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                # Match either the raw id or the canonical short name so we
                # don't miss rows recorded with the plugin prefix.
                if m.get("name") != skill_id and m.get("skill") != skill_id:
                    continue
                ts = _iso_to_epoch(m.get("ts") or "")
                if ts < apply_ts:
                    pre.append(m)
                else:
                    post.append(m)
    except OSError:
        return None

    n_threshold = int(cfg.get("revert_after_n_uses", 5))
    margin = float(cfg.get("revert_margin", 0.2))
    if len(post) < n_threshold or len(pre) == 0:
        return None

    def _rate(samples: list[dict]) -> float:
        if not samples:
            return 0.0
        succ = sum(1 for s in samples if s.get("outcome") == "done")
        return succ / len(samples)

    pre_rate = _rate(pre)
    post_rate = _rate(post)
    if (pre_rate - post_rate) < margin:
        return None
    return {
        "skill": skill_id,
        "proposal_id": proposal_id,
        "backup_path": backup,
        "pre_rate": round(pre_rate, 4),
        "post_rate": round(post_rate, 4),
        "n_pre": len(pre),
        "n_post": len(post),
    }


def _auto_revert_skill(decision: dict) -> bool:
    """Restore the SKILL.md from its .bak and audit the rollback.

    Cross-checks the proposal JSON to find the canonical ``skill_path``
    (the audit row only stores the absolute backup path, not the target).
    Best-effort: any failure becomes a ``revert_failed`` audit row.
    Returns True when the revert succeeded."""
    skill_id = decision["skill"]
    proposal_id = decision.get("proposal_id") or ""
    backup_str = decision.get("backup_path") or ""
    backup_path = Path(backup_str)

    pj = SKILL_PROPOSALS_DIR / f"{proposal_id}.json" if proposal_id else None
    if not pj or not pj.is_file():
        _audit_improvement(skill_id, "revert_failed", "proposal json missing",
                           proposal_id or None, backup_str or None, 0,
                           source="auto")
        return False
    try:
        obj = json.loads(pj.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        _audit_improvement(skill_id, "revert_failed", f"proposal parse: {e}",
                           proposal_id, backup_str, 0, source="auto")
        return False
    rel = obj.get("skill_path") or ""
    try:
        skill_path = (ROOT / rel).resolve()
        skill_path.relative_to(ROOT.resolve())
    except (ValueError, OSError):
        _audit_improvement(skill_id, "revert_failed", "invalid skill_path",
                           proposal_id, backup_str, 0, source="auto")
        return False
    if not skill_path.is_file():
        _audit_improvement(skill_id, "revert_failed", "skill file missing",
                           proposal_id, backup_str, 0, source="auto")
        return False
    if not backup_path.is_file():
        _audit_improvement(skill_id, "revert_failed", "backup missing",
                           proposal_id, backup_str, 0, source="auto")
        return False

    try:
        backup_content = backup_path.read_text(encoding="utf-8")
        skill_path.write_text(backup_content, encoding="utf-8")
    except OSError as e:
        _audit_improvement(skill_id, "revert_failed", f"write error: {e}",
                           proposal_id, backup_str, 0, source="auto")
        return False

    reason = (f"auto-revert: success_rate pre={decision['pre_rate']:.2f} "
              f"({decision['n_pre']}j) post={decision['post_rate']:.2f} "
              f"({decision['n_post']}j)")
    _audit_improvement(skill_id, "rolled_back", reason, proposal_id,
                       backup_str, int(obj.get("diff_lines") or 0),
                       source="auto")
    try:
        obj["status"] = "rolled_back"
        obj["rolled_back_at"] = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
        obj["regression"] = {
            "pre_rate": decision["pre_rate"],
            "post_rate": decision["post_rate"],
            "n_pre": decision["n_pre"],
            "n_post": decision["n_post"],
        }
        pj.write_text(json.dumps(obj, indent=2), encoding="utf-8")
    except OSError:
        pass
    return True


def _post_job_skill_actions(job_id: str, skill_ids: list[str]) -> None:
    """Combined post-job hook: revert-first, then improve.

    Order is intentional. If a skill just regressed and we revert it, the
    throttle (which counts ALL improver audit rows including rolled_back)
    blocks the immediate re-improvement on the same job."""
    cfg = _load_improver_config()
    proj = _project_skill_index()
    canonical: list[str] = []
    seen: set[str] = set()
    for raw in skill_ids:
        n = _skill_name_canonical(raw)
        if n in seen:
            continue
        seen.add(n)
        if n in proj:
            canonical.append(n)
    # 1. Auto-revert pass — synchronous (cheap, in-process).
    for sid in canonical:
        try:
            decision = _check_skill_regression(sid, cfg)
        except Exception:  # noqa: BLE001
            continue
        if not decision:
            continue
        try:
            _auto_revert_skill(decision)
        except Exception:  # noqa: BLE001
            continue
    # 2. Improver pass — spawns one daemon thread per eligible skill.
    _trigger_improvers_for_job(job_id, skill_ids)


def _purge_claude_transcript(session_id: str | None) -> None:
    """Delete the per-session JSONL Claude Code wrote for a one-shot
    background call (e.g. an improver run). Without this every improver
    invocation pollutes ``~/.claude/projects/<slug>/`` with a stray
    "OUTPUT FORMAT (STRICT)" session row in the user's chat history.
    Best-effort: missing dir / missing file / OS errors are swallowed."""
    if not session_id:
        return
    try:
        tdir = _transcripts_dir_for_cwd(ROOT)
        if tdir is None:
            return
        f = tdir / f"{session_id}.jsonl"
        if f.is_file():
            f.unlink()
    except OSError:
        pass


def _run_improver_for_skill(skill_id: str, skill_md_path: Path,
                            job_id: str, log_path: str | None,
                            cfg: dict) -> None:
    """End-to-end: read skill -> call LLM -> parse JSON -> persist proposal
    -> auto-apply if small. Best-effort: any failure is audited and the
    function returns silently. When the tool is ``claude`` we generate a
    dedicated ``--session-id`` and delete the resulting transcript at
    exit so background improver runs never show up in the chat list."""
    try:
        skill_content = skill_md_path.read_text(encoding="utf-8")
    except OSError as e:
        _audit_improvement(skill_id, "failed", f"read error: {e}", None, None, 0)
        return
    metrics = _aggregate_skill_metrics().get(skill_id) or {}
    log_excerpt = _read_log_excerpt(log_path)
    prompt = _build_improver_prompt(skill_id, skill_content, metrics, job_id, log_excerpt)

    # IMPORTANT (Windows): pass the prompt via stdin, not argv. Long prompts
    # on argv silently fail (claude emits only a "status:ready" stub and never
    # processes the request) — observed empirically. stdin works for any size.
    tool_bin = shutil.which(cfg["tool"]) or cfg["tool"]
    argv = [tool_bin, "-p", "--model", cfg["model"]]
    # Pin a session id ONLY for claude — codex doesn't write per-session
    # JSONLs into ~/.claude/projects/ so it doesn't have the same pollution
    # problem. The id lets _purge_claude_transcript know exactly which file
    # to delete in the finally block below.
    improver_sid: str | None = None
    if cfg.get("tool") == "claude":
        improver_sid = str(uuid.uuid4())
        argv += ["--session-id", improver_sid]
    try:
        try:
            proc = subprocess.run(
                argv, cwd=str(ROOT), input=prompt,
                capture_output=True, text=True,
                timeout=cfg.get("timeout_seconds", 120), encoding="utf-8",
                errors="replace",
            )
        except (subprocess.TimeoutExpired, OSError) as e:
            _audit_improvement(skill_id, "failed", f"subprocess error: {e}", None, None, 0)
            return
        if proc.returncode != 0:
            _audit_improvement(skill_id, "failed",
                               f"exit {proc.returncode}: {(proc.stderr or '')[:200]}",
                               None, None, 0)
            return

        parsed = _parse_improver_output(proc.stdout or "")
        if not parsed:
            _audit_improvement(skill_id, "no_change", "improver returned unparseable output",
                               None, None, 0)
            return
        new_content = parsed.get("new_content")
        if not isinstance(new_content, str) or not new_content.strip():
            _audit_improvement(skill_id, "no_change",
                               parsed.get("rationale") or "improver returned null",
                               None, None, 0)
            return

        diff_lines = _diff_line_count(skill_content, new_content)
        if diff_lines == 0:
            _audit_improvement(skill_id, "no_change", "no effective change", None, None, 0)
            return

        proposal = _write_proposal(skill_id, skill_md_path, skill_content,
                                   new_content, parsed, diff_lines, job_id)
        if diff_lines <= int(cfg.get("small_change_max_lines", 6)):
            _apply_improvement(skill_md_path, new_content, source="auto",
                               reason=parsed.get("change_summary", "") or "",
                               proposal_id=proposal["id"], skill_id=skill_id,
                               diff_lines=diff_lines)
        else:
            _audit_improvement(skill_id, "pending",
                               parsed.get("change_summary", "") or "",
                               proposal["id"], None, diff_lines)
    finally:
        _purge_claude_transcript(improver_sid)


def _trigger_improvers_for_job(job_id: str, skill_ids: list[str]) -> None:
    """Spawn one improver thread per project skill invoked by ``job_id``.
    Throttled via ``IMPROVEMENTS_LEDGER`` and config-gated."""
    cfg = _load_improver_config()
    if not cfg.get("enabled"):
        return
    if not shutil.which(cfg["tool"]):
        return  # CLI not on PATH; silently skip
    proj = _project_skill_index()
    with JOBS_LOCK:
        j = JOBS.get(job_id)
        log_path = j.get("log_path") if j else None
    seen: set[str] = set()
    for raw in skill_ids:
        name = _skill_name_canonical(raw)
        if name in seen:
            continue
        seen.add(name)
        path = proj.get(name)
        if not path:
            continue
        # Throttle: don't re-improve within min_interval_seconds.
        last = _last_improver_run_ts(name)
        if last and (time.time() - last) < int(cfg.get("min_interval_seconds", 300)):
            continue
        threading.Thread(
            target=_run_improver_for_skill,
            args=(name, path, job_id, log_path, cfg),
            daemon=True,
            name=f"improver-{name}",
        ).start()


_STOPWORDS = frozenset({
    # English filler/imperatives that say nothing about the work itself.
    "a","an","the","is","are","be","to","of","for","on","in","with","and","or",
    "i","you","we","it","this","that","these","those","do","does","did","done",
    "have","has","had","not","no","my","your","our","at","by","as","also","but",
    "if","when","then","else","so","just","want","wanted","need","ok","please",
    "help","add","make","build","create","run","fix","new","now","one","two",
    "three","what","how","why","into","from","over","very","more","most",
    # Portuguese (the user's language)
    "o","a","os","as","um","uma","de","do","da","dos","das","e","ou","que","quero",
    "para","com","em","no","na","nos","nas","ao","aos","á","à","é","ser","ter","tem",
    "também","tambem","mais","tarde","apos","após","depois","cada","sobre","fazer",
    "vou","podes","pode","tens","esta","este","isto","como","onde","quando","ja",
    "já","aqui","ali","ai","aí","ate","até","sim","nao","não","mas","só","so",
    "tudo","nada","muito","pouco","bem","mal","todos","todas","seu","sua","seus",
    "suas","meu","minha","meus","minhas","nosso","nossa","vamos","vai","faz",
})


def _tokenize_task(s: str) -> set[str]:
    """Lowercase + strip non-alphanumeric + drop stopwords/short tokens.
    Returns a set of canonical tokens used for Jaccard similarity."""
    if not s:
        return set()
    cleaned = re.sub(r"[^0-9A-Za-zÀ-ÿ]+", " ", s.lower())
    return {t for t in cleaned.split() if len(t) >= 3 and t not in _STOPWORDS}


def _iso_to_epoch(s: str) -> float:
    """Lossy ISO-8601 -> epoch seconds; returns 0 on parse failure."""
    if not s:
        return 0.0
    try:
        return _dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return 0.0


def _load_unique_jobs(max_age_days: int = 30) -> list[dict]:
    """Replay ``JOBS_PERSIST_FILE`` keeping the last snapshot per id and
    filtering to ``max_age_days``. Skips jobs without a meaningful task
    (test-mode placeholders)."""
    snapshots: dict[str, dict] = {}
    try:
        if not JOBS_PERSIST_FILE.is_file():
            return []
    except OSError:
        return []
    try:
        with JOBS_PERSIST_FILE.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    o = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                jid = o.get("id")
                if jid:
                    snapshots[jid] = o  # last write wins
    except OSError:
        return []
    cutoff_epoch = _dt.datetime.now(_dt.timezone.utc).timestamp() - max_age_days * 86400
    keep: list[dict] = []
    for o in snapshots.values():
        task = (o.get("task") or "").strip()
        if not task or task.lower() in {"(noop)", "noop", "test", "test job", "x"}:
            continue
        created = o.get("created_at") or o.get("started_at") or ""
        ts = _iso_to_epoch(created)
        if ts and ts < cutoff_epoch:
            continue
        keep.append(o)
    return keep


def _detect_skill_suggestions(threshold: float = 0.5, min_cluster: int = 3,
                              max_age_days: int = 30) -> list[dict]:
    """Greedy cluster of recent jobs by task-token Jaccard similarity.

    Each surviving cluster surfaces as "this looks repeated, maybe make a
    skill out of it". Pure read-only: works off ``jobs.jsonl`` + the
    skill_metrics ledger. Returns clusters sorted by size desc, then most
    recently seen first."""
    jobs = _load_unique_jobs(max_age_days=max_age_days)
    fps: list[tuple[dict, set[str]]] = []
    for j in jobs:
        toks = _tokenize_task(j.get("task") or "")
        if len(toks) < 2:
            continue
        fps.append((j, toks))

    # Optional skill-sequence index per job (helps when tasks are short).
    skill_seqs: dict[str, list[str]] = {}
    try:
        if SKILL_METRICS_FILE.is_file():
            with SKILL_METRICS_FILE.open("r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line.startswith("{"):
                        continue
                    try:
                        row = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    jid = row.get("job_id")
                    sk = row.get("name") or row.get("skill")
                    if jid and sk:
                        skill_seqs.setdefault(jid, []).append(sk)
    except OSError:
        pass

    used = [False] * len(fps)
    clusters: list[dict] = []
    for i in range(len(fps)):
        if used[i]:
            continue
        ji, ti = fps[i]
        group_idx = [i]
        for j in range(i + 1, len(fps)):
            if used[j]:
                continue
            jj, tj = fps[j]
            inter = len(ti & tj)
            union = len(ti | tj) or 1
            jaccard = inter / union
            same_skills = (
                skill_seqs.get(ji.get("id"))
                and skill_seqs.get(ji.get("id")) == skill_seqs.get(jj.get("id"))
            )
            if jaccard >= threshold or same_skills:
                group_idx.append(j)
        if len(group_idx) < min_cluster:
            continue
        for idx in group_idx:
            used[idx] = True
        cluster_jobs = [fps[k][0] for k in group_idx]
        token_counter: dict[str, int] = {}
        for k in group_idx:
            for t in fps[k][1]:
                token_counter[t] = token_counter.get(t, 0) + 1
        top_tokens = sorted(token_counter.items(), key=lambda kv: (-kv[1], kv[0]))[:6]
        kinds = sorted({(j.get("kind") or "") for j in cluster_jobs if j.get("kind")})
        skills_in_cluster: set[str] = set()
        for j in cluster_jobs:
            for sk in skill_seqs.get(j.get("id") or "", []):
                skills_in_cluster.add(sk)
        last_seen = max(
            (j.get("ended_at") or j.get("started_at") or j.get("created_at") or "")
            for j in cluster_jobs
        )
        sample_tasks: list[str] = []
        seen_samples: set[str] = set()
        for j in cluster_jobs:
            t = (j.get("task") or "").strip()
            short = t[:140]
            if short and short not in seen_samples:
                sample_tasks.append(short)
                seen_samples.add(short)
            if len(sample_tasks) >= 3:
                break
        suggested_name = "-".join(t for t, _ in top_tokens[:3]) or "repeated-task"
        clusters.append({
            "id": suggested_name,
            "suggested_name": suggested_name,
            "size": len(cluster_jobs),
            "top_tokens": [t for t, _ in top_tokens],
            "sample_tasks": sample_tasks,
            "kinds": kinds,
            "skills_invoked": sorted(skills_in_cluster),
            "last_seen": last_seen,
            "job_ids": [j.get("id") for j in cluster_jobs],
        })

    # Filter out clusters that have already been addressed: either a project
    # skill with the same slug exists, or a previous draft proposal for this
    # cluster was installed / accepted / rejected. Keeps the suggestions
    # panel relevant — no nagging about work the user already did.
    covered_cluster_ids: set[str] = set()
    covered_names: set[str] = set(_project_skill_index().keys())
    try:
        if SKILL_PROPOSALS_DIR.is_dir():
            for pj in SKILL_PROPOSALS_DIR.glob("*.json"):
                try:
                    o = json.loads(pj.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if o.get("kind") != "draft":
                    continue
                if o.get("status") not in ("installed", "accepted", "rejected"):
                    continue
                cid = o.get("cluster_id")
                if cid:
                    covered_cluster_ids.add(cid)
                slug = o.get("skill") or o.get("suggested_name")
                if slug:
                    covered_names.add(slug)
    except OSError:
        pass
    clusters = [
        c for c in clusters
        if c.get("id") not in covered_cluster_ids
           and c.get("suggested_name") not in covered_names
    ]

    clusters.sort(key=lambda c: (-c["size"], -_iso_to_epoch(c.get("last_seen") or "")))
    return clusters


def _skill_name_canonical(raw: str) -> str:
    """Strip plugin namespace prefix from a skill id (``a:b:c`` -> ``c``)."""
    if not raw:
        return ""
    return raw.rsplit(":", 1)[-1].strip()


def _extract_skills_from_stream_json(path: Path) -> dict[str, int]:
    """Scan a stream-json log/transcript and return ``{skill_id: count}``
    aggregating every ``tool_use`` with ``name == "Skill"``. Returns an
    empty dict on any failure (missing file, parse errors, etc.).

    The ``Skill`` tool's input is shaped ``{"skill": "<id>"}``; we walk
    every top-level JSON object looking for nested ``tool_use`` blocks in
    common message shapes (Anthropic stream-json + IDE transcripts both
    nest tool_use inside ``message.content[]`` arrays)."""
    counts: dict[str, int] = {}
    try:
        if not path.is_file():
            return counts
    except OSError:
        return counts

    def _visit(node) -> None:
        if isinstance(node, dict):
            if node.get("type") == "tool_use" and node.get("name") == "Skill":
                sid = (node.get("input") or {}).get("skill")
                if isinstance(sid, str) and sid:
                    counts[sid] = counts.get(sid, 0) + 1
            for v in node.values():
                _visit(v)
        elif isinstance(node, list):
            for v in node:
                _visit(v)

    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or not line.startswith("{"):
                    continue
                try:
                    obj = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                _visit(obj)
    except OSError:
        return counts
    return counts


def _record_skill_metrics(job_id: str) -> int:
    """Append one row per skill invoked in a finished job to
    ``SKILL_METRICS_FILE``. Returns the number of rows written.

    Sources:
      * Entry-skill from job ``kind`` (orchestrate/plan jobs always credit
        their entry skill even when the log isn't parseable JSON).
      * Stream-json ``Skill`` tool_use events in the job's log/transcript.

    Best-effort: any OS or parse failure swallows silently so the runner
    never crashes."""
    with JOBS_LOCK:
        j = JOBS.get(job_id)
        if not j:
            return 0
        snapshot = {
            "job_id": j["id"],
            "kind": j.get("kind") or "",
            "status": j.get("status") or "",
            "exit_code": j.get("exit_code"),
            "started_at": j.get("started_at"),
            "ended_at": j.get("ended_at"),
            "log_path": j.get("log_path"),
            "cost": j.get("cost") or {},
            "session_id": j.get("session_id"),
            "model": j.get("model"),
        }

    counts: dict[str, int] = {}
    entry_skill = JOB_KINDS.get(snapshot["kind"])
    if entry_skill:
        counts[entry_skill] = counts.get(entry_skill, 0) + 1

    log_path = snapshot.get("log_path")
    if log_path:
        try:
            scanned = _extract_skills_from_stream_json(Path(log_path))
        except Exception:  # noqa: BLE001 - never crash the runner
            scanned = {}
        for sid, n in scanned.items():
            counts[sid] = counts.get(sid, 0) + n

    if not counts:
        return 0

    cost = snapshot["cost"] if isinstance(snapshot["cost"], dict) else {}
    outcome = "done" if snapshot["exit_code"] == 0 else (snapshot["status"] or "failed")
    ts = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    rows = []
    for sid, n in counts.items():
        rows.append({
            "ts": ts,
            "skill": sid,
            "name": _skill_name_canonical(sid),
            "job_id": snapshot["job_id"],
            "kind": snapshot["kind"],
            "outcome": outcome,
            "exit_code": snapshot["exit_code"],
            "duration_ms": int(cost.get("duration_ms") or 0),
            "cost_usd": float(cost.get("cost_usd") or 0.0),
            "turns": int(cost.get("turns") or 0),
            "invocations": n,
            "session_id": snapshot.get("session_id"),
            "model": snapshot.get("model"),
        })

    try:
        SKILL_METRICS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with SKILL_METRICS_FILE.open("a", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, default=str) + "\n")
    except OSError:
        return 0

    # Phase 2/auto-revert hook: revert-first, then improve. Both are
    # throttled + config-gated inside the helper so this call is cheap and
    # safe even when the improver is disabled.
    try:
        _post_job_skill_actions(job_id, list(counts.keys()))
    except Exception:  # noqa: BLE001 - never break the runner
        pass

    return len(rows)


def _aggregate_skill_metrics() -> dict[str, dict]:
    """Roll up ``SKILL_METRICS_FILE`` into per-skill summaries the dashboard
    can render in skill cards. Keyed by both the raw skill id and the
    canonical short name so callers can look up either flavor."""
    by_skill: dict[str, dict] = {}
    try:
        if not SKILL_METRICS_FILE.is_file():
            return by_skill
    except OSError:
        return by_skill
    try:
        with SKILL_METRICS_FILE.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    row = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                sid = row.get("skill") or ""
                if not sid:
                    continue
                agg = by_skill.setdefault(sid, {
                    "skill": sid,
                    "name": row.get("name") or _skill_name_canonical(sid),
                    "total_jobs": 0,
                    "total_invocations": 0,
                    "successes": 0,
                    "failures": 0,
                    "total_cost_usd": 0.0,
                    "total_duration_ms": 0,
                    "last_used": None,
                    "last_outcome": None,
                    "recent": [],
                })
                agg["total_jobs"] += 1
                agg["total_invocations"] += int(row.get("invocations") or 1)
                if row.get("outcome") == "done":
                    agg["successes"] += 1
                else:
                    agg["failures"] += 1
                agg["total_cost_usd"] += float(row.get("cost_usd") or 0.0)
                agg["total_duration_ms"] += int(row.get("duration_ms") or 0)
                ts = row.get("ts")
                if ts and (agg["last_used"] is None or ts > agg["last_used"]):
                    agg["last_used"] = ts
                    agg["last_outcome"] = row.get("outcome")
                agg["recent"].append({
                    "ts": ts,
                    "job_id": row.get("job_id"),
                    "kind": row.get("kind"),
                    "outcome": row.get("outcome"),
                    "cost_usd": row.get("cost_usd"),
                    "duration_ms": row.get("duration_ms"),
                })
    except OSError:
        return by_skill
    for agg in by_skill.values():
        total = agg["total_jobs"] or 1
        agg["success_rate"] = round(agg["successes"] / total, 4)
        agg["avg_cost_usd"] = round(agg["total_cost_usd"] / total, 6)
        agg["avg_duration_ms"] = int(agg["total_duration_ms"] / total)
        agg["recent"] = sorted(agg["recent"], key=lambda r: r.get("ts") or "", reverse=True)[:10]
    return by_skill


def _scan_skills_dir(skills_dir: Path) -> list[dict]:
    """Return one record per ``<skills_dir>/<name>/SKILL.md`` containing
    the frontmatter ``name`` + ``description`` and a repo-relative path
    (or absolute path if the dir lives outside the repo).

    Tolerates missing dirs and unreadable files — returns ``[]`` rather
    than raising so callers can compose results across multiple roots."""
    out: list[dict] = []
    try:
        if not skills_dir.is_dir():
            return out
        subs = sorted(skills_dir.iterdir())
    except OSError:
        return out
    for sub in subs:
        try:
            if not sub.is_dir():
                continue
        except OSError:
            continue
        skill_md = sub / "SKILL.md"
        if not skill_md.is_file():
            continue
        name = sub.name
        desc = ""
        try:
            text = skill_md.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # Minimal frontmatter parse: lines between leading ``---``.
        lines = text.splitlines()
        if lines and lines[0].strip() == "---":
            for ln in lines[1:]:
                if ln.strip() == "---":
                    break
                m = re.match(r"^(name|description)\s*:\s*(.+)$", ln)
                if m:
                    key, val = m.group(1), m.group(2).strip().strip('"\'')
                    if key == "name":
                        name = val
                    elif key == "description":
                        desc = val
        try:
            rel = str(skill_md.relative_to(ROOT)).replace("\\", "/")
        except ValueError:
            rel = str(skill_md).replace("\\", "/")
        out.append({"name": name, "description": desc, "path": rel})
    return out


def _read_yaml_field(path: Path, field: str) -> dict:
    """Minimal YAML helper to pull a top-level mapping like `session: {...}`.

    Avoids a PyYAML dependency. Only handles the simple two-line shape used in
    .ai/models.yaml. Returns an empty dict on any failure.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    out: dict[str, str] = {}
    in_block = False
    for line in text.splitlines():
        stripped = line.rstrip()
        if not in_block:
            if stripped.startswith(field + ":"):
                in_block = True
            continue
        if not stripped:
            break
        if not stripped.startswith((" ", "\t")):
            break
        m = re.match(r"^\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*:\s*(\S.*)?$", stripped)
        if not m:
            continue
        val = (m.group(2) or "").strip()
        val = val.split("#", 1)[0].strip()  # strip inline comment
        out[m.group(1)] = val.strip('"\'')
    return out


def _spawn_job(
    job_id: str,
    kind: str,
    task: str,
    resume_session_id: str | None = None,
    permission_mode: str | None = None,
    fork_session_id: str | None = None,
) -> None:
    """Spawn a job in a worker thread and capture stdout+stderr to a log file.

    Dispatches by kind:
      * ``orchestrate`` / ``plan`` -> one-shot ``claude -p <prompt>``.
      * ``chat``                   -> long-lived ``claude --print
                                       --input-format stream-json
                                       --output-format stream-json`` session
                                       driven by JSON messages on stdin.
    """
    session = _read_yaml_field(ROOT / ".ai" / "models.yaml", "session")
    model = session.get("model") or "claude-sonnet-4-6"
    claude_bin = shutil.which("claude") or "claude"

    if kind == "chat":
        # Three possible session strategies:
        #  - fresh:  generate a new session_id, no --resume.
        #  - resume: reuse the same session_id (continues that conversation).
        #  - fork:   --resume + --fork-session keeps the history but writes
        #            new turns into a freshly-generated session_id.
        source_sid = fork_session_id or resume_session_id
        session_id = source_sid or str(uuid.uuid4())
        argv = _build_chat_argv(
            model=model,
            session_id=session_id,
            claude_bin=claude_bin,
            resume=bool(source_sid),
            fork=bool(fork_session_id),
            permission_mode=permission_mode,
        )
        # Whether new or resumed, the operator's task is always the first
        # user turn fed to claude as a stream-json envelope. (For pure
        # "just resume to view history" the caller would have to bypass
        # this endpoint - the HTTP layer requires task.)
        initial_stdin = _chat_user_message(task) if task else None
        with JOBS_LOCK:
            JOBS[job_id]["command"] = " ".join(argv[1:])
            JOBS[job_id]["session_id"] = session_id
            JOBS[job_id]["model"] = model
        _start_subprocess_job(
            job_id=job_id,
            kind=kind,
            task=task,
            argv=argv,
            initial_stdin=initial_stdin,
        )
        return

    if kind == "chat-codex":
        codex_session = session if isinstance(session, dict) else {}
        # Codex uses its own session/model defaults; fall back to the claude
        # session.model if no separate codex one is configured.
        codex_model = codex_session.get("codex_model") or model
        codex_bin = shutil.which("codex") or "codex"
        argv = _build_codex_chat_argv(
            model=codex_model,
            session_id=resume_session_id,
            codex_bin=codex_bin,
        )
        initial_stdin = (task + "\n").encode("utf-8")  # prompt over stdin
        with JOBS_LOCK:
            JOBS[job_id]["command"] = " ".join(argv[1:])
            JOBS[job_id]["session_id"] = resume_session_id or ""
            JOBS[job_id]["model"] = codex_model
        _start_subprocess_job(
            job_id=job_id,
            kind=kind,
            task=task,
            argv=argv,
            initial_stdin=initial_stdin,
        )
        return

    skill = JOB_KINDS[kind]
    prompt = (
        f"Use the {skill} skill.\n"
        f"Task: {task}\n\n"
        "Run non-interactively: never ask the user a clarifying question. "
        "If you genuinely cannot proceed, emit the Escalation block defined "
        "in .ai/workflow/dispatch.md and exit non-zero."
    )

    with JOBS_LOCK:
        JOBS[job_id]["command"] = f"{claude_bin} -p ... --model {model}"
        JOBS[job_id]["model"] = model

    _start_subprocess_job(
        job_id=job_id,
        kind=kind,
        task=task,
        argv=[claude_bin, "-p", prompt, "--model", model],
    )


_VALID_PERMISSION_MODES = {"acceptEdits", "auto", "bypassPermissions", "default", "dontAsk", "plan"}


def _build_chat_argv(
    model: str,
    session_id: str,
    claude_bin: str | None = None,
    resume: bool = False,
    permission_mode: str | None = None,
    fork: bool = False,
) -> list[str]:
    """Build the argv for an interactive ``claude`` chat session that
    communicates via JSON on stdin and JSON events on stdout.

    Every flag is load-bearing:
      * ``--print``                  -> required by stream-json modes
      * ``--input-format  stream-json`` -> we feed user turns as JSON
      * ``--output-format stream-json`` -> claude emits events as JSON
      * ``--include-partial-messages``  -> token-by-token streaming
      * ``--verbose``                -> required alongside stream-json output
      * ``--session-id``             -> stable id so the conversation can be
                                        resumed later via ``--resume``
      * ``--dangerously-skip-permissions`` -> dashboard sessions can't
                                        answer permission prompts; the
                                        operator is the only consent gate.
    """
    if claude_bin is None:
        claude_bin = shutil.which("claude") or "claude"
    argv = [
        claude_bin,
        "--print",
        "--input-format", "stream-json",
        "--output-format", "stream-json",
        "--include-partial-messages",
        "--verbose",
        "--model", model,
    ]
    # Permission strategy: explicit mode wins; otherwise fall back to the
    # bypass flag (the dashboard subprocess has no UI to answer prompts).
    if permission_mode and permission_mode in _VALID_PERMISSION_MODES:
        argv += ["--permission-mode", permission_mode]
    else:
        argv += ["--dangerously-skip-permissions"]
    if resume:
        argv += ["--resume", session_id]
        if fork:
            argv += ["--fork-session"]
    else:
        argv += ["--session-id", session_id]
    return argv


def _chat_user_message(text_or_blocks) -> bytes:
    """Wrap a user turn as a stream-json envelope. Accepts either a plain
    string (single text block) or a list of pre-built content blocks
    (text / image / etc) so the composer can send multimodal messages."""
    if isinstance(text_or_blocks, list):
        content = text_or_blocks
    else:
        content = [{"type": "text", "text": text_or_blocks}]
    obj = {"type": "user", "message": {"role": "user", "content": content}}
    return (json.dumps(obj) + "\n").encode("utf-8")


def _build_codex_chat_argv(model: str, session_id: str | None, codex_bin: str | None = None) -> list[str]:
    """Build the argv for one turn of a codex chat.

    Codex is one-shot per invocation: each user turn spawns its own process.
    The first turn uses ``codex exec --json``; subsequent turns use ``codex
    exec resume <session_id> --json`` to continue the same conversation.

    The prompt is passed via stdin (``-`` in the codex CLI convention) so
    we don't have to shell-quote arbitrary user text.
    """
    if codex_bin is None:
        codex_bin = shutil.which("codex") or "codex"
    argv = [codex_bin, "exec"]
    if session_id:
        argv += ["resume", session_id]
    argv += [
        "--json",
        "--skip-git-repo-check",
        "-m", model,
        "-",  # read prompt from stdin
    ]
    return argv


def _start_subprocess_job(
    job_id: str,
    kind: str,
    task: str,
    argv: list[str],
    initial_stdin: bytes | None = None,
) -> None:
    """Launch a subprocess in a worker thread, with stdin/stdout wired for
    interactive streaming.

    Output is written to a per-job log file AND broadcast to any SSE listeners
    via ``_publish_chunk``. Stdin is kept open as a PIPE so callers can write
    to it via :func:`_send_to_stdin`.

    This is the underlying primitive behind :func:`_spawn_job`. Tests inject
    their own ``argv`` to avoid depending on the ``claude`` CLI.
    """
    # Make sure the registry entry exists (HTTP-spawned jobs already do this;
    # tests that call this helper directly may not).
    with JOBS_LOCK:
        if job_id not in JOBS:
            JOBS[job_id] = {
                "id": job_id,
                "kind": kind,
                "task": task,
                "status": "queued",
                "created_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
                "pid": None,
                "log_path": None,
                "exit_code": None,
                "started_at": None,
                "ended_at": None,
            }
        sid_for_path = JOBS[job_id].get("session_id")

    # For chat jobs with a known session_id we route ``log_path`` to
    # claude's own transcript file (which claude writes anyway via
    # ``--session-id``). This avoids duplicating storage in
    # ``.ai/dashboard/jobs/`` — the transcript is the single source of
    # truth for the conversation. The pump still forwards stdout chunks
    # to SSE subscribers so live streaming keeps working, but it no
    # longer writes them to a local ``.log`` file for chat jobs.
    log_path: Path
    write_log_to_disk = True
    if kind == "chat" and sid_for_path:
        tdir = _transcripts_dir_for_cwd(ROOT)
        if tdir is not None:
            log_path = tdir / f"{sid_for_path}.jsonl"
            write_log_to_disk = False
        else:
            JOBS_DIR.mkdir(parents=True, exist_ok=True)
            log_path = JOBS_DIR / f"{job_id}.log"
    else:
        JOBS_DIR.mkdir(parents=True, exist_ok=True)
        log_path = JOBS_DIR / f"{job_id}.log"

    with JOBS_LOCK:
        JOBS[job_id]["status"] = "running"
        JOBS[job_id]["started_at"] = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
        JOBS[job_id]["log_path"] = str(log_path)
        JOBS[job_id]["proc"] = None
        JOBS[job_id]["stdin_lock"] = threading.Lock()
        JOBS[job_id]["subscribers"] = []

    def runner() -> None:
        try:
            # Binary mode + manual UTF-8 encoding so Windows doesn't translate
            # `\n` to `\r\n` on write (which compounds with the subprocess
            # already emitting `\r\n` on Windows, producing `\r\r\n` on disk).
            logf = log_path.open("wb") if write_log_to_disk else None
            try:
                header = f"# job {job_id} kind={kind}\n# task: {task}\n\n"
                if logf is not None:
                    logf.write(header.encode("utf-8"))
                    logf.flush()
                _publish_chunk(job_id, header)

                proc = subprocess.Popen(
                    argv,
                    cwd=str(ROOT),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    bufsize=0,
                )
                with JOBS_LOCK:
                    JOBS[job_id]["pid"] = proc.pid
                    JOBS[job_id]["proc"] = proc

                # Send the initial stdin payload (e.g. the first user
                # message for a chat job) before we start pumping output.
                if initial_stdin is not None and proc.stdin is not None:
                    try:
                        proc.stdin.write(initial_stdin)
                        proc.stdin.flush()
                    except (BrokenPipeError, OSError):
                        pass

                # Pump stdout bytes -> log file (if enabled) + subscribers.
                # Normalise CRLF/CR to LF so the SSE stream and the on-disk
                # log show the same canonical line endings regardless of
                # platform. For chat jobs the log file is skipped — the
                # transcript that claude itself writes is the persistent
                # record.
                #
                # IMPORTANT: publish only COMPLETE lines (everything up to
                # the last ``\n``), keeping any trailing partial line in the
                # buffer for the next iteration. claude emits one JSON record
                # per line; if we forwarded raw 1024-byte chunks, a long
                # record (the 8KB SessionStart hook context) would arrive at
                # the client split mid-string, and any consumer that does
                # JSON.parse per line would choke on the partial halves.
                #
                # For chat-kind jobs we ALSO inspect each complete line for
                # ``type=result`` events and update the live cost counter as
                # they arrive (so cost stays accurate even though we no
                # longer keep a local log file to scan post-mortem).
                assert proc.stdout is not None
                line_buf = ""
                track_cost = kind in {"chat", "chat-codex"}
                while True:
                    chunk = proc.stdout.read(1024)
                    if not chunk:
                        break
                    text = chunk.decode("utf-8", errors="replace")
                    text = text.replace("\r\n", "\n").replace("\r", "\n")
                    line_buf += text
                    # Split off everything up to (and including) the last newline.
                    last_nl = line_buf.rfind("\n")
                    if last_nl < 0:
                        # No newline yet — keep accumulating, publish nothing.
                        continue
                    publishable = line_buf[: last_nl + 1]
                    line_buf = line_buf[last_nl + 1:]
                    if logf is not None:
                        logf.write(publishable.encode("utf-8"))
                        logf.flush()
                    _publish_chunk(job_id, publishable)
                    if track_cost:
                        for line in publishable.split("\n"):
                            line = line.strip()
                            if not line.startswith("{"):
                                continue
                            try:
                                obj = json.loads(line)
                            except (json.JSONDecodeError, ValueError):
                                continue
                            if obj.get("type") == "result":
                                _update_job_cost(job_id, obj)

                # Flush any final partial line (no trailing newline). Add a
                # synthetic ``\n`` so downstream line-based parsers can still
                # cleanly bound it. This is the EOF tail — typically empty.
                if line_buf:
                    final = line_buf + "\n"
                    if logf is not None:
                        logf.write(final.encode("utf-8"))
                        logf.flush()
                    _publish_chunk(job_id, final)

                exit_code = proc.wait()
            finally:
                if logf is not None:
                    logf.close()

            with JOBS_LOCK:
                j = JOBS[job_id]
                if j["status"] == "cancelling":
                    j["status"] = "cancelled"
                else:
                    j["status"] = "done" if exit_code == 0 else "failed"
                j["exit_code"] = exit_code
                j["ended_at"] = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
            _persist_job(job_id)  # final state -> ledger
            try:
                _record_skill_metrics(job_id)
            except Exception:  # noqa: BLE001 - never break the runner
                pass
            _publish_chunk(job_id, None)  # sentinel: stream end
        except Exception as e:  # noqa: BLE001 (don't crash the server)
            with JOBS_LOCK:
                JOBS[job_id]["status"] = "failed"
                JOBS[job_id]["error"] = str(e)
                JOBS[job_id]["ended_at"] = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
            _persist_job(job_id)
            _publish_chunk(job_id, None)

    threading.Thread(target=runner, daemon=True, name=f"job-{job_id}").start()


def _publish_chunk(job_id: str, chunk: str | None) -> None:
    """Push a log chunk (or None=EOF) to every SSE subscriber of this job."""
    with JOBS_LOCK:
        subs = list(JOBS.get(job_id, {}).get("subscribers") or [])
    for q in subs:
        try:
            q.put_nowait(chunk)
        except Exception:  # noqa: BLE001
            pass


def _send_chat_blocks(job_id: str, blocks: list[dict]) -> tuple[bool, str]:
    """Write a pre-built stream-json content-block array to the subprocess
    stdin (used when the composer sends images / inlined files)."""
    with JOBS_LOCK:
        j = JOBS.get(job_id)
        if not j:
            return False, "not found"
        if j["status"] != "running":
            return False, "job not running"
        proc = j.get("proc")
        lock = j.get("stdin_lock")
    if not proc or proc.stdin is None or proc.stdin.closed:
        return False, "stdin not available"
    payload = _chat_user_message(blocks)
    try:
        with lock:
            proc.stdin.write(payload)
            proc.stdin.flush()
    except (BrokenPipeError, OSError) as e:
        return False, f"write failed: {e}"
    return True, ""


def _interrupt_chat_turn(job_id: str) -> tuple[bool, str]:
    """Ask the running chat subprocess to abort the current generation.

    Uses the Claude Agent SDK's stream-json ``control_request`` protocol:
    a JSON envelope with ``subtype:"interrupt"`` written to stdin. The
    subprocess stays alive — only the in-flight turn is cancelled, so the
    session can keep going with the next user message.
    Returns ``(ok, error)``."""
    with JOBS_LOCK:
        j = JOBS.get(job_id)
        if not j:
            return False, "not found"
        if j["status"] != "running":
            return False, "job not running"
        if j.get("kind") != "chat":
            return False, "interrupt is only supported for chat-kind jobs"
        proc = j.get("proc")
        lock = j.get("stdin_lock")
    if not proc or proc.stdin is None or proc.stdin.closed:
        return False, "stdin not available"
    payload = json.dumps({
        "type": "control_request",
        "request_id": str(uuid.uuid4()),
        "request": {"subtype": "interrupt"},
    }).encode("utf-8") + b"\n"
    try:
        with lock:
            proc.stdin.write(payload)
            proc.stdin.flush()
    except (BrokenPipeError, OSError) as e:
        return False, f"write failed: {e}"
    return True, ""


def _send_to_stdin(job_id: str, text: str) -> tuple[bool, str]:
    """Write a user turn to the subprocess stdin. For ``chat`` jobs the
    text is JSON-wrapped as a stream-json user envelope; otherwise it's
    written as a plain line. Returns (ok, error)."""
    with JOBS_LOCK:
        j = JOBS.get(job_id)
        if not j:
            return False, "not found"
        if j["status"] != "running":
            return False, "job not running"
        proc = j.get("proc")
        lock = j.get("stdin_lock")
        kind = j.get("kind", "")
    if not proc or proc.stdin is None or proc.stdin.closed:
        return False, "stdin not available"
    if kind == "chat":
        payload = _chat_user_message(text)
    else:
        payload = (text if text.endswith("\n") else text + "\n").encode("utf-8", "replace")
    try:
        with lock:
            proc.stdin.write(payload)
            proc.stdin.flush()
    except (BrokenPipeError, OSError) as e:
        return False, f"write failed: {e}"
    return True, ""


def _cancel_job(job_id: str) -> bool:
    with JOBS_LOCK:
        j = JOBS.get(job_id)
        if not j or j["status"] not in {"running", "queued"}:
            return False
        j["status"] = "cancelling"
        pid = j.get("pid")
    _persist_job(job_id)
    if pid:
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], check=False, capture_output=True)
            else:
                os.kill(pid, 15)
        except (ProcessLookupError, OSError):
            pass
    return True


_PID_ALIVE_CACHE: dict[int, tuple[float, bool]] = {}
_PID_ALIVE_TTL_SECONDS = 2.0


def _pid_is_alive(pid: int) -> bool:
    """Cross-platform PID liveness check. Returns False only when we have
    high confidence the PID is gone; for uncertain cases (permission
    errors, OS quirks) we return True so we don't spuriously fail jobs.

    Results are cached for ``_PID_ALIVE_TTL_SECONDS`` because callers like
    ``_reconcile_running_pids`` run on every ``GET /api/jobs`` and on
    Windows each miss spawns a ``tasklist`` subprocess. The TTL is small
    enough that a freshly-dead PID is still detected within ~2s."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    now = time.monotonic()
    cached = _PID_ALIVE_CACHE.get(pid)
    if cached is not None and (now - cached[0]) < _PID_ALIVE_TTL_SECONDS:
        return cached[1]
    if os.name == "nt":
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
                capture_output=True, text=True, timeout=2,
            )
        except (OSError, subprocess.TimeoutExpired):
            # Tasklist failure is ambiguous — don't cache, don't fail jobs.
            return True
        alive = f'"{pid}"' in (out.stdout or "")
    else:
        try:
            os.kill(pid, 0)
            alive = True
        except ProcessLookupError:
            alive = False
        except (PermissionError, OSError):
            return True  # ambiguous — don't cache
    _PID_ALIVE_CACHE[pid] = (now, alive)
    # Bound cache size; on a tiny dashboard this rarely matters but keep it
    # from growing unbounded if many distinct PIDs are queried.
    if len(_PID_ALIVE_CACHE) > 256:
        for stale_pid in [k for k, (ts, _) in _PID_ALIVE_CACHE.items() if (now - ts) >= _PID_ALIVE_TTL_SECONDS]:
            _PID_ALIVE_CACHE.pop(stale_pid, None)
    return alive


def _reconcile_running_pids() -> int:
    """Flip jobs marked ``running`` / ``queued`` / ``cancelling`` whose
    tracked PID is no longer alive into ``failed``. Jobs whose ``proc``
    handle is still ours and still reports no exit are left alone — the
    runner thread will close them out. Returns the number of jobs
    reconciled so the caller can log it."""
    flipped: list[str] = []
    with JOBS_LOCK:
        for jid, j in list(JOBS.items()):
            if j.get("status") not in {"running", "queued", "cancelling"}:
                continue
            pid = j.get("pid")
            if not pid:
                continue
            proc = j.get("proc")
            if proc is not None:
                try:
                    rc = proc.poll()
                except Exception:  # noqa: BLE001
                    rc = None
                if rc is None:
                    continue
            if _pid_is_alive(int(pid)):
                continue
            j["status"] = "failed"
            j["error"] = j.get("error") or "subprocess exited (dead PID detected)"
            j["ended_at"] = j.get("ended_at") or _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
            flipped.append(jid)
    for jid in flipped:
        _persist_job(jid)
    return len(flipped)


def _evict_old_jobs() -> None:
    """Keep JOBS dict bounded; remove oldest finished entries when over cap."""
    with JOBS_LOCK:
        if len(JOBS) <= JOBS_MAX:
            return
        finished = [
            (jid, j) for jid, j in JOBS.items()
            if j["status"] in {"done", "failed", "cancelled"}
        ]
        finished.sort(key=lambda x: x[1].get("ended_at") or "")
        for jid, _j in finished[: len(JOBS) - JOBS_MAX]:
            JOBS.pop(jid, None)


# Record types considered "interesting" for a chat-pane catch-up dump.
# Hook outputs, queue ops, file snapshots and attachment frames are
# operational metadata the human reader never wants to see.
_CHAT_CATCHUP_INCLUDE_TYPES = frozenset({
    "user",            # user turn (own + injected wrappers — client filters further)
    "assistant",       # assistant turn (text/tool_use/thinking blocks)
    "tool_use_result", # tool result (replaces pill state on client)
    "tool_result",
    "result",          # turn-done meta (cost / duration / num_turns)
    "system",          # init/shutdown/error subtypes (client filters subtype)
    "ai-title",        # session title (renames pane header)
})


def _tail_chat_catchup(path: Path, max_records: int = 100) -> str:
    """Return up to ``max_records`` recent conversation records from a chat
    transcript / log file, suitable for SSE catch-up.

    Filters out IDE-transcript noise (attachment / queue-operation /
    file-history-snapshot / last-prompt / summary / compaction) so the
    browser only has to render the actual conversation, not megabytes of
    plumbing. Records keep their original encoded line; we just decide
    which ones survive."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return ""
    kept: list[str] = []
    for raw in lines:
        stripped = raw.strip()
        if not stripped:
            continue
        if not stripped.startswith("{"):
            # Plain-text noise (deprecation warnings, etc) — drop from catch-up.
            continue
        try:
            obj = json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            continue
        t = obj.get("type")
        if t not in _CHAT_CATCHUP_INCLUDE_TYPES:
            continue
        kept.append(raw if raw.endswith("\n") else raw + "\n")
    if len(kept) > max_records:
        kept = kept[-max_records:]
    return "".join(kept)


def _lookup_session_task(session_id: str) -> str | None:
    """Best-effort: read the first user message from the Claude transcript
    matching this session_id so the timeline row can show what the run was
    about. Returns None when the transcript is missing or unreadable —
    callers must treat that as "unknown task" without erroring."""
    if not session_id or session_id == "unknown":
        return None
    try:
        tdir = _transcripts_dir_for_cwd(ROOT)
    except OSError:
        return None
    if tdir is None or not tdir.is_dir():
        return None
    f = tdir / f"{session_id}.jsonl"
    if not f.is_file():
        return None
    try:
        with f.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("type") != "user":
                    continue
                content = (rec.get("message") or {}).get("content")
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
                    text = " ".join(p for p in parts if p)
                else:
                    text = ""
                text = " ".join(text.split())  # collapse whitespace
                if not text:
                    continue
                # Skip IDE/system-injected "user" messages so the row shows the
                # first REAL prompt. Claude Code wraps editor state, system
                # reminders, command output, and tool results in <tag>...</tag>
                # envelopes that arrive as type=user but aren't what the
                # operator typed. Tag pattern: lowercase letters / underscores
                # / hyphens. If a user prompt legitimately starts with '<'
                # (e.g. a code snippet), it would have a space or quote before
                # the closing '>'.
                if re.match(r"^<[a-z][a-z0-9_-]*>", text):
                    continue
                return text[:120] + ("…" if len(text) > 120 else "")
    except OSError:
        return None
    return None


def _load_timeline_runs(max_events: int = 500) -> list[dict]:
    """Aggregate `phase_dispatch` events from EVENTS_FILE into per-session runs.

    Each returned run has the shape::

        {
          "session_id": "<id or 'unknown'>",
          "task": "<first user message from transcript, or None>",
          "tag":  "<primary 'tool/model' for the run, or 'mixed'>",
          "started_at":       "<ISO ts of first phase>",
          "ended_at":         "<ISO ts of last phase>",
          "total_duration_ms": <int>,   # ended_at - started_at
          "phases": [
            {
              "phase": "plan" | "execute" | ... | "unknown",
              "tool":  "claude" | "codex",
              "model": "<str>",
              "exit_code": 0 | <int> | None,
              "end_ts": "<ISO ts>",
              "duration_ms": <int>,   # ts diff vs previous phase in same session; 0 for the first
              "status": "success" | "failure" | "pending",
            },
            ...
          ],
        }

    Runs are sorted by `ended_at` descending (newest first). Only the most
    recent `max_events` lines of the file are scanned so the endpoint stays
    cheap regardless of historical volume.
    """
    if not EVENTS_FILE.exists():
        return []
    try:
        raw_lines = EVENTS_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    tail = raw_lines[-max_events:] if len(raw_lines) > max_events else raw_lines

    by_session: dict[str, list[dict]] = {}
    for line in tail:
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("kind") != "phase_dispatch":
            continue
        sid = ev.get("session_id") or "unknown"
        by_session.setdefault(sid, []).append(ev)

    runs: list[dict] = []
    for sid, events in by_session.items():
        events.sort(key=lambda e: _parse_iso_ts(e.get("ts")) or _dt.datetime.min.replace(tzinfo=_dt.timezone.utc))
        phases: list[dict] = []
        prev_dt: _dt.datetime | None = None
        tag_counter: dict[str, int] = {}
        for ev in events:
            ts = ev.get("ts") or ""
            cur_dt = _parse_iso_ts(ts)
            if cur_dt is None or prev_dt is None:
                duration_ms = 0
            else:
                duration_ms = max(0, int((cur_dt - prev_dt).total_seconds() * 1000))
            exit_code = ev.get("exit_code")
            if exit_code is None:
                status = "pending"
            elif exit_code == 0:
                status = "success"
            else:
                status = "failure"
            tool = ev.get("tool") or "unknown"
            model = ev.get("model") or "unknown"
            tag_counter[f"{tool}/{model}"] = tag_counter.get(f"{tool}/{model}", 0) + 1
            phases.append({
                "phase": ev.get("phase") or "unknown",
                "tool": tool,
                "model": model,
                "exit_code": exit_code,
                "end_ts": ts,
                "duration_ms": duration_ms,
                "status": status,
            })
            if cur_dt is not None:
                prev_dt = cur_dt
        if not phases:
            continue

        start_dt = _parse_iso_ts(phases[0]["end_ts"])
        end_dt = _parse_iso_ts(phases[-1]["end_ts"])
        if start_dt and end_dt:
            total_duration_ms = max(0, int((end_dt - start_dt).total_seconds() * 1000))
        else:
            total_duration_ms = 0

        if len(tag_counter) == 1:
            tag = next(iter(tag_counter))
        elif tag_counter:
            tag = "mixed"
        else:
            tag = "unknown"

        runs.append({
            "session_id": sid,
            "task": _lookup_session_task(sid),
            "tag": tag,
            "started_at": phases[0]["end_ts"],
            "ended_at": phases[-1]["end_ts"],
            "total_duration_ms": total_duration_ms,
            "phases": phases,
        })

    runs.sort(key=lambda r: r["ended_at"] or "", reverse=True)
    return runs


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    # ----- routing -----
    def do_GET(self):  # noqa: N802 (stdlib signature)
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/list":
            self._handle_list(urllib.parse.parse_qs(parsed.query))
            return
        if parsed.path == "/api/jobs":
            self._handle_jobs_list()
            return
        if parsed.path == "/api/sessions":
            self._handle_sessions_list()
            return
        if parsed.path == "/api/transcripts":
            self._handle_transcripts_list()
            return
        if parsed.path == "/api/usage/total":
            self._handle_usage_total()
            return
        if parsed.path == "/api/timeline":
            self._handle_timeline()
            return
        m = re.fullmatch(r"/api/transcripts/([0-9a-fA-F-]+)/stream", parsed.path)
        if m:
            self._handle_transcript_stream(m.group(1))
            return
        if parsed.path == "/api/skills":
            self._handle_skills_list()
            return
        if parsed.path == "/api/skills/all":
            self._handle_skills_all()
            return
        if parsed.path == "/api/skills/metrics":
            self._handle_skills_metrics(urllib.parse.parse_qs(parsed.query))
            return
        if parsed.path == "/api/skills/suggestions":
            self._handle_skills_suggestions(urllib.parse.parse_qs(parsed.query))
            return
        if parsed.path == "/api/skills/content":
            self._handle_skill_content(urllib.parse.parse_qs(parsed.query))
            return
        if parsed.path == "/api/skills/improvements":
            self._handle_skill_improvements(urllib.parse.parse_qs(parsed.query))
            return
        if parsed.path == "/api/skills/proposals":
            self._handle_proposals_list()
            return
        m = re.fullmatch(r"/api/skills/proposals/([A-Za-z0-9_\-]+)", parsed.path)
        if m:
            self._handle_proposal_get(m.group(1))
            return
        if parsed.path == "/api/files/list":
            self._handle_files_list(urllib.parse.parse_qs(parsed.query))
            return
        if parsed.path == "/api/files/read":
            self._handle_file_read(urllib.parse.parse_qs(parsed.query))
            return
        m = re.fullmatch(r"/api/jobs/([0-9a-f-]+)/stream", parsed.path)
        if m:
            self._handle_job_stream(m.group(1))
            return
        m = re.fullmatch(r"/api/jobs/([0-9a-f-]+)", parsed.path)
        if m:
            self._handle_job_get(m.group(1), urllib.parse.parse_qs(parsed.query))
            return
        super().do_GET()

    def do_POST(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        body = self._read_json_body()
        if body is None:
            return  # already responded with 400
        if parsed.path == "/api/memory":
            self._handle_memory(body)
        elif parsed.path == "/api/decisions":
            self._handle_decisions(body)
        elif parsed.path == "/api/events/clear":
            self._handle_events_clear()
        elif parsed.path == "/api/models/dispatch_mode":
            self._handle_dispatch_mode(body)
        elif parsed.path == "/api/models/phase":
            self._handle_phase_update(body)
        elif parsed.path == "/api/jobs":
            self._handle_jobs_create(body)
        else:
            m = re.fullmatch(r"/api/jobs/([0-9a-f-]+)/cancel", parsed.path)
            if m:
                self._handle_job_cancel(m.group(1))
                return
            m = re.fullmatch(r"/api/jobs/([0-9a-f-]+)/input", parsed.path)
            if m:
                self._handle_job_input(m.group(1), body)
                return
            m = re.fullmatch(r"/api/jobs/([0-9a-f-]+)/interrupt", parsed.path)
            if m:
                self._handle_job_interrupt(m.group(1))
                return
            m = re.fullmatch(r"/api/skills/proposals/([A-Za-z0-9_\-]+)/(accept|reject)", parsed.path)
            if m:
                self._handle_proposal_decision(m.group(1), m.group(2))
                return
            m = re.fullmatch(r"/api/skills/suggestions/([A-Za-z0-9_\-]+)/draft", parsed.path)
            if m:
                self._handle_suggestion_draft(m.group(1))
                return
            self._json(404, {"error": "unknown endpoint", "path": parsed.path})

    # ----- helpers -----
    def _read_json_body(self) -> dict | None:
        length = int(self.headers.get("Content-Length") or "0")
        if length <= 0:
            return {}
        if length > 64 * 1024:
            self._json(413, {"error": "payload too large"})
            return None
        try:
            raw = self.rfile.read(length).decode("utf-8")
            return json.loads(raw) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            self._json(400, {"error": "invalid JSON", "detail": str(e)})
            return None

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:  # quieter logs
        sys.stderr.write(f"[dashboard] {fmt % args}\n")

    # ----- GET handlers -----
    def _handle_list(self, qs: dict[str, list[str]]) -> None:
        rel = (qs.get("path", [""])[0] or "").lstrip("/").replace("\\", "/")
        target = (ROOT / rel).resolve()
        try:
            target.relative_to(ROOT)
        except ValueError:
            self._json(403, {"error": "path outside repo root"})
            return
        if not target.is_dir():
            self._json(404, {"error": "not a directory", "path": rel})
            return
        entries = sorted(p.name for p in target.iterdir() if not p.name.startswith("."))
        self._json(200, {"path": rel, "entries": entries})

    # ----- POST handlers -----
    def _handle_memory(self, body: dict) -> None:
        topic = (body.get("topic") or "").strip()
        fact = (body.get("fact") or "").strip()
        if not topic or not fact:
            self._json(400, {"error": "topic and fact are required"})
            return
        if not re.fullmatch(r"[a-z0-9_-]{1,32}", topic):
            self._json(400, {"error": "topic must be lowercase letters, digits, '-' or '_' (max 32)"})
            return
        if len(fact) > 500:
            self._json(400, {"error": "fact must be 500 chars or fewer"})
            return
        # Single-line fact: collapse whitespace
        fact_single = " ".join(fact.split())
        date = _dt.date.today().strftime("%Y-%m-%d")
        line = f"- {date} [{topic}] {fact_single}\n"
        path = ROOT / ".ai" / "memory.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            existing = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            existing = ""
        if existing and not existing.endswith("\n"):
            existing += "\n"
        _write_text_lf(path, existing + line)
        self._json(200, {"ok": True, "line": line.rstrip()})

    def _handle_decisions(self, body: dict) -> None:
        date = (body.get("date") or _dt.date.today().strftime("%Y-%m-%d")).strip()
        decision = (body.get("decision") or "").strip()
        why = (body.get("why") or "").strip()
        consequence = (body.get("consequence") or "").strip()
        revisit = (body.get("revisit") or "").strip()
        if not decision or not why:
            self._json(400, {"error": "decision and why are required"})
            return
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
            self._json(400, {"error": "date must be YYYY-MM-DD"})
            return
        for label, val in [("decision", decision), ("why", why), ("consequence", consequence), ("revisit", revisit)]:
            if len(val) > 1000:
                self._json(400, {"error": f"{label} must be 1000 chars or fewer"})
                return
        entry = (
            f"\n## {date} — {decision.splitlines()[0]}\n"
            f"- Date: {date}\n"
            f"- Decision: {decision}\n"
            f"- Why: {why}\n"
            f"- Consequence: {consequence or '—'}\n"
            f"- Revisit conditions: {revisit or '—'}\n"
        )
        path = ROOT / ".ai" / "decisions.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            existing = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            existing = ""
        if existing and not existing.endswith("\n"):
            existing += "\n"
        _write_text_lf(path, existing + entry)
        self._json(200, {"ok": True, "entry": entry})

    def _handle_events_clear(self) -> None:
        path = ROOT / ".ai" / "events.jsonl"
        try:
            if path.exists():
                path.unlink()
        except OSError as e:
            self._json(500, {"error": "could not clear events", "detail": str(e)})
            return
        self._json(200, {"ok": True})

    # ----- jobs -----
    def _job_summary(self, j: dict) -> dict:
        out = {
            "id": j["id"],
            "kind": j["kind"],
            "task": j["task"][:120],
            "status": j["status"],
            "created_at": j.get("created_at"),
            "started_at": j.get("started_at"),
            "ended_at": j.get("ended_at"),
            "exit_code": j.get("exit_code"),
            "command": j.get("command"),
            "session_id": j.get("session_id"),
            "model": j.get("model"),
            "tags": list(j.get("tags") or []),
        }
        # Chat jobs surface aggregated cost so the UI can show running
        # totals. Prefer the live counter (updated by the pump in real
        # time); fall back to scanning the log file post-hoc for older
        # jobs whose live counter wasn't tracked. For terminal jobs the
        # scanned cost is cached back onto the job entry so subsequent
        # /api/jobs polls don't re-scan the log file every time.
        if j.get("kind") in {"chat", "chat-codex"}:
            live = j.get("cost")
            if isinstance(live, dict):
                out["cost"] = live
            elif j.get("log_path"):
                cost = _extract_cost_from_log(Path(j["log_path"]))
                if cost is not None:
                    out["cost"] = cost
                    if j.get("status") in _TERMINAL_JOB_STATUSES:
                        j["cost"] = cost
        return out

    def _handle_jobs_create(self, body: dict) -> None:
        kind = (body.get("kind") or "").strip()
        task = (body.get("task") or "").strip()
        resume_session_id = (body.get("resume_session_id") or "").strip() or None
        if kind not in JOB_KINDS:
            self._json(400, {"error": f"kind must be one of {sorted(JOB_KINDS)}"})
            return
        if not task:
            self._json(400, {"error": "task is required"})
            return
        if len(task) > 4000:
            self._json(400, {"error": "task must be 4000 chars or fewer"})
            return
        if resume_session_id and len(resume_session_id) > 80:
            self._json(400, {"error": "resume_session_id must be 80 chars or fewer"})
            return
        # Optional fork: like resume but adds --fork-session so the new
        # turns land in a fresh session id instead of overwriting the
        # original transcript.
        fork_session_id = (body.get("fork_session_id") or "").strip() or None
        if fork_session_id and len(fork_session_id) > 80:
            self._json(400, {"error": "fork_session_id must be 80 chars or fewer"})
            return
        if fork_session_id and resume_session_id:
            self._json(400, {"error": "set either fork_session_id or resume_session_id, not both"})
            return
        # Optional permission mode for chat jobs. Falls back to the legacy
        # ``--dangerously-skip-permissions`` flag when not provided so the
        # default behaviour is unchanged.
        permission_mode = (body.get("permission_mode") or "").strip() or None
        if permission_mode and permission_mode not in _VALID_PERMISSION_MODES:
            self._json(400, {"error": f"permission_mode must be one of {sorted(_VALID_PERMISSION_MODES)}"})
            return
        # Optional tag list: lowercase short slugs only, max 8 tags per job,
        # so the persistence ledger stays small and the resume filter works.
        tags_raw = body.get("tags") or []
        if not isinstance(tags_raw, list):
            self._json(400, {"error": "tags must be a list of strings"})
            return
        if len(tags_raw) > 8:
            self._json(400, {"error": "max 8 tags per job"})
            return
        tags: list[str] = []
        for t in tags_raw:
            if not isinstance(t, str):
                self._json(400, {"error": "tags must be strings"})
                return
            t = t.strip()
            if not t:
                continue
            if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,23}", t):
                self._json(400, {"error": f"invalid tag {t!r} (lowercase a-z0-9_-, 1-24 chars)"})
                return
            if t not in tags:
                tags.append(t)
        # The specific CLI we need depends on the kind.
        required_bin = "codex" if kind == "chat-codex" else "claude"
        if not shutil.which(required_bin):
            self._json(503, {"error": f"`{required_bin}` CLI not found on PATH"})
            return
        job_id = str(uuid.uuid4())
        now = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
        with JOBS_LOCK:
            JOBS[job_id] = {
                "id": job_id,
                "kind": kind,
                "task": task,
                "status": "queued",
                "created_at": now,
                "pid": None,
                "log_path": None,
                "exit_code": None,
                "started_at": None,
                "ended_at": None,
                "tags": tags,
            }
        _spawn_job(
            job_id,
            kind,
            task,
            resume_session_id=resume_session_id,
            permission_mode=permission_mode,
            fork_session_id=fork_session_id,
        )
        _persist_job(job_id)  # capture the initial queued/running snapshot
        _evict_old_jobs()
        with JOBS_LOCK:
            self._json(201, self._job_summary(JOBS[job_id]))

    def _handle_jobs_list(self) -> None:
        # Reconcile dead PIDs first so the response never shows zombie
        # "running" rows whose subprocess was killed externally. Cheap on
        # POSIX (signal-0); a few-ms tasklist call on Windows.
        _reconcile_running_pids()
        with JOBS_LOCK:
            items = [self._job_summary(j) for j in JOBS.values()]
        items.sort(key=lambda x: x.get("created_at") or "", reverse=True)
        self._json(200, {"jobs": items})

    def _handle_transcripts_list(self) -> None:
        """List the IDE transcript files for the current repo - the JSONL
        files Claude Code (the VSCode/Cursor extension) writes for every
        session in ``~/.claude/projects/<slug>/<session_id>.jsonl``.

        Each entry carries a ``task`` preview (first real user message) so
        the Resume-session picker can show a meaningful label, not just the
        session UUID."""
        tdir = _transcripts_dir_for_cwd(ROOT)
        if tdir is None:
            self._json(200, {"transcripts": [], "note": "no ~/.claude/projects directory for this repo"})
            return
        files = sorted(tdir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        # Cap task-preview reads so very large project histories don't slow
        # down the picker. Older entries still appear in the list, just
        # without a task preview.
        TASK_PREVIEW_LIMIT = 60
        items = []
        for idx, p in enumerate(files):
            try:
                st = p.stat()
            except OSError:
                continue
            task = _lookup_session_task(p.stem) if idx < TASK_PREVIEW_LIMIT else None
            items.append({
                "session_id": p.stem,
                "size_bytes": st.st_size,
                "modified": _dt.datetime.fromtimestamp(st.st_mtime, _dt.timezone.utc).isoformat(timespec="seconds"),
                "path": str(p.relative_to(tdir.parent)),
                "task": task,
            })
        self._json(200, {"transcripts": items, "dir": str(tdir)})

    def _handle_usage_total(self) -> None:
        """Aggregate token usage across every Claude transcript for this
        repo. Powers the overview's "Tokens used" card."""
        self._json(200, _aggregate_project_token_usage())

    def _handle_timeline(self) -> None:
        """Pipeline Gantt data — phase_dispatch events from .ai/events.jsonl
        grouped per session_id. Powers the Timeline view."""
        self._json(200, {"runs": _load_timeline_runs()})

    # ----- composer helpers (skills, files) -----

    def _handle_skills_list(self) -> None:
        """List slash-command skills the composer can autocomplete. Reads
        the ``name`` + ``description`` frontmatter from every
        ``.claude/skills/<name>/SKILL.md`` in the repo."""
        skills_dir = ROOT / ".claude" / "skills"
        items = [
            {"name": e["name"], "description": e["description"]}
            for e in _scan_skills_dir(skills_dir)
        ]
        self._json(200, {"skills": items})

    def _handle_skills_all(self) -> None:
        """Consolidated skill catalog across both models.

        Reads three locations and emits one flat list plus a per-source
        summary so the dashboard can render group cards + a filterable grid:
          * ``project``       -> ``<repo>/.claude/skills``  (workflow skills)
          * ``claude_global`` -> ``~/.claude/skills``       (Claude user skills)
          * ``codex_global``  -> ``~/.codex/skills``        (Codex user skills)

        Each entry carries ``metrics: null`` as a forward-looking hook for
        the auto skill-improver that will record per-skill performance
        after jobs."""
        home = Path.home()
        sources = [
            ("project",       "Project workflow", "claude", ROOT / ".claude" / "skills"),
            ("claude_global", "Claude (global)",  "claude", home / ".claude" / "skills"),
            ("codex_global",  "Codex (global)",   "codex",  home / ".codex"   / "skills"),
        ]
        metrics_by_skill = _aggregate_skill_metrics()
        # Build a secondary index keyed by canonical short name so on-disk
        # skills (which usually carry no plugin prefix) still find matching
        # telemetry rows recorded with the prefix.
        metrics_by_name: dict[str, dict] = {}
        for agg in metrics_by_skill.values():
            n = agg.get("name") or ""
            if n and n not in metrics_by_name:
                metrics_by_name[n] = agg
        all_skills: list[dict] = []
        source_meta: dict[str, dict] = {}
        for src_id, label, tool, path in sources:
            entries = _scan_skills_dir(path)
            for e in entries:
                metrics = metrics_by_skill.get(e["name"]) or metrics_by_name.get(e["name"])
                all_skills.append({
                    "name": e["name"],
                    "description": e["description"],
                    "path": e["path"],
                    "source": src_id,
                    "source_label": label,
                    "tool": tool,
                    "metrics": metrics,
                })
            source_meta[src_id] = {
                "label": label,
                "tool": tool,
                "path": str(path),
                "exists": path.is_dir(),
                "count": len(entries),
            }
        self._json(200, {"skills": all_skills, "sources": source_meta})

    def _handle_skills_suggestions(self, qs: dict[str, list[str]]) -> None:
        """Detect clusters of repeated work in the persistent job ledger and
        propose them as candidate skills. Pure read; no LLM call.

        Tunable via query params: ``threshold`` (0..1, default 0.5),
        ``min_cluster`` (default 3), ``days`` (default 30)."""
        def _qfloat(key: str, default: float) -> float:
            try:
                return float(qs.get(key, [str(default)])[0])
            except (TypeError, ValueError):
                return default

        def _qint(key: str, default: int) -> int:
            try:
                return int(qs.get(key, [str(default)])[0])
            except (TypeError, ValueError):
                return default

        threshold = max(0.0, min(1.0, _qfloat("threshold", 0.5)))
        min_cluster = max(2, _qint("min_cluster", 3))
        days = max(1, min(365, _qint("days", 30)))
        try:
            clusters = _detect_skill_suggestions(
                threshold=threshold,
                min_cluster=min_cluster,
                max_age_days=days,
            )
        except Exception as e:  # noqa: BLE001 - never break the dashboard
            self._json(500, {"error": "detector failed", "detail": str(e)})
            return
        self._json(200, {
            "suggestions": clusters,
            "params": {"threshold": threshold, "min_cluster": min_cluster, "days": days},
        })

    def _handle_proposals_list(self) -> None:
        """List every proposal under ``SKILL_PROPOSALS_DIR`` with status."""
        items: list[dict] = []
        if SKILL_PROPOSALS_DIR.is_dir():
            for p in sorted(SKILL_PROPOSALS_DIR.glob("*.json"),
                            key=lambda x: x.stat().st_mtime, reverse=True):
                try:
                    obj = json.loads(p.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                # Compact summary; full body is fetched via the detail endpoint.
                items.append({
                    "id": obj.get("id"),
                    "skill": obj.get("skill"),
                    "skill_path": obj.get("skill_path"),
                    "ts": obj.get("ts"),
                    "kind": obj.get("kind") or "improve",
                    "status": obj.get("status") or "pending",
                    "change_summary": obj.get("change_summary", ""),
                    "diff_lines": obj.get("diff_lines"),
                    "applied_at": obj.get("applied_at"),
                    "applied_via": obj.get("applied_via"),
                    "job_id": obj.get("job_id"),
                })
        self._json(200, {"proposals": items})

    def _handle_proposal_get(self, proposal_id: str) -> None:
        """Return one proposal with old + new content for diff rendering."""
        pj = SKILL_PROPOSALS_DIR / f"{proposal_id}.json"
        if not pj.is_file():
            self._json(404, {"error": "proposal not found"})
            return
        try:
            obj = json.loads(pj.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            self._json(500, {"error": "could not read proposal", "detail": str(e)})
            return
        for key, fname in (("old_content", f"{proposal_id}.old.md"),
                           ("new_content", f"{proposal_id}.new.md")):
            p = SKILL_PROPOSALS_DIR / fname
            try:
                obj[key] = p.read_text(encoding="utf-8") if p.is_file() else ""
            except OSError:
                obj[key] = ""
        self._json(200, obj)

    def _handle_proposal_decision(self, proposal_id: str, decision: str) -> None:
        """Apply or reject a pending proposal. Accept writes the new content
        to the skill path (with .bak backup); reject just marks the proposal
        rejected and leaves the skill untouched."""
        pj = SKILL_PROPOSALS_DIR / f"{proposal_id}.json"
        if not pj.is_file():
            self._json(404, {"error": "proposal not found"})
            return
        try:
            obj = json.loads(pj.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            self._json(500, {"error": "could not read proposal", "detail": str(e)})
            return
        # Drafts may be in status="accepted" from the older proposal-only
        # behaviour. Allow re-accepting those so the user can retro-install.
        is_redo_draft = (decision == "accept"
                         and obj.get("kind") == "draft"
                         and obj.get("status") == "accepted")
        if obj.get("status") not in (None, "pending") and not is_redo_draft:
            self._json(409, {"error": f"proposal already {obj.get('status')}"})
            return

        if decision == "reject":
            obj["status"] = "rejected"
            obj["applied_at"] = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
            try:
                pj.write_text(json.dumps(obj, indent=2), encoding="utf-8")
            except OSError as e:
                self._json(500, {"error": "write failed", "detail": str(e)})
                return
            _audit_improvement(obj.get("skill") or "", "rejected",
                               obj.get("change_summary", ""),
                               proposal_id, None,
                               int(obj.get("diff_lines") or 0),
                               source="manual")
            self._json(200, {"ok": True, "id": proposal_id, "status": "rejected"})
            return

        # decision == "accept"
        kind = obj.get("kind") or "improve"
        if kind == "draft":
            # New-skill draft: create the real skill file at
            # .claude/skills/<slug>/SKILL.md. Refuse to overwrite an
            # existing skill — the user must reject + re-draft (or rename)
            # if there's a collision.
            slug_raw = obj.get("skill") or obj.get("suggested_name") or ""
            slug = re.sub(r"[^a-z0-9-]+", "-", slug_raw.lower()).strip("-")
            if not slug or len(slug) > 80:
                self._json(400, {"error": f"invalid skill slug: {slug_raw!r}"})
                return
            target_dir = ROOT / ".claude" / "skills" / slug
            target_md = target_dir / "SKILL.md"
            if target_md.is_file():
                self._json(409, {
                    "error": "skill already exists at target path",
                    "target_path": f".claude/skills/{slug}/SKILL.md",
                    "hint": "Reject this draft and rename the slug, or "
                            "delete the existing skill first.",
                })
                return
            new_md = SKILL_PROPOSALS_DIR / f"{proposal_id}.new.md"
            try:
                new_content = new_md.read_text(encoding="utf-8")
            except OSError as e:
                self._json(500, {"error": "could not read draft body", "detail": str(e)})
                return
            try:
                target_dir.mkdir(parents=True, exist_ok=True)
                target_md.write_text(new_content, encoding="utf-8")
            except OSError as e:
                self._json(500, {"error": "write failed", "detail": str(e)})
                return
            target_rel = f".claude/skills/{slug}/SKILL.md"
            obj["status"] = "installed"
            obj["applied_at"] = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
            obj["applied_via"] = "manual"
            obj["target_path"] = target_rel
            obj["installed_path"] = target_rel
            try:
                pj.write_text(json.dumps(obj, indent=2), encoding="utf-8")
            except OSError:
                pass  # file is already on disk; audit it anyway
            _audit_improvement(slug, "installed",
                               f"draft installed -> {target_rel}",
                               proposal_id, None,
                               int(obj.get("diff_lines") or 0),
                               source="manual")
            self._json(200, {
                "ok": True, "id": proposal_id, "status": "installed",
                "installed_path": target_rel,
                "note": f"Skill created at {target_rel}.",
            })
            return

        # kind == "improve": apply to the actual skill file.
        rel = obj.get("skill_path") or ""
        try:
            skill_path = (ROOT / rel).resolve()
            skill_path.relative_to(ROOT.resolve())
        except (ValueError, OSError):
            self._json(400, {"error": "invalid skill_path on proposal"})
            return
        if not skill_path.is_file():
            self._json(404, {"error": "skill file no longer exists", "path": rel})
            return
        new_md = SKILL_PROPOSALS_DIR / f"{proposal_id}.new.md"
        try:
            new_content = new_md.read_text(encoding="utf-8")
        except OSError as e:
            self._json(500, {"error": "could not read proposal body", "detail": str(e)})
            return
        ok = _apply_improvement(
            skill_path, new_content,
            source="manual",
            reason=obj.get("change_summary", "") or "",
            proposal_id=proposal_id,
            skill_id=obj.get("skill") or skill_path.parent.name,
            diff_lines=int(obj.get("diff_lines") or 0),
        )
        if not ok:
            self._json(500, {"error": "apply failed (see improvements.jsonl)"})
            return
        self._json(200, {"ok": True, "id": proposal_id, "status": "applied"})

    def _handle_suggestion_draft(self, cluster_id: str) -> None:
        """Phase 5: dispatch an LLM to draft a SKILL.md from a suggestion
        cluster. Saves the result as a ``kind=draft`` proposal — never
        writes into ``.claude/skills/`` directly."""
        # Find the cluster (clusters are computed on demand, no persistence).
        clusters = _detect_skill_suggestions()
        cluster = next((c for c in clusters if c.get("id") == cluster_id), None)
        if not cluster:
            self._json(404, {"error": "cluster not found", "id": cluster_id})
            return
        cfg = _load_improver_config()
        if not shutil.which(cfg["tool"]):
            self._json(503, {"error": f"`{cfg['tool']}` CLI not on PATH"})
            return

        samples = "\n".join(f"- {s}" for s in (cluster.get("sample_tasks") or []))
        tokens = ", ".join(cluster.get("top_tokens") or [])
        skills = ", ".join(cluster.get("skills_invoked") or []) or "(none recorded)"
        prompt = (
            "You are drafting a NEW project skill (SKILL.md) for a repeated "
            "pattern of work detected in the user's recent jobs.\n\n"
            f"## Pattern fingerprint\n- Repetitions: {cluster.get('size')}\n"
            f"- Top tokens: {tokens}\n"
            f"- Skills invoked across cluster: {skills}\n"
            f"- Suggested slug: `{cluster.get('suggested_name')}`\n\n"
            f"## Sample tasks\n{samples}\n\n"
            "## Required output\n"
            "Return ONLY a JSON object on a single line — no prose, no fences.\n"
            "Schema:\n"
            '  {"name": "<lowercase-slug>", "description": "<one sentence trigger>", '
            '"new_content": "<full SKILL.md content with --- frontmatter>"}\n'
            "The SKILL.md must start with YAML frontmatter (name, description), then "
            "be a short, opinionated guide to executing this pattern. Keep it under "
            "~40 lines."
        )
        # Same stdin trick as the improver: long argv prompts fail silently on Windows.
        tool_bin = shutil.which(cfg["tool"]) or cfg["tool"]
        try:
            proc = subprocess.run(
                [tool_bin, "-p", "--model", cfg["model"]],
                cwd=str(ROOT), input=prompt,
                capture_output=True, text=True,
                timeout=int(cfg.get("timeout_seconds", 120)),
                encoding="utf-8", errors="replace",
            )
        except (subprocess.TimeoutExpired, OSError) as e:
            self._json(500, {"error": "subprocess error", "detail": str(e)})
            return
        if proc.returncode != 0:
            self._json(500, {"error": f"exit {proc.returncode}",
                             "stderr": (proc.stderr or "")[:300]})
            return
        parsed = _parse_improver_output(proc.stdout or "")
        if not parsed or not isinstance(parsed.get("new_content"), str):
            self._json(500, {"error": "draft output unparseable",
                             "stdout_tail": (proc.stdout or "")[-300:]})
            return

        # Persist as a kind=draft proposal.
        SKILL_PROPOSALS_DIR.mkdir(parents=True, exist_ok=True)
        ts_dt = _dt.datetime.now(_dt.timezone.utc)
        slug_in = parsed.get("name") or cluster.get("suggested_name") or "new-skill"
        slug = re.sub(r"[^a-z0-9]+", "-", slug_in.lower()).strip("-") or "new-skill"
        pid = f"_new-{slug}-{ts_dt.strftime('%Y%m%d-%H%M%S')}"
        new_content = parsed["new_content"]
        payload = {
            "id": pid,
            "kind": "draft",
            "skill": slug,
            "suggested_name": slug,
            "skill_path": None,
            "target_path": f".claude/skills/{slug}/SKILL.md",
            "ts": ts_dt.isoformat(timespec="seconds"),
            "cluster_id": cluster_id,
            "cluster_size": cluster.get("size"),
            "description": parsed.get("description", ""),
            "change_summary": parsed.get("description", "")
                              or f"Draft from cluster of {cluster.get('size')} jobs",
            "rationale": f"Detected pattern across {cluster.get('size')} repeated jobs",
            "diff_lines": len(new_content.splitlines()),
            "status": "pending",
            "applied_at": None,
            "applied_via": None,
        }
        (SKILL_PROPOSALS_DIR / f"{pid}.json").write_text(
            json.dumps(payload, indent=2), encoding="utf-8")
        (SKILL_PROPOSALS_DIR / f"{pid}.old.md").write_text("", encoding="utf-8")
        (SKILL_PROPOSALS_DIR / f"{pid}.new.md").write_text(new_content, encoding="utf-8")
        _audit_improvement(slug, "pending",
                           f"draft from cluster {cluster_id}",
                           pid, None, payload["diff_lines"], source="manual")
        self._json(201, payload)

    def _handle_skill_content(self, qs: dict[str, list[str]]) -> None:
        """Return the SKILL.md content for one skill identified by
        ``source`` (project / claude_global / codex_global) + ``name``
        (directory name). Reads any of the three known roots, including
        global skill dirs that live outside the repo."""
        source = (qs.get("source", [""])[0] or "").strip()
        name = (qs.get("name", [""])[0] or "").strip()
        if not source or not name:
            self._json(400, {"error": "source and name are required"})
            return
        if not re.fullmatch(r"[a-zA-Z0-9_:\-.]+", name):
            self._json(400, {"error": "invalid skill name"})
            return
        home = Path.home()
        roots = {
            "project":       ROOT / ".claude" / "skills",
            "claude_global": home / ".claude" / "skills",
            "codex_global":  home / ".codex"  / "skills",
        }
        root = roots.get(source)
        if root is None:
            self._json(400, {"error": f"unknown source: {source}"})
            return
        skill_md = root / name / "SKILL.md"
        try:
            if not skill_md.is_file():
                self._json(404, {"error": "skill not found",
                                 "source": source, "name": name})
                return
            content = skill_md.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            self._json(500, {"error": str(e)})
            return
        # Cap at ~256KB so a huge file doesn't blow up the modal.
        cap = 256 * 1024
        truncated = len(content) > cap
        if truncated:
            content = content[:cap]
        self._json(200, {
            "source": source, "name": name,
            "path": str(skill_md),
            "content": content,
            "truncated": truncated,
        })

    def _handle_skill_improvements(self, qs: dict[str, list[str]]) -> None:
        """Return all rows from ``IMPROVEMENTS_LEDGER`` for one skill
        (matched on canonical name). Used by the skill detail modal to show
        the per-skill audit trail."""
        skill = (qs.get("skill", [""])[0] or "").strip()
        if not skill:
            self._json(400, {"error": "skill is required"})
            return
        if not re.fullmatch(r"[a-zA-Z0-9_:\-.]+", skill):
            self._json(400, {"error": "invalid skill name"})
            return
        rows: list[dict] = []
        try:
            if IMPROVEMENTS_LEDGER.is_file():
                with IMPROVEMENTS_LEDGER.open("r", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        line = line.strip()
                        if not line.startswith("{"):
                            continue
                        try:
                            o = json.loads(line)
                        except (json.JSONDecodeError, ValueError):
                            continue
                        if o.get("skill") == skill:
                            rows.append(o)
        except OSError as e:
            self._json(500, {"error": str(e)})
            return
        rows.sort(key=lambda r: r.get("ts") or "", reverse=True)
        self._json(200, {"skill": skill, "improvements": rows})

    def _handle_skills_metrics(self, qs: dict[str, list[str]]) -> None:
        """Return per-skill aggregated metrics. With ``?skill=<id>`` returns
        a single skill's detail (incl. ``recent`` invocations)."""
        all_metrics = _aggregate_skill_metrics()
        skill = (qs.get("skill", [""])[0] or "").strip()
        if skill:
            agg = all_metrics.get(skill)
            if not agg:
                for v in all_metrics.values():
                    if v.get("name") == skill:
                        agg = v
                        break
            if not agg:
                self._json(404, {"error": "no metrics for skill", "skill": skill})
                return
            self._json(200, agg)
            return
        # Strip the ``recent`` array on the list response to keep payload small.
        compact = []
        for agg in all_metrics.values():
            row = {k: v for k, v in agg.items() if k != "recent"}
            compact.append(row)
        compact.sort(key=lambda r: r.get("last_used") or "", reverse=True)
        self._json(200, {"metrics": compact})

    def _handle_files_list(self, qs: dict[str, list[str]]) -> None:
        """Return repo-relative file paths that match ``prefix`` for the
        ``@`` autocomplete. Uses ``git ls-files`` for fast indexed search
        when available; falls back to a glob."""
        prefix = (qs.get("prefix", [""])[0] or "").lower()
        limit = 30
        files: list[str] = []
        git = shutil.which("git")
        if git:
            try:
                out = subprocess.run(
                    [git, "ls-files"], cwd=str(ROOT), capture_output=True,
                    text=True, timeout=5,
                )
                if out.returncode == 0:
                    for line in out.stdout.splitlines():
                        if not line:
                            continue
                        if prefix and prefix not in line.lower():
                            continue
                        files.append(line)
                        if len(files) >= limit:
                            break
            except (subprocess.TimeoutExpired, OSError):
                files = []
        # Fallback: walk repo top-level (cheap, no recursion bomb).
        if not files:
            for p in ROOT.rglob("*"):
                if not p.is_file():
                    continue
                rel = str(p.relative_to(ROOT)).replace("\\", "/")
                if prefix and prefix not in rel.lower():
                    continue
                files.append(rel)
                if len(files) >= limit:
                    break
        self._json(200, {"files": files})

    def _handle_file_read(self, qs: dict[str, list[str]]) -> None:
        """Read a repo-relative file's content. Refuses paths that escape
        the repo root."""
        rel = (qs.get("path", [""])[0] or "").strip()
        if not rel:
            self._json(400, {"error": "path is required"})
            return
        try:
            resolved = (ROOT / rel).resolve()
            resolved.relative_to(ROOT.resolve())
        except (ValueError, OSError):
            self._json(403, {"error": "path outside repo root"})
            return
        if not resolved.is_file():
            self._json(404, {"error": "not a file"})
            return
        try:
            data = resolved.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            self._json(500, {"error": str(e)})
            return
        # Cap response to ~256KB so a giant file can't blow up the chat.
        cap = 256 * 1024
        truncated = len(data) > cap
        if truncated:
            data = data[:cap]
        self._json(200, {"path": rel, "content": data, "truncated": truncated})

    def _handle_transcript_stream(self, session_id: str) -> None:
        """SSE: tail an IDE transcript JSONL file. Emits any existing
        content first (catch-up), then continues forwarding bytes as the
        file grows (live mirror of the ongoing IDE session)."""
        tdir = _transcripts_dir_for_cwd(ROOT)
        path = (tdir / f"{session_id}.jsonl") if tdir else None
        if not path or not path.is_file():
            self._json(404, {"error": "transcript not found", "session_id": session_id})
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        # Catch-up: flush existing content first.
        try:
            with path.open("rb") as fh:
                existing = fh.read()
                pos = fh.tell()
        except OSError:
            return
        if existing:
            if not self._write_sse_frame(existing.decode("utf-8", "replace").replace("\r\n", "\n")):
                return

        # Live tail: poll for appended bytes. Exit when client disconnects
        # or after a long idle period (defensive cap).
        last_size = pos
        idle_ticks = 0
        max_idle_ticks = 240  # ~ 4 minutes at 1s; client will reconnect
        while idle_ticks < max_idle_ticks:
            try:
                size = path.stat().st_size
            except OSError:
                break
            if size > last_size:
                idle_ticks = 0
                try:
                    with path.open("rb") as fh:
                        fh.seek(last_size)
                        chunk = fh.read(size - last_size)
                except OSError:
                    break
                if not self._write_sse_frame(chunk.decode("utf-8", "replace").replace("\r\n", "\n")):
                    return
                last_size = size
            else:
                idle_ticks += 1
                try:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    return
            time.sleep(1.0)
        self._write_sse_event("end", "{}")

    def _handle_sessions_list(self) -> None:
        """List chat sessions (claude + codex) that have a session_id, so the
        dashboard can offer "Resume" picker entries."""
        with JOBS_LOCK:
            sessions = []
            for j in JOBS.values():
                if j.get("kind") not in {"chat", "chat-codex"}:
                    continue
                sid = j.get("session_id")
                if not sid:
                    continue
                sessions.append({
                    "session_id": sid,
                    "kind": j.get("kind"),
                    "task": (j.get("task") or "")[:120],
                    "model": j.get("model"),
                    "started_at": j.get("started_at"),
                    "ended_at": j.get("ended_at"),
                    "status": j.get("status"),
                    "last_job_id": j.get("id"),
                })
        sessions.sort(key=lambda s: s.get("started_at") or "", reverse=True)
        self._json(200, {"sessions": sessions})

    def _handle_job_get(self, job_id: str, qs: dict[str, list[str]]) -> None:
        try:
            tail = max(1, min(2000, int((qs.get("tail", ["200"])[0]))))
        except ValueError:
            tail = 200
        with JOBS_LOCK:
            j = JOBS.get(job_id)
            summary = self._job_summary(j) if j else None
            log_path = j.get("log_path") if j else None
        if not summary:
            self._json(404, {"error": "job not found"})
            return
        log_lines: list[str] = []
        if log_path and Path(log_path).exists():
            try:
                # Tail the last N lines without loading the whole file.
                with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                    log_lines = list(deque(f, maxlen=tail))
            except OSError:
                log_lines = []
        summary["log_tail"] = "".join(log_lines)
        # _job_summary already prefers the live cost counter; only fall
        # back to log-scanning here if neither was set.
        if summary.get("kind") in {"chat", "chat-codex"} and "cost" not in summary and log_path:
            cost = _extract_cost_from_log(Path(log_path))
            if cost is not None:
                summary["cost"] = cost
        self._json(200, summary)

    def _handle_job_interrupt(self, job_id: str) -> None:
        with JOBS_LOCK:
            exists = job_id in JOBS
        if not exists:
            self._json(404, {"error": "job not found"})
            return
        ok, err = _interrupt_chat_turn(job_id)
        if not ok:
            code = 404 if err == "not found" else 409
            self._json(code, {"error": err})
            return
        self._json(200, {"ok": True})

    def _handle_job_cancel(self, job_id: str) -> None:
        if _cancel_job(job_id):
            self._json(200, {"ok": True})
        else:
            self._json(409, {"error": "job not running or not found"})

    def _handle_job_input(self, job_id: str, body: dict) -> None:
        with JOBS_LOCK:
            j = JOBS.get(job_id)
            exists = j is not None
            kind = j.get("kind") if j else ""
        if not exists:
            self._json(404, {"error": "job not found"})
            return
        text = body.get("text") or ""
        images = body.get("images") or []
        files = body.get("files") or []
        if not isinstance(text, str):
            self._json(400, {"error": "text must be a string"})
            return
        if len(text) > 8000:
            self._json(400, {"error": "text must be 8000 chars or fewer"})
            return
        if not isinstance(images, list) or not isinstance(files, list):
            self._json(400, {"error": "images and files must be arrays"})
            return
        if not text and not images and not files:
            self._json(400, {"error": "text, images or files is required"})
            return

        # For chat jobs we can build a richer content array (text + image
        # + file blocks). For other kinds we fall back to plain-text stdin.
        if kind == "chat" and (images or files):
            blocks = self._compose_multimodal_blocks(text, images, files)
            if isinstance(blocks, tuple):  # error
                code, payload = blocks
                self._json(code, payload)
                return
            ok, err = _send_chat_blocks(job_id, blocks)
        else:
            if not text:
                self._json(400, {"error": "text is required for non-chat jobs"})
                return
            ok, err = _send_to_stdin(job_id, text)
        if not ok:
            code = 404 if err == "not found" else 409
            self._json(code, {"error": err})
            return
        self._json(200, {"ok": True})

    def _compose_multimodal_blocks(self, text: str, images: list, files: list):
        """Validate + assemble a stream-json content array from a composer
        payload. Returns the blocks list on success or a ``(code, payload)``
        tuple on validation error."""
        blocks: list[dict] = []
        if text:
            blocks.append({"type": "text", "text": text})
        # Inline file contents as fenced text blocks. The agent treats
        # each as part of the user turn (no IDE-style @-mention expansion
        # since stream-json doesn't run that pass).
        for rel in files:
            if not isinstance(rel, str) or not rel.strip():
                return 400, {"error": "files entries must be non-empty strings"}
            try:
                resolved = (ROOT / rel).resolve()
                resolved.relative_to(ROOT.resolve())
            except (ValueError, OSError):
                return 403, {"error": f"file path outside repo: {rel}"}
            if not resolved.is_file():
                return 404, {"error": f"file not found: {rel}"}
            try:
                content = resolved.read_text(encoding="utf-8", errors="replace")
            except OSError as e:
                return 500, {"error": f"could not read {rel}: {e}"}
            cap = 64 * 1024
            if len(content) > cap:
                content = content[:cap] + "\n...[truncated]"
            blocks.append({
                "type": "text",
                "text": f"<file path=\"{rel}\">\n{content}\n</file>",
            })
        # Image blocks: each must have base64 ``data`` and ``media_type``.
        for img in images:
            if not isinstance(img, dict):
                return 400, {"error": "images entries must be objects"}
            data = img.get("data")
            mt = img.get("media_type") or "image/png"
            if not isinstance(data, str) or not data:
                return 400, {"error": "image data must be a base64 string"}
            blocks.append({
                "type": "image",
                "source": {"type": "base64", "media_type": mt, "data": data},
            })
        return blocks

    def _handle_job_stream(self, job_id: str) -> None:
        """Server-Sent Events stream of the subprocess output.

        Strategy:
          1. Take a snapshot of the existing log file and flush it as one
             `data:` frame (so reconnecting clients catch up).
          2. Register a queue as a subscriber; each chunk written by the
             runner thread gets forwarded as a `data:` frame.
          3. Terminate when the runner publishes the EOF sentinel (None).
        """
        import queue as _queue

        with JOBS_LOCK:
            j = JOBS.get(job_id)
            if not j:
                self._json(404, {"error": "job not found"})
                return
            log_path = j.get("log_path")
            status = j.get("status")
            subs = j.setdefault("subscribers", [])
            q: _queue.Queue = _queue.Queue(maxsize=1024)
            subs.append(q)

        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()

            # 1. Catch-up: flush what's already on disk. For chat jobs whose
            # log_path is the IDE transcript file (potentially MBs of history
            # — hooks, attachments, queue ops), we tail just the recent
            # conversation records so the browser doesn't choke parsing the
            # entire backlog. For non-chat jobs (orchestrate/plan/codex) the
            # log file is dashboard-owned and small; full dump stays.
            with JOBS_LOCK:
                catchup_kind = JOBS.get(job_id, {}).get("kind")
            if log_path and Path(log_path).exists():
                try:
                    if catchup_kind == "chat":
                        existing = _tail_chat_catchup(Path(log_path))
                    else:
                        existing = Path(log_path).read_text(encoding="utf-8", errors="replace")
                except OSError:
                    existing = ""
                if existing:
                    self._write_sse_frame(existing)

            if status not in {"running", "queued"}:
                # Job already finished — close immediately after catch-up.
                self._write_sse_event("end", "{}")
                return

            # 2. Live tail until EOF sentinel arrives.
            while True:
                try:
                    chunk = q.get(timeout=15)
                except _queue.Empty:
                    # Heartbeat keeps the connection alive through proxies.
                    try:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        return
                    continue
                if chunk is None:
                    self._write_sse_event("end", "{}")
                    return
                if not self._write_sse_frame(chunk):
                    return
        finally:
            with JOBS_LOCK:
                try:
                    subs.remove(q)
                except ValueError:
                    pass

    def _write_sse_frame(self, text: str) -> bool:
        """Encode ``text`` as one SSE ``data:`` frame; one logical line per
        SSE ``data:`` field. Returns False if the client disconnected."""
        out = []
        # Per SSE spec, each newline in the payload becomes a separate data: line.
        for line in text.split("\n"):
            out.append("data: " + line + "\n")
        out.append("\n")
        try:
            self.wfile.write("".join(out).encode("utf-8", "replace"))
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return False
        return True

    def _write_sse_event(self, event: str, data: str) -> bool:
        try:
            self.wfile.write(f"event: {event}\ndata: {data}\n\n".encode("utf-8"))
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return False
        return True

    def _handle_dispatch_mode(self, body: dict) -> None:
        mode = (body.get("mode") or "").strip()
        if mode not in {"auto", "manual"}:
            self._json(400, {"error": "mode must be 'auto' or 'manual'"})
            return
        path = ROOT / ".ai" / "models.yaml"
        if not path.exists():
            self._json(404, {"error": "models.yaml not found"})
            return
        text = path.read_text(encoding="utf-8")
        # Replace existing `dispatch_mode: <value>` line (with optional inline comment), or insert near top.
        line_re = re.compile(r"^(dispatch_mode:\s*)\S+(\s*(?:#.*)?)$", re.M)
        if line_re.search(text):
            new_text = line_re.sub(rf"\g<1>{mode}\g<2>", text, count=1)
        else:
            # Insert after the first non-comment, non-blank line — keep it simple.
            new_text = f"dispatch_mode: {mode}    # auto | manual\n\n" + text
        _write_text_lf(path, new_text)
        self._json(200, {"ok": True, "mode": mode})

    # ----- phase config edit -----
    _PHASES = {"session", "plan", "execute", "review", "rescue", "maintenance", "bootstrap"}
    _TOOLS = {"claude", "codex"}
    _PHASE_MODES = {"inline", "agent", "dispatcher"}
    _REASONING = {"xhigh", "high", "medium", "low"}

    def _handle_phase_update(self, body: dict) -> None:
        phase = (body.get("phase") or "").strip()
        if phase not in self._PHASES:
            self._json(400, {"error": f"phase must be one of {sorted(self._PHASES)}"})
            return
        # All fields optional; only those present are updated.
        updates: dict[str, str | None] = {}
        if "tool" in body:
            tool = (body.get("tool") or "").strip()
            if tool not in self._TOOLS:
                self._json(400, {"error": f"tool must be one of {sorted(self._TOOLS)}"})
                return
            updates["tool"] = tool
        if "model" in body:
            model = (body.get("model") or "").strip()
            if not model or len(model) > 80 or not re.fullmatch(r"[A-Za-z0-9._\-]+", model):
                self._json(400, {"error": "model must be 1-80 chars [A-Za-z0-9._-]"})
                return
            updates["model"] = model
        if "mode" in body:
            mode = (body.get("mode") or "").strip()
            if mode and mode not in self._PHASE_MODES:
                self._json(400, {"error": f"mode must be one of {sorted(self._PHASE_MODES)} or empty"})
                return
            updates["mode"] = mode or None  # empty => remove the line
        if "reasoning_effort" in body:
            re_eff = (body.get("reasoning_effort") or "").strip()
            if re_eff and re_eff not in self._REASONING:
                self._json(400, {"error": f"reasoning_effort must be one of {sorted(self._REASONING)} or empty"})
                return
            updates["reasoning_effort"] = re_eff or None

        if not updates:
            self._json(400, {"error": "no updatable fields provided (tool, model, mode, reasoning_effort)"})
            return

        path = ROOT / ".ai" / "models.yaml"
        if not path.exists():
            self._json(404, {"error": "models.yaml not found"})
            return
        try:
            new_text = _patch_phase_block(path.read_text(encoding="utf-8"), phase, updates)
        except ValueError as e:
            self._json(404, {"error": str(e)})
            return
        _write_text_lf(path, new_text)
        self._json(200, {"ok": True, "phase": phase, "updated": updates})


def _patch_phase_block(text: str, phase: str, updates: dict[str, str | None]) -> str:
    """Update fields under a top-level YAML mapping like ``plan:\\n  tool: ...``.

    For each key in updates:
      - value is a string -> replace existing `  <key>: <old>` line, or insert
        as the first child line after the header
      - value is None     -> remove the `  <key>: ...` line if present
    """
    lines = text.splitlines(keepends=False)
    n = len(lines)
    header_idx = None
    for i, ln in enumerate(lines):
        if re.match(rf"^{re.escape(phase)}\s*:\s*(#.*)?$", ln):
            header_idx = i
            break
    if header_idx is None:
        raise ValueError(f"phase block `{phase}:` not found in models.yaml")
    # Find end of this block (next non-indented, non-blank line)
    end_idx = n
    for j in range(header_idx + 1, n):
        ln = lines[j]
        if ln.strip() == "":
            continue
        if not ln.startswith((" ", "\t")):
            end_idx = j
            break
    block = lines[header_idx + 1 : end_idx]
    # Track existing keys
    key_re = re.compile(r"^(\s+)([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(\S.*)?$")
    indent = "  "
    for ln in block:
        m = key_re.match(ln)
        if m:
            indent = m.group(1)
            break

    def render(key: str, val: str) -> str:
        return f"{indent}{key}: {val}"

    new_block: list[str] = list(block)
    for key, val in updates.items():
        existing_idx = None
        for k, ln in enumerate(new_block):
            m = key_re.match(ln)
            if m and m.group(2) == key:
                existing_idx = k
                break
        if val is None:
            if existing_idx is not None:
                new_block.pop(existing_idx)
            continue
        if existing_idx is not None:
            # Preserve inline comment if any
            ln = new_block[existing_idx]
            m = re.match(r"^(\s+[A-Za-z_][A-Za-z0-9_]*\s*:\s*)\S+(\s*(?:#.*)?)$", ln)
            if m:
                new_block[existing_idx] = f"{m.group(1)}{val}{m.group(2)}"
            else:
                new_block[existing_idx] = render(key, val)
        else:
            # Insert as the last non-empty child line
            insert_at = len(new_block)
            while insert_at > 0 and new_block[insert_at - 1].strip() == "":
                insert_at -= 1
            new_block.insert(insert_at, render(key, val))

    new_lines = lines[: header_idx + 1] + new_block + lines[end_idx:]
    out = "\n".join(new_lines)
    if text.endswith("\n") and not out.endswith("\n"):
        out += "\n"
    return out


def main() -> None:
    # Replay the on-disk job ledger so sessions, costs and history
    # survive `python serve.py` restarts.
    _load_persisted_jobs()
    # Prune stale per-job .log files. Chat jobs route to claude's
    # transcript now so the dir mostly holds demo/orchestrate/codex logs;
    # this keeps it bounded.
    pruned = _prune_old_logs(JOBS_DIR, max_age_days=7, keep_newest=50)
    if pruned:
        print(f"[dashboard] pruned {pruned} old .log file(s) from {JOBS_DIR}")
    last_err: OSError | None = None
    for candidate in (PORT, PORT + 1, PORT + 2, PORT + 3):
        try:
            httpd = socketserver.ThreadingTCPServer(("127.0.0.1", candidate), Handler)
        except OSError as e:
            last_err = e
            continue
        with httpd:
            url = f"http://localhost:{candidate}/.ai/dashboard/"
            print(f"AI workflow dashboard: {url}")
            print("Press Ctrl+C to stop.")
            try:
                httpd.serve_forever()
            except KeyboardInterrupt:
                print("\nstopped.")
        return
    raise SystemExit(f"could not bind to any port starting at {PORT}: {last_err}")


if __name__ == "__main__":
    main()
