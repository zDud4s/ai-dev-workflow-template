import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DASHBOARD = ROOT / ".ai" / "dashboard"
APP = DASHBOARD / "app"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _rule_body(selector: str, css: str) -> str:
    match = re.search(rf"(?m)^[ \t]*{re.escape(selector)}\s*\{{", css)
    assert match, f"{selector} rule not found"
    start = match.end() - 1
    depth = 0
    for idx in range(start, len(css)):
        if css[idx] == "{":
            depth += 1
        elif css[idx] == "}":
            depth -= 1
            if depth == 0:
                return css[start + 1 : idx]
    raise AssertionError(f"unbalanced braces in {selector} rule")


def test_scanline_overlay_does_not_use_blend_mode_and_main_scrolls_smoothly():
    css = _text(DASHBOARD / "styles.css")

    body_after = _rule_body("body::after", css)
    assert "mix-blend-mode" not in body_after

    main = _rule_body("main", css)
    assert "scroll-behavior: smooth" in main


def test_pipelines_js_has_no_nul_bytes():
    data = (APP / "pipelines.js").read_bytes()
    assert b"\x00" not in data


def test_core_nav_click_updates_aria_selected():
    core_js = _text(APP / "core.js")
    assert "aria-selected" in core_js


def test_events_autorefresh_interval_is_view_gated():
    jobs_js = _text(APP / "jobs.js")
    assert "view-events" in jobs_js
    assert re.search(
        r"setInterval\(function\s*\(\)\s*\{[^}]*view-events[^}]*loadEvents\(\)",
        jobs_js,
        re.DOTALL,
    ), "events auto-refresh interval should guard on the active Events view"


def test_idle_timers_skip_background_tabs():
    analytics_js = _text(APP / "analytics.js")
    start_idx = analytics_js.find("function startAutoRefresh")
    assert start_idx != -1
    start_region = analytics_js[start_idx : start_idx + 400]
    assert "document.hidden" in start_region

    canvas_js = _text(APP / "canvas.js")
    heartbeat_idx = canvas_js.find("_canvasHeartbeat = setInterval")
    assert heartbeat_idx != -1
    heartbeat_region = canvas_js[heartbeat_idx : heartbeat_idx + 400]
    assert "document.hidden" in heartbeat_region
