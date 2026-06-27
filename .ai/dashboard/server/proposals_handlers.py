"""Request handlers for skill-improvement proposals + manual improve.

Covers /api/skills/proposals (list), /api/skills/proposals/<id> (get),
the accept/reject decision route, POST /api/skills/<name>/improve and the
/api/suggestions/<cluster>/draft endpoint. Extracted from serve.py as the
``ProposalRoutes`` mixin; Handler inherits it so routing and
``serve.Handler._handle_proposal_decision`` resolve via MRO. Helpers each method
closes over are imported from their owning ``server.*`` modules (importing them
from serve would be circular).
"""
from __future__ import annotations

import datetime as _dt
import json
import re
import subprocess

from server.http_base import _SUGGESTION_HTTP_TIMEOUT_MAX, _SUGGESTION_SEMAPHORE
from server.improver import (
    _detect_skill_suggestions,
    _load_improver_config,
    _project_skill_index,
    _run_improver_for_skill,
)
from server.improver_io import (
    _apply_improvement,
    _audit_improvement,
    _check_held_out_gate,
    _supersede_prior_pending,
)
from server.llm_output import _parse_improver_output
from server.paths import ROOT, SKILL_PROPOSALS_DIR
from server.skill_tree import _create_skill_in_both_trees
from server.validation import _safe_which, _skill_name_canonical


