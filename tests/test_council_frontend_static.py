"""Static checks for the Council dashboard frontend.

Mirrors tests/test_todos_frontend_static.py — asserts against the source text
of index.html / styles.css (and later app/council.js) without a browser.
"""
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
INDEX_HTML = ROOT / ".ai" / "dashboard" / "index.html"
STYLES_CSS = ROOT / ".ai" / "dashboard" / "styles.css"
COUNCIL_JS = ROOT / ".ai" / "dashboard" / "app" / "council.js"


# --- Markup (Task 3.1) -----------------------------------------------------

def test_council_tab_registered_in_runtime_section():
    src = INDEX_HTML.read_text(encoding="utf-8")
    runtime_label = src.index('class="section-label" role="presentation">runtime<')
    tab_council = src.index('id="tab-council"')
    settings_label = src.index('class="section-label" role="presentation">settings<')
    # Council tab lives in the runtime nav group, before the settings group.
    assert runtime_label < tab_council < settings_label
    assert 'data-view="council"' in src


def test_council_view_section_has_three_stages_and_editor():
    src = INDEX_HTML.read_text(encoding="utf-8")
    assert 'id="view-council"' in src
    # Seat editor + question form + three stage containers + history list.
    for anchor in (
        'id="council-seats"',
        'id="council-question"',
        'id="council-run"',
        'id="council-stage1"',
        'id="council-stage2"',
        'id="council-stage3"',
        'id="council-history"',
    ):
        assert anchor in src, anchor


def test_council_script_included():
    src = INDEX_HTML.read_text(encoding="utf-8")
    assert re.search(r'<script[^>]+src="app/council\.js"', src)


# --- council.js (Task 3.2) -------------------------------------------------

def test_council_js_fetches_config_and_posts_runs():
    src = COUNCIL_JS.read_text(encoding="utf-8")
    assert "/api/council/config" in src
    assert "/api/council/runs" in src


def test_council_js_streams_via_eventsource():
    src = COUNCIL_JS.read_text(encoding="utf-8")
    assert "EventSource" in src
    assert "/stream" in src


def test_council_js_sanitizes_model_output():
    src = COUNCIL_JS.read_text(encoding="utf-8")
    assert "DOMPurify.sanitize" in src
    lines = src.splitlines()
    for match in re.finditer(r"\binnerHTML\s*=", src):
        line_no = src.count("\n", 0, match.start())
        context = "\n".join(lines[max(0, line_no - 3):line_no + 1])
        assert "DOMPurify.sanitize" in context, f"unsanitized innerHTML near line {line_no + 1}"


def test_council_js_registers_view_and_exports_loader():
    src = COUNCIL_JS.read_text(encoding="utf-8")
    assert 'data-view="council"' in src
    assert "function initCouncil" in src
    assert "window.loadCouncil" in src
    assert "DOMContentLoaded" in src


def test_council_js_agent_seats_force_claude_model():
    # Agent personas can only run on claude models (codex has no --agent), so
    # the editor must guard against an agent seat on a codex model.
    src = COUNCIL_JS.read_text(encoding="utf-8")
    assert "claude" in src and "codex" in src


# --- Styles (Task 3.3) -----------------------------------------------------

def test_council_styles_exist_and_use_tokens():
    src = STYLES_CSS.read_text(encoding="utf-8")
    assert ".council-" in src
    # Theming convention: council rules must not hardcode hex colors — pull a
    # window around each .council- selector block and forbid #rrggbb there.
    for m in re.finditer(r"\.council-[\w-]*", src):
        block = src[m.start():m.start() + 400]
        assert not re.search(r"#[0-9a-fA-F]{6}\b", block), src[m.start():m.start() + 80]


def test_council_partial_is_linked_in_index():
    # styles.css is the unlinked source of record; the browser only loads the
    # styles/*.css partials. The council section must be extracted into its own
    # partial AND linked, or the Council view renders with zero styling.
    partial = ROOT / ".ai" / "dashboard" / "styles" / "council.css"
    assert partial.exists(), "styles/council.css partial is missing"
    assert ".council" in partial.read_text(encoding="utf-8")
    src = INDEX_HTML.read_text(encoding="utf-8")
    assert re.search(r'<link[^>]+href="styles/council\.css"', src), \
        "styles/council.css is not linked in index.html"


def test_every_style_partial_is_linked_in_index():
    # Regression guard for the orphaned-partial class of bug: any file dropped
    # into styles/ must be wired into index.html's cascade, else its rules
    # silently never load.
    styles_dir = ROOT / ".ai" / "dashboard" / "styles"
    src = INDEX_HTML.read_text(encoding="utf-8")
    linked = set(re.findall(r'href="styles/([\w-]+\.css)"', src))
    for partial in sorted(styles_dir.glob("*.css")):
        assert partial.name in linked, f"{partial.name} exists but is not linked in index.html"
