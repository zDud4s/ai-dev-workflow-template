"""Request handlers for agent suggestions + agent proposals.

Covers POST /api/agents/suggest, /api/agents/proposals (list),
/api/agents/proposals/<id> (get) and the accept/reject decision route.
Extracted from serve.py as the ``AgentSuggestRoutes`` mixin; Handler inherits it
so routing and ``serve.Handler._handle_agent_suggest`` resolve via MRO. Helpers
each method closes over are imported from their owning ``server.*`` modules
(importing them from serve would be circular).

The skills auto-improver runs on telemetry: every job emits per-skill success
rows, and clusters of repeated tasks become "draft a SKILL.md" proposals. Agents
don't have that signal — no agent_metrics.jsonl, no per-agent success rate.
Instead this flow asks an LLM to look at three cheap signals (git log + recent
job task descriptions + existing agent catalog) and propose new agents on
demand. One-shot, never automatic. It reuses the improver config block from
.ai/models.yaml (tool, model, timeout) and persists each suggestion as a
{pid}.json + {pid}.body.md pair under AGENT_PROPOSALS_DIR; Accept writes the
actual agent file at .claude/agents/<slug>.md (refusing to overwrite), Reject
just marks status="rejected".
"""
from __future__ import annotations

import datetime as _dt
import json
import re
import subprocess
import uuid

from server.agent_suggest import (
    _build_agent_suggester_prompt,
    _git_log_excerpt,
    _load_editable_agent_names,
    _parse_agent_suggestions_output,
    _persist_agent_proposal,
    _recent_job_tasks,
)
from server.http_base import _SUGGESTION_HTTP_TIMEOUT_MAX, _SUGGESTION_SEMAPHORE
from server.improver import _load_improver_config, _purge_claude_transcript
from server.paths import AGENT_PROPOSALS_DIR, ROOT
from server.storage import _write_text_lf
from server.validation import _safe_which


