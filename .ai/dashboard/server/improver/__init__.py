"""Auto-improver runtime: config, sweep/run loops, signals, transcript purge.

Extracted from serve.py. This is the runner layer that drives the auto-improver
subsystem, sitting on top of server/improver/io.py (the audit/proposal/apply
I/O layer) and server/metrics.py (per-skill telemetry):

  * Config + index -- ``_load_improver_config`` (merges models.yaml over
    ``_IMPROVER_DEFAULTS``), ``_project_skill_index``, ``_read_log_excerpt``,
    ``_diff_line_count``.
  * Per-skill run   -- ``_run_improver_for_skill`` spawns the improver LLM
    against one skill, gates the diff (auto-apply small / propose large), and
    records the outcome through ``io``; ``_post_job_skill_actions``
    (revert-first, then improve) is the post-job hook, ``_trigger_improvers_for_job``
    and the ``_periodic_improver_sweep`` / ``_periodic_improver_loop`` schedule it.
  * Lifecycle       -- shutdown signal chaining + the tracked-improver-sid set
    (``_IMPROVER_TRACKED_SIDS``) and the periodic transcript-purge loop that
    cleans up improver chat transcripts.
  * Suggestions     -- ``_detect_skill_suggestions`` (+ ``_tokenize_task`` /
    ``_load_unique_jobs`` / ``_STOPWORDS``) clusters recent job tasks into
    candidate NEW skills.
  * Job telemetry   -- ``_record_skill_metrics`` (+ ``_extract_skills_from_stream_json``)
    is installed by serve as the job runner's completion hook; it appends
    per-skill rows and fires ``_post_job_skill_actions``.

serve.py re-exports every name here via a shim and installs ``_record_skill_metrics``
as ``server.jobs.record_skill_metrics_hook``. Everything imported here flows one
way (improver -> io / metrics / jobs.state / foundations); nothing here
is imported back, so there is no cycle.
"""
from __future__ import annotations

import atexit
import datetime as _dt
import difflib
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from server.improver._transcript_policy import classify_transcript, load_ledger_rows
from server.config import _read_yaml_field
from server.improver.io import (
    _apply_improvement,
    _audit_improvement,
    _auto_revert_skill,
    _build_improver_prompt,
    _check_skill_regression,
    _has_audit_signal,
    _last_improver_run_ts,
    _recent_rejected_proposals,
    _write_proposal,
)
from server.jobs.state import JOB_KINDS, JOBS, JOBS_LOCK
from server.llm_output import _parse_improver_output
from server.metrics import _aggregate_skill_metrics
from server.paths import (
    IMPROVEMENTS_LEDGER,
    JOBS_PERSIST_FILE,
    ROOT,
    SKILL_METRICS_FILE,
    SKILL_PROPOSALS_DIR,
)
from server.skills.config import _scan_skills_dir
from server.storage import _load_jsonl_cached
from server.transcripts.paths import _transcripts_dir_for_cwd
from server.validation import _iso_to_epoch, _safe_which, _skill_name_canonical

# Improver subsystem state (moved from serve.py).
_IMPROVER_TRACKED_SIDS: set[str] = set()
_IMPROVER_TRACKED_SIDS_LOCK = threading.Lock()
_IMPROVER_SHUTDOWN_HANDLERS_INSTALLED = False
_SKILL_METRICS_LOCK = threading.Lock()

_IMPROVER_DEFAULTS = {
    "enabled": True,
    "tool": "claude",
    "model": "claude-haiku-4-5",
    "small_change_max_lines": 6,    # auto-apply threshold (added+removed lines)
    "min_interval_seconds": 300,    # per-skill throttle (job-triggered runs)
    "timeout_seconds": 120,         # subprocess wall-clock cap
    # Periodic structural audit: visits every project skill on this cadence
    # regardless of whether the skill was invoked by any job. Without it the
    # job-triggered improver never wakes for skills the user doesn't run
    # (catch-22: a buggy skill nobody calls never gets fixed). 21600s = 6h.
    "sweep_interval_seconds": 21600,
    # Cap how many skills the sweep audits per wake. Keeps the sweep cheap
    # on first run after a long idle (where every skill is throttle-eligible)
    # and bounds concurrent LLM cost. Audited skills are picked by oldest
    # last-improver-run first, so over multiple wakes the sweep makes a full
    # pass.
    "sweep_batch_max": 4,
    # Auto-revert safety net: if a skill that received an `applied` proposal
    # later shows success-rate regression by >= ``revert_margin`` over the
    # next ``revert_after_n_uses`` invocations, restore the .bak silently.
    "revert_after_n_uses": 5,
    "revert_margin": 0.2,
}


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
              "timeout_seconds", "revert_after_n_uses",
              "sweep_interval_seconds", "sweep_batch_max"):
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


