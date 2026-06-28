"""Request handlers for /api/pipelines/* and /api/agent-orchestrations/*.

Extracted from serve.py as a mixin so ``Handler`` stays a thin routing shell.
The methods are unchanged — they still resolve ``self._json`` / ``self._csrf_guard``
via the assembled ``Handler`` (MRO), and ``serve.Handler._handle_pipeline_get``
keeps working for tests. The module-level helpers each method closes over are
imported here from their owning ``server.*`` modules (rather than from serve,
which would be circular).
"""
from __future__ import annotations

import json
import os
import re

from server.agent_runs import (
    _agent_run_metrics_by_slug,
    _list_agent_runs,
    _parse_agent_run,
)
from server.http_base import MAX_PIPELINE_PUT_BYTES
from server.paths import AGENT_RUNS_DIR, PIPELINES_DIR, ROOT
from server.pipelines import _list_pipelines
from server.runtime import _browser_cross_origin_blocked
from server.validation import _is_under_trusted_dir


class PipelineRoutes:
    """Pipeline + agent-orchestration endpoints, mixed into ``Handler``."""

    def _agent_orchestrations_origin_guard(self) -> bool:
        if _browser_cross_origin_blocked(self.headers):
            self._json(403, {"error": "origin not allowed"})
            return False
        return True

    def _pipelines_origin_guard(self) -> bool:
        if _browser_cross_origin_blocked(self.headers):
            self._json(403, {"error": "origin not allowed"})
            return False
        return True

    def _handle_pipelines_list(self) -> None:
        if not self._pipelines_origin_guard():
            return
        self._json(200, {"pipelines": _list_pipelines()})

    def _handle_pipeline_get(self, slug: str) -> None:
        if not self._pipelines_origin_guard():
            return
        if not re.fullmatch(r"[a-z0-9-]+", slug or ""):
            self._json(400, {"error": "invalid slug"})
            return
        candidate = PIPELINES_DIR / f"{slug}.yaml"
        if not candidate.is_file():
            self._json(404, {"error": "pipeline not found", "slug": slug})
            return
        try:
            resolved_realpath = os.path.realpath(str(candidate.resolve(strict=True)))
        except OSError:
            self._json(404, {"error": "pipeline not found", "slug": slug})
            return
        dir_realpath = os.path.realpath(str(PIPELINES_DIR))
        if not _is_under_trusted_dir(resolved_realpath, dir_realpath):
            self._json(400, {"error": "path outside trusted dir"})
            return
        try:
            import yaml as _yaml_mod  # local import — keeps top-level free of PyYAML
            parsed = _yaml_mod.safe_load(candidate.read_text(encoding="utf-8")) or {}
        except Exception as e:
            self._json(400, {"error": f"yaml parse error: {e}"})
            return
        # A truthy non-mapping root (list/scalar) would make {**parsed} raise
        # TypeError outside the try → unhandled 500. Mirror the PUT handler's
        # guard. (safe_load(...) or {} only coerces falsy/None.)
        if not isinstance(parsed, dict):
            self._json(400, {"error": "pipeline root must be a mapping", "slug": slug})
            return
        payload = {"slug": slug, **parsed}
        self._json(200, payload)

    def _handle_pipeline_put(self, slug: str) -> None:
        # PUT is state-changing: CSRF-guarded (which also enforces origin).
        if not self._csrf_guard():
            return
        if not re.fullmatch(r"[a-z0-9-]+", slug or ""):
            self._json(400, {"error": "invalid slug"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_PIPELINE_PUT_BYTES:
            # DoS guard: declared Content-Length already disqualifies this
            # request, so we never decode or validate the payload. Drain the
            # inbound bytes (bounded by the cap + a small margin) in 8 KiB
            # chunks before responding so Windows doesn't reset the TCP
            # connection mid-receive (visible to the client as a
            # ConnectionAbortedError instead of the expected 400). Close the
            # connection after the response so we don't keep state for what
            # is — by declaration — an abusive request.
            if length > 0:
                drain_cap = MAX_PIPELINE_PUT_BYTES + 8 * 1024
                remaining = min(length, drain_cap)
                try:
                    while remaining > 0:
                        chunk = self.rfile.read(min(8192, remaining))
                        if not chunk:
                            break
                        remaining -= len(chunk)
                except OSError:
                    pass
            self.close_connection = True
            self._json(400, {"error": "missing or oversized body"})
            return
        raw = self.rfile.read(length)
        try:
            request = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._json(400, {"error": "invalid json body"})
            return
        yaml_text = request.get("yaml") if isinstance(request, dict) else None
        if not isinstance(yaml_text, str):
            self._json(400, {"error": "missing 'yaml' field"})
            return
        import yaml as _yaml_mod
        try:
            parsed = _yaml_mod.safe_load(yaml_text)
        except _yaml_mod.YAMLError as e:
            self._json(400, {"error": f"yaml parse error: {e}"})
            return
        if not isinstance(parsed, dict):
            self._json(400, {"error": "yaml root must be a mapping"})
            return
        # Validator lives next to serve.py in .ai/dashboard/. Local import so
        # serve.py doesn't pay the import cost on every other handler.
        from pipeline_schema import validate as _validate_pipeline_yaml
        ok, errors = _validate_pipeline_yaml(parsed)
        if not ok:
            self._json(400, {"errors": [{"message": e} for e in errors]})
            return
        target = PIPELINES_DIR / f"{slug}.yaml"
        target_realpath = os.path.realpath(str(target))
        dir_realpath = os.path.realpath(str(PIPELINES_DIR))
        if not _is_under_trusted_dir(target_realpath, dir_realpath):
            self._json(400, {"error": "path outside trusted dir"})
            return
        PIPELINES_DIR.mkdir(parents=True, exist_ok=True)
        canonical = _yaml_mod.safe_dump(parsed, sort_keys=False, default_flow_style=False)
        try:
            target.write_text(canonical, encoding="utf-8")
        except OSError as e:
            print(f"[serve] failed to write pipeline {slug}: {e}", flush=True)
            self._json(500, {"error": f"could not write pipeline: {e}"})
            return
        # Best-effort repo-relative path for client display. Falls back to
        # the absolute path if PIPELINES_DIR has been monkey-patched outside
        # ROOT (the unit tests do this with tmp_path).
        try:
            rel = str(target.relative_to(ROOT)).replace("\\", "/")
        except ValueError:
            rel = str(target).replace("\\", "/")
        self._json(200, {"slug": slug, "path": rel})

    def _handle_pipeline_delete(self, slug: str) -> None:
        if not self._csrf_guard():
            return
        if not re.fullmatch(r"[a-z0-9-]+", slug or ""):
            self._json(400, {"error": "invalid slug"})
            return
        target = PIPELINES_DIR / f"{slug}.yaml"
        if not target.is_file():
            self._json(404, {"error": "pipeline not found", "slug": slug})
            return
        try:
            target_realpath = os.path.realpath(str(target.resolve(strict=True)))
        except OSError:
            self._json(404, {"error": "pipeline not found", "slug": slug})
            return
        dir_realpath = os.path.realpath(str(PIPELINES_DIR))
        if not _is_under_trusted_dir(target_realpath, dir_realpath):
            self._json(400, {"error": "path outside trusted dir"})
            return
        target.unlink()
        self._json(200, {"slug": slug, "deleted": True})

    def _handle_agent_orchestrations_list(self) -> None:
        if not self._agent_orchestrations_origin_guard():
            return
        self._json(200, {"runs": _list_agent_runs()})

    def _handle_agent_orchestration_get(self, slug: str) -> None:
        if not self._agent_orchestrations_origin_guard():
            return
        if not re.fullmatch(r"[A-Za-z0-9._-]+", slug or ""):
            self._json(400, {"error": "invalid task slug"})
            return
        match = None
        for run in _list_agent_runs():
            if run.get("task_slug") == slug:
                match = run
                break
        if match is None:
            self._json(404, {"error": "agent orchestration not found", "task_slug": slug})
            return
        try:
            candidate = ROOT / str(match.get("path") or "")
            resolved = candidate.resolve(strict=True)
        except OSError:
            self._json(404, {"error": "agent orchestration file not found", "task_slug": slug})
            return
        resolved_realpath = os.path.realpath(str(resolved))
        runs_realpath = os.path.realpath(str(AGENT_RUNS_DIR))
        if not _is_under_trusted_dir(resolved_realpath, runs_realpath):
            self._json(400, {"error": "agent orchestration path is outside trusted dir"})
            return
        parsed = _parse_agent_run(candidate)
        self._json(200, {
            "task_slug": parsed.get("task_slug"),
            "date": parsed.get("date"),
            "objective": parsed.get("objective"),
            "output_hint": parsed.get("output_hint"),
            "dag": parsed.get("dag") or [],
            "handoff": parsed.get("handoff") or "",
            "metrics": match.get("metrics") or _agent_run_metrics_by_slug().get(slug, []),
        })
