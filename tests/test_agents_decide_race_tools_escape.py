"""Static-lint guards for two batch-6 fixes in agents.js:

1.  decideAgentProposal() must use an epoch counter (not just the
    `_currentAgentProposalId` snapshot) to detect stale callbacks.

    Background -- the id-only snapshot catches "user opened a DIFFERENT
    proposal mid-flight" but NOT "user re-clicked accept on the SAME
    proposal" (an accidental double-tap, a stuck-draft re-Accept, etc.).
    In that case both the older and newer in-flight handlers see
    `propId === _currentAgentProposalId` and the older response can still
    win, mutating the modal with stale state. The defence is a monotonic
    `_decideAgentProposalEpoch` ticked at function entry and compared
    after every await -- exactly mirroring the `_decideProposalEpoch`
    pattern in skills.js (decideProposal).

2.  Every interpolation of an attacker-controlled `tools` field must be
    routed through escape() (or equivalent). The agents catalog renders
    the raw `tools` field into a tooltip `title` attribute on the
    metrics row; without escaping, a malicious tools string could break
    the attribute or inject markup. Same applies to the meta line and
    the proposal modal meta.

These tests pin the source-level shape so future refactors don't
silently regress either fix.
"""

from __future__ import annotations

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


def _function_body(src: str, name: str, *, is_async: bool = False) -> str:
    """Return brace-balanced body of a top-level function (incl. braces)."""
    prefix = r"async\s+" if is_async else r"(?:async\s+)?"
    pat = re.compile(prefix + r"function\s+" + re.escape(name) + r"\s*\(")
    m = pat.search(src)
    assert m, f"function {name!r} not found in agents.js"
    i = src.find("{", m.end())
    assert i != -1, f"opening brace for {name!r} not found"
    depth = 0
    j = i
    while j < len(src):
        c = src[j]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return src[i:j + 1]
        j += 1
    raise AssertionError(f"could not find end of function {name!r}")


# --- Target 1: epoch race guard in decideAgentProposal ----------------------


def test_decide_agent_proposal_epoch_module_var_exists():
    """A module-scope `_decideAgentProposalEpoch` counter must exist.

    Without a module-scope counter we cannot tick it on entry; the
    epoch snapshot logic would have nothing to compare against.
    """
    src = _src()
    assert re.search(
        r"\bvar\s+_decideAgentProposalEpoch\s*=\s*0\b", src
    ), (
        "agents.js must declare `var _decideAgentProposalEpoch = 0;` at "
        "module scope so decideAgentProposal can tick it on entry"
    )


def test_decide_agent_proposal_snapshots_epoch_before_await():
    """The epoch must be captured into a local before the first await.

    Pattern: `const epoch = ++_decideAgentProposalEpoch;` (or any local
    that increments the counter) must appear before the first `await`.
    """
    body = _function_body(_src(), "decideAgentProposal", is_async=True)
    await_match = re.search(r"\bawait\b", body)
    assert await_match, "expected at least one await in decideAgentProposal"
    pre_await = body[: await_match.start()]
    snapshot = re.search(
        r"\b(?:var|let|const)\s+\w+\s*=\s*\+\+\s*_decideAgentProposalEpoch\b",
        pre_await,
    )
    assert snapshot, (
        "decideAgentProposal must capture `++_decideAgentProposalEpoch` "
        "into a local before the first await; got pre-await section:\n"
        f"{pre_await}"
    )


def test_decide_agent_proposal_guards_epoch_after_await():
    """After every await the function must compare the snapshotted epoch
    against the current `_decideAgentProposalEpoch`.

    Either an unequal epoch OR an unequal id means a newer call owns the
    UI now -- the older call must drop its mutations on the floor. The
    skills.js mirror uses `epoch !== _decideProposalEpoch || propId !==
    _currentProposalId`; we accept the same shape for the agents twin.
    """
    body = _function_body(_src(), "decideAgentProposal", is_async=True)
    await_match = re.search(r"\bawait\b", body)
    assert await_match, "expected at least one await in decideAgentProposal"
    post_await = body[await_match.end():]
    # At least one post-await guard must check the epoch.
    guard = re.search(r"!==\s*_decideAgentProposalEpoch", post_await)
    assert guard, (
        "decideAgentProposal must guard against a stale epoch after an "
        "await (e.g. `if (epoch !== _decideAgentProposalEpoch) return;`)"
    )


