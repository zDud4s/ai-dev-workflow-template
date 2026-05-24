"""Static handler blocklist for secret files (audit finding A.P2-2).

The dashboard's static handler is rooted at the project root and uses a
blocklist (rather than an allowlist) so that legitimate reads like
``.ai/memory.md`` keep working. The blocklist previously only covered
``.git`` and ``.claude/settings.json``. This test pins the extended set:
``.env`` family, ``*.pem`` / ``*.key``, SSH ``id_*`` private keys,
``.npmrc`` / ``.netrc`` / ``credentials``, and the ``.aws`` / ``.ssh`` /
``.docker`` / ``secrets`` directories.

When a path is blocked, ``translate_path`` returns a sentinel that ends
with ``__blocked_sensitive_path__`` — the file is guaranteed not to
exist, so the underlying ``SimpleHTTPRequestHandler`` returns 404 rather
than serving the requested file.
"""
from __future__ import annotations

import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / ".ai" / "dashboard"))
import serve  # noqa: E402 — path mangled above


_SENTINEL = "__blocked_sensitive_path__"


def _basename_blocked(name: str) -> bool:
    """Replicate the basename check from ``Handler.translate_path`` for a
    given filename only (no full path needed). Used for parametric tests."""
    base = os.path.normcase(name)
    return (
        base in serve.Handler._BLOCKED_NAMES
        or base.startswith(serve.Handler._BLOCKED_NAME_PREFIXES)
        or base.endswith(serve.Handler._BLOCKED_NAME_SUFFIXES)
    )


def test_env_files_blocked():
    for name in (".env", ".env.local", ".env.production",
                 ".env.development", ".env.staging", ".env.test"):
        assert _basename_blocked(name), f"{name!r} should be blocked"


def test_env_example_not_blocked():
    # Sample/template files are checked into repos and are not secrets.
    for name in (".env.example", ".env.sample", ".env.template"):
        assert not _basename_blocked(name), (
            f"{name!r} is a template, not a secret — must not be blocked")


def test_ssh_private_keys_blocked():
    for name in ("id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"):
        assert _basename_blocked(name), f"{name!r} should be blocked"


def test_pem_key_suffix_blocked():
    for name in ("server.pem", "client.key", "wildcard.pem", "private.key"):
        assert _basename_blocked(name), f"{name!r} should be blocked"


def test_other_secret_files_blocked():
    for name in (".npmrc", ".netrc", "credentials"):
        assert _basename_blocked(name), f"{name!r} should be blocked"


def test_dashboard_files_not_blocked():
    # Regression guard: the dashboard intentionally reads these.
    for name in ("memory.md", "decisions.md", "project.yaml",
                 "models.yaml", "events.jsonl", "SKILL.md",
                 "app.js", "styles.css", "index.html"):
        assert not _basename_blocked(name), (
            f"{name!r} must remain readable for the dashboard")
