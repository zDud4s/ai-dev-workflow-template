"""Batch 8 (final) regression guards for the dashboard's CSS / HTML /
pty_session.py LOW + PERF residuals.

Scope per ``docs/bug-hunt-status.md`` "LOW abertos" / "PERF abertos":

  * ``styles.css``  — hardcoded ``#111`` removed from ``.tl-bar.pending``;
    unified ``var(--token, fallback)`` pairs back to bare ``var(--token)``
    (the fallbacks are dead since every token is defined in ``:root``);
    stale ``--glass-1`` comment corrected.
  * ``index.html`` — proposal / detail modals now carry the WAI-ARIA dialog
    trio (``role="dialog"`` + ``aria-modal="true"`` + ``aria-labelledby``),
    and modal-close ``<button>`` elements expose an ``aria-label`` so the
    text content ``"close"`` isn't the only handle screen readers get.
  * ``pty_session.py`` — three classes of LOW: (1) dead in-function
    ``import`` aliases of names already imported at module top
    (``struct as _struct`` ×2, ``time as _time`` ×2 + unused-in-kill ``time``),
    (2) magic-number timeouts (bare ``0.5`` / ``0.05``) extracted into the
    module-level constants ``REAP_TIMEOUT_S`` / ``REAP_POLL_INTERVAL_S`` /
    ``EOF_REAP_TIMEOUT_S``, and (3) the kill() docstring/comment now states
    the rationale (no more orphan import).

Pre-existing test debt (e.g. ``test_styles_index_batch6_dedupe_inline.py``
failures stemming from the Gemini revert ``9072946``) is NOT addressed
here — out of batch 8 scope.
"""
from __future__ import annotations

import ast
import inspect
import pathlib
import re
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
DASH = ROOT / ".ai" / "dashboard"
STYLES_CSS = DASH / "styles.css"
INDEX_HTML = DASH / "index.html"
PTY_PY = DASH / "scripts" / "pty_session.py"

# Insert .ai/dashboard/scripts on sys.path so we can ``import pty_session``
# directly (matches the pattern used by every other ``test_pty_*.py``).
sys.path.insert(0, str(DASH / "scripts"))
import pty_session as _pty  # noqa: E402  (path-injection requires this order)


def _css() -> str:
    return STYLES_CSS.read_text(encoding="utf-8")


def _html() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


def _pty_src() -> str:
    return PTY_PY.read_text(encoding="utf-8")


def _strip_css_comments(src: str) -> str:
    """Drop ``/* ... */`` comments so "no X" assertions don't false-positive
    on documentation that explains why X is intentionally absent."""
    return re.sub(r"/\*.*?\*/", "", src, flags=re.DOTALL)


def _strip_py_comments_and_docstrings(src: str) -> str:
    """Drop ``# ...`` comments AND triple-quoted strings so "no import X"
    assertions don't false-positive on the comment / docstring that explains
    why the import was removed in the first place.
    """
    # Drop triple-quoted strings (greedy non-overlapping).
    no_triples = re.sub(
        r'(?s)"""[^"]*"""|\'\'\'[^\']*\'\'\'',
        "",
        src,
    )
    # Drop `# comment` to end-of-line.
    return re.sub(r"#[^\n]*", "", no_triples)


# ----------------------------------------------------------------------
# styles.css — LOW fixes
# ----------------------------------------------------------------------
def test_tl_bar_pending_uses_token_not_hardcoded_111() -> None:
    """The last hardcoded ``#111`` on ``.tl-bar.pending`` must be gone.

    Previously: ``color: #111;`` against a saturated ``--warn`` background
    drifts WCAG when the warn token is retuned; bind to ``--on-accent`` so
    the contrast tracks the token system.
    """
    css = _css()
    pattern = re.compile(
        r"#view-timeline\s+\.tl-bar\.pending\s*\{([^}]*)\}"
    )
    match = pattern.search(css)
    assert match, ".tl-bar.pending rule missing from styles.css"
    body = match.group(1)
    # No hardcoded #111 anywhere in the body.
    assert "#111" not in body, (
        ".tl-bar.pending must not hardcode #111 — bind to a CSS variable"
    )
    # And the color SHOULD be a var() reference now.
    assert re.search(r"color\s*:\s*var\(--[a-z-]+\)", body), (
        ".tl-bar.pending color must resolve via a CSS variable"
    )