# Improver audit-signal + throttle reads (_last_improver_run_ts,
# _has_audit_signal, _recent_rejected_proposals, _RECENT_FAILURE_MAX_AGE_DAYS)
# moved to server/improver/io.py and are re-exported via the shim above.



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


# _build_improver_prompt moved to server/improver/io.py (re-exported via shim).



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


# _parse_improver_output (extract-one-JSON-object-from-LLM-stdout) moved to
# server/llm_output.py and is re-exported via the shim above.


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


# Improver audit ledger + proposal/apply/regression layer (_audit_improvement,
# _write_proposal, _supersede_prior_pending, _apply_improvement, _check_held_out_gate,
# _check_skill_regression, _auto_revert_skill) moved to server/improver/io.py and are
# re-exported via the shim above.



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
        except Exception as e:  # noqa: BLE001 — never crash the runner
            print(f"[serve] regression check failed for {sid}: {e}", flush=True)
            continue
        if not decision:
            continue
        try:
            _auto_revert_skill(decision)
        except Exception as e:  # noqa: BLE001 — never crash the runner
            print(f"[serve] auto-revert failed for {sid}: {e}", flush=True)
            continue
    # 2. Improver pass — spawns one daemon thread per eligible skill.
    _trigger_improvers_for_job(job_id, skill_ids)


def _purge_claude_transcript(session_id: str | None) -> bool:
    """Delete the per-session JSONL Claude Code wrote for a one-shot
    background call (e.g. an improver run). Without this every improver
    invocation pollutes ``~/.claude/projects/<slug>/`` with a stray
    "OUTPUT FORMAT (STRICT)" session row in the user's chat history.
    Best-effort: missing dir / missing file / OS errors are swallowed."""
    if not session_id:
        return False
    try:
        tdir = _transcripts_dir_for_cwd(ROOT)
        if tdir is None:
            return False
        f = tdir / f"{session_id}.jsonl"
        if not f.is_file():
            return True
        for attempt in range(3):
            try:
                os.unlink(f)
                if attempt:
                    print(
                        f"[serve] transcript deleted for {session_id} after {attempt + 1} attempts",
                        flush=True,
                    )
                return True
            except FileNotFoundError:
                return True
            except (PermissionError, OSError) as e:
                if attempt == 2:
                    print(f"[serve] transcript delete failed for {session_id}: {e}", flush=True)
                    return False
                time.sleep(0.05)
    except OSError as e:
        # Best-effort delete (file may be locked on Windows, or removed
        # by a concurrent caller). Log so the operator can see why a
        # stale transcript stuck around.
        print(f"[serve] transcript delete failed for {session_id}: {e}", flush=True)
        return False
    return True


def _snapshot_tracked_improver_sids() -> list[str]:
    with _IMPROVER_TRACKED_SIDS_LOCK:
        sids = sorted(_IMPROVER_TRACKED_SIDS)
        _IMPROVER_TRACKED_SIDS.clear()
    return sids


def _purge_all_tracked_improver_sids() -> None:
    for sid in _snapshot_tracked_improver_sids():
        _purge_claude_transcript(sid)


def _chain_improver_shutdown_signal(signum: int, frame, previous_handler) -> None:
    _purge_all_tracked_improver_sids()
    if callable(previous_handler):
        previous_handler(signum, frame)
        raise SystemExit(128 + signum)
    if previous_handler == signal.SIG_IGN:
        return
    if previous_handler == signal.SIG_DFL:
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)
    raise SystemExit(128 + signum)


