"""Regression tests for the agent-proposal modal stale-race fix.

`decideAgentProposal` runs async work (fetch + follow-up reloads) while the
user can navigate to a different proposal in the modal. Without an entry
snapshot and post-await guards, the in-flight handler would mutate the
modal that is now showing proposal B with UI state intended for proposal
A (close it, enable its buttons, set its message). These tests pin the
source-level shape of the fix so future refactors don't silently regress
it.
"""

import re
from pathlib import Path


AGENTS_JS = (
    Path(__file__).resolve().parent.parent
    / ".ai"
    / "dashboard"
    / "app"
    / "agents.js"
)


def _src() -> str:
    return AGENTS_JS.read_text(encoding="utf-8")


def _decide_body(src: str) -> str:
    """Return the source of the decideAgentProposal function body.

    We slice from the function declaration to the next top-level
    function declaration at the same indentation. This is a deliberately
    loose match — we only need a block that definitely contains the
    function and nothing past it.
    """
    m = re.search(
        r"async function decideAgentProposal\([^)]*\)\s*\{",
        src,
    )
    assert m, "decideAgentProposal not found in agents.js"
    start = m.start()
    # Find the next top-level function declaration after this one.
    nxt = re.search(
        r"\n    (?:async\s+)?function\s+\w+\s*\(",
        src[m.end():],
    )
    end = m.end() + (nxt.start() if nxt else len(src) - m.end())
    return src[start:end]


def test_decide_agent_proposal_snapshots_id():
    """A local snapshot of the proposal id must exist BEFORE any await.

    Pattern: `var propId = _currentAgentProposalId;` (or `const`/`let`,
    or any local identifier) must appear before the first `await` token
    in the function body.
    """
    body = _decide_body(_src())
    # Find the first await in the body.
    await_match = re.search(r"\bawait\b", body)
    assert await_match, "expected at least one await in decideAgentProposal"
    pre_await = body[: await_match.start()]
    snapshot = re.search(
        r"\b(?:var|let|const)\s+\w+\s*=\s*_currentAgentProposalId\b",
        pre_await,
    )
    assert snapshot, (
        "decideAgentProposal must snapshot _currentAgentProposalId "
        "into a local before the first await; got pre-await section:\n"
        f"{pre_await}"
    )


def test_decide_agent_proposal_guards_against_stale_modal():
    """At least one post-await guard like `!== _currentAgentProposalId` must exist.

    Without this guard, async work would race against the user opening
    another proposal and mutate the wrong modal.
    """
    body = _decide_body(_src())
    await_match = re.search(r"\bawait\b", body)
    assert await_match, "expected at least one await in decideAgentProposal"
    post_await = body[await_match.end():]
    guard = re.search(r"!==\s*_currentAgentProposalId", post_await)
    assert guard, (
        "decideAgentProposal must guard against a stale snapshot after "
        "an await (e.g. `if (propId !== _currentAgentProposalId) return;`)"
    )
