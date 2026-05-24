from __future__ import annotations

import pytest
import os


URL = "http://localhost:8766/.ai/dashboard/"


def _sync_playwright():
    if os.name == "nt":
        try:
            from asyncio.windows_utils import pipe
            h1, h2 = pipe(duplex=True, overlapped=(False, True))
            # Newer Pythons return raw int handles instead of NamedPipe objects.
            # Both shapes are acceptable as a "playwright is workable" probe;
            # close whichever method exists.
            for h in (h1, h2):
                close = getattr(h, "Close", None)
                if close is None:
                    try:
                        os.close(h)
                    except (OSError, TypeError):
                        pass
                else:
                    close()
        except (PermissionError, OSError, ImportError, AttributeError) as exc:
            pytest.skip(f"playwright unavailable: {exc}")
    try:
        from playwright.sync_api import Error, sync_playwright
    except ImportError:
        pytest.skip("playwright not installed")
    return Error, sync_playwright


def test_no_horizontal_overflow_at_420():
    Error, sync_playwright = _sync_playwright()
    try:
        pw = sync_playwright()
        p = pw.__enter__()
    except PermissionError as exc:
        pytest.skip(f"playwright unavailable: {exc}")
    try:
        try:
            browser = p.chromium.launch(headless=True)
        except Error as exc:
            pytest.skip(f"playwright browser unavailable: {exc}")
        try:
            page = browser.new_page(viewport={"width": 420, "height": 900})
            try:
                page.goto(URL, wait_until="domcontentloaded", timeout=15000)
            except Error as exc:
                pytest.skip(f"dashboard unavailable at {URL}: {exc}")
            page.click('button[data-view="agents"]')
            try:
                page.wait_for_load_state("networkidle", timeout=10000)
            except Error:
                pass
            page.set_viewport_size({"width": 420, "height": 900})
            page.wait_for_timeout(300)
            scroll_width = page.evaluate("document.documentElement.scrollWidth")
            assert scroll_width <= 422
        finally:
            browser.close()
    finally:
        pw.__exit__(None, None, None)
