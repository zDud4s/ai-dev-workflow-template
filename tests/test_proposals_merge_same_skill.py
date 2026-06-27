"""Regression tests for the same-skill proposal merge behaviour.

When the improver (or draft generator) creates a new pending proposal for
a skill that already has a pending proposal, the older one must collapse
out of the pending list — the newer vision supersedes it. On disk the
older file stays, just flipped to ``status="superseded"`` so the audit
trail is intact.

These tests pin three guarantees:

  1. ``_supersede_prior_pending`` marks every prior pending proposal for
     the same (skill, kind) as superseded, leaving unrelated proposals,
     different-kind proposals, and already-terminal proposals untouched.
  2. ``_write_proposal`` calls the helper after writing, and records the
     merged ids under ``merged_from`` on the new proposal.
  3. ``_handle_proposals_list``'s defensive pass collapses legacy
     duplicates already on disk (created before the eager-supersede
     hook landed) and surfaces ``merged_count`` on the survivor.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / ".ai" / "dashboard"))
import serve  # noqa: E402 — path mangled above
import server.improver_io as _io  # noqa: E402 — _write_proposal/_supersede_prior_pending read consts here (follows-the-move)


@pytest.fixture
def tmp_proposals(tmp_path, monkeypatch):
    """Redirect SKILL_PROPOSALS_DIR + ROOT to a clean temp tree."""
    proposals_dir = tmp_path / "proposals" / "skills"
    proposals_dir.mkdir(parents=True)
    monkeypatch.setattr(serve, "SKILL_PROPOSALS_DIR", proposals_dir)
    monkeypatch.setattr(serve, "ROOT", tmp_path)
    monkeypatch.setattr(_io, "SKILL_PROPOSALS_DIR", proposals_dir)  # follows-the-move
    monkeypatch.setattr(_io, "ROOT", tmp_path)  # follows-the-move
    return proposals_dir


def _write(proposals_dir: Path, pid: str, **fields):
    """Persist a minimal proposal triple."""
    payload = {
        "id": pid,
        "skill": fields.get("skill", "foo"),
        "kind": fields.get("kind", "improve"),
        "status": fields.get("status", "pending"),
        "ts": fields.get("ts", "2026-05-29T10:00:00+00:00"),
        "change_summary": fields.get("change_summary", ""),
        "diff_lines": fields.get("diff_lines", 5),
    }
    (proposals_dir / f"{pid}.json").write_text(json.dumps(payload), encoding="utf-8")
    (proposals_dir / f"{pid}.old.md").write_text(fields.get("old", "old"), encoding="utf-8")
    (proposals_dir / f"{pid}.new.md").write_text(fields.get("new", "new"), encoding="utf-8")
    return payload


def _read(proposals_dir: Path, pid: str) -> dict:
    return json.loads((proposals_dir / f"{pid}.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 1. _supersede_prior_pending — direct unit test
# ---------------------------------------------------------------------------


def test_supersede_marks_only_same_skill_same_kind_pending(tmp_proposals):
    _write(tmp_proposals, "old-1", skill="foo", kind="improve", status="pending")
    _write(tmp_proposals, "old-2", skill="foo", kind="improve", status="pending")
    # Unrelated proposals that MUST survive untouched.
    _write(tmp_proposals, "other-skill", skill="bar", kind="improve", status="pending")
    _write(tmp_proposals, "other-kind", skill="foo", kind="draft", status="pending")
    _write(tmp_proposals, "already-applied", skill="foo", kind="improve", status="applied")
    _write(tmp_proposals, "new-1", skill="foo", kind="improve", status="pending")

    merged = serve._supersede_prior_pending("foo", "new-1", "improve")

    assert set(merged) == {"old-1", "old-2"}
    assert _read(tmp_proposals, "old-1")["status"] == "superseded"
    assert _read(tmp_proposals, "old-1")["superseded_by"] == "new-1"
    assert _read(tmp_proposals, "old-2")["status"] == "superseded"
    # Untouched neighbours.
    assert _read(tmp_proposals, "other-skill")["status"] == "pending"
    assert _read(tmp_proposals, "other-kind")["status"] == "pending"
    assert _read(tmp_proposals, "already-applied")["status"] == "applied"
    # The triggering proposal itself is never marked.
    assert _read(tmp_proposals, "new-1")["status"] == "pending"


def test_supersede_returns_empty_when_no_prior(tmp_proposals):
    _write(tmp_proposals, "lonely", skill="foo", kind="improve", status="pending")
    assert serve._supersede_prior_pending("foo", "lonely", "improve") == []
    assert _read(tmp_proposals, "lonely")["status"] == "pending"


def test_supersede_skips_when_skill_empty(tmp_proposals):
    _write(tmp_proposals, "p1", skill="foo", kind="improve", status="pending")
    # An empty skill_id is a no-op — nothing to disambiguate against.
    assert serve._supersede_prior_pending("", "ignored", "improve") == []
    assert _read(tmp_proposals, "p1")["status"] == "pending"


# ---------------------------------------------------------------------------
# 2. _write_proposal — eager supersede + merged_from annotation
# ---------------------------------------------------------------------------


def test_write_proposal_collapses_prior_pending_and_records_merged_from(
    tmp_proposals, tmp_path, monkeypatch,
):
    skill_path = tmp_path / "skill.md"
    skill_path.write_text("disk content", encoding="utf-8")

    # Freeze the timestamp so the two writes don't collide on pid generation
    # (pid is `<slug>-<YYYYMMDD-HHMMSS>`). We advance the second call by one
    # second to keep ids distinct without depending on real wall-clock.
    import datetime as _dt
    real_dt = _dt.datetime
    counter = {"n": 0}

    class _FrozenDateTime(real_dt):
        @classmethod
        def now(cls, tz=None):
            counter["n"] += 1
            return real_dt(2026, 5, 29, 10, 0, counter["n"], tzinfo=tz)

    monkeypatch.setattr(serve._dt, "datetime", _FrozenDateTime)

    parsed = {"change_summary": "tighten triggers", "rationale": "rephrase"}
    first = serve._write_proposal("foo", skill_path, "old", "new-v1", parsed, 3, "job-a")
    second = serve._write_proposal("foo", skill_path, "old", "new-v2", parsed, 4, "job-b")

    assert first["id"] != second["id"]
    # First (older) was rewritten with the superseded status.
    older_on_disk = _read(tmp_proposals, first["id"])
    assert older_on_disk["status"] == "superseded"
    assert older_on_disk["superseded_by"] == second["id"]
    # Second (newer) absorbed the older — merged_from points back to it.
    newer_on_disk = _read(tmp_proposals, second["id"])
    assert newer_on_disk["status"] == "pending"
    assert newer_on_disk.get("merged_from") == [first["id"]]


# ---------------------------------------------------------------------------
# 3. _handle_proposals_list — defensive pass + merged_count surface
# ---------------------------------------------------------------------------


def _invoke_list():
    """Call ``_handle_proposals_list`` against a minimal handler stub.

    We avoid MagicMock(spec=Handler) here — that gives mocked attribute
    lookups instead of routing ``self._json`` to the patched class method,
    so a tiny purpose-built stub is cleaner."""
    captured: dict = {}

    class _Stub:
        def _json(self, status, body):
            captured["status"] = status
            captured["body"] = body

    serve.Handler._handle_proposals_list(_Stub())
    return captured


def test_list_endpoint_collapses_legacy_duplicates_and_emits_merged_count(
    tmp_proposals,
):
    # Two same-skill pending proposals already on disk — created BEFORE the
    # eager-supersede hook (this is what users currently have). Different
    # mtimes so the sort is deterministic: newer.json is touched last and
    # wins the merge.
    older = _write(tmp_proposals, "foo-old", skill="foo", kind="improve",
                   status="pending", ts="2026-05-29T09:00:00+00:00")
    import os, time
    t_old = time.time() - 60
    os.utime(tmp_proposals / "foo-old.json", (t_old, t_old))
    newer = _write(tmp_proposals, "foo-new", skill="foo", kind="improve",
                   status="pending", ts="2026-05-29T10:00:00+00:00")
    # Plus an unrelated standalone that must NOT get merged_count.
    _write(tmp_proposals, "bar-only", skill="bar", kind="improve",
           status="pending", ts="2026-05-29T10:00:00+00:00")

    captured = _invoke_list()
    assert captured["status"] == 200

    by_id = {p["id"]: p for p in captured["body"]["proposals"]}
    # Older was flipped to superseded on disk AND in the response.
    assert by_id["foo-old"]["status"] == "superseded"
    assert _read(tmp_proposals, "foo-old")["status"] == "superseded"
    # Newer survives as pending with merged_count=2 (itself + the absorbed older).
    assert by_id["foo-new"]["status"] == "pending"
    assert by_id["foo-new"]["merged_count"] == 2
    # Standalone has merged_count=1 (just itself, no merges).
    assert by_id["bar-only"]["merged_count"] == 1
