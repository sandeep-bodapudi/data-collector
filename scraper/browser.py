"""Optional headless-browser reader for websites whose contacts only appear after JavaScript runs.

Trial on 46 real Telangana sites where the plain reader found no email (2026-10-19): the plain reader found an email on
6, a real browser on 12 (Naresh IT, IIDE, Lifeboat, Korukonda...). It costs ~17 s and ~300 MB per site, so it is:

  - opt-in: BROWSER_FALLBACK=1, and only where Playwright + its Chromium are installed (`pip install playwright`,
    `python -m playwright install chromium`). The small hosted server leaves it off.
  - a fallback: used only when the plain reader found no email or was refused (see jobs._read_site).
  - capped per run: BROWSER_MAX_SITES (default 40).
  - light: images, fonts and video are never downloaded; robots.txt is checked for every page, as for the plain reader.

Playwright's sync API must be used from the thread that started it, while runs read sites from a thread pool, so one
service thread owns the browser and the pool hands it URLs through a queue. The browser closes itself after
IDLE_SECONDS without work, so it does not hold memory between runs.
"""
from __future__ import annotations

import os
import queue
import threading
from concurrent.futures import Future
from urllib.parse import urlparse

IDLE_SECONDS = 120
PAGE_TIMEOUT_MS = 25000
SETTLE_MS = 8000
MAX_CONTACT_LINKS = 2
CONTACT_WORDS = ("contact", "reach", "enquir", "inquir", "about", "locate", "branches", "address")
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36 "
              "OneBridgeDataCollector/2.0 (internal business research tool)")


def enabled() -> bool:
    """On only when asked for and actually installed."""
    if os.environ.get("BROWSER_FALLBACK") != "1":
        return False
    try:
        import playwright.sync_api  # noqa: F401
        return True
    except Exception:
        return False


def max_sites() -> int:
    try:
        return max(0, int(os.environ.get("BROWSER_MAX_SITES", "40")))
    except ValueError:
        return 40


class _Service:
    def __init__(self):
        self._q: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def read(self, url: str, allowed=None, timeout: float = 90.0) -> list[tuple[str, str]]:
        """[(html, final_url), ...] for the page and up to MAX_CONTACT_LINKS contact pages. Raises on failure."""
        fut: Future = Future()
        with self._lock:
            if not (self._thread and self._thread.is_alive()):
                self._thread = threading.Thread(target=self._loop, name="browser-reader", daemon=True)
                self._thread.start()
            self._q.put((url, allowed, fut))
        return fut.result(timeout=timeout)

    def _loop(self):
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--disable-dev-shm-usage", "--disable-gpu"])
            ctx = browser.new_context(user_agent=USER_AGENT)
            ctx.route("**/*", lambda route: route.abort()
                      if route.request.resource_type in ("image", "media", "font") else route.continue_())
            page = ctx.new_page()
            try:
                while True:
                    try:
                        url, allowed, fut = self._q.get(timeout=IDLE_SECONDS)
                    except queue.Empty:
                        with self._lock:  # idle: close, unless a request slipped in while deciding
                            if self._q.empty():
                                self._thread = None
                                return
                        continue
                    if fut.set_running_or_notify_cancel():
                        try:
                            if page.is_closed():
                                page = ctx.new_page()
                            fut.set_result(_read(page, url, allowed))
                        except BaseException as e:  # noqa: BLE001 - always answer the caller
                            fut.set_exception(e)
                            try:
                                page.close()
                            except Exception:
                                pass
                            page = ctx.new_page()
            finally:
                try:
                    browser.close()
                except Exception:
                    pass


def _settle(page):
    try:
        page.wait_for_load_state("networkidle", timeout=SETTLE_MS)
    except Exception:
        pass  # a page that keeps polling never goes idle; what has rendered so far is enough


def _read(page, url: str, allowed=None) -> list[tuple[str, str]]:
    if allowed and not allowed(url):
        return []
    page.goto(url, timeout=PAGE_TIMEOUT_MS, wait_until="domcontentloaded")
    _settle(page)
    out = [(page.content(), page.url)]
    host = (urlparse(page.url).hostname or "").removeprefix("www.")
    links = page.eval_on_selector_all("a[href]", "els => els.map(e => [e.href, (e.innerText || '').toLowerCase()])")
    picks: list[str] = []
    for href, text in links:
        h = (urlparse(href).hostname or "").removeprefix("www.")
        if not h or not (h == host or h.endswith("." + host)):
            continue  # stay on the business's own site
        if any(w in (href.lower() + " " + text) for w in CONTACT_WORDS) and href.rstrip("/") != page.url.rstrip("/"):
            if href not in picks:
                picks.append(href)
    for href in picks[:MAX_CONTACT_LINKS]:
        if allowed and not allowed(href):
            continue
        try:
            page.goto(href, timeout=PAGE_TIMEOUT_MS, wait_until="domcontentloaded")
            _settle(page)
            out.append((page.content(), page.url))
        except Exception:
            continue
    return out


_SERVICE = _Service()


def read_site(url: str, allowed=None) -> list[tuple[str, str]]:
    return _SERVICE.read(url, allowed)
