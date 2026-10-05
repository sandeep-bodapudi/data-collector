"""Polite HTTP fetcher: respects robots.txt, rate-limits per domain, never logs in."""
import threading
import time
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import UnicodeDammit

USER_AGENT = "OnebridgeDataCollector/1.0 (+public-data research; contact: admin)"
TIMEOUT = 20
PER_DOMAIN_DELAY = 1.5  # seconds between requests to the same site
MAX_BYTES = 3_000_000

# Sites that hide content behind a login or forbid scraping. We keep their search
# result (title/snippet/link) but never fetch the page itself.
SKIP_FETCH_DOMAINS = (
    "facebook.com", "instagram.com", "linkedin.com", "twitter.com", "x.com",
    "tiktok.com", "pinterest.com", "quora.com", "reddit.com", "youtube.com",
    "google.com", "maps.google.com", "play.google.com", "apps.apple.com",
)


def domain_of(url: str) -> str:
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def is_skipped(url: str) -> bool:
    d = domain_of(url)
    return any(d == s or d.endswith("." + s) for s in SKIP_FETCH_DOMAINS)


class Fetcher:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en;q=0.9",
        })
        self._robots: dict[str, RobotFileParser | None] = {}
        self._last_hit: dict[str, float] = {}
        self._lock = threading.Lock()

    def _robots_for(self, url: str) -> RobotFileParser | None:
        p = urlparse(url)
        base = f"{p.scheme}://{p.netloc}"
        with self._lock:
            if base in self._robots:
                return self._robots[base]
        rp = None
        try:
            r = self.session.get(base + "/robots.txt", timeout=10)
            if r.status_code == 200:
                rp = RobotFileParser()
                rp.parse(r.text.splitlines())
        except requests.RequestException:
            pass
        with self._lock:
            self._robots[base] = rp
        return rp

    def allowed(self, url: str) -> bool:
        rp = self._robots_for(url)
        return True if rp is None else rp.can_fetch(USER_AGENT, url)

    def _wait_turn(self, url: str):
        d = domain_of(url)
        while True:
            with self._lock:
                now = time.time()
                last = self._last_hit.get(d, 0)
                if now - last >= PER_DOMAIN_DELAY:
                    self._last_hit[d] = now
                    return
                wait = PER_DOMAIN_DELAY - (now - last)
            time.sleep(wait)

    def get_html(self, url: str) -> tuple[str | None, str]:
        """Return (html, status_note). html is None when not fetched."""
        if is_skipped(url):
            return None, "skipped (login/social site)"
        if not self.allowed(url):
            return None, "blocked by robots.txt"
        self._wait_turn(url)
        try:
            r = self.session.get(url, timeout=TIMEOUT, stream=True, allow_redirects=True)
            ctype = r.headers.get("Content-Type", "")
            if r.status_code != 200:
                return None, f"HTTP {r.status_code}"
            if "html" not in ctype and "xml" not in ctype:
                return None, f"not a web page ({ctype.split(';')[0] or 'unknown'})"
            content = r.raw.read(MAX_BYTES, decode_content=True)
            declared = [r.encoding] if "charset" in ctype.lower() and r.encoding else []
            text = UnicodeDammit(content, declared, is_html=True).unicode_markup
            return text or content.decode("utf-8", errors="replace"), "ok"
        except requests.RequestException as e:
            return None, f"error: {type(e).__name__}"