class ProposalRoutes:
    """Skill-improvement proposal + manual-improve endpoints, mixed into ``Handler``."""

    def _handle_proposals_list(self) -> None:
        """List every proposal under ``SKILL_PROPOSALS_DIR`` with status.

        Defensive merge pass: legacy duplicates (multiple pending proposals
        for the same skill+kind) are collapsed here too — the newest wins,
        the rest are marked ``superseded`` on disk so the next call sees a
        clean state. New writes already supersede prior pending via
        ``_supersede_prior_pending`` at creation time."""
        items: list[dict] = []
        if SKILL_PROPOSALS_DIR.is_dir():
            loaded: list[tuple[Path, dict]] = []
            for p in sorted(SKILL_PROPOSALS_DIR.glob("*.json"),
                            key=lambda x: x.stat().st_mtime, reverse=True):
                try:
                    obj = json.loads(p.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                loaded.append((p, obj))
            # First pass: collapse legacy same-skill+kind pending duplicates.
            # `loaded` is mtime-desc, so the FIRST occurrence per (kind, skill)
            # is the newest and survives; the rest get superseded.
            seen_pending: dict[tuple[str, str], dict] = {}
            now_iso = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
            for p, obj in loaded:
                status = obj.get("status") or "pending"
                if status != "pending":
                    continue
                skill = obj.get("skill") or ""
                kind = obj.get("kind") or "improve"
                key = (kind, skill)
                if not skill:
                    continue
                winner = seen_pending.get(key)
                if winner is None:
                    seen_pending[key] = obj
                    continue
                # `obj` is older than `winner` — mark it superseded.
                obj["status"] = "superseded"
                obj["applied_at"] = now_iso
                obj["applied_via"] = "merged-into-newer"
                obj["superseded_by"] = winner.get("id")
                try:
                    p.write_text(json.dumps(obj, indent=2), encoding="utf-8")
                except OSError as e:
                    print(f"[serve] list-time supersede {p.name} failed: {e}",
                          flush=True)
                # Bump the winner's merged_from list so the UI can show
                # how many proposals collapsed into this one.
                merged_from = list(winner.get("merged_from") or [])
                older_id = obj.get("id")
                if older_id and older_id not in merged_from:
                    merged_from.append(older_id)
                    winner["merged_from"] = merged_from
                    # Find the winner's path to persist the bump.
                    for wp, wobj in loaded:
                        if wobj is winner:
                            try:
                                wp.write_text(json.dumps(winner, indent=2),
                                              encoding="utf-8")
                            except OSError as e:
                                print(f"[serve] list-time merged_from update "
                                      f"{wp.name} failed: {e}", flush=True)
                            break
            # Second pass: build the response summary.
            for _, obj in loaded:
                merged_from = obj.get("merged_from") or []
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
                    "merged_count": len(merged_from) + 1 if merged_from else 1,
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
                print(f"[serve] failed to write proposal {pj} (reject): {e}", flush=True)
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
                print(f"[serve] could not read draft body {new_md}: {e}", flush=True)
                self._json(500, {"error": "could not read draft body"})
                return
            try:
                install_info = _create_skill_in_both_trees(slug, new_content)
            except OSError as e:
                print(f"[serve] draft install write failed for {target_md}: {e}", flush=True)
                self._json(500, {"error": "write failed"})
                return
            target_rel = install_info["claude_path"]
            obj["status"] = "installed"
            obj["applied_at"] = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
            obj["applied_via"] = "manual"
            obj["target_path"] = target_rel
            obj["installed_path"] = target_rel
            if install_info["agents_path"]:
                obj["agents_installed_path"] = install_info["agents_path"]
            try:
                pj.write_text(json.dumps(obj, indent=2), encoding="utf-8")
            except OSError as e:
                # SKILL.md already on disk; status stays "pending" in the
                # proposal file. Audit still runs so the ledger reflects truth.
                print(f"[serve] failed to write proposal {pj} (installed draft): {e}", flush=True)
            audit_reason = f"draft installed -> {target_rel}"
            if install_info["agents_path"]:
                audit_reason += f" (+ {install_info['agents_path']})"
            _audit_improvement(slug, "installed",
                               audit_reason,
                               proposal_id, None,
                               int(obj.get("diff_lines") or 0),
                               source="manual")
            note = f"Skill created at {target_rel}."
            if install_info["agents_path"]:
                note += f" Also mirrored to {install_info['agents_path']}."
            elif install_info["agents_skipped_reason"]:
                note += f" (.agents skipped: {install_info['agents_skipped_reason']})"
            self._json(200, {
                "ok": True, "id": proposal_id, "status": "installed",
                "installed_path": target_rel,
                "agents_installed_path": install_info["agents_path"],
                "note": note,
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
        held_out = _check_held_out_gate(proposal_id)
        obj["held_out"] = held_out
        try:
            pj.write_text(json.dumps(obj, indent=2), encoding="utf-8")
        except OSError as e:
            print(f"[serve] failed to write proposal {pj} (held-out gate): {e}", flush=True)
        if held_out.get("decision") == "block":
            self._json(409, {
                "error": "proposal regresses the held-out set",
                "held_out": held_out,
            })
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
            self._json(500, {"error": "apply failed (see .ai/ledgers/improvements.jsonl)"})
            return
        self._json(200, {"ok": True, "id": proposal_id, "status": "applied"})

    def _handle_skill_improve_now(self, skill_name: str) -> None:
        """Manual structural-audit trigger for one project skill. Bypasses
        the per-skill throttle (the operator is asking explicitly) and
        selects the ``manual=True`` prompt variant so the model audits
        the skill structurally rather than gating on a job log.

        Shares ``_SUGGESTION_SEMAPHORE`` with /draft and /agents/suggest:
        all three spawn one ``claude -p`` / ``codex`` subprocess on the
        request thread; without the cap a handful of concurrent clients
        can exhaust the thread pool. Returns the audit outcome inline so
        the UI can show "applied / pending / no_change" without a second
        round-trip to /api/skills/proposals."""
        cfg = _load_improver_config()
        if not cfg.get("enabled"):
            self._json(409, {"error": "improver disabled",
                             "hint": "Set improver.enabled=true in .ai/models.yaml"})
            return
        if not _safe_which(cfg["tool"]):
            self._json(503, {"error": "improver CLI not on PATH",
                             "tool": cfg.get("tool")})
            return
        proj = _project_skill_index()
        canonical = _skill_name_canonical(skill_name)
        path = proj.get(canonical) or proj.get(skill_name)
        if not path:
            self._json(404, {"error": "skill not found in project scope",
                             "skill": skill_name,
                             "hint": "Manual improve only edits .claude/skills/"
                                     " — plugin and user-scope skills are read-only."})
            return
        if not _SUGGESTION_SEMAPHORE.acquire(blocking=False):
            self.send_response(429)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Retry-After", "30")
            body = json.dumps({"error": "too many concurrent improver requests; try again later"}).encode("utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        # Cap the subprocess timeout the same way /draft does so a long
        # cfg.timeout_seconds can't pin a request thread.
        cfg_capped = dict(cfg)
        cfg_capped["timeout_seconds"] = min(
            int(cfg.get("timeout_seconds", 120)),
            _SUGGESTION_HTTP_TIMEOUT_MAX,
        )
        try:
            result = _run_improver_for_skill(
                canonical, path, job_id=None, log_path=None,
                cfg=cfg_capped, manual=True, force=True,
            )
        except Exception as e:  # noqa: BLE001 — never 500 silently
            print(f"[serve] manual improve crashed for {canonical}: {e}", flush=True)
            self._json(500, {"error": "improver crashed", "detail": str(e)})
            return
        finally:
            _SUGGESTION_SEMAPHORE.release()
        self._json(200, {
            "ok": True,
            "skill": canonical,
            "status": result.get("status"),
            "proposal_id": result.get("proposal_id"),
            "diff_lines": result.get("diff_lines"),
            "change_summary": result.get("change_summary") or "",
            "reason": result.get("reason") or "",
        })

    def _handle_suggestion_draft(self, cluster_id: str) -> None:
        """Phase 5: dispatch an LLM to draft a SKILL.md from a suggestion
        cluster. Saves the result as a ``kind=draft`` proposal — never
        writes into ``.claude/skills/`` directly."""
        # Global cap on concurrent draft/suggest subprocesses — both this
        # endpoint and /api/agents/suggest share the same `claude -p` / `codex`
        # binary and each can pin a request thread for `timeout_seconds`
        # (default 120s). Without the cap, N concurrent clients exhaust the
        # thread pool. Reply 429 with Retry-After so the UI can back off.
        if not _SUGGESTION_SEMAPHORE.acquire(blocking=False):
            self.send_response(429)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Retry-After", "30")
            body = json.dumps({"error": "too many concurrent draft requests; try again later"}).encode("utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        try:
            # Find the cluster (clusters are computed on demand, no persistence).
            clusters = _detect_skill_suggestions()
            cluster = next((c for c in clusters if c.get("id") == cluster_id), None)
            if not cluster:
                self._json(404, {"error": "cluster not found", "id": cluster_id})
                return
            cfg = _load_improver_config()
            if not _safe_which(cfg["tool"]):
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
            tool_bin = _safe_which(cfg["tool"]) or cfg["tool"]
            # Cap the request-thread wait at _SUGGESTION_HTTP_TIMEOUT_MAX
            # so a long ``cfg["timeout_seconds"]`` (up to 3600s) can't
            # park the dashboard via this interactive endpoint.
            http_timeout = min(
                int(cfg.get("timeout_seconds", 120)),
                _SUGGESTION_HTTP_TIMEOUT_MAX,
            )
            try:
                proc = subprocess.run(
                    [tool_bin, "-p", "--model", cfg["model"]],
                    cwd=str(ROOT), input=prompt,
                    capture_output=True, text=True,
                    timeout=http_timeout,
                    encoding="utf-8", errors="replace",
                )
            except (subprocess.TimeoutExpired, OSError) as e:
                print(f"[serve] improver subprocess error: {e}", flush=True)
                self._json(500, {"error": "subprocess error"})
                return
            if proc.returncode != 0:
                print(f"[serve] improver exit {proc.returncode}: {(proc.stderr or '')[:300]}", flush=True)
                self._json(500, {"error": f"exit {proc.returncode}"})
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
            try:
                (SKILL_PROPOSALS_DIR / f"{pid}.json").write_text(
                    json.dumps(payload, indent=2), encoding="utf-8")
                (SKILL_PROPOSALS_DIR / f"{pid}.old.md").write_text("", encoding="utf-8")
                (SKILL_PROPOSALS_DIR / f"{pid}.new.md").write_text(new_content, encoding="utf-8")
            except OSError as e:
                # Partial write: at least one of the three files may have
                # landed but the proposal is incomplete and the modal will
                # 500 trying to open it. Log + 500 so the operator sees the
                # cause rather than getting an opaque "unparseable" later.
                print(f"[serve] persist draft proposal {pid} failed: {e}", flush=True)
                self._json(500, {"error": "could not persist draft proposal", "detail": str(e)})
                return
            merged_in = _supersede_prior_pending(slug, pid, "draft")
            if merged_in:
                payload["merged_from"] = merged_in
                try:
                    (SKILL_PROPOSALS_DIR / f"{pid}.json").write_text(
                        json.dumps(payload, indent=2), encoding="utf-8")
                except OSError as e:
                    # Same best-effort policy as _write_proposal: the new
                    # draft is already on disk; merged_from is metadata.
                    print(f"[serve] draft {pid} merged_from update failed: {e}", flush=True)
            _audit_improvement(slug, "pending",
                               f"draft from cluster {cluster_id}",
                               pid, None, payload["diff_lines"], source="manual")
            self._json(201, payload)
        finally:
            _SUGGESTION_SEMAPHORE.release()
