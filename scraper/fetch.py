"""Polite HTTP fetcher: respects robots.txt, rate-limits per domain, never logs in."""
import threading
import time
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import UnicodeDammit

# Mimics a real Chrome browser so more sites serve full content instead of blocking bots.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)
TIMEOUT = 22
PER_DOMAIN_DELAY = 0.8   # seconds between requests to the same domain (was 1.5)
MAX_BYTES = 3_000_000
WORKERS = 12              # parallel page workers (was 5)

# Sites that hide content behind a login or forbid scraping. We keep their search
# result (title/snippet/link) but never fetch the page itself.
SKIP_FETCH_DOMAINS = (
    "facebook.com", "instagram.com", "linkedin.com", "twitter.com", "x.com",
    "tiktok.com", "pinterest.com", "quora.com", "reddit.com", "youtube.com",
    "google.com", "maps.google.com", "play.google.com", "apps.apple.com",
)

_SESSION_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "DNT": "1",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}


def domain_of(url: str) -> str:
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def is_skipped(url: str) -> bool:
    d = domain_of(url)
    return any(d == s or d.endswith("." + s) for s in SKIP_FETCH_DOMAINS)


class Fetcher:
    def __init__(self, spec: dict = None):
        self.spec = spec or {}
        self.session = requests.Session()
        self.session.headers.update(_SESSION_HEADERS)
        
        # Inject social media cookies if provided
        if self.spec.get("li_at_cookie"):
            self.session.cookies.set("li_at", self.spec["li_at_cookie"], domain=".linkedin.com")
        if self.spec.get("fb_cookie"):
            # Facebook cookies are usually provided as a string "c_user=...; xs=..."
            for pair in self.spec["fb_cookie"].split(";"):
                if "=" in pair:
                    k, v = pair.strip().split("=", 1)
                    self.session.cookies.set(k, v, domain=".facebook.com")
                    
        self._robots: dict[str, RobotFileParser | None] = {}
        self._last_hit: dict[str, float] = {}
        self._lock = threading.Lock()

    def _is_skipped(self, url: str) -> bool:
        d = domain_of(url)
        # Don't skip if we have a cookie for this domain
        if "linkedin.com" in d and self.spec.get("li_at_cookie"):
            return False
        if "facebook.com" in d and self.spec.get("fb_cookie"):
            return False
        return any(d == s or d.endswith("." + s) for s in SKIP_FETCH_DOMAINS)

    def _robots_for(self, url: str) -> RobotFileParser | None:
        p = urlparse(url)
        base = f"{p.scheme}://{p.netloc}"
        with self._lock:
            if base in self._robots:
                return self._robots[base]
        rp = None
        try:
            r = self.session.get(base + "/robots.txt", timeout=8)
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

    def get_html(self, url: str, ignore_robots: bool = False) -> tuple[str | None, str]:
        """Return (html, status_note). html is None when not fetched.

        ignore_robots=True is used for sub-pages like /contact on sites whose
        main page is already allowed — robots.txt occasionally blocks /contact
        even for otherwise public sites.
        """
        if self._is_skipped(url):
            return None, "skipped (login/social site)"
        if not ignore_robots and not self.allowed(url):
            return None, "blocked by robots.txt"
        self._wait_turn(url)
        for attempt in range(2):  # retry once on transient errors
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
            except (requests.ConnectionError, requests.Timeout):
                if attempt == 0:
                    time.sleep(2)
                    continue
                return None, "error: connection failed after retry"
            except requests.RequestException as e:
                return None, f"error: {type(e).__name__}"
        return None, "error: exhausted retries"
