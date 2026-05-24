"""Runtime + structural tests for skills.js `renderUnifiedDiff`.

These tests EXECUTE the diff renderer (via Node, against the real source)
to confirm it emits standard unified-diff hunk headers with the correct
amount of leading + trailing context. They also re-assert the static
structural guarantees (epoch-counter on `decideProposal`, empty-list
clearing on `loadSkillProposals`).

The runtime side lives because the previous custom compactor passed all
the static asserts in `test_skills_diff_modal.py` while still emitting a
flat add/del list with no `@@` markers — reviewers literally could not
tell whether two changes were adjacent or 200 lines apart.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SKILLS_JS = ROOT / ".ai" / "dashboard" / "app" / "skills.js"
RUNNER_JS = Path(__file__).resolve().parent / "_diff_render_runner.js"

# Skip the runtime tests when node isn't on PATH (CI images vary). The
# static-source tests still execute either way.
NODE = shutil.which("node")
requires_node = pytest.mark.skipif(
    NODE is None, reason="node not on PATH; runtime renderer tests skipped"
)


def _render(old_text: str, new_text: str) -> str:
    """Invoke the JS renderer with the given fixture, return the HTML blob."""
    assert NODE, "_render called without node available"
    spec = json.dumps({"oldText": old_text, "newText": new_text})
    proc = subprocess.run(
        [NODE, str(RUNNER_JS)],
        input=spec,
        capture_output=True,
        text=True,
        timeout=15,
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"diff runner failed (rc={proc.returncode})\n"
            f"--- stderr ---\n{proc.stderr}\n--- stdout ---\n{proc.stdout}"
        )
    return json.loads(proc.stdout)["html"]


# Regex matching the standard unified-diff header `@@ -a,b +c,d @@`. We
# accept a header inside a `diff-hunk-sep` span (that's how skills.js wraps
# it) but the regex itself only cares about the @@-bracketed substring.
HUNK_HDR_RE = re.compile(r"@@ -(\d+),(\d+) \+(\d+),(\d+) @@")


def _hunk_starts(html: str) -> list[int]:
    """Return the character offsets of every `@@ -a,b +c,d @@` header."""
    return [m.start() for m in HUNK_HDR_RE.finditer(html)]


def _ctx_lines_before(html: str, change_offset: int, hunk_start: int) -> int:
    """Count `diff-ctx` spans between a hunk header and the next change."""
    section = html[hunk_start:change_offset]
    return len(re.findall(r'class="diff-line diff-ctx"', section))


# ----- Runtime: hunk header presence -----


@requires_node
def test_renderer_emits_at_sign_hunk_headers():
    """The renderer MUST emit a `@@ -a,b +c,d @@` marker for every hunk.
    Without it, reviewers can't tell whether two changes are adjacent or
    hundreds of lines apart — the diff effectively lies about distance."""
    old = "\n".join(["line" + str(i) for i in range(1, 21)] + ["OLD"] + ["line" + str(i) for i in range(21, 41)])
    new = "\n".join(["line" + str(i) for i in range(1, 21)] + ["NEW"] + ["line" + str(i) for i in range(21, 41)])
    html = _render(old, new)
    starts = _hunk_starts(html)
    assert len(starts) == 1, (
        f"expected exactly 1 hunk header for a single-line change, "
        f"found {len(starts)} in:\n{html}"
    )


@requires_node
def test_renderer_first_hunk_has_three_leading_context_lines():
    """The leading-context off-by-one: the FIRST change of each hunk used
    to have zero context lines before it. With proper unified-diff hunks
    the renderer must emit exactly CONTEXT=3 lines of context immediately
    after the `@@` header (or fewer if the file starts within 3 lines of
    the change)."""
    # Build a 30-line file with a single change at line 20 so 3 leading
    # context lines (17, 18, 19) come BEFORE the change and 3 trailing
    # context lines (21, 22, 23) come AFTER.
    old_lines = [f"line{i}" for i in range(1, 31)]
    new_lines = list(old_lines)
    new_lines[19] = "CHANGED"
    html = _render("\n".join(old_lines), "\n".join(new_lines))

    starts = _hunk_starts(html)
    assert starts, f"no `@@` header emitted; got:\n{html}"

    # First change marker after the hunk header.
    first_change = re.search(r'class="diff-line diff-(?:add|del)"', html)
    assert first_change, f"no add/del lines in output:\n{html}"

    ctx_before = _ctx_lines_before(html, first_change.start(), starts[0])
    assert ctx_before == 3, (
        f"expected 3 leading-context lines between the `@@` header and the "
        f"first change, got {ctx_before}. This is the off-by-one bug where "
        f"the leading-context slice was emitting zero lines.\n{html}"
    )


@requires_node
def test_renderer_two_distant_changes_split_into_two_hunks():
    """If two changes are more than 2*CONTEXT lines apart, the renderer
    must produce two separate hunks (matching `diff -U3` behaviour) instead
    of merging them into one giant hunk with bogus middle context."""
    # 30 lines with edits at index 5 and 25 (gap = 20 lines >> 2*3=6).
    old_lines = [f"row{i}" for i in range(30)]
    new_lines = list(old_lines)
    new_lines[5] = "EDIT_A"
    new_lines[25] = "EDIT_B"
    html = _render("\n".join(old_lines), "\n".join(new_lines))
    starts = _hunk_starts(html)
    assert len(starts) == 2, (
        f"expected 2 hunk headers for distant changes, got {len(starts)}. "
        f"Output:\n{html}"
    )


@requires_node
def test_renderer_nearby_changes_merge_into_single_hunk():
    """Two changes within 2*CONTEXT lines of each other should share a
    single hunk so the trailing/leading context overlap doesn't get
    duplicated."""
    old_lines = [f"r{i}" for i in range(20)]
    new_lines = list(old_lines)
    new_lines[8] = "EDIT_A"
    new_lines[10] = "EDIT_B"  # 2-line gap = well inside CONTEXT*2
    html = _render("\n".join(old_lines), "\n".join(new_lines))
    starts = _hunk_starts(html)
    assert len(starts) == 1, (
        f"expected 1 merged hunk for nearby changes (gap=2), got {len(starts)}. "
        f"Output:\n{html}"
    )


@requires_node
def test_renderer_hunk_header_arithmetic_is_consistent():
    """The lengths declared in `@@ -a,b +c,d @@` must equal the number of
    old/new lines inside the hunk. Wrong arithmetic produces a header that
    looks credible but lies about how much of the file it covers."""
    old_lines = [f"x{i}" for i in range(10)]
    new_lines = list(old_lines)
    new_lines[4] = "EDITED"
    html = _render("\n".join(old_lines), "\n".join(new_lines))

    m = HUNK_HDR_RE.search(html)
    assert m, f"no hunk header in:\n{html}"
    _, decl_old_len, _, decl_new_len = (int(x) for x in m.groups())

    # Count ctx + del + add lines AFTER the header. (Single hunk in this
    # fixture, so we don't have to slice between headers.)
    section = html[m.end():]
    ctx = len(re.findall(r'class="diff-line diff-ctx"', section))
    dels = len(re.findall(r'class="diff-line diff-del"', section))
    adds = len(re.findall(r'class="diff-line diff-add"', section))

    assert ctx + dels == decl_old_len, (
        f"hunk declares oldLen={decl_old_len}, but the body has "
        f"{ctx} ctx + {dels} del = {ctx + dels} old-side lines."
    )
    assert ctx + adds == decl_new_len, (
        f"hunk declares newLen={decl_new_len}, but the body has "
        f"{ctx} ctx + {adds} add = {ctx + adds} new-side lines."
    )


@requires_node
def test_renderer_no_orphan_changes_without_a_hunk_header():
    """Every `diff-add` / `diff-del` span MUST be preceded somewhere
    earlier in the output by a `@@` header. A change line floating before
    any hunk marker is the regression we are guarding against."""
    old_lines = [f"q{i}" for i in range(40)]
    new_lines = list(old_lines)
    new_lines[10] = "FIRST_EDIT"
    new_lines[30] = "SECOND_EDIT"
    html = _render("\n".join(old_lines), "\n".join(new_lines))

    # Walk the output once: every time we see a change line, the most-
    # recent `@@` header must already exist before it.
    pattern = re.compile(r'class="diff-line diff-(add|del)"|@@ -\d+,\d+ \+\d+,\d+ @@')
    seen_header = False
    for m in pattern.finditer(html):
        if m.group().startswith("@@"):
            seen_header = True
            continue
        assert seen_header, (
            f"change line found at offset {m.start()} with no preceding "
            f"`@@` header. This is the orphan-change regression.\n{html}"
        )


@requires_node
def test_renderer_identical_files_produce_no_hunks():
    """An identical pair should emit a friendly no-changes marker, not a
    bogus zero-length hunk header (which would mislead reviewers into
    thinking something changed)."""
    text = "a\nb\nc\nd\ne"
    html = _render(text, text)
    assert not HUNK_HDR_RE.search(html), (
        f"identical files should not produce a `@@` hunk header; got:\n{html}"
    )
    assert "identical" in html or "no change" in html.lower(), (
        f"identical files should produce a clear empty-state marker; got:\n{html}"
    )


@requires_node
def test_renderer_caps_at_lcs_line_cap_with_fallback_banner():
    """Files above LCS_LINE_CAP must fall through to the raw-dump path
    (preserving the cap from batch 3) instead of attempting an LCS on an
    enormous matrix."""
    # LCS_LINE_CAP is 2000 in skills.js; build a file with 2100 lines.
    big = "\n".join(f"L{i}" for i in range(2100))
    other = "\n".join(f"M{i}" for i in range(2100))
    html = _render(big, other)
    assert "too large for LCS" in html, (
        "expected the fallback banner when file exceeds LCS_LINE_CAP; "
        f"first 200 chars: {html[:200]!r}"
    )


# ----- Static: epoch counter on decideProposal (race guard) -----


def _src() -> str:
    return SKILLS_JS.read_text(encoding="utf-8")


def _decide_proposal_body() -> str:
    src = _src()
    start = src.index("async function decideProposal(")
    rest = src[start:]
    nxt = re.search(r"\n    function\s+\w+\s*\(", rest)
    end = nxt.start() if nxt else len(rest)
    return rest[:end]


def test_decide_proposal_uses_epoch_counter():
    """The proposal-id snapshot catches "user opened a DIFFERENT proposal
    mid-flight"; an epoch counter (mirroring `_skillDetailEpoch`) catches
    the residual race where the user clicks accept TWICE on the same
    proposal and the older response would otherwise win after the newer."""
    src = _src()
    # Module-level epoch declaration must exist.
    assert re.search(r"var\s+_decideProposalEpoch\s*=\s*0\b", src), (
        "skills.js must declare a module-level `_decideProposalEpoch` "
        "counter at the top of the proposals section (mirroring "
        "`_skillDetailEpoch`)."
    )

    body = _decide_proposal_body()
    # Body must tick the epoch (increment-then-snapshot) before the await.
    tick = re.search(r"(?:const|let|var)\s+epoch\s*=\s*\+\+_decideProposalEpoch\b", body)
    assert tick, (
        "decideProposal must snapshot a local `epoch = ++_decideProposalEpoch` "
        "at entry so the LATER call always wins on resume."
    )

    first_await = body.find("await")
    assert first_await != -1
    assert tick.start() < first_await, (
        "The epoch snapshot must occur BEFORE the first `await` — "
        "incrementing after the await defeats the whole guard."
    )

    # And there must be at least one `epoch !== _decideProposalEpoch` guard
    # AFTER the first await.
    guard = re.search(r"epoch\s*!==?\s*_decideProposalEpoch", body[first_await:])
    assert guard, (
        "After the await, decideProposal must compare its captured `epoch` "
        "against the live `_decideProposalEpoch` and bail (or skip UI "
        "updates) when they differ — same shape as `_skillDetailEpoch`."
    )


def test_decide_proposal_epoch_guard_covers_both_paths():
    """The epoch guard must protect BOTH the success path and the error
    path. A guard that only covers `then` while the catch still flips the
    foreign modal's buttons is a half-fix."""
    body = _decide_proposal_body()
    # Split the body at the try/catch boundary (`} catch (e) {` style) so
    # we don't accidentally match the `.catch(() => ({}))` on the json
    # parse, which lives INSIDE the try block.
    boundary = re.search(r"\}\s*catch\s*\(\s*\w+\s*\)\s*\{", body)
    assert boundary, "could not locate try/catch boundary in decideProposal"
    success_half = body[:boundary.start()]
    error_half = body[boundary.start():]
    assert re.search(r"epoch\s*!==?\s*_decideProposalEpoch", success_half), (
        "success path of decideProposal must guard against stale epoch"
    )
    assert re.search(r"epoch\s*!==?\s*_decideProposalEpoch", error_half), (
        "error path of decideProposal must ALSO guard against stale epoch — "
        "otherwise a stale error response flips a different modal's buttons"
    )