def _install_improver_shutdown_handlers() -> None:
    global _IMPROVER_SHUTDOWN_HANDLERS_INSTALLED
    if _IMPROVER_SHUTDOWN_HANDLERS_INSTALLED:
        return
    _IMPROVER_SHUTDOWN_HANDLERS_INSTALLED = True
    atexit.register(_purge_all_tracked_improver_sids)

    signals = [signal.SIGINT]
    if hasattr(signal, "SIGTERM"):
        signals.append(signal.SIGTERM)
    for sig in signals:
        try:
            previous = signal.getsignal(sig)
            signal.signal(
                sig,
                lambda signum, frame, previous_handler=previous: _chain_improver_shutdown_signal(
                    signum,
                    frame,
                    previous_handler,
                ),
            )
        except (ValueError, OSError):
            continue


def _purge_stale_improver_transcripts_once() -> dict[str, int]:
    counts = {"orphan": 0, "resolved": 0, "unmatched_pre_audit": 0, "keep": 0, "failed": 0}
    tdir = _transcripts_dir_for_cwd(ROOT)
    if tdir is None:
        return counts
    ledger_rows = load_ledger_rows(IMPROVEMENTS_LEDGER)
    now = time.time()
    for path in sorted(tdir.glob("*.jsonl")):
        bucket = classify_transcript(path, ledger_rows, now)
        counts[bucket] += 1
        if bucket == "keep":
            continue
        try:
            path.unlink()
        except OSError as e:
            counts["failed"] += 1
            print(f"[serve] stale transcript delete failed for {path}: {e}", flush=True)
    return counts


def _periodic_transcript_purge_loop(interval_seconds: int = 86400, *, run_once: bool = False) -> None:
    """Daemon loop that purges stale improver transcript backlog daily."""
    if os.environ.get("AI_WORKFLOW_DISABLE_IMPROVER"):
        return
    while True:
        try:
            counts = _purge_stale_improver_transcripts_once()
            candidates = counts["orphan"] + counts["resolved"] + counts["unmatched_pre_audit"]
            if candidates or counts["failed"]:
                print(
                    "[serve] improver transcript purge: "
                    f"orphan={counts['orphan']} resolved={counts['resolved']} "
                    f"unmatched_pre_audit={counts['unmatched_pre_audit']} "
                    f"failed={counts['failed']}",
                    flush=True,
                )
        except Exception as e:  # noqa: BLE001 - loop must never die
            print(f"[serve] improver transcript purge loop error: {e}", flush=True)
        if run_once:
            return
        time.sleep(max(60, int(interval_seconds)))


