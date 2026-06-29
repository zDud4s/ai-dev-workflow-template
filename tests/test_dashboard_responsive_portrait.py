from __future__ import annotations

import os

import pytest


URL = "http://localhost:8766/.ai/dashboard/"

pytestmark = pytest.mark.browser


def _sync_playwright():
    if os.name == "nt":
        try:
            from asyncio.windows_utils import pipe
            h1, h2 = pipe(duplex=True, overlapped=(False, True))
            for handle in (h1, h2):
                close = getattr(handle, "Close", None)
                if close is not None:
                    close()
                else:
                    try:
                        os.close(handle)
                    except OSError:
                        pass
        except PermissionError as exc:
            pytest.skip(f"playwright unavailable: {exc}")
    try:
        from playwright.sync_api import Error, sync_playwright
    except ImportError:
        pytest.skip("playwright not installed")
    return Error, sync_playwright


def _page(viewport: dict[str, int]):
    Error, sync_playwright = _sync_playwright()
    try:
        pw = sync_playwright()
        p = pw.__enter__()
    except PermissionError as exc:
        pytest.skip(f"playwright unavailable: {exc}")
    browser = None
    try:
        try:
            browser = p.chromium.launch(headless=True)
        except Error as exc:
            pytest.skip(f"playwright browser unavailable: {exc}")
        page = browser.new_page(viewport=viewport)
        try:
            page.goto(URL, wait_until="domcontentloaded", timeout=15000)
        except Error as exc:
            pytest.skip(f"dashboard unavailable at {URL}: {exc}")
        page.set_viewport_size(viewport)
        page.wait_for_timeout(300)
        return pw, p, browser, page
    except BaseException:
        if browser is not None:
            browser.close()
        pw.__exit__(None, None, None)
        raise


def _close(pw, p, browser) -> None:
    try:
        browser.close()
    finally:
        pw.__exit__(None, None, None)


def _grid_columns(page, selector: str) -> list[str]:
    return page.locator(selector).evaluate(
        "el => getComputedStyle(el).gridTemplateColumns.split(' ').filter(Boolean)"
    )


def test_no_hscroll_portrait_tablet():
    pw, p, browser, page = _page({"width": 1000, "height": 1300})
    try:
        scroll_width = page.evaluate("document.documentElement.scrollWidth")
        assert scroll_width <= 1002
    finally:
        _close(pw, p, browser)


def test_no_hscroll_portrait_phone():
    pw, p, browser, page = _page({"width": 320, "height": 700})
    try:
        scroll_width = page.evaluate("document.documentElement.scrollWidth")
        assert scroll_width <= 322
    finally:
        _close(pw, p, browser)


def test_split_collapses_at_portrait_768():
    pw, p, browser, page = _page({"width": 768, "height": 1024})
    try:
        page.evaluate(
            """() => {
                const el = document.createElement('div');
                el.id = 'split-probe';
                el.className = 'split';
                el.innerHTML = '<div></div><div></div>';
                document.body.appendChild(el);
            }"""
        )
        assert len(_grid_columns(page, "#split-probe")) == 1
    finally:
        _close(pw, p, browser)


def test_toast_centered_below_sidebar_bp():
    pw, p, browser, page = _page({"width": 1000, "height": 1300})
    try:
        page.evaluate(
            """() => {
                const root = document.getElementById('toast-root');
                root.innerHTML = '<div class="toast in"><span class="toast-text">Saved</span></div>';
            }"""
        )
        box = page.locator("#toast-root").bounding_box()
        assert box is not None
        assert abs((box["x"] + (box["width"] / 2)) - 500) <= 2
    finally:
        _close(pw, p, browser)


def test_vh_caps_short_landscape():
    pw, p, browser, page = _page({"width": 900, "height": 480})
    try:
        page.evaluate(
            """() => {
                const host = document.createElement('section');
                host.innerHTML = `
                    <div class="list"></div>
                    <div class="doc"></div>
                    <div class="yaml-tree"></div>
                    <div class="term-pane"><div></div></div>
                    <pre class="log"></pre>
                `;
                document.body.appendChild(host);
            }"""
        )
        caps = page.evaluate(
            """() => {
                const px = selector => getComputedStyle(document.querySelector(selector)).maxHeight;
                return {
                    list: px('.list'),
                    doc: px('.doc'),
                    yaml: px('.yaml-tree'),
                    term: px('.term-pane'),
                    log: px('pre.log'),
                };
            }"""
        )
        px = {key: float(value.removesuffix("px")) for key, value in caps.items()}
        assert px["list"] <= 356
        assert px["doc"] <= 356
        assert px["yaml"] <= 288
        assert px["term"] <= 336
        assert px["log"] <= 264
    finally:
        _close(pw, p, browser)


def test_touch_targets_preserved_portrait():
    pw, p, browser, page = _page({"width": 768, "height": 1024})
    try:
        page.evaluate(
            """() => {
                const button = document.createElement('button');
                button.className = 'term-icon-btn';
                button.textContent = 'x';
                document.body.appendChild(button);
            }"""
        )
        sizes = page.evaluate(
            """() => {
                const nav = document.querySelector('nav button');
                const term = document.querySelector('.term-icon-btn');
                return {
                    nav: parseFloat(getComputedStyle(nav).minHeight),
                    termHeight: parseFloat(getComputedStyle(term).minHeight),
                    termWidth: parseFloat(getComputedStyle(term).minWidth),
                };
            }"""
        )
        assert sizes["nav"] >= 44
        assert sizes["termHeight"] >= 44
        assert sizes["termWidth"] >= 44
    finally:
        _close(pw, p, browser)


def test_focus_outline_preserved_portrait():
    pw, p, browser, page = _page({"width": 768, "height": 1024})
    try:
        page.evaluate(
            """() => {
                const button = document.createElement('button');
                button.id = 'focus-probe';
                button.textContent = 'Focus';
                document.body.prepend(button);
            }"""
        )
        page.keyboard.press("Tab")
        focus = page.evaluate(
            """() => {
                const el = document.activeElement;
                const style = getComputedStyle(el);
                return {
                    id: el.id,
                    outlineStyle: style.outlineStyle,
                    outlineWidth: parseFloat(style.outlineWidth),
                };
            }"""
        )
        assert focus["id"] == "focus-probe"
        assert focus["outlineStyle"] != "none"
        assert focus["outlineWidth"] >= 2
    finally:
        _close(pw, p, browser)
