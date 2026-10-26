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
from concurrent.futures import Future, TimeoutError as FutureTimeout
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


class BrowserUnavailable(RuntimeError):
    """The browser could not be started (e.g. Chromium not installed). Raised at once, not after a long wait."""


RETRY_AFTER_FAILURE = 600  # seconds before trying to start a browser again after a failed start
WAIT_LIMIT = 30 * 60       # a caller waits at most this long in the queue (one site takes up to ~2 minutes)


class _Service:
    def __init__(self):
        self._q: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._failed: tuple[float, str] | None = None  # (when, why) the last start failed

    def run(self, task, *args, timeout: float = WAIT_LIMIT):
        """Run task(page, *args) on the browser thread and return its result. One browser serves every caller in turn,
        so the wait covers the queue ahead, not just this site; a caller that gives up withdraws its request so the
        browser never works for nobody."""
        import time as _t

        with self._lock:
            if self._failed and _t.time() - self._failed[0] < RETRY_AFTER_FAILURE:
                raise BrowserUnavailable(self._failed[1])
            fut: Future = Future()
            if not (self._thread and self._thread.is_alive()):
                self._thread = threading.Thread(target=self._loop, name="browser-reader", daemon=True)
                self._thread.start()
            self._q.put((task, args, fut))
        try:
            return fut.result(timeout=timeout)
        except FutureTimeout:
            fut.cancel()
            raise

    def read(self, url: str, allowed=None) -> list[tuple[str, str]]:
        """[(html, final_url), ...] for the page and up to MAX_CONTACT_LINKS contact pages. Raises on failure."""
        return self.run(_read, url, allowed)

    def _fail_all(self, why: str):
        import time as _t

        with self._lock:
            self._failed = (_t.time(), why)
            self._thread = None
            while True:
                try:
                    _task, _args, fut = self._q.get_nowait()
                except queue.Empty:
                    break
                if fut.set_running_or_notify_cancel():
                    fut.set_exception(BrowserUnavailable(why))

    def _loop(self):
        try:
            from playwright.sync_api import sync_playwright

            pw_cm = sync_playwright()
            p = pw_cm.__enter__()
        except BaseException as e:  # noqa: BLE001
            return self._fail_all(f"browser could not start ({type(e).__name__})")
        try:
            try:
                browser = p.chromium.launch(headless=True, args=["--disable-dev-shm-usage", "--disable-gpu"])
            except BaseException as e:  # noqa: BLE001 - e.g. 'playwright install chromium' never run
                return self._fail_all(f"browser could not start ({type(e).__name__}: {str(e)[:120]})")
            self._failed = None
            ctx = browser.new_context(user_agent=USER_AGENT, locale="en-US")
            ctx.route("**/*", lambda route: route.abort()
                      if route.request.resource_type in ("image", "media", "font") else route.continue_())
            page = ctx.new_page()
            try:
                while True:
                    try:
                        task, args, fut = self._q.get(timeout=IDLE_SECONDS)
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
                            fut.set_result(task(page, *args))
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
        finally:
            try:
                pw_cm.__exit__(None, None, None)
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


def run(task, *args, timeout: float = WAIT_LIMIT):
    """Run task(page, *args) on the shared browser (used by the Google search / Maps readers)."""
    return _SERVICE.run(task, *args, timeout=timeout)
