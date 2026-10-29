"""Tavily: Map picks which pages to scan; Search supplies current fix guidance that each fix cites."""
import asyncio
import json
import logging
import os
import time
from pathlib import Path
from urllib.parse import urldefrag, urlsplit

from tavily import AsyncTavilyClient

from .llm import env_flag

log = logging.getLogger("accessfix.research")

CACHE_PATH = Path(os.getenv("CACHE_DIR", ".cache")) / "tavily.json"
MAP_INSTRUCTIONS = "Key user-facing pages: home, contact, signup, checkout, forms"
KEY_PAGE_WORDS = ("contact", "signup", "sign-up", "register", "checkout", "cart", "login", "account", "subscribe", "form", "order", "book")

_cache: dict[str, object] = {}
_cache_lock = asyncio.Lock()
try:
    _cache.update(json.loads(CACHE_PATH.read_text()))
except (OSError, ValueError):
    pass


async def _save_cache():
    async with _cache_lock:
        try:
            CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            CACHE_PATH.write_text(json.dumps(_cache))
        except OSError as e:
            log.warning("could not write Tavily cache: %s", e)


MOCK_SOURCES = {
    "image-alt": [("Images Tutorial | Web Accessibility Initiative (WAI) | W3C", "https://www.w3.org/WAI/tutorials/images/"),
                  ("Understanding SC 1.1.1: Non-text Content | WAI | W3C", "https://www.w3.org/WAI/WCAG22/Understanding/non-text-content.html")],
    "button-name": [("ARIA: button role - MDN Web Docs", "https://developer.mozilla.org/en-US/docs/Web/Accessibility/ARIA/Reference/Roles/button_role"),
                    ("Understanding SC 4.1.2: Name, Role, Value | WAI | W3C", "https://www.w3.org/WAI/WCAG22/Understanding/name-role-value.html")],
    "link-name": [("Understanding SC 2.4.4: Link Purpose (In Context) | WAI | W3C", "https://www.w3.org/WAI/WCAG22/Understanding/link-purpose-in-context.html"),
                  ("aria-label - MDN Web Docs", "https://developer.mozilla.org/en-US/docs/Web/Accessibility/ARIA/Reference/Attributes/aria-label")],
    "label": [("Labeling Controls | Forms Tutorial | WAI | W3C", "https://www.w3.org/WAI/tutorials/forms/labels/"),
              ("<label>: The Label element - MDN Web Docs", "https://developer.mozilla.org/en-US/docs/Web/HTML/Reference/Elements/label")],
    "select-name": [("Labeling Controls | Forms Tutorial | WAI | W3C", "https://www.w3.org/WAI/tutorials/forms/labels/")],
    "html-has-lang": [("Understanding SC 3.1.1: Language of Page | WAI | W3C", "https://www.w3.org/WAI/WCAG22/Understanding/language-of-page.html")],
    "frame-title": [("H64: Using the title attribute of the iframe element | W3C", "https://www.w3.org/WAI/WCAG22/Techniques/html/H64")],
    "color-contrast": [("Understanding SC 1.4.3: Contrast (Minimum) | WAI | W3C", "https://www.w3.org/WAI/WCAG22/Understanding/contrast-minimum.html"),
                       ("Color contrast - MDN Web Docs", "https://developer.mozilla.org/en-US/docs/Web/Accessibility/Guides/Understanding_WCAG/Perceivable/Color_contrast")],
}


class Research:
    """One per run; records every Tavily call for the agent trace."""

    def __init__(self, mock: bool | None = None):
        self.mock = env_flag("MOCK_TAVILY", "1") if mock is None else mock
        self.calls: list[dict] = []
        self._client = None if self.mock else AsyncTavilyClient(api_key=os.getenv("TAVILY_API_KEY", ""))
        self.mock_delay = float(os.getenv("MOCK_DELAY", "0.35"))

    def _record(self, kind, detail, started, cached=False, error=None):
        entry = {"tool": f"Tavily {kind}", "detail": detail, "latency_ms": round((time.perf_counter() - started) * 1000),
                 "cached": cached, "mock": self.mock}
        if error:
            entry["error"] = error
        self.calls.append(entry)

    async def map(self, url: str) -> list[str]:
        """Return discovered URLs (may be empty). Raises on API failure so the caller can log it."""
        started = time.perf_counter()
        key = f"map::{url}"
        if self.mock:
            await asyncio.sleep(self.mock_delay)
            base = url.split("#")[0]
            self._record("Map", url, started)
            return [base, f"{base}#menu", f"{base}#order", f"{base}#visit"]
        if key in _cache:
            self._record("Map", url, started, cached=True)
            return list(_cache[key])
        try:
            resp = await self._client.map(url, max_depth=1, limit=20, instructions=MAP_INSTRUCTIONS, timeout=45)
        except Exception as e:
            self._record("Map", url, started, error=str(e)[:200])
            raise
        results = [r if isinstance(r, str) else r.get("url", "") for r in resp.get("results", [])]
        results = [r for r in results if r]
        self._record("Map", f"{url} → {len(results)} URLs", started)
        _cache[key] = results
        await _save_cache()
        return results

    async def search(self, rule_id: str, help_text: str, criterion: str) -> list[dict]:
        """Return up to 3 sources: {title, url, content (≤500 chars)}."""
        started = time.perf_counter()
        query = f"how to fix {help_text} WCAG {criterion} accessible HTML example".replace("  ", " ")
        key = f"search::{query}"
        if self.mock:
            await asyncio.sleep(self.mock_delay / 2)
            self._record("Search", query, started)
            return [{"title": t, "url": u, "content": f"Canned guidance for {rule_id} (mock mode)."}
                    for t, u in MOCK_SOURCES.get(rule_id, [])]
        if key in _cache:
            self._record("Search", query, started, cached=True)
            return list(_cache[key])
        try:
            resp = await self._client.search(query, max_results=3, search_depth="basic", timeout=30)
        except Exception as e:
            self._record("Search", query, started, error=str(e)[:200])
            log.warning("Tavily search failed for %s: %s", rule_id, e)
            return []
        sources = [{"title": r.get("title", "")[:160], "url": r.get("url", ""), "content": (r.get("content") or "")[:500]}
                   for r in resp.get("results", [])[:3]]
        self._record("Search", query, started)
        _cache[key] = sources
        await _save_cache()
        return sources


def choose_pages(input_url: str, discovered: list[str], max_pages: int) -> list[str]:
    """Always include the input URL; then prefer same-site key pages (forms, checkout, contact)."""
    base = urldefrag(input_url)[0]
    host = urlsplit(base).hostname
    seen, others, key_pages = {base}, [], []
    for raw in discovered:
        u = urldefrag(raw)[0]
        if u in seen or urlsplit(u).hostname != host or urlsplit(u).scheme not in ("http", "https"):
            continue
        seen.add(u)
        (key_pages if any(w in u.lower() for w in KEY_PAGE_WORDS) else others).append(u)
    return ([base] + key_pages + others)[:max(1, max_pages)]