def test_no_hardcoded_111_in_styles() -> None:
    """Defensive sweep — ``#111`` is the documented LOW; if it reappears
    anywhere outside the CSS-mask placeholder block, fail loudly.
    """
    css_no_comments = _strip_css_comments(_css())
    # Allow the literal #000 used by the SVG mask trick — it's a renderer
    # protocol, not a colour choice.
    bad = re.findall(r"#11[1-9]\b", css_no_comments)
    assert not bad, "unexpected #11x hardcoded color survived: " + repr(bad)


def test_var_fallbacks_unified_to_bare_form() -> None:
    """Every token consumed in styles.css must be unconditionally defined in
    ``:root`` — so a ``var(--token, fallback)`` pair only adds dead-code
    weight and is inconsistent with the other 130+ bare ``var(--token)``
    consumers. Batch 8 unifies the form.
    """
    css_no_comments = _strip_css_comments(_css())
    # Find every var(--…) reference and assert it has no fallback.
    refs = re.findall(r"var\(--[a-z0-9-]+(?:,[^)]+)?\)", css_no_comments)
    assert refs, "expected at least one var() reference"
    fallbacks = [r for r in refs if "," in r]
    assert not fallbacks, (
        "var() fallbacks should be unified to bare form — found: "
        + ", ".join(sorted(set(fallbacks)))
    )


def test_glass_1_comment_names_real_consumer_not_only_dead_one() -> None:
    """The ``--glass-1`` declaration comment used to claim the alias was
    consumed by ``.scroll-fade`` — a selector that no longer exists. Batch
    8 corrects the comment to point to the real consumer (``nav button:hover``)
    while preserving the historical note about ``.scroll-fade`` being the
    old consumer (for future readers tracing the lineage).
    """
    css = _css()
    # Locate the --glass-1 declaration block (the comment immediately
    # above it).
    region = re.search(
        r"/\*[^*]*--glass-1[^*]*?\*/\s*--glass-1\s*:",
        css,
        flags=re.DOTALL,
    )
    assert region, "--glass-1 declaration block not found"
    comment = region.group(0).lower()
    # The new comment must name the real consumer.
    assert "nav button" in comment, (
        "--glass-1 comment should reference its actual consumer (nav button:hover)"
    )
    # And it must NOT claim that .scroll-fade is the *current* consumer.
    # (The old phrasing "still consumed by .scroll-fade" was misleading.)
    assert "still consumed by .scroll-fade" not in comment, (
        "--glass-1 comment must not claim .scroll-fade is the current consumer"
    )


def test_glass_1_token_still_consumed_by_nav_button_hover() -> None:
    """Regression guard for the LOW fix: keep the consumer alive.

    Even though the comment was wrong, removing the variable would have
    broken the nav-hover affordance. Make sure the var stays declared AND
    the consumer keeps reading it.
    """
    css = _css()
    assert re.search(r"--glass-1\s*:", css), "--glass-1 must remain declared"
    # The consumer at nav button:hover must still read var(--glass-1).
    nav_hover = re.search(
        r"nav\s+button:hover\s*\{([^}]*)\}", css, flags=re.DOTALL
    )
    assert nav_hover, "nav button:hover rule missing"
    assert "var(--glass-1)" in nav_hover.group(1), (
        "nav button:hover must keep reading var(--glass-1)"
    )


# ----------------------------------------------------------------------
# index.html — modal a11y
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "modal_id,title_id",
    [
        ("proposal-modal", "proposal-modal-title"),
        ("skill-detail-modal", "skill-detail-title"),
        ("agent-detail-modal", "agent-detail-title"),
        ("agent-proposal-modal", "agent-proposal-title"),
    ],
)
def test_proposal_modals_have_dialog_aria(modal_id: str, title_id: str) -> None:
    """Each ``.proposal-modal`` overlay must declare the WAI-ARIA dialog
    trio so assistive tech announces it as a modal, traps focus, and reads
    the heading as the dialog name. Previously every modal was a bare ``div``
    with ``hidden`` — invisible to screen readers as a dialog.
    """
    html = _html()
    pattern = (
        r'<div[^>]*\bid="' + re.escape(modal_id) + r'"[^>]*>'
    )
    match = re.search(pattern, html)
    assert match, "modal element missing: #" + modal_id
    tag = match.group(0)
    assert 'role="dialog"' in tag, (
        f"#{modal_id} must declare role=\"dialog\" (got: {tag})"
    )
    assert 'aria-modal="true"' in tag, (
        f"#{modal_id} must declare aria-modal=\"true\""
    )
    assert (
        f'aria-labelledby="{title_id}"' in tag
    ), f"#{modal_id} must reference its title via aria-labelledby"