def _run_improver_for_skill(skill_id: str, skill_md_path: Path,
                            job_id: str | None, log_path: str | None,
                            cfg: dict, *, manual: bool = False,
                            force: bool = False) -> dict:
    """End-to-end: read skill -> call LLM -> parse JSON -> persist proposal
    -> auto-apply if small. Best-effort: any failure is audited and the
    function returns a status dict (never raises). When the tool is
    ``claude`` we generate a dedicated ``--session-id`` and delete the
    resulting transcript at exit so background improver runs never show
    up in the chat list.

    ``manual=True`` (used by the manual /api/skills/<name>/improve
    endpoint and the periodic batch sweep) selects the structural-audit
    variant of the prompt and audits with ``source="manual"`` so the
    proposal is distinguishable from job-triggered runs.

    ``force=True`` bypasses the telemetry gate that normally skips
    structural audits on healthy skills with no failure signal. The
    "Improve now" button passes this because the operator's click is
    itself the signal; the periodic sweep does not.

    Returns a dict with at minimum ``{"status": <audit_status>}`` plus a
    ``proposal_id`` when one was created. Callers that don't care can
    ignore the return value."""
    source = "manual" if manual else "auto"
    try:
        skill_content = skill_md_path.read_text(encoding="utf-8")
    except OSError as e:
        _audit_improvement(skill_id, "failed", f"read error: {e}", None, None, 0,
                           source=source)
        return {"status": "failed", "reason": f"read error: {e}"}
    metrics = _aggregate_skill_metrics().get(skill_id) or {}
    recent_outcomes = metrics.get("recent") or []
    if manual and not force:
        should, reason = _has_audit_signal(skill_id, recent_outcomes)
        if not should:
            _audit_improvement(skill_id, "no_change", reason, None, None, 0,
                               source=source)
            return {"status": "no_change", "reason": reason}
    log_excerpt = _read_log_excerpt(log_path)
    rejected_history = _recent_rejected_proposals(skill_id)
    prompt = _build_improver_prompt(skill_id, skill_content, metrics, job_id,
                                    log_excerpt, manual=manual,
                                    recent_outcomes=recent_outcomes,
                                    rejected_history=rejected_history)

    # IMPORTANT (Windows): pass the prompt via stdin, not argv. Long prompts
    # on argv silently fail (claude emits only a "status:ready" stub and never
    # processes the request) — observed empirically. stdin works for any size.
    tool_bin = _safe_which(cfg["tool"]) or cfg["tool"]
    argv = [tool_bin, "-p", "--model", cfg["model"]]
    # Pin a session id ONLY for claude — codex doesn't write per-session
    # JSONLs into ~/.claude/projects/ so it doesn't have the same pollution
    # problem. The id lets _purge_claude_transcript know exactly which file
    # to delete in the finally block below.
    improver_sid: str | None = None
    if cfg.get("tool") == "claude":
        improver_sid = str(uuid.uuid4())
        with _IMPROVER_TRACKED_SIDS_LOCK:
            _IMPROVER_TRACKED_SIDS.add(improver_sid)
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
            _audit_improvement(skill_id, "failed", f"subprocess error: {e}",
                               None, None, 0, source=source)
            return {"status": "failed", "reason": f"subprocess error: {e}"}
        if proc.returncode != 0:
            reason = f"exit {proc.returncode}: {(proc.stderr or '')[:200]}"
            _audit_improvement(skill_id, "failed", reason, None, None, 0,
                               source=source)
            return {"status": "failed", "reason": reason}

        parsed = _parse_improver_output(proc.stdout or "")
        if not parsed:
            _audit_improvement(skill_id, "no_change",
                               "improver returned unparseable output",
                               None, None, 0, source=source)
            return {"status": "no_change",
                    "reason": "improver returned unparseable output"}
        new_content = parsed.get("new_content")
        if not isinstance(new_content, str) or not new_content.strip():
            reason = parsed.get("rationale") or "improver returned null"
            _audit_improvement(skill_id, "no_change", reason,
                               None, None, 0, source=source)
            return {"status": "no_change", "reason": reason}

        diff_lines = _diff_line_count(skill_content, new_content)
        if diff_lines == 0:
            _audit_improvement(skill_id, "no_change", "no effective change",
                               None, None, 0, source=source)
            return {"status": "no_change", "reason": "no effective change"}

        try:
            proposal = _write_proposal(skill_id, skill_md_path, skill_content,
                                       new_content, parsed, diff_lines, job_id)
        except OSError as e:
            # _write_proposal already logged the underlying cause; record
            # a "failed" audit row so the operator-facing ledger reflects
            # the dropped improver run rather than appearing to succeed.
            _audit_improvement(skill_id, "failed", f"proposal write error: {e}",
                               None, None, diff_lines, source=source)
            return {"status": "failed",
                    "reason": f"proposal write error: {e}"}
        # Manual triggers (the "Improve now" button) ALWAYS produce a
        # pending proposal — the operator clicked because they want to
        # review the change, so a small-diff auto-apply would be a
        # surprising silent write. Only background / job-triggered runs
        # use the size-based auto-apply shortcut.
        if not manual and diff_lines <= int(cfg.get("small_change_max_lines", 6)):
            _apply_improvement(skill_md_path, new_content, source=source,
                               reason=parsed.get("change_summary", "") or "",
                               proposal_id=proposal["id"], skill_id=skill_id,
                               diff_lines=diff_lines)
            return {"status": "applied", "proposal_id": proposal["id"],
                    "diff_lines": diff_lines,
                    "change_summary": parsed.get("change_summary", "") or ""}
        _audit_improvement(skill_id, "pending",
                           parsed.get("change_summary", "") or "",
                           proposal["id"], None, diff_lines, source=source)
        return {"status": "pending", "proposal_id": proposal["id"],
                "diff_lines": diff_lines,
                "change_summary": parsed.get("change_summary", "") or ""}
    finally:
        if improver_sid:
            with _IMPROVER_TRACKED_SIDS_LOCK:
                _IMPROVER_TRACKED_SIDS.discard(improver_sid)
        _purge_claude_transcript(improver_sid)