# ----- Static: empty proposals list clears stale content -----


def test_loadskill_proposals_clears_wrap_on_empty():
    """When the proposals list becomes empty after a previous render with
    items, the container must be CLEARED (not just hidden via display:none)
    — otherwise toggling the block visible again would flash the old
    proposals back into view for a frame."""
    src = _src()
    # Locate loadSkillProposals body via brace-matching.
    start = src.index("async function loadSkillProposals(")
    i = src.index("{", start)
    depth = 0
    j = i
    while j < len(src):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                break
        j += 1
    body = src[start:j + 1]

    # The empty branch must wipe `wrap.innerHTML` before/while hiding the
    # block. Accept either `wrap.innerHTML = ""` or a clear() helper.
    empty_branch = re.search(
        r"if\s*\(\s*!visible\.length\s*\)\s*\{(.+?)\}",
        body,
        re.DOTALL,
    )
    assert empty_branch, "could not locate the empty-visible branch"
    inner = empty_branch.group(1)
    assert re.search(r"wrap\.innerHTML\s*=\s*[\"']\s*[\"']", inner) or "wrap.replaceChildren" in inner, (
        "empty-proposals branch must clear `wrap.innerHTML` (not just "
        "toggle block visibility), otherwise stale cards remain in the DOM "
        "and flash back when the block becomes visible again."
    )