@pytest.mark.parametrize(
    "close_id",
    [
        "proposal-modal-close",
        "skill-detail-close",
        "agent-detail-close",
        "agent-proposal-close",
    ],
)
def test_modal_close_buttons_have_aria_label(close_id: str) -> None:
    """Every modal-close ``<button>`` must carry an explicit ``aria-label``.

    The visible text ``close`` is lowercase and identical across four
    modals — without aria-label, an AT user navigating "by button" hears
    four identical "close" entries with no way to disambiguate.
    """
    html = _html()
    pattern = re.compile(
        r'<button[^>]*\bid="' + re.escape(close_id) + r'"[^>]*>'
    )
    match = pattern.search(html)
    assert match, "close button missing: #" + close_id
    tag = match.group(0)
    assert re.search(r'aria-label="[^"]+"', tag), (
        f"#{close_id} must carry an aria-label (got: {tag})"
    )


# ----------------------------------------------------------------------
# pty_session.py — LOW fixes
# ----------------------------------------------------------------------
def test_reap_constants_are_module_level() -> None:
    """The three reap timing constants must be importable from the module
    namespace — that's the whole point of pulling them out of the bare
    magic-number form.
    """
    assert hasattr(_pty, "REAP_TIMEOUT_S"), (
        "pty_session must expose REAP_TIMEOUT_S as a module-level constant"
    )
    assert hasattr(_pty, "REAP_POLL_INTERVAL_S"), (
        "pty_session must expose REAP_POLL_INTERVAL_S"
    )
    assert hasattr(_pty, "EOF_REAP_TIMEOUT_S"), (
        "pty_session must expose EOF_REAP_TIMEOUT_S"
    )
    # Sanity bounds — the previous bare 0.5 / 0.05 values must be preserved
    # (this is a refactor, not a behaviour change).
    assert _pty.REAP_TIMEOUT_S == 0.5
    assert _pty.REAP_POLL_INTERVAL_S == 0.05
    assert _pty.EOF_REAP_TIMEOUT_S == 0.05
    # And the EOF reap should be no longer than the regular reap budget.
    assert _pty.EOF_REAP_TIMEOUT_S <= _pty.REAP_TIMEOUT_S


def test_posix_kill_uses_named_constant_not_magic_number() -> None:
    """``_PosixPty.kill`` must call ``_reap_with_timeout`` with the named
    constant — not a bare ``0.5`` literal. Otherwise tuning the timeout
    requires editing two unrelated lines (one for SIGTERM, one for SIGKILL).
    """
    src = inspect.getsource(_pty._PosixPty.kill)
    code_only = _strip_py_comments_and_docstrings(src)
    assert "REAP_TIMEOUT_S" in code_only, (
        "_PosixPty.kill must reference REAP_TIMEOUT_S — not a magic number"
    )
    # And the bare 0.5 literal must be gone from real code.
    assert "0.5" not in code_only, (
        "_PosixPty.kill must not still carry a bare 0.5 literal"
    )


def test_posix_reap_uses_named_poll_interval() -> None:
    """``_reap_with_timeout`` must sleep via ``REAP_POLL_INTERVAL_S`` —
    the bare ``time.sleep(0.05)`` should be replaced.
    """
    src = inspect.getsource(_pty._PosixPty._reap_with_timeout)
    code_only = _strip_py_comments_and_docstrings(src)
    assert "REAP_POLL_INTERVAL_S" in code_only, (
        "_reap_with_timeout must reference REAP_POLL_INTERVAL_S"
    )
    # The dead per-call `import time as _time` must also be gone.
    assert "import time" not in code_only, (
        "_reap_with_timeout must NOT re-import time — module-level "
        "_time_mod is already available"
    )
    # And the sleep is now via _time_mod, not _time.
    assert "_time_mod.sleep" in code_only, (
        "_reap_with_timeout must sleep via _time_mod (module-level handle)"
    )


