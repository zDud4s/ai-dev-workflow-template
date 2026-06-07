from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_canvas_html_loads_panecore_and_engine():
    html = (ROOT / ".ai/dashboard/app/canvas.html").read_text(encoding="utf-8")

    # The canvas page must load its scripts sibling-relative (it lives under
    # app/, so srcs are "./<file>.js") and in dependency order: the shared
    # engine (core/skills), then PaneCore, then the canvas-specific engine
    # (split-tree, canvas-bus) and finally the boot file canvas.js.
    # pane-helpers.js (the pure render leaves PaneCore depends on) MUST load
    # before pane-core.js — pane-core.js is the isolated canvas renderer and
    # resolves termSetPillState / termRefreshCost / termExportMarkdown /
    # renderBashCommand / termPtyWsUrl etc. as globals from pane-helpers.js.
    order = [
        "core.js",
        "skills.js",
        "pane-helpers.js",
        "pane-core.js",
        "split-tree.js",
        "canvas-bus.js",
        "canvas.js",
    ]
    indices = []
    for name in order:
        # Look for a <script ... src referencing this file. Restrict to the
        # script-src form so a stray mention elsewhere can't satisfy it.
        needle = 'src="./' + name + '"'
        idx = html.find(needle)
        assert idx != -1, f"{name} not loaded via {needle}"
        indices.append(idx)

    assert indices == sorted(indices), (
        "script tags out of order: " + repr(list(zip(order, indices)))
    )