def test_decide_agent_proposal_guards_epoch_on_error_path():
    """The error (catch) path must also drop a stale handler's mutations.

    Without the guard, a failing in-flight request from proposal A would
    re-enable the accept/reject buttons on proposal B's modal because the
    user navigated mid-flight.
    """
    body = _function_body(_src(), "decideAgentProposal", is_async=True)
    catch_match = re.search(r"\}\s*catch\s*\(", body)
    assert catch_match, "expected a catch block in decideAgentProposal"
    catch_block = body[catch_match.end():]
    guard = re.search(r"!==\s*_decideAgentProposalEpoch", catch_block)
    assert guard, (
        "decideAgentProposal catch{} must guard against a stale epoch "
        "before flipping accept/reject buttons on a possibly-different "
        "modal"
    )


def test_decide_agent_proposal_keeps_id_snapshot():
    """The epoch fix must NOT remove the existing id snapshot.

    Both guards together (epoch + id) are needed because the id alone
    misses double-clicks on the same proposal, and the epoch alone
    misses cases where the OLD id is captured but no tick happened
    between two opens. Keep both -- skills.js does.
    """
    body = _function_body(_src(), "decideAgentProposal", is_async=True)
    await_match = re.search(r"\bawait\b", body)
    assert await_match, "expected at least one await in decideAgentProposal"
    pre_await = body[: await_match.start()]
    snapshot = re.search(
        r"\b(?:var|let|const)\s+\w+\s*=\s*_currentAgentProposalId\b",
        pre_await,
    )
    assert snapshot, (
        "decideAgentProposal must still snapshot _currentAgentProposalId "
        "into a local before the first await -- the id catches a class "
        "of races the epoch alone cannot"
    )


# --- Target 2: tools field escaping in tooltip + meta + proposal ------------


def test_tools_field_in_grid_tooltip_is_escaped():
    """The card-grid `title=` tooltip must wrap a.tools in escape().

    `title="${a.tools}"` would let a tools string containing `"` close
    the attribute early and inject arbitrary HTML attributes. We accept
    any helper named escape* or *Html* (escape, escHtml, etc.) so future
    rename of the helper still satisfies the lint.
    """
    src = _src()
    # All title="..." attributes that mention an unprefixed `a.tools`.
    # Match the *value* of title= between the first opening "${" and the
    # closing }". This is strict enough to flag a raw interpolation.
    bad = re.findall(
        r'title\s*=\s*"\$\{\s*a\.tools\s*\}"',
        src,
    )
    assert not bad, (
        f"agents.js renders raw `a.tools` into a title attribute "
        f"({len(bad)} occurrence(s)). Wrap in escape(...) so quotes / "
        f"angle brackets in the tools field cannot break the attribute "
        f"or inject markup."
    )


def test_tools_field_grid_tooltip_uses_escape_helper():
    """Positive check: at least one `title="${escape(...a.tools...)}"`
    interpolation exists where the grid renders the metrics row tooltip.

    Without this, the negative test above could pass simply because
    someone deleted the tooltip altogether -- regressing the feature.
    """
    src = _src()
    # Match any HTML-escape helper around `a.tools` inside a title attr.
    has_escape = re.search(
        r'title\s*=\s*"\$\{\s*(?:escape|escHtml|escapeHtml)\s*\(\s*a\.tools[^)]*\)\s*\}"',
        src,
    )
    assert has_escape, (
        "Expected the grid card to render the raw tools string in a "
        "tooltip via `title=\"${escape(a.tools)}\"` so reviewers can "
        "still inspect the raw value safely"
    )


def test_all_tools_interpolations_routed_through_escape():
    """Every `tools` interpolation that lands in markup must use escape*.

    We scan for `${...tools...}` inside template literals and reject any
    occurrence that doesn't wrap the value in escape / escHtml. The
    safe metric-pill rendering of *parsed* tool tokens is fine because
    those use the local `t` variable (already escaped), so we restrict
    the check to interpolations that reference a `.tools` property
    directly (the raw, attacker-controlled CSV/JSON string).
    """
    src = _src()
    # Match any ${...} that contains the substring `.tools` (e.g.
    # `a.tools`, `cached.tools`, `p.tools`). We then assert that each
    # such interpolation also calls an escape helper inside the braces.
    interps = re.findall(r"\$\{[^}]*\.tools[^}]*\}", src)
    assert interps, (
        "expected at least one `${...tools...}` interpolation in "
        "agents.js -- did the file shape change?"
    )
    bad = [
        s for s in interps
        if not re.search(r"\b(?:escape|escHtml|escapeHtml)\s*\(", s)
    ]
    assert not bad, (
        f"agents.js interpolates `.tools` into markup without escape(): "
        f"{bad!r}. Wrap each occurrence in escape(...) -- a hostile JSON "
        f"payload with HTML/quote characters in the tools field would "
        f"otherwise leak into attributes or DOM."
    )
