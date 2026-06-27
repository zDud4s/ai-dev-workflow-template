"""Batch-7 regression coverage for serve.py.

What this batch landed:

1. JSONL parse-cache extension to the remaining bypass sites:
     * ``_load_auto_select_ranking`` (METRICS_FILE)
     * ``_load_timeline_runs``        (EVENTS_FILE)
     * ``_last_improver_run_ts``      (IMPROVEMENTS_LEDGER)
     * ``_should_revert_skill``       (IMPROVEMENTS_LEDGER + SKILL_METRICS_FILE)

   The docs/bug-hunt-status.md "Batch 6" entry claimed timeline+auto-select
   were already migrated; the actual commit landed the helper but never
   touched the callers (likely undone by 9072946 "revert Gemini integration").
   This batch finishes the migration.

2. HTTP-bound suggestion subprocess wall-clock cap. The interactive
   ``/api/suggestions/<id>/draft`` and ``/api/agents/suggest`` endpoints
   used ``cfg["timeout_seconds"]`` directly (default 120s, upper bound
   3600s via _IMPROVER_TIMEOUT_BOUNDS). Even with the 2-slot semaphore,
   a 1-hour subprocess pinning a request thread is a trivial DoS.
   ``_SUGGESTION_HTTP_TIMEOUT_MAX = 60`` caps the wait regardless of cfg.

All tests assert behavioural invariants (identity-based cache-hit proofs,
mtime-bump invalidation, source-level guards that callers route through
the helper). The cache helper preserves the ``errors="replace"`` invariant
on the underlying read — that invariant is re-validated here so a future
refactor of the helper can't silently regress UTF-8 robustness.
"""
from __future__ import annotations

import inspect
import json
import os
import pathlib
import sys
import time

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SERVE_PATH = REPO_ROOT / ".ai" / "dashboard" / "serve.py"

sys.path.insert(0, str(REPO_ROOT / ".ai" / "dashboard"))
import serve  # noqa: E402 — path mangled above
import server.analytics as _an  # analytics readers resolve consts in their namespace (follows-the-move)
import server.improver_io as _io  # noqa: E402 — _last_improver_run_ts/_check_skill_regression read consts here (follows-the-move)


SRC = SERVE_PATH.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Hygiene: the JSONL cache is module-global. Clear before AND after every
# test so writes from one test never bleed into the next.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_jsonl_cache():
    with serve._JSONL_CACHE_LOCK:
        serve._JSONL_CACHE.clear()
    yield
    with serve._JSONL_CACHE_LOCK:
        serve._JSONL_CACHE.clear()


# ---------------------------------------------------------------------------
# 1. _load_auto_select_ranking — METRICS_FILE now routed via cache
# ---------------------------------------------------------------------------


