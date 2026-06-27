"""Auto-improver I/O layer: audit ledger, proposals, apply, regression gates.

Extracted from serve.py. This is the persistence + decision layer beneath the
improver runner (which lives in server/improver.py):

  * Audit trail -- ``_audit_improvement`` appends one row per improver decision
    to ``IMPROVEMENTS_LEDGER`` under ``_IMPROVEMENTS_LEDGER_LOCK`` (cross-process
    file lock); ``_last_improver_run_ts`` / ``_recent_rejected_proposals`` /
    ``_has_audit_signal`` read it back to throttle and gate sweeps.
  * Proposals  -- ``_write_proposal`` materialises a pending diff under
    ``SKILL_PROPOSALS_DIR`` (superseding prior pending ones via
    ``_supersede_prior_pending``); ``_apply_improvement`` writes the new SKILL.md
    (backing up the old under ``SKILL_BACKUPS_DIR``, mirroring to .agents via
    ``server.skill_tree``) and records the outcome.
  * Gates      -- ``_check_held_out_gate`` (defers to the eval harness),
    ``_check_skill_regression`` (success-rate drop in ``SKILL_METRICS_FILE``),
    and ``_auto_revert_skill`` (roll back a regressed skill from its proposal).
  * Prompt     -- ``_build_improver_prompt`` assembles the model instruction.

serve.py re-exports every name here via a shim, and server/improver.py imports
the ones its runner/sweep need directly.
"""
from __future__ import annotations

import datetime as _dt
import importlib
import json
import os
import re
import sys
import threading
import time
from pathlib import Path

from server.paths import (
    IMPROVEMENTS_LEDGER,
    ROOT,
    SKILL_BACKUPS_DIR,
    SKILL_METRICS_FILE,
    SKILL_PROPOSALS_DIR,
)
from server.skill_tree import _mirror_claude_skill_to_agents
from server.storage import _load_jsonl_cached
from server.validation import _iso_to_epoch

_IMPROVEMENTS_LEDGER_LOCK = threading.Lock()


def _last_improver_run_ts(skill_id: str) -> float:
    """Look at the audit ledger for the most recent improvement attempt
    against this skill, return epoch seconds (or 0 if never).

    Reads through ``_load_jsonl_cached`` so the auto-improver scheduler
    (which calls this for every project skill on every wake) doesn't
    re-parse the ledger N times per cycle. The cache preserves the prior
    silent-skip-on-corrupt-row behaviour the hand-rolled loop relied on.
    """
    last = 0.0
    for o in _load_jsonl_cached(IMPROVEMENTS_LEDGER):
        if not isinstance(o, dict) or o.get("skill") != skill_id:
            continue
        ts = _iso_to_epoch(o.get("ts") or "")
        if ts > last:
            last = ts
    return last


# A failure only counts as a "concrete pain signal" if it is RECENT. Without
# this window, demo-seed or long-resolved failures sit in a rarely-run skill's
# last-N telemetry forever, so the periodic sweep re-audits that skill on every
# wake and the LLM emits a fresh speculative (often wrong) edit each time. The
# 7-day horizon mirrors the transcript-policy STALE_DAYS.
_RECENT_FAILURE_MAX_AGE_DAYS = 7


def _has_audit_signal(skill_id: str,
                      recent_outcomes: list[dict] | None,
                      *, now: float | None = None) -> tuple[bool, str]:
    """Decide whether a structural audit is worth invoking the LLM for.

    Returns ``(should_audit, reason)``. The periodic sweep uses this to
    skip skills with no concrete failure signal — without it, the LLM is
    rubric-bound to find SOMETHING in every healthy skill (the 7 criteria
    are broad enough that no skill satisfies all of them perfectly), so
    every audit pollutes the proposal queue with low-signal suggestions.

    Triggers (any one is enough):
    1. First-time audit (never visited before) — sanity sweep.
    2. ≥1 failure within the last ``_RECENT_FAILURE_MAX_AGE_DAYS`` days in
       ``recent_outcomes`` — concrete, *current* pain signal. Failures we can
       prove are older than that window are ignored; a failure whose ``ts`` is
       missing/unparseable is conservatively still counted.

    ``now`` (epoch seconds) is injectable for tests; defaults to wall clock.

    The manual "Improve now" button bypasses this gate (caller passes
    ``force=True`` to ``_run_improver_for_skill``) because the user's
    click is itself the signal."""
    if _last_improver_run_ts(skill_id) == 0:
        return True, "first-time audit"
    if recent_outcomes:
        now_epoch = time.time() if now is None else now
        cutoff = now_epoch - _RECENT_FAILURE_MAX_AGE_DAYS * 86400
        failed = 0
        for r in recent_outcomes:
            if not isinstance(r, dict):
                continue
            if str(r.get("outcome") or "").lower() not in {"failed", "error"}:
                continue
            ts = _iso_to_epoch(r.get("ts") or "")
            if ts and ts < cutoff:
                continue  # provably stale failure — not a current signal
            failed += 1
        if failed >= 1:
            return True, f"{failed} recent failure(s)"
    return False, "no failure signal (skipped to avoid speculative proposals)"


