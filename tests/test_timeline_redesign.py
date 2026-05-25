import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JOBS_JS = ROOT / ".ai" / "dashboard" / "app" / "jobs.js"
INDEX_HTML = ROOT / ".ai" / "dashboard" / "index.html"
STYLES_CSS = ROOT / ".ai" / "dashboard" / "styles.css"

JS = JOBS_JS.read_text(encoding="utf-8")
HTML = INDEX_HTML.read_text(encoding="utf-8")
CSS = STYLES_CSS.read_text(encoding="utf-8")


def test_ac1_global_axis_markup_and_sticky_css():
    assert "tl-axis" in HTML
    assert re.search(r"#view-timeline\s+\.tl-axis\s*{[^}]*position:\s*sticky", CSS, re.S)


def test_ac2_filters_and_persisted_state_exist():
    assert "tl-toolbar" in HTML
    assert "dashboard.timeline.state.v1" in JS
    assert "localStorage" in JS
    assert 'dataset.wired === "1"' in JS


def test_ac3_kpi_markup_and_renderer_exist():
    assert "tl-kpi" in HTML
    assert "tl-kpi-sessions" in HTML
    assert "function _tlRenderKpi" in JS


def test_ac4_sparkline_markup_and_renderer_exist():
    assert "tl-sparkline" in HTML
    assert "function _tlRenderSparkline" in JS
    assert "bucketCount = 24" in JS


def test_ac5_phase_strip_and_untagged_label_exist():
    assert "tl-phase-strip" in JS
    assert "untagged" in JS
    assert "+${untagged} untagged" in JS


def test_ac6_narrow_bars_and_old_legend_removed():
    assert '[data-narrow="1"]' in CSS
    assert 'data-narrow="1"' in JS
    assert "tl-legend" not in HTML


def test_ac7_caveat_is_visible_without_hint_details():
    assert "tl-caveat" in HTML
    assert "tl-hint-details" not in HTML


def test_ac8_bar_animation_is_gated_after_mount():
    assert re.search(r"#view-timeline\[data-tl-mounted=\"1\"\]\s+\.tl-bar\s*{[^}]*animation:\s*none", CSS, re.S)
    assert "dataset.tlMounted" in JS


def test_ac9_copy_zebra_and_responsive_label_column_exist():
    assert "tl-row-copy" in CSS
    assert "tl-row-copy" in JS
    assert "nth-child(even)" in CSS
    assert "minmax(240px" in CSS
