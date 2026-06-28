"""Request handlers for the skills + agents catalog and metrics endpoints.

Covers /api/skills, /api/skills/all, /api/skills/content, /api/skills/metrics,
/api/skills/improvements, /api/skills/suggestions, /api/agents/content and
/api/agents/all. Extracted from serve.py as the ``SkillRoutes`` mixin; Handler
inherits it so routing and ``serve.Handler._handle_skills_all`` resolve via MRO.
Helpers each method closes over are imported from their owning ``server.*``
modules (importing them from serve would be circular).
"""
from __future__ import annotations

import re
import time
from pathlib import Path

from server.improver import _detect_skill_suggestions
from server.metrics import _aggregate_skill_metrics
from server.paths import IMPROVEMENTS_LEDGER, ROOT
from server.skills.config import (
    _AGENTS_ALL_CACHE,
    _CATALOG_TTL_SECONDS,
    _SKILLS_ALL_CACHE,
    _scan_agents_dir,
    _scan_skills_dir,
)
from server.storage import _load_jsonl_cached


class SkillRoutes:
    """Skills + agents catalog / content / metrics endpoints, mixed into ``Handler``."""

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
        after jobs.

        Cached for ``_CATALOG_TTL_SECONDS`` because dashboard boot fires
        this endpoint in parallel with /api/agents/all and /api/usage/total
        — without the cache the FS walks across 3 dirs + the metrics
        aggregator add ~300-500 ms to first paint."""
        now_mono = time.monotonic()
        if _SKILLS_ALL_CACHE["data"] is not None and (now_mono - _SKILLS_ALL_CACHE["at"]) < _CATALOG_TTL_SECONDS:
            self._json(200, _SKILLS_ALL_CACHE["data"])
            return
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
        payload = {"skills": all_skills, "sources": source_meta}
        _SKILLS_ALL_CACHE["data"] = payload
        _SKILLS_ALL_CACHE["at"] = now_mono
        self._json(200, payload)

    def _handle_agent_content(self, qs: dict[str, list[str]]) -> None:
        """Return the raw markdown of an agent file by path.

        Security: the requested path is resolved to an absolute path and
        verified to live under one of the four catalog roots returned by
        ``_handle_agents_all``. Anything outside those roots is rejected
        (403). This is the same trust boundary the catalog itself uses —
        plugin trees are read-only by design, but `.md` content is safe
        to surface for inspection."""
        raw = (qs.get("path") or [""])[0]
        if not raw:
            self._json(400, {"error": "missing path"})
            return
        home = Path.home()
        allowed_roots = [
            ROOT / ".claude" / "agents",
            home / ".claude" / "agents",
            home / ".claude" / "plugins" / "marketplaces",
            home / ".claude" / "plugins" / "cache",
        ]
        # Accept repo-relative paths (catalog returns those for project
        # agents) and absolute paths (catalog returns those for user +
        # plugin agents).
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = ROOT / candidate
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError):
            self._json(404, {"error": "agent file not found", "path": raw})
            return
        if resolved.suffix != ".md":
            self._json(400, {"error": "not a .md file"})
            return
        ok = False
        for root in allowed_roots:
            try:
                root_resolved = root.resolve(strict=False)
            except (OSError, RuntimeError):
                continue
            try:
                resolved.relative_to(root_resolved)
                ok = True
                break
            except ValueError:
                continue
        if not ok:
            self._json(403, {"error": "path is outside the agent catalog roots"})
            return
        try:
            text = resolved.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            print(f"[serve] agent read failed for {resolved}: {e}", flush=True)
            self._json(500, {"error": "read failed"})
            return
        max_bytes = 256 * 1024
        truncated = False
        if len(text.encode("utf-8", errors="replace")) > max_bytes:
            text = text[: max_bytes // 2]
            truncated = True
        self._json(200, {"content": text, "truncated": truncated, "path": str(resolved)})

    def _handle_agents_all(self) -> None:
        """Consolidated agent catalog across project + user + plugin scopes.

        Reads four locations and emits one flat list plus a per-source
        summary so the dashboard can render group cards + a filterable grid:
          * ``project``       -> ``<repo>/.claude/agents/*.md``      (editable)
          * ``user``          -> ``~/.claude/agents/*.md``           (editable)
          * ``plugin_market`` -> ``~/.claude/plugins/marketplaces/**/agents/*.md`` (read-only)
          * ``plugin_cache``  -> ``~/.claude/plugins/cache/**/agents/*.md``        (read-only)

        Plugin agents are surfaced so the user can spot duplication with
        their own agents but are never editable from the dashboard.

        Cached for ``_CATALOG_TTL_SECONDS`` — the plugin_market and
        plugin_cache scans use recursive ``glob("**/agents/*.md")`` which
        can walk thousands of plugin files; without the cache, every
        dashboard tab switch back to Agents would re-walk them."""
        now_mono = time.monotonic()
        if _AGENTS_ALL_CACHE["data"] is not None and (now_mono - _AGENTS_ALL_CACHE["at"]) < _CATALOG_TTL_SECONDS:
            self._json(200, _AGENTS_ALL_CACHE["data"])
            return
        home = Path.home()
        sources = [
            ("project",       "Project",          True,  ROOT / ".claude" / "agents",                       False),
            ("user",          "User (global)",    True,  home / ".claude" / "agents",                       False),
            ("plugin_market", "Plugin (market)",  False, home / ".claude" / "plugins" / "marketplaces",     True),
            ("plugin_cache",  "Plugin (cache)",   False, home / ".claude" / "plugins" / "cache",            True),
        ]
        all_agents: list[dict] = []
        source_meta: dict[str, dict] = {}
        for src_id, label, editable, path, recursive in sources:
            entries = _scan_agents_dir(path, recursive=recursive)
            for e in entries:
                all_agents.append({
                    "name": e["name"],
                    "description": e["description"],
                    "tools": e["tools"],
                    "model": e["model"],
                    "path": e["path"],
                    "source": src_id,
                    "source_label": label,
                    "editable": editable,
                })
            source_meta[src_id] = {
                "label": label,
                "editable": editable,
                "path": str(path),
                "exists": path.is_dir(),
                "count": len(entries),
            }
        # Duplicate-name detection across all sources for cross-scope hints.
        name_counts: dict[str, int] = {}
        for a in all_agents:
            name_counts[a["name"]] = name_counts.get(a["name"], 0) + 1
        for a in all_agents:
            a["duplicate"] = name_counts[a["name"]] > 1
        payload = {"agents": all_agents, "sources": source_meta}
        _AGENTS_ALL_CACHE["data"] = payload
        _AGENTS_ALL_CACHE["at"] = now_mono
        self._json(200, payload)

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
        # Reject `..` and `.` in any segment: the regex below already
        # forbids `/` and `\`, but `..` would otherwise resolve outside the
        # skills root (e.g. `?source=codex_global&name=..` -> ~/.codex/SKILL.md).
        if not re.fullmatch(r"[a-zA-Z0-9_:-][a-zA-Z0-9_:\-.]*", name) or ".." in name.split("."):
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
            # Containment check: even with the regex above, resolve() +
            # relative_to() is the canonical defense against symlink/junction
            # escapes inside the skills tree.
            skill_md.resolve(strict=False).relative_to(root.resolve())
        except ValueError:
            self._json(403, {"error": "path is outside the skills root"})
            return
        except OSError as e:
            print(f"[serve] skill content resolve failed for {skill_md}: {e}", flush=True)
            self._json(500, {"error": "resolve failed"})
            return
        try:
            if not skill_md.is_file():
                self._json(404, {"error": "skill not found",
                                 "source": source, "name": name})
                return
            content = skill_md.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            print(f"[serve] skill content read failed for {skill_md}: {e}", flush=True)
            self._json(500, {"error": "read failed"})
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
        rows = [o for o in _load_jsonl_cached(IMPROVEMENTS_LEDGER) if o.get("skill") == skill]
        # Sort a copy — never mutate the cached list, the next caller would see
        # rows in reverse-chronological order without going through this filter.
        rows = sorted(rows, key=lambda r: r.get("ts") or "", reverse=True)
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