def _recent_rejected_proposals(skill_id: str, limit: int = 10) -> list[str]:
    """Reasons from recent ``rejected`` ledger rows for this skill, newest
    first, capped at ``limit``. Fed into the improver prompt so the LLM
    doesn't re-propose the same fix the operator already turned down."""
    rejected: list[tuple[float, str]] = []
    for o in _load_jsonl_cached(IMPROVEMENTS_LEDGER):
        if not isinstance(o, dict) or o.get("skill") != skill_id:
            continue
        if o.get("status") != "rejected":
            continue
        reason = (o.get("reason") or "").strip()
        if not reason:
            continue
        ts = _iso_to_epoch(o.get("ts") or "")
        rejected.append((ts, reason[:200]))
    rejected.sort(reverse=True)
    return [r for _, r in rejected[:limit]]


def _build_improver_prompt(skill_id: str, skill_content: str,
                           metrics: dict, job_id: str | None,
                           log_excerpt: str, *,
                           manual: bool = False,
                           recent_outcomes: list[dict] | None = None,
                           rejected_history: list[str] | None = None) -> str:
    """Craft the one-shot prompt sent to the model.

    The schema and ``no change`` example are intentionally front-loaded so
    smaller models (Haiku) don't drift into prose. The skill content and
    log are delimited with ``<<<...>>>`` markers (not triple-backticks)
    because SKILL.md itself often contains fenced code blocks.

    ``manual=True`` is set by the periodic batch sweep and the manual
    "Improve now" endpoint. In that mode the model is asked to audit the
    skill structurally (description quality, output format, allowlist fit,
    stale references) rather than gating on log-excerpt failure signals.
    Without this, the model returns ``no_change`` on essentially every
    healthy skill — the original prompt told it to do exactly that.

    ``recent_outcomes`` is the last N rows from the per-skill telemetry
    (most recent first). Aggregate ``success_rate`` alone hides
    deterioration: a skill at 80% overall might be at 30% over the last
    week. Listing recent outcomes lets the model reason about trend."""
    rate = round((metrics.get("success_rate") or 0.0) * 100) if metrics else None
    summary = (
        f"success_rate={rate}% over {metrics.get('total_jobs',0)} jobs"
        f", avg_cost=${metrics.get('avg_cost_usd',0):.4f}"
        f", avg_duration={int((metrics.get('avg_duration_ms') or 0)/1000)}s"
    ) if metrics and metrics.get("total_jobs") else "no telemetry yet"

    # Compact "done/failed/done/failed/..." line so a haiku-class model
    # can spot a recent-failure cluster at a glance. Truncated to 20.
    if recent_outcomes:
        recent_line = ", ".join(
            (r.get("outcome") or "?") for r in recent_outcomes[:20]
        )
    else:
        recent_line = "(none)"

    if manual:
        role = (
            "ROLE: You are auditing a project skill STRUCTURALLY against "
            "the rubric below. There is no single failing job to anchor "
            "this on — your job is to find the most impactful improvement "
            "the skill itself needs, regardless of whether the last run "
            "succeeded. Propose ONE focused edit when ANY criterion misses; "
            "return no_change only when the skill clearly satisfies all "
            "criteria.\n\n"
            "RUBRIC (one fix per pass, prioritise the lowest-scoring criterion):\n"
            "  1. Description quality: starts with a verb; trigger phrases "
            "cover both the explicit ask and implicit phrasings; specific "
            "not generic.\n"
            "  2. Output format declared: the skill states WHAT it returns "
            "to the caller (markdown report, JSON shape, file path, etc.).\n"
            "  3. Workflow / process steps are explicit (numbered phases, "
            "checklist, or clearly demarcated stages).\n"
            "  4. Edge-cases / refusal conditions named for known failure "
            "modes.\n"
            "  5. Tool allowlist matches what the body actually does (least "
            "privilege — review-only skill should not imply Write/Edit).\n"
            "  6. Currency: no references to paths, sibling skills, or "
            "commands that no longer exist.\n"
            "  7. Recent-failure trend: if the recent_outcomes line shows "
            "≥3 failures in the last 10 invocations, add a guardrail or "
            "tighten an instruction tied to the apparent failure mode.\n\n"
            "Keep edits small (≤ ~12 line delta for structural fixes; ≤6 "
            "for content tweaks). Preserve frontmatter name unchanged. "
            "You MAY tighten the description.\n\n"
        )
    else:
        role = (
            "ROLE: You are reviewing a project skill after one of its "
            "invocations. Propose a refinement when EITHER the log excerpt "
            "shows ambiguity / failure / missing guardrails, OR the recent "
            "outcomes line shows a failure cluster (≥3 failed in the last "
            "10) that the skill could address structurally. Be precise — "
            "do not rewrite working sections. Keep edits small (≤ ~6 line "
            "delta) and keep frontmatter name/description intact.\n\n"
        )
    return (
        "OUTPUT FORMAT (STRICT): Respond with ONE JSON object. NO prose, "
        "NO commentary, NO markdown fences. If you write anything other "
        "than a JSON object, the output is INVALID.\n\n"
        "Schema:\n"
        '  {"change_summary": "<short str>", "rationale": "<short str>", '
        '"new_content": <full new SKILL.md as string OR null>}\n\n'
        'When no change is warranted: '
        '{"change_summary":"none","rationale":"<why>","new_content":null}\n\n'
        f"{role}"
        f"SKILL: {skill_id}\n"
        f"TELEMETRY: {summary}\n"
        f"RECENT_OUTCOMES (last 20, newest first): {recent_line}\n"
        f"JOB: {job_id or '(manual structural review — no specific job)'}\n"
        f"MODE: {'manual structural audit' if manual else 'post-job review'}\n"
        + (
            "PRIOR REJECTED PROPOSALS for this skill (DO NOT re-propose these fixes — "
            "the operator already turned them down):\n  - "
            + "\n  - ".join(rejected_history)
            + "\n"
            if rejected_history else ""
        )
        + "\n"
        "=== Current SKILL.md (between markers) ===\n"
        f"<<<SKILL\n{skill_content}\nSKILL>>>\n\n"
        "=== Job log excerpt (between markers, may be empty) ===\n"
        f"<<<LOG\n{log_excerpt}\nLOG>>>\n\n"
        "Now respond with ONLY the JSON object."
    )


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
        with _IMPROVEMENTS_LEDGER_LOCK:
            with IMPROVEMENTS_LEDGER.open("a", encoding="utf-8") as f:
                line = json.dumps(row, default=str) + "\n"
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
    except OSError as e:
        # Audit ledger is best-effort; never break the improver pipeline.
        # Log so a silently-dropped audit row is traceable.
        print(f"[serve] audit_improvement write failed for {skill_id}: {e}", flush=True)


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
    try:
        (SKILL_PROPOSALS_DIR / f"{pid}.json").write_text(
            json.dumps(payload, indent=2), encoding="utf-8")
        (SKILL_PROPOSALS_DIR / f"{pid}.old.md").write_text(old, encoding="utf-8")
        (SKILL_PROPOSALS_DIR / f"{pid}.new.md").write_text(new, encoding="utf-8")
    except OSError as e:
        # Background improver thread: a raised OSError would kill it
        # silently with no operator-facing trace. Log + re-raise so the
        # caller's outer try/except (around the whole improver run) can
        # record the failure in the audit ledger.
        print(f"[serve] _write_proposal {pid} failed: {e}", flush=True)
        raise
    merged_in = _supersede_prior_pending(skill_id, pid, "improve")
    if merged_in:
        payload["merged_from"] = merged_in
        try:
            (SKILL_PROPOSALS_DIR / f"{pid}.json").write_text(
                json.dumps(payload, indent=2), encoding="utf-8")
        except OSError as e:
            # Non-fatal: the new proposal is already on disk and usable;
            # the merged_from annotation is best-effort metadata.
            print(f"[serve] _write_proposal {pid} merged_from update failed: {e}", flush=True)
    return payload


