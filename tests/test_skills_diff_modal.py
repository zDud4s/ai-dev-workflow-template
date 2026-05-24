"""Static-lint tests for skills.js diff renderer + proposal modal race.

These tests inspect the source of `.ai/dashboard/app/skills.js` to verify
the structural fixes are present:

  1. `renderUnifiedDiff` emits hunk separators and uses symmetric leading /
     trailing context (no longer silently drops every `ctx` line after the
     first 3 with no marker).

  2. `decideProposal` snapshots `_currentProposalId` before its `await` and
     guards against the modal having been swapped out before the request
     resolves (so stale handlers don't flip a different modal's buttons).
"""

import re
from pathlib import Path

import pytest

SKILLS_JS = (
    Path(__file__).resolve().parent.parent
    / ".ai" / "dashboard" / "app" / "skills.js"
)


def _src() -> str:
    return SKILLS_JS.read_text(encoding="utf-8")


def _decide_proposal_body() -> str:
    """Return the textual body of the `decideProposal` function.

    We slice from the function header to the next top-level `function `
    declaration so the assertions only see this function's contents.
    """
    src = _src()
    start = src.index("async function decideProposal(")
    # Find the next top-level function declaration after decideProposal.
    rest = src[start:]
    nxt = re.search(r"\n    function\s+\w+\s*\(", rest)
    end = nxt.start() if nxt else len(rest)
    return rest[:end]


def test_diff_emits_hunk_separator():
    """The diff renderer must emit a hunk-separator marker so reviewers can
    tell when context has been collapsed (instead of silently telescoping
    distant changes together)."""
    src = _src()
    assert "diff-hunk-sep" in src, (
        "renderUnifiedDiff must emit a `diff-hunk-sep` span when it "
        "collapses a long ctx region. Found no such marker in skills.js."
    )


@pytest.mark.skip(
    reason="legacy ctx-compactor internals; renderer rewritten as unified-diff hunk emitter — see test_skills_diff_renderer_hunks.py"
)
def test_diff_compactor_handles_long_ctx_region():
    """The compactor must detect ctx regions longer than the
    leading+trailing window (6 lines), not just `ctxRun > 3` which silently
    hid every context line past the first 3."""
    src = _src()
    # Accept either an explicit `> 6` check or a CTX_THRESHOLD constant set
    # to CONTEXT * 2. Reject the old `ctxRun > 3` cliff.
    has_threshold = bool(
        re.search(r">\s*6\b", src)
        or re.search(r"CTX_THRESHOLD", src)
        or re.search(r"CONTEXT\s*\*\s*2", src)
    )
    assert has_threshold, (
        "renderUnifiedDiff should detect ctx regions longer than "
        "leading+trailing context (e.g. `ctxRun > 6` or a "
        "CTX_THRESHOLD = CONTEXT * 2 constant). The legacy `ctxRun > 3` "
        "cliff silently hid context lines."
    )
    # And confirm the old cliff pattern (with the trailing-3 fudge that
    # caused the bug) is gone.
    assert "ctxRun > 3 && idx < seq.length - 3" not in src, (
        "Legacy compactor pattern `ctxRun > 3 && idx < seq.length - 3` "
        "still present — this is the bug that hid context lines."
    )


def test_decide_proposal_snapshots_id():
    """`decideProposal` must capture `_currentProposalId` into a local
    BEFORE it awaits, so a later assignment to `_currentProposalId` (from
    opening a different proposal modal) doesn't corrupt the URL path or
    later comparisons."""
    body = _decide_proposal_body()

    # Find the snapshot assignment.
    snapshot_match = re.search(
        r"\b(?:var|let|const)\s+propId\s*=\s*_currentProposalId\b",
        body,
    )
    assert snapshot_match, (
        "decideProposal must snapshot `_currentProposalId` into a local "
        "named `propId` (or similar) before awaiting the network call."
    )

    # And the snapshot must come before the first `await`.
    first_await = body.find("await")
    assert first_await != -1, "decideProposal lost its `await` — unexpected."
    assert snapshot_match.start() < first_await, (
        "The `propId` snapshot must occur BEFORE the first `await` in "
        "decideProposal — otherwise the value can already be stale when "
        "we capture it."
    )


def test_decide_proposal_guards_against_stale_modal():
    """After awaiting the network call, `decideProposal` must check whether
    the user navigated to a different proposal modal in the meantime. If
    they did, the function must return without touching the now-foreign
    modal's `#proposal-msg` / button state."""
    body = _decide_proposal_body()

    # Look for a guard comparing the snapshot to the live global.
    guard = re.search(
        r"propId\s*!==?\s*_currentProposalId",
        body,
    )
    assert guard, (
        "decideProposal must compare its snapshotted `propId` against the "
        "live `_currentProposalId` after the await (e.g. "
        "`if (propId !== _currentProposalId) return;`) so a stale handler "
        "doesn't flip a different modal's buttons."
    )

    # The guard must appear AFTER the first await (otherwise it's tautological).
    first_await = body.find("await")
    assert guard.start() > first_await, (
        "The stale-modal guard must come AFTER the first await in "
        "decideProposal — before the await it would always be a no-op."
    )