def _trigger_improvers_for_job(job_id: str, skill_ids: list[str]) -> None:
    """Spawn one improver thread per project skill invoked by ``job_id``.
    Throttled via ``IMPROVEMENTS_LEDGER`` and config-gated."""
    cfg = _load_improver_config()
    if not cfg.get("enabled"):
        return
    if not _safe_which(cfg["tool"]):
        return  # CLI not on PATH (or on an untrusted PATH entry); silently skip
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


# Last-wake timestamp for the periodic sweep loop. Module-level (not in
# JOBS) because the sweep is global, not per-job. Initialised to 0 so the
# first wake of the loop always runs a sweep after the boot delay.
_LAST_IMPROVER_SWEEP_TS: float = 0.0
_IMPROVER_SWEEP_LOCK = threading.Lock()


def _periodic_improver_sweep(cfg: dict | None = None) -> dict:
    """Visit every project skill on a structural-audit pass. Picks the K
    most-stale skills (by last improver-run timestamp), then runs
    ``_run_improver_for_skill`` for each with ``manual=True``. Respects
    the same per-skill throttle as job-triggered runs so we don't double-
    audit a skill that the job hook just visited.

    Returns ``{"audited": [...], "skipped": [...]}`` so the caller (the
    loop, or a future on-demand "sweep now" endpoint) can log + surface a
    summary. Exceptions inside one skill's audit are swallowed so a
    single broken skill doesn't kill the whole sweep."""
    cfg = cfg or _load_improver_config()
    out = {"audited": [], "skipped": [], "disabled": False}
    if not cfg.get("enabled"):
        out["disabled"] = True
        return out
    if not _safe_which(cfg["tool"]):
        out["disabled"] = True
        return out
    proj = _project_skill_index()
    if not proj:
        return out
    throttle = int(cfg.get("min_interval_seconds", 300))
    now = time.time()
    # Sort skills by oldest last-run first so a long-lived dashboard
    # eventually covers every skill (rather than starving the alphabet
    # tail behind a "name < X" filter).
    candidates: list[tuple[float, str, Path]] = []
    for name, path in proj.items():
        last = _last_improver_run_ts(name)
        if last and (now - last) < throttle:
            out["skipped"].append({"skill": name, "reason": "throttled",
                                   "last_run_ago_s": int(now - last)})
            continue
        candidates.append((last or 0.0, name, path))
    candidates.sort(key=lambda t: t[0])  # oldest first
    cap = max(1, int(cfg.get("sweep_batch_max", 4)))
    for _, name, path in candidates[:cap]:
        try:
            result = _run_improver_for_skill(name, path, job_id=None,
                                             log_path=None, cfg=cfg,
                                             manual=True)
        except Exception as e:  # noqa: BLE001 — never crash the sweep
            print(f"[serve] sweep audit failed for {name}: {e}", flush=True)
            out["skipped"].append({"skill": name, "reason": f"crash: {e}"})
            continue
        out["audited"].append({"skill": name, "result": result})
    # Mark remaining (over-cap) candidates as deferred so the operator log
    # is honest about partial coverage.
    for _, name, _path in candidates[cap:]:
        out["skipped"].append({"skill": name, "reason": "over-batch-cap"})
    return out