def _supersede_prior_pending(skill_id: str, new_pid: str, new_kind: str) -> list[str]:
    """Mark every prior pending proposal targeting the same skill+kind as
    ``superseded`` so only the newest pending one survives in the dashboard.

    Each older proposal stays on disk (history is preserved) but flips out
    of the pending list. The new proposal absorbs them: we return their ids
    so the caller can record a ``merged_from`` field.

    Returns the list of superseded proposal ids (may be empty)."""
    if not skill_id or not SKILL_PROPOSALS_DIR.is_dir():
        return []
    superseded: list[str] = []
    now_iso = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    for pj in SKILL_PROPOSALS_DIR.glob("*.json"):
        try:
            obj = json.loads(pj.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if obj.get("id") == new_pid:
            continue
        if obj.get("skill") != skill_id:
            continue
        if (obj.get("kind") or "improve") != new_kind:
            continue
        if obj.get("status") not in (None, "pending"):
            continue
        obj["status"] = "superseded"
        obj["applied_at"] = now_iso
        obj["applied_via"] = "merged-into-newer"
        obj["superseded_by"] = new_pid
        try:
            pj.write_text(json.dumps(obj, indent=2), encoding="utf-8")
        except OSError as e:
            # Best-effort: a failed mark leaves the older proposal in the
            # pending list, which the list endpoint will dedupe again on
            # the next call. Log so the operator notices repeated failures.
            print(f"[serve] supersede {pj.name} failed: {e}", flush=True)
            continue
        superseded.append(obj.get("id") or pj.stem)
    return superseded


# Dual-tree skill mirror (_BRIDGE_SKILLS_NO_MIRROR, _mirror_claude_skill_to_agents,
# _create_skill_in_both_trees) moved to server/skill_tree.py, re-exported via shim.

def _apply_improvement(skill_path: Path, new_content: str, source: str,
                       reason: str, proposal_id: str | None,
                       skill_id: str, diff_lines: int) -> bool:
    """Backup -> overwrite -> audit -> mirror to .agents. Returns True on
    success of the overwrite. Skill files are git-tracked so a
    ``git diff`` is always available as a second safety net beyond the
    on-disk .bak. The codex-side mirror is best-effort: a failed mirror
    is logged but doesn't fail the apply (the .claude/ copy already has
    the new content; the operator can re-run .ai/scripts/sync_skills.py)."""
    SKILL_BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    ts = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
    slug = re.sub(r"[^a-z0-9]+", "-", skill_id.lower()).strip("-") or "skill"
    backup_path = SKILL_BACKUPS_DIR / f"{slug}-{ts}.md.bak"
    try:
        # ``errors="replace"`` so a SKILL.md that has been hand-edited with
        # a non-UTF-8 sequence doesn't crash the apply path with a
        # ``UnicodeDecodeError`` (manifested as an unrelated 500). The
        # replaced bytes get backed up too — operators can recover from the
        # .bak. Better than losing the whole proposal flow.
        original = skill_path.read_text(encoding="utf-8", errors="replace")
        backup_path.write_text(original, encoding="utf-8")
        tmp_path = skill_path.with_name(skill_path.name + ".tmp")
        tmp_path.write_text(new_content, encoding="utf-8")
        os.replace(str(tmp_path), str(skill_path))
    except OSError as e:
        _audit_improvement(skill_id, "failed", f"write error: {e}",
                           proposal_id, None, diff_lines, source=source)
        return False
    _audit_improvement(skill_id, "applied", reason, proposal_id,
                       str(backup_path), diff_lines, source=source)
    # Mirror to the .agents/skills tree so the Codex side picks up the
    # change. Best-effort — log + continue on failure so a sync miss
    # doesn't roll back an otherwise-successful apply.
    mirrored, mirror_msg = _mirror_claude_skill_to_agents(skill_path)
    if mirrored:
        print(f"[serve] mirrored {skill_id} -> {mirror_msg}", flush=True)
    elif mirror_msg.startswith("error:"):
        print(f"[serve] mirror to .agents failed for {skill_id}: {mirror_msg}",
              flush=True)
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
            except (OSError, json.JSONDecodeError) as e:
                # On-disk proposal stays "pending" while we already applied the
                # SKILL.md change. Best-effort here so caller still completes,
                # but operators need to see this drift.
                print(f"[serve] failed to write proposal {pj} (apply): {e}", flush=True)
    return True


def _check_held_out_gate(proposal_id: str) -> dict:
    try:
        eval_root = str(ROOT / ".ai" / "eval")
        if eval_root not in sys.path:
            sys.path.insert(0, eval_root)
        gate = importlib.import_module("harness.gate")
        verdict = gate.evaluate_proposal(proposal_id)
        if not isinstance(verdict, dict):
            return {"decision": "allow", "reason": "gate error: invalid verdict"}
        return verdict
    except Exception as e:
        return {"decision": "allow", "reason": f"gate error: {e}"}


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
    # Route both ledger reads through ``_load_jsonl_cached`` — the
    # auto-revert sweep runs this for every applied proposal on every
    # wake; the cache turns a 100MB re-parse into a single ``stat()``.
    rows = [
        o for o in _load_jsonl_cached(IMPROVEMENTS_LEDGER)
        if isinstance(o, dict) and o.get("skill") == skill_id
    ]
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

    pre: list[dict] = []
    post: list[dict] = []
    for m in _load_jsonl_cached(SKILL_METRICS_FILE):
        if not isinstance(m, dict):
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
    except OSError as e:
        # Proposal already rolled back on disk; the .json record is now stale.
        print(f"[serve] failed to write proposal {pj} (rollback): {e}", flush=True)
    return True
