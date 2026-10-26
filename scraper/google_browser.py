"""Google Search and Google Maps read in a real browser - a PC-only fallback (owner's choice, 2026-10-26).

Used only when BROWSER_FALLBACK=1 and Playwright is installed (scraper/browser.py), and only when the better route is
not available: Google Maps through the official Places API when a key is set, web search through a Brave key.

Ground rules, followed on every search:
  - one search at a time on the shared browser, with an unhurried pause between searches (GOOGLE_BROWSER_GAP seconds,
    default 12) and between place pages (3 s) - like a person, not a crawler;
  - a cookie/consent page is answered with "Reject all" (the most private choice);
  - if Google shows its "unusual traffic" / CAPTCHA page, we STOP using Google for the rest of the run and say so. We never
    try to solve or get around that check;
  - at most GOOGLE_BROWSER_MAX_SEARCHES searches per run (default 25).
Google's terms do not allow automated searching; this exists because the owner asked for it on their own PC, at small
volume. It is never used on the hosted server (no browser there).

The page-reading parts (parse_search_html, parse_place_html, maps_links) are plain functions on HTML, so they are tested
without a browser; only the navigation runs in Playwright.
"""
from __future__ import annotations

import os
import random
import re
import time
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

from bs4 import BeautifulSoup

BLOCK_MARKERS = ("/sorry/", "unusual traffic", "id=\"captcha-form\"", "g-recaptcha", "detected unusual")
END_OF_LIST = ("You've reached the end of the list", "reached the end of the list")


class GoogleBlocked(Exception):
    """Google asked to confirm a human is searching. We stop; we do not work around it."""


def gap() -> float:
    try:
        return max(3.0, float(os.environ.get("GOOGLE_BROWSER_GAP", "12")))
    except ValueError:
        return 12.0


def max_searches() -> int:
    try:
        return max(0, int(os.environ.get("GOOGLE_BROWSER_MAX_SEARCHES", "25")))
    except ValueError:
        return 25


def looks_blocked(url: str, html: str) -> bool:
    low = (html or "")[:200000].lower()
    return "/sorry/" in (url or "") or any(m.lower() in low for m in BLOCK_MARKERS)


# ---------------------------------------------------------------- pure parsers
def _real_href(href: str) -> str:
    """Google wraps some result links as /url?q=<real>."""
    if href.startswith("/url?"):
        q = parse_qs(urlparse(href).query).get("q", [""])[0]
        return unquote(q)
    return href


def parse_search_html(html: str) -> list[dict]:
    """[{title, href, body}] from a Google results page: each organic result is a link wrapping an <h3>."""
    soup = BeautifulSoup(html or "", "lxml")
    root = soup.select_one("#search") or soup
    out, seen = [], set()
    for h3 in root.select("a h3"):
        a = h3.find_parent("a")
        href = _real_href(a.get("href", "")) if a else ""
        host = urlparse(href).hostname or ""
        if not href.startswith("http") or host.endswith("google.com") or host.endswith("googleusercontent.com") or href in seen:
            continue
        seen.add(href)
        block = a.find_parent("div", attrs={"data-snc": True}) or a.find_parent("div", class_=re.compile(r"\bg\b")) or a.parent
        snippet = ""
        if block:
            cand = block.select_one("[data-sncf], .VwiC3b, [style*='-webkit-line-clamp']")
            snippet = cand.get_text(" ", strip=True) if cand else ""
        out.append({"title": h3.get_text(" ", strip=True), "href": href, "body": snippet[:400]})
    return out


_COORDS = re.compile(r"!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)")
_AT = re.compile(r"@(-?\d+\.\d+),(-?\d+\.\d+)")


def maps_links(html: str) -> list[str]:
    """Place links in a Google Maps results list, in order, without duplicates and without "Sponsored" entries (seen
    live: an advertiser from another part of the city listed first for an Ameerpet search)."""
    soup = BeautifulSoup(html or "", "lxml")
    out = []
    for a in soup.select('a[href*="/maps/place/"]'):
        href = a.get("href", "")
        card = a.find_parent(attrs={"role": "article"}) or (a.parent.parent if a.parent else None)
        if card is not None and "Sponsored" in card.get_text(" ", strip=True)[:300]:
            continue
        if href and href not in out:
            out.append(href)
    return out


