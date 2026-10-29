"""Playwright + axe-core: load a page, optionally apply patches, and run the checker."""
import asyncio
import base64
import logging
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import Browser, Error as PlaywrightError, Route, async_playwright

from . import security
from .patches import APPLY_JS, MARK_JS, RUN_AXE_JS

log = logging.getLogger("accessfix.scanner")

AXE_SOURCE = (Path(__file__).parent / "vendor" / "axe.min.js").read_text()
AXE_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"]
AXE_OPTIONS = {
    "runOnly": {"type": "tag", "values": AXE_TAGS},
    "resultTypes": ["violations"],
    "elementRef": True,
    "frameWaitTime": 5000,
}
VIEWPORT = {"width": 1280, "height": 800}


class ScanError(RuntimeError):
    pass


class Scanner:
    """One shared Chromium; every scan gets a fresh context."""

    def __init__(self):
        self._pw = None
        self._browser: Browser | None = None
        self._lock = asyncio.Lock()

    async def start(self):
        async with self._lock:
            if self._browser and self._browser.is_connected():
                return
            if not self._pw:
                self._pw = await async_playwright().start()
            self._browser = await self._pw.chromium.launch(args=["--disable-dev-shm-usage"])

    async def stop(self):
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()
        self._browser = self._pw = None

    async def _guard(self, route: Route):
        url = route.request.url
        try:
            if urlsplit(url).scheme in ("http", "https") and not await security.url_allowed(url):
                log.warning("blocked request to %s", url)
                await route.abort("blockedbyclient")
            else:
                await route.continue_()
        except PlaywrightError:
            pass  # page closed mid-request

    async def scan(self, url: str, marks: list[dict] | None = None, patches: list[dict] | None = None,
                   screenshot: bool = True, timeout: float = 75) -> dict:
        try:
            return await asyncio.wait_for(self._scan(url, marks or [], patches or [], screenshot), timeout)
        except asyncio.TimeoutError:
            raise ScanError(f"Scanning {url} took longer than {int(timeout)}s")

    async def _scan(self, url, marks, patches, screenshot) -> dict:
        if not (self._browser and self._browser.is_connected()):
            await self.start()
        context = await self._browser.new_context(bypass_csp=True, viewport=VIEWPORT)
        try:
            page = await context.new_page()
            await page.route("**/*", self._guard)
            try:
                await page.goto(url, wait_until="networkidle", timeout=20000)
            except PlaywrightError:
                try:
                    await page.goto(url, wait_until="load", timeout=30000)
                except PlaywrightError as e:
                    raise ScanError(f"Couldn't load {url}: {e.message.splitlines()[0]}")

            for frame in page.frames:
                try:
                    await frame.add_script_tag(content=AXE_SOURCE)
                except PlaywrightError:
                    if frame == page.main_frame:
                        raise ScanError("Couldn't inject axe-core into the page")

            marked = await page.evaluate(MARK_JS, marks) if marks else []
            applied = await page.evaluate(APPLY_JS, patches) if patches else []
            violations = await page.evaluate(RUN_AXE_JS, AXE_OPTIONS)

            shot = None
            if screenshot:
                await page.evaluate("window.scrollTo(0, 0)")
                shot = base64.b64encode(await page.screenshot(type="jpeg", quality=60)).decode()
            return {
                "url": page.url,
                "title": await page.title(),
                "violations": violations,
                "marked": marked,
                "applied": applied,
                "screenshot": shot,
            }
        finally:
            await context.close()
