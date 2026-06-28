"""Hardening tests for ``serve.py``.

Covers two recent fixes:

* The path-traversal fix in ``_handle_list``: the handler previously checked
  ``target.relative_to(ROOT)`` against the *unresolved* ROOT, so a symlink
  inside the repo pointing outside ``ROOT`` would slip past the guard even
  though ``target = (ROOT / rel).resolve()`` had already followed the link.
  Fix consistently uses ``ROOT.resolve()``.

* The generic JSONL mtime cache ``_load_jsonl_cached``: list/aggregate
  endpoints used to re-parse the entire JSONL ledger on every call. The
  cache returns the same parsed list reference until the file's mtime
  changes, so a cache hit is a single ``stat()`` + dict lookup. The tests
  here pin behaviour for the cache contract — identity on hit, refresh on
  mtime bump, tolerance of malformed lines, ``[]`` on missing file.
"""
from __future__ import annotations

import os
import pathlib
import sys
import time

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / ".ai" / "dashboard"))
import serve  # noqa: E402 — path mangled above
import server.handlers.project as _ph  # noqa: E402 — _handle_list reads ROOT here (follows-the-move)


# ---------------------------------------------------------------------------
# _handle_list — symlink escape
# ---------------------------------------------------------------------------


class _FakeHandler:
    """Minimal stand-in for ``serve.Handler`` so we can call ``_handle_list``
    directly without spinning up the full HTTP server. Captures whatever
    payload ``_handle_list`` would have sent so the test can assert on the
    status code."""

    def __init__(self) -> None:
        self.responses: list[tuple[int, dict]] = []

    def _json(self, status: int, payload: dict) -> None:
        self.responses.append((status, payload))


def _can_make_symlink(tmp_path: pathlib.Path) -> bool:
    """Return True iff the running process can create symlinks at
    ``tmp_path``. On Windows this typically requires either Developer Mode
    or admin privileges, so the test skips cleanly when neither is on."""
    src = tmp_path / "__symlink_probe_target"
    link = tmp_path / "__symlink_probe_link"
    src.mkdir()
    try:
        os.symlink(src, link, target_is_directory=True)
    except (OSError, NotImplementedError, AttributeError):
        return False
    finally:
        # Clean up — best-effort, irrelevant if it fails.
        try:
            if link.exists() or link.is_symlink():
                link.unlink()
        except OSError:
            pass
        try:
            src.rmdir()
        except OSError:
            pass
    return True


def test_handle_list_rejects_symlink_escape(tmp_path, monkeypatch):
    """A symlink *inside* the repo pointing to a directory *outside* the repo
    must not let ``_handle_list`` walk through it. The bug: ``target =
    (ROOT / rel).resolve()`` follows the symlink, but ``target.relative_to(ROOT)``
    was comparing against the *unresolved* ROOT — so the escape slipped past
    when ROOT had any symlinks/junctions in its own path.

    We construct a fake ROOT with a directory that contains a symlink pointing
    to a sibling tree outside ROOT, then assert ``_handle_list`` refuses to
    enumerate the escape target."""
    if not _can_make_symlink(tmp_path):
        pytest.skip("symlinks not supported on this platform / privilege level")

    # Lay out:
    #   tmp_path/repo/              <- fake ROOT
    #   tmp_path/repo/inside/       <- legitimate subdir
    #   tmp_path/escape_target/     <- outside ROOT
    #   tmp_path/repo/inside/link   -> tmp_path/escape_target  (symlink)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "inside").mkdir()
    escape_target = tmp_path / "escape_target"
    escape_target.mkdir()
    (escape_target / "secret.md").write_text("should not leak", encoding="utf-8")
    os.symlink(escape_target, repo / "inside" / "link", target_is_directory=True)

    monkeypatch.setattr(serve, "ROOT", repo)
    monkeypatch.setattr(_ph, "ROOT", repo)  # _handle_list reads ROOT in its own module

    h = _FakeHandler()
    # Drive the bound method directly off the Handler class so we don't trip
    # over BaseHTTPRequestHandler's __init__. ``_handle_list`` only touches
    # ``self._json`` and module-level ``ROOT`` — the fake handler satisfies
    # both contracts.
    serve.Handler._handle_list(h, {"path": ["inside/link"]})

    assert h.responses, "_handle_list should have produced exactly one response"
    status, payload = h.responses[0]
    assert status in (403, 404), (
        f"symlink escape should be rejected (403) or treated as not-a-dir "
        f"(404); got {status} payload={payload}"
    )
    # And the directory listing must not have leaked any of the outside files.
    assert "entries" not in payload or "secret.md" not in payload.get("entries", []), (
        "outside-of-ROOT file leaked through symlink"
    )