class AgentSuggestRoutes:
    """Agent suggestion + agent proposal endpoints, mixed into ``Handler``."""

    def _handle_agent_suggest(self) -> None:
        """POST /api/agents/suggest — spawn a one-shot LLM that proposes new
        agents based on recent git + recent jobs + existing agents. Persists
        zero or more {pid}.json + .body.md proposals. Returns the count and
        the new proposal ids so the UI can refresh the list."""
        # Shares the rate-limit budget with _handle_suggestion_draft above
        # (same CLI binary, same long subprocess timeout). 429 + Retry-After
        # when the global budget is saturated.
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
            cfg = _load_improver_config()
            tool_bin = _safe_which(cfg["tool"])
            if not tool_bin:
                self._json(503, {"error": f"`{cfg['tool']}` CLI not on PATH"})
                return
            existing = _load_editable_agent_names()
            recent_tasks = _recent_job_tasks()
            git_log = _git_log_excerpt()
            prompt = _build_agent_suggester_prompt(git_log, recent_tasks, existing)
            argv = [tool_bin, "-p", "--model", cfg["model"]]
            improver_sid: str | None = None
            if cfg["tool"] == "claude":
                improver_sid = str(uuid.uuid4())
                argv += ["--session-id", improver_sid]
            # Mirror _handle_suggestion_draft: cap the wall-clock so a long
            # ``cfg["timeout_seconds"]`` can't park the dashboard via this
            # interactive endpoint.
            http_timeout = min(
                int(cfg.get("timeout_seconds", 120)),
                _SUGGESTION_HTTP_TIMEOUT_MAX,
            )
            try:
                try:
                    proc = subprocess.run(
                        argv,
                        cwd=str(ROOT), input=prompt,
                        capture_output=True, text=True,
                        timeout=http_timeout,
                        encoding="utf-8", errors="replace",
                    )
                except (subprocess.TimeoutExpired, OSError) as e:
                    self._json(500, {"error": "subprocess error", "detail": str(e)})
                    return
                if proc.returncode != 0:
                    self._json(500, {"error": f"exit {proc.returncode}",
                                     "stderr": (proc.stderr or "")[:300]})
                    return
                suggestions = _parse_agent_suggestions_output(proc.stdout or "")
                if suggestions is None:
                    self._json(500, {"error": "suggester output unparseable",
                                     "stdout_tail": (proc.stdout or "")[-300:]})
                    return
                signal_summary = {
                    "commits": len([l for l in (git_log or "").splitlines() if l.strip()]),
                    "jobs": len(recent_tasks),
                    "existing": len(existing),
                }
                if not suggestions:
                    self._json(200, {"count": 0, "proposal_ids": [],
                                     "note": "no suggestions",
                                     "signal_summary": signal_summary})
                    return
                ids: list[str] = []
                for s in suggestions:
                    pid = _persist_agent_proposal(s, source_signal=signal_summary)
                    if pid:
                        ids.append(pid)
                self._json(200, {"count": len(ids), "proposal_ids": ids,
                                 "signal_summary": signal_summary})
            finally:
                _purge_claude_transcript(improver_sid)
        finally:
            _SUGGESTION_SEMAPHORE.release()

    def _handle_agent_proposals_list(self) -> None:
        """GET /api/agents/proposals — compact summary of every proposal on
        disk, newest first. Body content is fetched separately via the
        detail endpoint to keep the list response small."""
        items: list[dict] = []
        if AGENT_PROPOSALS_DIR.is_dir():
            for p in sorted(AGENT_PROPOSALS_DIR.glob("*.json"),
                            key=lambda x: x.stat().st_mtime, reverse=True):
                try:
                    obj = json.loads(p.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                items.append({
                    "id": obj.get("id"),
                    "name": obj.get("name"),
                    "slug": obj.get("slug"),
                    "description": obj.get("description"),
                    "trigger_phrasings": obj.get("trigger_phrasings") or [],
                    "confidence": obj.get("confidence"),
                    "ts": obj.get("ts"),
                    "status": obj.get("status") or "pending",
                    "applied_at": obj.get("applied_at"),
                    "installed_path": obj.get("installed_path"),
                    "target_path": obj.get("target_path"),
                })
        self._json(200, {"proposals": items})

    def _handle_agent_proposal_get(self, proposal_id: str) -> None:
        """GET /api/agents/proposals/<id> — full payload + body for the
        proposal modal. Path-validates the id to prevent traversal."""
        if not re.fullmatch(r"_agent-[a-z0-9-]+-\d{8}-\d{6}", proposal_id):
            self._json(400, {"error": "invalid proposal id"})
            return
        pj = AGENT_PROPOSALS_DIR / f"{proposal_id}.json"
        if not pj.is_file():
            self._json(404, {"error": "proposal not found"})
            return
        try:
            obj = json.loads(pj.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            self._json(500, {"error": "could not read proposal", "detail": str(e)})
            return
        body_path = AGENT_PROPOSALS_DIR / f"{proposal_id}.body.md"
        try:
            obj["body"] = body_path.read_text(encoding="utf-8") if body_path.is_file() else ""
        except OSError:
            obj["body"] = ""
        self._json(200, obj)

    def _handle_agent_proposal_decision(self, proposal_id: str, decision: str) -> None:
        """POST /api/agents/proposals/<id>/(accept|reject).

        Accept materialises the agent at .claude/agents/<slug>.md (refusing
        to overwrite an existing file — the user must reject + rename to
        re-create). Reject just flips the status and leaves the proposal on
        disk so it stays auditable."""
        if not re.fullmatch(r"_agent-[a-z0-9-]+-\d{8}-\d{6}", proposal_id):
            self._json(400, {"error": "invalid proposal id"})
            return
        pj = AGENT_PROPOSALS_DIR / f"{proposal_id}.json"
        if not pj.is_file():
            self._json(404, {"error": "proposal not found"})
            return
        try:
            obj = json.loads(pj.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            self._json(500, {"error": "could not read proposal", "detail": str(e)})
            return
        if obj.get("status") not in (None, "pending"):
            self._json(409, {"error": f"proposal already {obj.get('status')}"})
            return

        if decision == "reject":
            obj["status"] = "rejected"
            obj["applied_at"] = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
            try:
                pj.write_text(json.dumps(obj, indent=2), encoding="utf-8")
            except OSError as e:
                print(f"[serve] failed to write proposal {pj} (agent reject): {e}", flush=True)
                self._json(500, {"error": "write failed", "detail": str(e)})
                return
            self._json(200, {"ok": True, "id": proposal_id, "status": "rejected"})
            return

        # decision == "accept" — materialise the agent file.
        slug = (obj.get("slug") or "").strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,79}", slug):
            self._json(400, {"error": f"invalid slug: {slug!r}"})
            return
        agents_root = (ROOT / ".claude" / "agents").resolve()
        target = (agents_root / f"{slug}.md").resolve()
        try:
            target.relative_to(agents_root)
        except ValueError:
            self._json(400, {"error": "slug escapes agents directory"})
            return
        target_rel = f".claude/agents/{slug}.md"
        if target.is_file():
            self._json(409, {
                "error": "agent already exists",
                "target_path": target_rel,
                "hint": "Reject this proposal and rename the slug, or "
                        "delete the existing agent first.",
            })
            return
        # Build the agent file from the proposal payload. Only emit `tools:`
        # when non-empty so we don't accidentally pin an empty allowlist.
        front_lines = ["---", f"name: {slug}",
                       f"description: {(obj.get('description') or '').strip()}",
                       "model: sonnet"]
        tools = (obj.get("tools") or "").strip()
        if tools:
            front_lines.append(f"tools: {tools}")
        front_lines += ["---", ""]
        body = obj.get("body") or ""
        if not body:
            body_path = AGENT_PROPOSALS_DIR / f"{proposal_id}.body.md"
            try:
                body = body_path.read_text(encoding="utf-8") if body_path.is_file() else ""
            except OSError:
                body = ""
        content = "\n".join(front_lines) + body.lstrip("\n")
        if not content.endswith("\n"):
            content += "\n"
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            _write_text_lf(target, content)
        except OSError as e:
            print(f"[serve] agent install write failed for {target}: {e}", flush=True)
            self._json(500, {"error": "write failed"})
            return
        obj["status"] = "installed"
        obj["applied_at"] = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
        obj["installed_path"] = target_rel
        try:
            pj.write_text(json.dumps(obj, indent=2), encoding="utf-8")
        except OSError as e:
            # Agent .md is already on disk so the install is effectively done;
            # the proposal JSON just won't reflect "installed" until next
            # decision. Log so a chronic write failure (Windows file-lock,
            # permissions drift) is discoverable rather than silent.
            print(f"[serve] failed to write proposal {pj} (agent installed): {e}", flush=True)
        self._json(200, {
            "ok": True, "id": proposal_id, "status": "installed",
            "installed_path": target_rel,
            "note": f"Agent created at {target_rel}.",
        })