def test_posix_read_eof_uses_named_constant() -> None:
    """The EOF-triggered reap inside ``_PosixPty.read`` must call
    ``_reap_with_timeout(EOF_REAP_TIMEOUT_S)`` — the previous bare ``0.05``
    obscured that this was deliberately shorter than the kill-path budget.
    """
    src = inspect.getsource(_pty._PosixPty.read)
    code_only = _strip_py_comments_and_docstrings(src)
    assert "EOF_REAP_TIMEOUT_S" in code_only, (
        "_PosixPty.read must reference EOF_REAP_TIMEOUT_S for the EOF reap"
    )


def test_posix_kill_dropped_unused_time_import() -> None:
    """The previous ``import time as _time`` inside ``kill()`` was a dead
    branch (nothing in the method body uses ``_time``). Batch 8 removed it.
    """
    src = inspect.getsource(_pty._PosixPty.kill)
    code_only = _strip_py_comments_and_docstrings(src)
    # The only stdlib import that belongs in kill() is signal.
    assert "import signal" in code_only, "kill() must import signal"
    # And there must be no `import time` line lurking — that was the dead one.
    assert not re.search(r"^\s*import\s+time\b", code_only, flags=re.MULTILINE), (
        "kill() must not import time (the previous _time alias was unused)"
    )


def test_posix_init_dropped_redundant_struct_alias() -> None:
    """``_PosixPty.__init__`` previously imported ``struct as _struct`` even
    though ``struct`` is already at module top (consumed by the
    ``except (OSError, struct.error)`` line in the same method). Batch 8
    drops the dead alias and uses the module-level name directly.
    """
    src = inspect.getsource(_pty._PosixPty.__init__)
    code_only = _strip_py_comments_and_docstrings(src)
    assert not re.search(r"\bimport\s+struct\b", code_only), (
        "_PosixPty.__init__ must not re-import struct (in real code lines)"
    )
    # And the body must call struct.pack via the module-level name.
    assert "struct.pack(" in code_only, (
        "_PosixPty.__init__ must call struct.pack via the module-level "
        "name"
    )


def test_posix_resize_dropped_redundant_struct_alias() -> None:
    """Same dead-alias removal in ``_PosixPty.resize`` — module-level
    ``struct`` covers the call AND the exception handler in one symbol.
    """
    src = inspect.getsource(_pty._PosixPty.resize)
    code_only = _strip_py_comments_and_docstrings(src)
    assert not re.search(r"\bimport\s+struct\b", code_only), (
        "_PosixPty.resize must not re-import struct (in real code lines)"
    )
    assert "struct.pack(" in code_only, (
        "_PosixPty.resize must call struct.pack via the module-level name"
    )


def test_pty_module_top_imports_remain_minimal() -> None:
    """Belt-and-braces: parse the module AST and confirm the top-level
    imports are exactly the cross-platform stdlib set (POSIX-only stuff
    like ``fcntl``/``termios``/``pty``/``signal`` MUST still live inside
    the methods so the module remains importable on Windows hosts).
    """
    tree = ast.parse(_pty_src())
    top_imports = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            for n in node.names:
                top_imports.append(n.name)
        elif isinstance(node, ast.ImportFrom):
            top_imports.append(node.module or "")
    # Cross-platform names that MUST be at the top.
    for required in ("codecs", "errno", "logging", "os",
                     "shutil", "struct", "sys", "threading", "time", "abc"):
        assert required in top_imports, (
            f"expected `{required}` to be imported at module top "
            f"(got {top_imports})"
        )
    # POSIX-only modules MUST stay out of the module-top imports — they
    # would otherwise break ``import pty_session`` on Windows entirely.
    for posix_only in ("fcntl", "termios", "pty"):
        assert posix_only not in top_imports, (
            f"{posix_only} must NOT be imported at module top (POSIX-only)"
        )


def test_reap_timing_constants_documented() -> None:
    """The named-constant block must carry a comment block explaining each
    name — otherwise the future reader needs to git-blame to learn what
    ``REAP_POLL_INTERVAL_S`` means.
    """
    src = _pty_src()
    # Locate the constant block.
    block = re.search(
        r"REAP_TIMEOUT_S\s*=.*?EOF_REAP_TIMEOUT_S\s*=",
        src,
        flags=re.DOTALL,
    )
    assert block, "named timing constants are missing or out of order"
    # And the documentation block immediately above must reference each name.
    preceding = src[max(0, block.start() - 800): block.start()]
    for name in ("REAP_TIMEOUT_S", "REAP_POLL_INTERVAL_S", "EOF_REAP_TIMEOUT_S"):
        assert name in preceding, (
            f"timing-constants doc block must explain {name}"
        )