# ---------------------------------------------------------------------------
# _load_jsonl_cached — contract tests
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_jsonl_cache():
    """Reset the module-level cache between tests so one test's writes don't
    leave a stale entry for the next test's path. The fixture runs around
    every test in this module — cheap and avoids per-test boilerplate."""
    with serve._JSONL_CACHE_LOCK:
        serve._JSONL_CACHE.clear()
    yield
    with serve._JSONL_CACHE_LOCK:
        serve._JSONL_CACHE.clear()


def test_jsonl_cache_returns_same_object_on_unchanged_mtime(tmp_path):
    """A second read with no file modification must return the exact same
    ``list`` object — the whole point of the cache is to avoid the parse on
    repeat calls. Identity (``is``) is the strongest signal that the cache
    served the call."""
    p = tmp_path / "ledger.jsonl"
    p.write_text('{"id": "a", "v": 1}\n{"id": "b", "v": 2}\n', encoding="utf-8")

    first = serve._load_jsonl_cached(p)
    second = serve._load_jsonl_cached(p)

    assert first is second, "cache hit must return same list reference"
    assert first == [{"id": "a", "v": 1}, {"id": "b", "v": 2}]


def test_jsonl_cache_invalidates_on_mtime_change(tmp_path):
    """Bumping the file's mtime must force a re-parse. We force mtime via
    ``os.utime`` rather than racing the filesystem clock — on Windows the
    mtime resolution is ~100ns but multiple writes inside the same tick
    would otherwise share a timestamp and look unchanged to the cache."""
    p = tmp_path / "ledger.jsonl"
    p.write_text('{"id": "a"}\n', encoding="utf-8")
    v1 = serve._load_jsonl_cached(p)
    assert v1 == [{"id": "a"}]

    # Rewrite with different content and explicitly advance mtime so the
    # cache sees a fresh stat. ``time.time() + 1`` is the simplest reliable
    # bump that beats any filesystem timestamp granularity.
    p.write_text('{"id": "a"}\n{"id": "b"}\n', encoding="utf-8")
    new_ts = time.time() + 1.0
    os.utime(p, (new_ts, new_ts))

    v2 = serve._load_jsonl_cached(p)
    assert v2 == [{"id": "a"}, {"id": "b"}]
    assert v1 is not v2, "post-modification read must return a fresh list"


def test_jsonl_cache_handles_malformed_lines(tmp_path):
    """A single corrupt line must not poison the whole endpoint — the
    original hand-rolled readers all swallowed ``json.JSONDecodeError``
    silently. The cache helper preserves that behaviour."""
    p = tmp_path / "ledger.jsonl"
    p.write_text(
        '{"id": "a"}\n'
        'this is not json at all\n'
        '{"id": "b"}\n',
        encoding="utf-8",
    )

    rows = serve._load_jsonl_cached(p)

    assert rows == [{"id": "a"}, {"id": "b"}], (
        "malformed middle line must be skipped silently, valid rows kept"
    )


def test_jsonl_cache_returns_empty_on_missing(tmp_path):
    """A path that doesn't exist returns ``[]`` rather than raising — every
    caller used to special-case this with ``if not X.exists(): return ...``;
    the helper centralises that branch."""
    missing = tmp_path / "definitely-not-here.jsonl"
    assert not missing.exists()

    rows = serve._load_jsonl_cached(missing)

    assert rows == []
    # And we must NOT have cached an empty result for the missing path —
    # otherwise the next call after the file is created would still see []
    # until something else triggered an mtime change.
    with serve._JSONL_CACHE_LOCK:
        assert str(missing) not in serve._JSONL_CACHE, (
            "missing files must not pollute the cache"
        )