def parse_place_html(html: str, url: str) -> dict | None:
    """One Google Maps place page -> a row in our Places columns. None when there is no name."""
    soup = BeautifulSoup(html or "", "lxml")
    h1 = soup.select_one("h1")
    name = h1.get_text(" ", strip=True) if h1 else ""
    if len(name) < 2:
        return None

    def item(selector: str, prefix: str = "") -> str:
        el = soup.select_one(selector)
        if not el:
            return ""
        text = el.get("aria-label") or el.get_text(" ", strip=True)
        return re.sub(rf"^\s*{prefix}\s*:?\s*", "", text, flags=re.I).strip() if prefix else text.strip()

    from .extract import normalize_phone

    phone = ""
    ph = soup.select_one('[data-item-id^="phone:tel:"]')
    if ph:
        phone = ph.get("data-item-id", "").split("phone:tel:", 1)[-1] or item('[data-item-id^="phone:tel:"]', "Phone")
        phone = normalize_phone(phone) if phone else ""
    site = soup.select_one('a[data-item-id="authority"]')
    rating = soup.select_one('[role="img"][aria-label*="star"]')
    category = soup.select_one('button[jsaction*="category"]')
    m = _COORDS.search(url or "") or _AT.search(url or "")
    closed = "permanently closed" in soup.get_text(" ", strip=True).lower()[:5000]
    if closed:
        return None
    return {
        "Name": name, "Category": category.get_text(" ", strip=True) if category else "",
        "Address": item('[data-item-id="address"]', "Address"),
        "City": "", "State": "", "Postcode": "",
        "Phone": phone, "Email": "",
        "Website": site.get("href", "") if site else "",
        "Opening Hours": "",
        "Latitude": float(m.group(1)) if m else "", "Longitude": float(m.group(2)) if m else "",
        "Google Maps Link": url.split("?")[0] if url else "",
        "Other Details": _rating(rating.get("aria-label", "")) if rating else "",
        "Data Source": "Google Maps (browser)",
    }


def _rating(label: str) -> str:
    m = re.search(r"(\d(?:\.\d)?)\s*star", label or "")
    return f"Rating {m.group(1)}" if m else ""


# ---------------------------------------------------------------- browser tasks (run on the shared browser thread)
def _answer_consent(page):
    if "consent." not in page.url:
        return
    for label in ("Reject all", "Reject All", "Alle ablehnen", "Tout refuser"):
        try:
            page.get_by_role("button", name=label).first.click(timeout=4000)
            page.wait_for_load_state("domcontentloaded", timeout=10000)
            return
        except Exception:
            continue


def _check(page):
    if looks_blocked(page.url, page.content()):
        raise GoogleBlocked("Google asked to confirm a person is searching.")


def _search_task(page, query: str, n: int) -> list[dict]:
    page.goto(f"https://www.google.com/search?q={quote_plus(query)}&hl=en&num={min(max(n, 10), 30)}",
              wait_until="domcontentloaded", timeout=40000)
    _answer_consent(page)
    _check(page)
    try:
        page.wait_for_selector("#search", timeout=15000)
    except Exception:
        _check(page)
    return parse_search_html(page.content())[:n]


def _maps_task(page, query: str, limit: int) -> list[dict]:
    page.goto(f"https://www.google.com/maps/search/{quote_plus(query)}?hl=en", wait_until="domcontentloaded", timeout=45000)
    _answer_consent(page)
    _check(page)
    try:
        page.wait_for_selector('div[role="feed"], h1', timeout=25000)
    except Exception:
        _check(page)
        return []
    # A search that matches exactly one place opens that place directly.
    if not page.query_selector('div[role="feed"]'):
        row = parse_place_html(page.content(), page.url)
        return [row] if row else []
    links, still = [], 0
    while len(links) < limit and still < 4:
        page.eval_on_selector('div[role="feed"]', "el => el.scrollBy(0, el.scrollHeight)")
        page.wait_for_timeout(1800)
        html = page.content()
        found = maps_links(html)
        still = still + 1 if len(found) == len(links) else 0
        links = found
        if any(m in html for m in END_OF_LIST):
            break
    rows = []
    for href in links[:limit]:
        page.wait_for_timeout(int(3000 + random.random() * 1500))
        try:
            page.goto(href, wait_until="domcontentloaded", timeout=40000)
            _check(page)
            page.wait_for_selector("h1", timeout=15000)
            page.wait_for_timeout(800)
        except GoogleBlocked:
            raise
        except Exception:
            continue
        row = parse_place_html(page.content(), page.url)
        if row:
            rows.append(row)
    return rows


# ---------------------------------------------------------------- what the runs call
class Session:
    """Per-run state: search budget, pacing, and the stop after a block."""

    def __init__(self, say=None):
        self.say = say or (lambda m: None)
        self.blocked = False
        self.used = 0
        self._last = 0.0

    def _turn(self) -> bool:
        if self.blocked or self.used >= max_searches():
            return False
        wait = self._last + gap() - time.time()
        if wait > 0:
            time.sleep(wait)
        self.used += 1
        self._last = time.time()
        return True

    def _guarded(self, task, *args):
        from . import browser

        try:
            return browser.run(task, *args)
        except GoogleBlocked:
            self.blocked = True
            self.say("  Google asked to confirm a person is searching, so Google is not used again in this run "
                     "(we never try to get around that check). The other sources carry on.")
            return []
        except Exception as e:  # noqa: BLE001 - a broken page is not the end of the run
            self.say(f"  Google in the browser failed ({type(e).__name__}); carrying on without it.")
            return []

    def search(self, query: str, n: int) -> list[dict]:
        if not self._turn():
            return []
        return self._guarded(_search_task, query, n)

    def maps(self, query: str, limit: int) -> list[dict]:
        if not self._turn():
            return []
        return self._guarded(_maps_task, query, limit)
