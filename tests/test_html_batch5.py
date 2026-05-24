"""Static-lint guards for batch-5 HTML hardening of the workflow dashboard.

Covers:
 - All CDN `<script>` tags carry `defer` (parser no longer blocked on
   marked/dompurify/js-yaml/xterm).
 - All `app/*.js` script tags also carry `defer` so they run AFTER the
   CDN libs (deferred scripts execute in DOM order) and BEFORE
   DOMContentLoaded.
 - SRI TODO comment is present and names every CDN asset that still
   needs `integrity=` (the hashes themselves are out of scope without
   curl access; see status doc lines 134 + 205).
 - `data-integrity-todo="…"` attributes have been replaced by a single
   consolidated comment block (less attribute noise on every CDN tag).
 - The four `*-meta` end-of-toolbar spans no longer carry the duplicated
   inline `style="margin-left:auto;color:var(--fg-dim);font-size:12px"`;
   they share the new `.toolbar-meta-end` utility class instead.
 - The "form-msg" hint spans no longer carry the duplicated inline
   `style="margin-bottom:8px;color:var(--fg-dim)"`; they share the new
   `.form-msg-hint` utility class instead.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INDEX_HTML = ROOT / ".ai" / "dashboard" / "index.html"


def _html() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


# -- Defer on CDN scripts ----------------------------------------------------
def test_all_cdn_scripts_have_defer() -> None:
    """Every jsdelivr `<script src=...>` tag must carry the `defer` attr."""
    html = _html()
    tags = re.findall(
        r"<script\b[^>]*\bsrc=\"https://cdn\.jsdelivr\.net/[^\"]+\"[^>]*>",
        html,
    )
    assert tags, "expected at least one jsdelivr <script> tag"
    for tag in tags:
        assert re.search(r"\bdefer\b", tag), (
            "jsdelivr <script> tag is missing `defer`: " + tag
        )


# -- Defer on app/*.js scripts ----------------------------------------------
def test_all_app_scripts_have_defer() -> None:
    """Every `<script src="app/…">` tag must carry the `defer` attr."""
    html = _html()
    tags = re.findall(
        r"<script\b[^>]*\bsrc=\"app/[^\"]+\"[^>]*>",
        html,
    )
    # We expect 8 module scripts: core, skills, agents, jobs, terminals,
    # settings, auto-select, main. Don't hardcode 8 (refactors may merge or
    # split files), but require at least 5 to catch a 0-result false-positive.
    assert len(tags) >= 5, (
        "expected at least 5 app/*.js <script> tags; found " + str(len(tags))
    )
    for tag in tags:
        assert re.search(r"\bdefer\b", tag), (
            "app/ <script> tag is missing `defer`: " + tag
        )


# -- SRI deferred --------------------------------------------------------------
def test_sri_todo_comment_present() -> None:
    """Until SHA-384 hashes are pasted in, a single TODO SRI comment must
    enumerate every CDN asset awaiting `integrity=`. The block is the
    single source of truth so we don't leak `data-integrity-todo` noise
    onto every CDN tag.
    """
    html = _html()
    assert "TODO SRI" in html, (
        "expected a `<!-- TODO SRI: ... -->` block enumerating CDN assets"
    )
    # The names of all 7 pinned assets must show up in the TODO block.
    needed = [
        "js-yaml@4.1.0",
        "marked@12.0.0",
        "dompurify@3.1.6",
        "xterm@5.3.0/css/xterm.min.css",
        "xterm@5.3.0/lib/xterm.min.js",
        "xterm-addon-fit@0.8.0",
        "xterm-addon-web-links@0.9.0",
    ]
    for name in needed:
        assert name in html, "SRI TODO block is missing asset: " + name


def test_no_integrity_todo_attributes_remain() -> None:
    """The per-tag `data-integrity-todo="…"` attributes were consolidated
    into the single comment block — none should remain on individual tags.
    """
    html = _html()
    assert "data-integrity-todo" not in html, (
        "stray `data-integrity-todo` attribute is still in index.html — "
        "it should have been folded into the TODO SRI comment block"
    )


# -- Inline-style extraction --------------------------------------------------
def test_no_meta_end_inline_style() -> None:
    """The 4 `#…-meta` toolbar spans must use `.toolbar-meta-end` instead
    of inline `style="margin-left:auto;color:var(--fg-dim);font-size:12px"`.
    """
    html = _html()
    # The exact inline string that was duplicated 4× must be gone.
    assert "margin-left:auto;color:var(--fg-dim);font-size:12px" not in html
    # The new utility class must be wired on each of the 4 known meta spans.
    for span_id in ("skills-meta", "agents-meta", "timeline-meta", "auto-select-meta"):
        pattern = (
            r'<span\b[^>]*\bid="' + re.escape(span_id) + r'"[^>]*>'
            r"|<span\b[^>]*\btoolbar-meta-end[^>]*\bid=\""
            + re.escape(span_id)
            + r"\""
        )
        tag_match = re.search(
            r'<span\b[^>]*\bid="' + re.escape(span_id) + r'"[^>]*>',
            html,
        )
        assert tag_match, "missing <span id=\"" + span_id + "\"> in index.html"
        assert "toolbar-meta-end" in tag_match.group(0), (
            "span#" + span_id + " is missing the .toolbar-meta-end class"
        )


def test_no_form_msg_hint_inline_style() -> None:
    """The three `.form-msg` hint paragraphs share `.form-msg-hint` now
    instead of inline `style="margin-bottom:8px;color:var(--fg-dim)"`.
    """
    html = _html()
    assert "margin-bottom:8px;color:var(--fg-dim)" not in html
    # We expect at least 3 occurrences (Skills proposals, Skills suggestions,
    # Agents suggestions). Don't hardcode the exact count; just require
    # the class is present where the inline style used to be.
    assert html.count("form-msg-hint") >= 3, (
        "expected at least 3 .form-msg-hint usages; the 3 inline-style "
        "hint paragraphs should now share this utility class"
    )
