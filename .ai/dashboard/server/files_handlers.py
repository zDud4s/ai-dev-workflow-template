"""Request handlers for the repo file browser endpoints.

Covers /api/files/list (autocomplete over tracked files) and /api/files/read.
Extracted from serve.py as the ``FileRoutes`` mixin; Handler inherits it so
routing and ``serve.Handler._handle_file_read`` resolve via MRO. ``_is_blocked_path``
lives here too (the multimodal composer reaches it via ``self``); the
``_BLOCKED_*`` class attributes it consults stay on Handler.
"""
from __future__ import annotations

import os
import subprocess

from server.git_utils import _git_lsfiles_cached, _git_lsfiles_put
from server.http_base import SKIP_DIRS
from server.paths import ROOT
from server.validation import _safe_which


class FileRoutes:
    """Repo file-browser endpoints, mixed into ``Handler``."""

    def _handle_files_list(self, qs: dict[str, list[str]]) -> None:
        """Return repo-relative file paths that match ``prefix`` for the
        ``@`` autocomplete. Uses ``git ls-files`` for fast indexed search
        when available; falls back to a glob."""
        prefix = (qs.get("prefix", [""])[0] or "").lower()
        limit = 30
        files: list[str] = []
        # Track whether the git path actually produced a file list. The rglob
        # fallback must fire only when git is unavailable/failed — NOT merely
        # when git succeeded with zero matches for this prefix (the normal
        # autocomplete case), which would otherwise trigger a full repo walk
        # per keystroke and surface untracked files git never lists.
        git_ok = False
        git = _safe_which("git")
        if git:
            # Cache hits are invalidated by .git/index mtime so autocomplete
            # doesn't spawn ``git ls-files`` on every keystroke.
            lines = _git_lsfiles_cached(ROOT)
            if lines is None:
                try:
                    out = subprocess.run(
                        [git, "ls-files"], cwd=str(ROOT), capture_output=True,
                        text=True, timeout=5,
                    )
                    if out.returncode == 0:
                        lines = out.stdout.splitlines()
                        _git_lsfiles_put(ROOT, lines)
                except (subprocess.TimeoutExpired, OSError):
                    lines = None
            if lines is not None:
                git_ok = True
                for line in lines:
                    if not line:
                        continue
                    # Apply SKIP_DIRS to the git-fast path too — tracked
                    # secrets under .venv/ / node_modules/ / vendor/ used
                    # to be enumerable via ?prefix= because the filter
                    # only protected the slow rglob fallback.
                    if any(part in SKIP_DIRS for part in line.split("/")):
                        continue
                    # Don't reveal secret-named files in the autocomplete
                    # suggestion list either — _handle_file_read blocks
                    # reading them but mere discovery is also a leak.
                    base = line.rsplit("/", 1)[-1].lower()
                    if (base in self._BLOCKED_NAMES
                            or base.startswith(self._BLOCKED_NAME_PREFIXES)
                            or base.endswith(self._BLOCKED_NAME_SUFFIXES)):
                        continue
                    if prefix and prefix not in line.lower():
                        continue
                    files.append(line)
                    if len(files) >= limit:
                        break
        # Fallback: walk the repo when ``git ls-files`` isn't available
        # (no-git checkouts, broken HEAD, etc.). ``SKIP_DIRS`` keeps the
        # walk off the obvious hot paths (``.git/objects`` alone can be
        # hundreds of thousands of entries) and stops the autocomplete
        # endpoint leaking ``.venv`` / ``node_modules`` paths into the
        # suggestion list. Gate on ``git_ok`` (git unavailable/failed), not
        # ``not files`` (which also fires on a normal zero-match prefix).
        if not git_ok:
            try:
                for p in ROOT.rglob("*"):
                    try:
                        parts = p.relative_to(ROOT).parts
                    except ValueError:
                        continue
                    if any(part in SKIP_DIRS for part in parts):
                        continue
                    if not p.is_file():
                        continue
                    base = parts[-1].lower()
                    if (base in self._BLOCKED_NAMES
                            or base.startswith(self._BLOCKED_NAME_PREFIXES)
                            or base.endswith(self._BLOCKED_NAME_SUFFIXES)):
                        continue
                    rel = "/".join(parts)
                    if prefix and prefix not in rel.lower():
                        continue
                    files.append(rel)
                    if len(files) >= limit:
                        break
            except OSError as e:
                print(f"[serve] files-list fallback walk failed: {e}", flush=True)
        self._json(200, {"files": files})

    def _is_blocked_path(self, resolved) -> bool:
        """Return True when the already-resolved path matches the secrets
        blocklist (basename in _BLOCKED_NAMES / prefix / suffix, or path
        under any _BLOCKED_PATHS prefix). Caller is responsible for the
        repo-root containment check; this helper only enforces the
        secrets-name/path policy used by ``_handle_file_read`` and the
        multimodal composer."""
        resolved_norm = os.path.normcase(str(resolved)).replace("/", os.sep)
        base = os.path.basename(resolved_norm)
        if (base in self._BLOCKED_NAMES
                or base.startswith(self._BLOCKED_NAME_PREFIXES)
                or base.endswith(self._BLOCKED_NAME_SUFFIXES)):
            return True
        for blocked in self._BLOCKED_PATHS:
            blocked_norm = blocked.replace("/", os.sep)
            if resolved_norm == blocked_norm or resolved_norm.startswith(blocked_norm + os.sep):
                return True
        return False

    def _handle_file_read(self, qs: dict[str, list[str]]) -> None:
        """Read a repo-relative file's content. Refuses paths that escape
        the repo root, and routes the same ``_BLOCKED_PATHS`` /
        ``_BLOCKED_NAMES`` blocklist the static handler uses so secrets
        (``.ssh``, ``.aws``, ``.env``, ``id_rsa``, ``*.pem`` ...) can't
        leak through this API endpoint either."""
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
        if self._is_blocked_path(resolved):
            self._json(403, {"error": "path is blocked"})
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