def _periodic_improver_loop() -> None:
    """Daemon loop. Wakes every minute, runs the sweep when the
    configured ``sweep_interval_seconds`` has elapsed since the last
    sweep. Cheap idle path (one stat() through the cached ledger reads
    inside ``_periodic_improver_sweep``)."""
    global _LAST_IMPROVER_SWEEP_TS
    # Boot delay — let the server finish coming up before the first sweep
    # so a 0-skill window during initial imports doesn't get audited.
    time.sleep(30)
    while True:
        try:
            cfg = _load_improver_config()
            interval = max(60, int(cfg.get("sweep_interval_seconds", 21600)))
            with _IMPROVER_SWEEP_LOCK:
                due = (time.time() - _LAST_IMPROVER_SWEEP_TS) >= interval
            if due and cfg.get("enabled"):
                summary = _periodic_improver_sweep(cfg)
                with _IMPROVER_SWEEP_LOCK:
                    _LAST_IMPROVER_SWEEP_TS = time.time()
                n_aud = len(summary.get("audited") or [])
                n_skp = len(summary.get("skipped") or [])
                print(f"[serve] improver sweep: audited={n_aud} skipped={n_skp}",
                      flush=True)
        except Exception as e:  # noqa: BLE001 — loop must never die
            print(f"[serve] improver sweep loop error: {e}", flush=True)
        time.sleep(60)


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


# _iso_to_epoch moved to server/validation.py (re-exported via the shim above).


def _load_unique_jobs(max_age_days: int = 30) -> list[dict]:
    """Replay ``JOBS_PERSIST_FILE`` keeping the last snapshot per id and
    filtering to ``max_age_days``. Skips jobs without a meaningful task
    (test-mode placeholders)."""
    snapshots: dict[str, dict] = {}
    for o in _load_jsonl_cached(JOBS_PERSIST_FILE):
        jid = o.get("id")
        if jid:
            snapshots[jid] = o  # last write wins
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
    for row in _load_jsonl_cached(SKILL_METRICS_FILE):
        jid = row.get("job_id")
        sk = row.get("name") or row.get("skill")
        if jid and sk:
            skill_seqs.setdefault(jid, []).append(sk)

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
    except OSError as e:
        # Best-effort filter — losing a few "covered" entries just means
        # the user sees a previously-addressed cluster again. Log so a
        # systemic problem (permissions, missing dir) is visible.
        print(f"[serve] proposals coverage scan failed: {e}", flush=True)
    clusters = [
        c for c in clusters
        if c.get("id") not in covered_cluster_ids
           and c.get("suggested_name") not in covered_names
    ]

    clusters.sort(key=lambda c: (-c["size"], -_iso_to_epoch(c.get("last_seen") or "")))
    return clusters


# _skill_name_canonical moved to server/validation.py (re-exported via the shim above).


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
        except Exception as e:  # noqa: BLE001 - never crash the runner
            # Log so operators can find malformed stream-json transcripts.
            print(f"[serve] skill scan failed for {log_path}: {e}", flush=True)
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
        with _SKILL_METRICS_LOCK:
            with SKILL_METRICS_FILE.open("a", encoding="utf-8") as f:
                line = "".join(json.dumps(row, default=str) + "\n" for row in rows)
                if sys.platform == "win32":
                    try:
                        import msvcrt
                        msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
                        try:
                            f.write(line)
                            f.flush()
                        finally:
                            try:
                                f.seek(0)
                                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                            except OSError as e:
                                # Unlock failed; the OS releases the byte-range
                                # lock on handle close anyway, but a recurring
                                # trace here points at a flaky fs/handle.
                                print(f"[serve] file unlock failed: {e}", flush=True)
                    except (ImportError, OSError):
                        # Lock acquisition failed (rare) - fall back to a plain
                        # write rather than dropping the event entirely.
                        f.write(line)
                else:
                    try:
                        import fcntl
                        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                        try:
                            f.write(line)
                        finally:
                            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                    except (ImportError, OSError):
                        f.write(line)
    except OSError:
        return 0

    # Phase 2/auto-revert hook: revert-first, then improve. Both are
    # throttled + config-gated inside the helper so this call is cheap and
    # safe even when the improver is disabled.
    try:
        _post_job_skill_actions(job_id, list(counts.keys()))
    except Exception as e:  # noqa: BLE001 - never break the runner
        print(f"[serve] post-job skill actions failed for {job_id}: {e}", flush=True)

    return len(rows)
