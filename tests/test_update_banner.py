"""Smoke test for the dashboard "Update available" banner.

Server is assumed to already be running at http://127.0.0.1:8765.
Runs three checks:
  1. Banner appears when `has_updates` is true (clean localStorage).
  2. "View update" button switches to the Settings tab.
  3. "Dismiss" button stores the upstream sha and hides the banner; reload
     keeps it hidden for the same sha.
"""

import sys
from playwright.sync_api import sync_playwright

URL = "http://127.0.0.1:8765/.ai/dashboard/index.html"


def fail(msg: str) -> None:
    print("FAIL:", msg)
    sys.exit(1)


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context()
        page = ctx.new_page()

        # 1. clean slate then load dashboard
        page.goto(URL)
        page.wait_for_load_state("domcontentloaded")
        page.evaluate("() => { try { localStorage.removeItem('dash.updateCheck'); localStorage.removeItem('dash.updateDismissedSha'); } catch (_) {} }")
        page.reload()
        page.wait_for_load_state("networkidle")

        # 2. banner should be present (check returns has_updates=true in this repo)
        banner = page.locator("#update-banner")
        try:
            banner.wait_for(state="visible", timeout=30000)
        except Exception:
            page.screenshot(path="tests/banner_missing.png", full_page=True)
            fail("banner never appeared — see tests/banner_missing.png")

        title = banner.locator(".update-banner-title").inner_text()
        meta = banner.locator(".update-banner-meta").inner_text()
        print("banner shown:", title, "|", meta)
        if "New workflow version available" not in title:
            fail(f"unexpected title: {title!r}")
        if "upstream" not in meta or "installed" not in meta:
            fail(f"unexpected meta: {meta!r}")
        page.screenshot(path="tests/banner_visible.png", full_page=True)

        # 3. clicking "View update" should switch to settings tab
        banner.locator(".update-banner-action").click()
        page.wait_for_timeout(400)
        active_view = page.evaluate("() => document.querySelector('.view.active')?.id")
        if active_view != "view-settings":
            fail(f"View update did not switch to settings (got {active_view!r})")
        print("View update: switched to", active_view)

        # 4. reload, dismiss, then reload again — banner should stay hidden
        page.evaluate("() => { try { localStorage.removeItem('dash.updateCheck'); localStorage.removeItem('dash.updateDismissedSha'); } catch (_) {} }")
        page.reload()
        page.wait_for_load_state("networkidle")
        banner.wait_for(state="visible", timeout=30000)
        banner.locator(".update-banner-close").click()
        page.wait_for_timeout(400)
        if page.locator("#update-banner").count() != 0:
            fail("dismiss did not remove banner")
        dismissed_sha = page.evaluate("() => localStorage.getItem('dash.updateDismissedSha')")
        if not dismissed_sha:
            fail("dismiss did not persist the upstream sha")
        print("dismissed sha:", dismissed_sha[:12])

        # 5. reload should not bring banner back for same sha
        page.reload()
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(1500)
        if page.locator("#update-banner").count() != 0:
            fail("banner reappeared after dismiss for same sha")
        print("dismiss persists across reload OK")

        browser.close()

    print("ALL CHECKS PASSED")


if __name__ == "__main__":
    main()