def test_auto_select_uses_jsonl_cache_object_identity(tmp_path, monkeypatch):
    """Two back-to-back reads of an unchanged METRICS_FILE must return the
    same list object — the cache short-circuits at the dict lookup."""
    metrics = tmp_path / "metrics.jsonl"
    sample = {
        "ts": "2026-05-24T00:00:00+00:00",
        "phase": "execute",
        "size": "small",
        "risk": "low",
        "budget": "medium",
        "tool": "claude",
        "model": "claude-sonnet-4-5",
        "reasoning_effort": None,
        "exit_code": 0,
        "handoff_complete": True,
        "review_verdict": "approve",
        "duration_ms": 1500,
    }
    metrics.write_text(
        "\n".join(json.dumps(sample) for _ in range(5)) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(serve, "METRICS_FILE", metrics)
    monkeypatch.setattr(_an, "METRICS_FILE", metrics)  # follows-the-move

    # First call: parses the file (cache miss).
    r1 = serve._load_auto_select_ranking(min_samples=1)
    assert r1["samples"] == 5
    assert r1["groups"], "expected one ranking group"

    # Cache-hit identity proof: two back-to-back helper calls return the
    # exact same list object.
    a = serve._load_jsonl_cached(metrics)
    b = serve._load_jsonl_cached(metrics)
    assert a is b, "_load_jsonl_cached must short-circuit on unchanged mtime"

    # The aggregate output stays identical across the cache hit.
    r2 = serve._load_auto_select_ranking(min_samples=1)
    assert r2 == r1


def test_auto_select_mtime_bump_invalidates_cache(tmp_path, monkeypatch):
    """When METRICS_FILE's mtime changes the cache must re-parse from disk."""
    metrics = tmp_path / "metrics.jsonl"
    base = {"phase": "plan", "size": "small", "risk": "low", "budget": "small",
            "tool": "claude", "model": "x", "reasoning_effort": None,
            "exit_code": 0, "handoff_complete": True, "review_verdict": "approve",
            "duration_ms": 1000, "ts": "2026-05-24T00:00:00+00:00"}
    metrics.write_text(json.dumps(base) + "\n", encoding="utf-8")
    monkeypatch.setattr(serve, "METRICS_FILE", metrics)
    monkeypatch.setattr(_an, "METRICS_FILE", metrics)  # follows-the-move

    first = serve._load_jsonl_cached(metrics)
    # Force a forward mtime jump (the comparator uses ``st_mtime_ns ==``).
    new_ts = time.time() + 2.0
    os.utime(metrics, (new_ts, new_ts))
    second = serve._load_jsonl_cached(metrics)
    assert first is not second, "mtime bump must produce a fresh parse"


def test_auto_select_source_no_direct_read_text():
    """Source-level guard: the helper must not call ``METRICS_FILE.read_text(``
    any more — that was exactly the bypass batch 7 closed."""
    body = inspect.getsource(serve._load_auto_select_ranking)
    assert "METRICS_FILE.read_text" not in body, (
        "_load_auto_select_ranking still reads METRICS_FILE directly; bypasses cache"
    )
    assert "_load_jsonl_cached(METRICS_FILE)" in body, (
        "_load_auto_select_ranking should pull rows through _load_jsonl_cached"
    )


# ---------------------------------------------------------------------------
# 2. _load_timeline_runs — EVENTS_FILE now routed via cache
# ---------------------------------------------------------------------------


def test_timeline_runs_uses_jsonl_cache(tmp_path, monkeypatch):
    """Timeline endpoint must read EVENTS_FILE through the cache helper."""
    events = tmp_path / "events.jsonl"
    ev = {
        "ts": "2026-05-24T00:00:00+00:00",
        "kind": "phase_dispatch",
        "session_id": "s7",
        "phase": "plan",
        "tool": "claude",
        "model": "claude-sonnet-4-5",
        "exit_code": 0,
    }
    events.write_text(json.dumps(ev) + "\n", encoding="utf-8")
    monkeypatch.setattr(serve, "EVENTS_FILE", events)
    monkeypatch.setattr(_an, "EVENTS_FILE", events)  # follows-the-move

    runs = serve._load_timeline_runs()
    assert len(runs) == 1
    assert runs[0]["session_id"] == "s7"
    assert runs[0]["phases"][0]["phase"] == "plan"

    # Cache identity.
    a = serve._load_jsonl_cached(events)
    b = serve._load_jsonl_cached(events)
    assert a is b, "events.jsonl cache must short-circuit on unchanged mtime"


def test_timeline_source_no_direct_read_text():
    """Source-level guard mirroring the auto-select check."""
    body = inspect.getsource(serve._load_timeline_runs)
    assert "EVENTS_FILE.read_text" not in body, (
        "_load_timeline_runs still reads EVENTS_FILE directly; bypasses cache"
    )
    assert "_load_jsonl_cached(EVENTS_FILE)" in body, (
        "_load_timeline_runs should pull rows through _load_jsonl_cached"
    )


def test_timeline_ignores_non_phase_dispatch(tmp_path, monkeypatch):
    """Non-phase_dispatch rows still get filtered out after the cache
    migration. Regression guard: ``ev.get("kind") != "phase_dispatch"``."""
    events = tmp_path / "events.jsonl"
    rows = [
        {"ts": "2026-05-24T00:00:00+00:00", "kind": "phase_dispatch",
         "session_id": "s1", "phase": "plan", "tool": "claude",
         "model": "x", "exit_code": 0},
        {"ts": "2026-05-24T00:00:01+00:00", "kind": "other",
         "session_id": "s1"},  # MUST be filtered
    ]
    events.write_text("\n".join(json.dumps(r) for r in rows) + "\n",
                      encoding="utf-8")
    monkeypatch.setattr(serve, "EVENTS_FILE", events)
    monkeypatch.setattr(_an, "EVENTS_FILE", events)  # follows-the-move
    runs = serve._load_timeline_runs()
    assert len(runs) == 1
    assert len(runs[0]["phases"]) == 1, (
        f"non-phase_dispatch rows should be filtered: {runs[0]['phases']!r}"
    )


# ---------------------------------------------------------------------------
# 3. _last_improver_run_ts — IMPROVEMENTS_LEDGER now routed via cache
# ---------------------------------------------------------------------------


def test_last_improver_run_ts_uses_cache(tmp_path, monkeypatch):
    ledger = tmp_path / "improvements.jsonl"
    rows = [
        {"skill": "frobnicate", "status": "applied",
         "ts": "2026-05-23T10:00:00+00:00"},
        {"skill": "frobnicate", "status": "rolled_back",
         "ts": "2026-05-24T11:00:00+00:00"},
        {"skill": "other-skill", "status": "applied",
         "ts": "2026-05-25T12:00:00+00:00"},  # different skill: must be ignored
    ]
    ledger.write_text("\n".join(json.dumps(r) for r in rows) + "\n",
                      encoding="utf-8")
    monkeypatch.setattr(serve, "IMPROVEMENTS_LEDGER", ledger)
    monkeypatch.setattr(_io, "IMPROVEMENTS_LEDGER", ledger)  # follows-the-move

    ts = serve._last_improver_run_ts("frobnicate")
    # The newest row for that skill: 2026-05-24T11:00:00Z = epoch 1779613200.
    assert ts > 0
    expected = serve._iso_to_epoch("2026-05-24T11:00:00+00:00")
    assert ts == expected

    # Cache identity proof.
    a = serve._load_jsonl_cached(ledger)
    b = serve._load_jsonl_cached(ledger)
    assert a is b, "ledger cache must short-circuit"


def test_last_improver_run_ts_returns_zero_when_no_file(tmp_path, monkeypatch):
    monkeypatch.setattr(serve, "IMPROVEMENTS_LEDGER", tmp_path / "missing.jsonl")
    monkeypatch.setattr(_io, "IMPROVEMENTS_LEDGER", tmp_path / "missing.jsonl")  # follows-the-move
    assert serve._last_improver_run_ts("anything") == 0.0


def test_last_improver_source_no_manual_open():
    """Source-level guard: the helper must NOT do its own ``.open("r"``
    on the ledger any more — that was the bypass batch 7 closed."""
    body = inspect.getsource(serve._last_improver_run_ts)  # follows the shim
    assert "IMPROVEMENTS_LEDGER.open" not in body, (
        "_last_improver_run_ts still opens the ledger directly; bypasses cache"
    )
    assert "_load_jsonl_cached(IMPROVEMENTS_LEDGER)" in body


# ---------------------------------------------------------------------------
# 4. _should_revert_skill — both ledgers routed via cache
# ---------------------------------------------------------------------------


def test_check_skill_regression_source_uses_cache():
    """Source-level guard: ``_check_skill_regression`` must use the cache for
    BOTH IMPROVEMENTS_LEDGER and SKILL_METRICS_FILE — those are the two
    bypass sites batch 7 closed in this function (it's the auto-revert
    decider feeding ``_auto_revert_skill``)."""
    body = inspect.getsource(serve._check_skill_regression)  # follows the shim
    assert "IMPROVEMENTS_LEDGER.open" not in body, (
        "_check_skill_regression still opens improvements ledger directly"
    )
    assert "SKILL_METRICS_FILE.open" not in body, (
        "_check_skill_regression still opens skill_metrics directly"
    )
    assert "_load_jsonl_cached(IMPROVEMENTS_LEDGER)" in body
    assert "_load_jsonl_cached(SKILL_METRICS_FILE)" in body


# ---------------------------------------------------------------------------
# 5. Cache helper still honours ``errors="replace"`` for partially-written
# UTF-8. Regression: a future refactor of the helper could silently drop
# this; pin the invariant via a real malformed-bytes file.
# ---------------------------------------------------------------------------


def test_jsonl_cache_helper_handles_invalid_utf8(tmp_path):
    """Half-written multi-byte UTF-8 must not 500 the cache helper."""
    p = tmp_path / "metrics.jsonl"
    p.write_bytes(
        json.dumps({"phase": "plan", "ok": True}).encode("utf-8") + b"\n"
        # Lone continuation byte mid-line — would raise UnicodeDecodeError
        # without errors="replace".
        + b"{\"phase\": \"\xc3execute\", \"ok\": false}\n"
        + json.dumps({"phase": "review", "ok": True}).encode("utf-8") + b"\n"
    )
    rows = serve._load_jsonl_cached(p)
    # At minimum the well-formed rows survived.
    phases = [r.get("phase") for r in rows if isinstance(r, dict)]
    assert "plan" in phases
    assert "review" in phases


# ---------------------------------------------------------------------------
# 6. _SUGGESTION_HTTP_TIMEOUT_MAX — wall-clock cap on the request-thread
# subprocess wait. cfg["timeout_seconds"] can be up to 3600s; the
# interactive endpoint must NOT honour that without an upper bound.
# ---------------------------------------------------------------------------


def test_suggestion_http_timeout_max_constant_is_sane():
    assert hasattr(serve, "_SUGGESTION_HTTP_TIMEOUT_MAX"), (
        "_SUGGESTION_HTTP_TIMEOUT_MAX must exist as a module-level cap"
    )
    cap = serve._SUGGESTION_HTTP_TIMEOUT_MAX
    assert isinstance(cap, int) and cap > 0
    # Must be tighter than cfg["timeout_seconds"] default (120s) so the cap
    # actually wins; must also be lower than _IMPROVER_TIMEOUT_BOUNDS upper
    # (3600s) so the cap can't be bypassed.
    assert cap <= 120, (
        "the HTTP cap must be at least as tight as the default cfg timeout"
    )
    assert cap < 3600, (
        "the HTTP cap must be strictly below the cfg upper bound"
    )


def test_suggestion_draft_source_caps_subprocess_timeout():
    """Source-level guard: ``_handle_suggestion_draft`` must compose the
    ``timeout=`` arg as ``min(cfg.get('timeout_seconds', 120),
    _SUGGESTION_HTTP_TIMEOUT_MAX)`` — not the raw cfg value."""
    body = inspect.getsource(serve.Handler._handle_suggestion_draft)
    assert "_SUGGESTION_HTTP_TIMEOUT_MAX" in body, (
        "_handle_suggestion_draft does not cap subprocess timeout — DoS risk"
    )


def test_agent_suggest_source_caps_subprocess_timeout():
    """Same guard for ``_handle_agent_suggest``."""
    body = inspect.getsource(serve.Handler._handle_agent_suggest)
    assert "_SUGGESTION_HTTP_TIMEOUT_MAX" in body, (
        "_handle_agent_suggest does not cap subprocess timeout — DoS risk"
    )


# ---------------------------------------------------------------------------
# 7. End-to-end source sweep: NO JSONL ledger path may be ``.read_text``
# or hand-opened anywhere in serve.py except inside the cache helper itself
# and inside the persistence appenders (which open in "a" mode, not "r").
# ---------------------------------------------------------------------------


# JOBS_PERSIST_FILE is exempt: `_load_persisted_jobs` does a one-time full-file
# boot scan (last-wins dedup + on-shrink compaction) that the deque-bounded
# cache helper cannot serve. All other ledgers go through `_load_jsonl_cached`.
@pytest.mark.parametrize(
    "ledger_constant",
    ["METRICS_FILE", "EVENTS_FILE",
     "SKILL_METRICS_FILE", "IMPROVEMENTS_LEDGER"],
)
def test_no_ledger_is_read_outside_the_cache_helper(ledger_constant):
    """Comprehensive guard: every reader of a known JSONL ledger goes
    through ``_load_jsonl_cached``. Writes (open with ``"a"``) are fine
    and explicitly excluded."""
    # Detect any line that does ``<LEDGER>.read_text`` OR
    # ``<LEDGER>.open("r"`` — both are the bypass patterns we just closed.
    bad_read_text = f"{ledger_constant}.read_text"
    bad_open_read = f'{ledger_constant}.open("r'
    assert bad_read_text not in SRC, (
        f"{ledger_constant} is still read with .read_text somewhere — "
        f"bypasses _load_jsonl_cached"
    )
    assert bad_open_read not in SRC, (
        f"{ledger_constant} is still manually opened for reading "
        f"somewhere — bypasses _load_jsonl_cached"
    )
