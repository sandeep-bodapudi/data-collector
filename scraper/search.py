"""Web search with fallbacks, cheapest/most reliable first.

1. Brave Search API, if BRAVE_API_KEY is set (free tier: 2,000 queries/month, https://brave.com/search/api/).
2. Google Programmable Search, if GOOGLE_CSE_KEY + GOOGLE_CSE_CX are set (free tier: 100 queries/DAY,
   https://programmablesearchengine.google.com/ - create an engine, "Search the entire web", then
   https://developers.google.com/custom-search/v1/introduction for the API key). Good as a reliable top-up once
   Brave's monthly quota or the free engines below run dry for the day.
   (Between 1 and 2: the person's own Serper / Tavily / Firecrawl keys, if they added any - KEYED_SEARCH below; all
   three have free plans.)
3. Otherwise free search engines through the `ddgs` library, tried one by one. These work well from office PCs,
   but datacenter IPs (cloud servers) get blocked often and unpredictably - which engine works varies run to run.
   "bing" is deliberately not in this list: this library doesn't actually have a Bing backend, and passing that
   name silently falls back to trying everything at once, which wastes requests and makes the log lie about
   which engine actually answered.
"""
import hashlib
import json
import os
import threading
import time
from contextlib import contextmanager
from datetime import date

import requests
from ddgs import DDGS

BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"
GOOGLE_CSE_URL = "https://www.googleapis.com/customsearch/v1"
# A short pause between trying successive free engines for the SAME query. Querying one engine right after another
# with no gap at all, on a cloud server's shared IP, is the same kind of rapid-fire request pattern that makes a
# single engine start refusing (see scraper/jobs.py's _current_gap/SEARCH_GAP, measured for *between* searches);
# trying every free engine back-to-back within one search is the same risk, just compressed into a few seconds.
FREE_ENGINE_GAP = float(os.environ.get("FREE_ENGINE_GAP_SECONDS", "1.5"))
# ddgs's own default is 5s. Real cause of "each lookup takes up to a minute": when an engine is being slow or
# half-blocking rather than cleanly refusing, every one of the up to 16 engine attempts one lookup can make (see
# discover.find_website) sits for the FULL timeout before giving up and trying the next - 16 x 5s is most of a
# minute on its own, before any of the deliberate pacing gaps are even added. Trimmed to a still-generous 4s;
# a genuinely working engine essentially never needs the last second of a 5s budget to answer.
FREE_ENGINE_TIMEOUT = float(os.environ.get("FREE_ENGINE_TIMEOUT_SECONDS", "4"))
FREE_ENGINES = ["duckduckgo", "yahoo", "brave", "google", "mojeek", "startpage"]


class SearchBlocked(Exception):
    """Every search option refused the request (common from cloud servers without a paid key)."""


class _BraveQuotaExhausted(Exception):
    """Brave answered 429: this key's quota (per-second or the free plan's 2,000/month) is used up."""


# Once a key is found to be exhausted, every later call in this process skips Brave for it instead of hitting the
# same 429 again - checked and set once per key, not re-tried every query. A process restart clears this (the real
# quota doesn't reset that way, but it's cheap to find out again rather than remember it past a restart).
_exhausted_keys: set[str] = set()
_exhausted_lock = threading.Lock()


def _key_id(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def brave_exhausted(key: str) -> bool:
    return _key_id(key) in _exhausted_keys


def _mark_exhausted(key: str) -> bool:
    """True only for the call that first discovers this key is exhausted, so the caller logs it once, not per query."""
    kid = _key_id(key)
    with _exhausted_lock:
        if kid in _exhausted_keys:
            return False
        _exhausted_keys.add(kid)
        return True


def _next_reset_guess() -> str:
    """Brave's free plan resets monthly; the exact day isn't published, so this names the 1st of next month as our
    best guess, not a promise - said that way in the message this builds into."""
    today = date.today()
    year, month = (today.year, today.month + 1) if today.month < 12 else (today.year + 1, 1)
    return date(year, month, 1).strftime("%B %-d" if os.name != "nt" else "%B 1")


# Each user brings their own Brave key (Settings -> Search connector). A job runs its searches on several worker
# threads, and threads don't share each other's state, so the key is set on every thread that searches for it.
_thread_keys = threading.local()


@contextmanager
def search_keys(brave: str = "", extra: dict | None = None):
    """Use this user's search keys for every search made inside the block, on this thread. `extra` holds the other
    keyed search services (KEYED_SEARCH: serper, tavily, firecrawl) by name."""
    previous = (getattr(_thread_keys, "brave", None), getattr(_thread_keys, "extra", None))
    _thread_keys.brave = brave or ""
    _thread_keys.extra = {k: v for k, v in (extra or {}).items() if k in KEYED_SEARCH_NAMES and v}
    try:
        yield
    finally:
        _thread_keys.brave, _thread_keys.extra = previous


def _brave_key() -> str:
    return getattr(_thread_keys, "brave", None) or ""


KEYED_SEARCH_NAMES = ("serper", "tavily", "firecrawl")  # tried in this order after Brave; see KEYED_SEARCH below


def _extra_key(name: str) -> str:
    return (getattr(_thread_keys, "extra", None) or {}).get(name, "")


def keyed_search_set() -> bool:
    """Any official, key-based search is usable on this thread (Brave or one of KEYED_SEARCH) - the free engines'
    IP-based blocking doesn't apply, so pacing can be shorter and "no engine answered" warnings don't fit."""
    return brave_key_set() or any(_extra_key(n) and not brave_exhausted(_extra_key(n)) for n in KEYED_SEARCH_NAMES)


def firecrawl_key() -> str:
    """This user's Firecrawl key on this thread, if set and not out of credit - also used to fetch blocked pages."""
    key = _extra_key("firecrawl")
    return "" if not key or brave_exhausted(key) else key


def brave_key_set() -> bool:
    key = _brave_key()
    return bool(key) and not brave_exhausted(key)


# Brave's free tier allows about one request per second per key. Requests on the same key are spaced out so a
# parallel job can't trip that limit; different users' keys have their own limit, so they don't wait on each other.
BRAVE_MIN_INTERVAL = float(os.environ.get("BRAVE_MIN_INTERVAL_SECONDS", "1.1"))
_brave_lock = threading.Lock()
_brave_next_slot: dict[str, float] = {}


def _brave_wait(key: str) -> None:
    slot_id = hashlib.sha256(key.encode()).hexdigest()  # never keep the raw key as a dict key
    with _brave_lock:
        now = time.monotonic()
        start = max(now, _brave_next_slot.get(slot_id, 0.0))
        _brave_next_slot[slot_id] = start + BRAVE_MIN_INTERVAL
    if start > now:
        time.sleep(start - now)


def _brave(query: str, region: str, max_results: int, key: str) -> list[dict]:
    country = "ALL" if region == "wt-wt" else region.split("-")[0].upper()
    out = []
    for offset in range(0, 10):  # Brave allows up to 10 pages of 20 results
        _brave_wait(key)
        r = requests.get(BRAVE_URL, headers={"X-Subscription-Token": key, "Accept": "application/json"},
                         params={"q": query, "count": 20, "offset": offset, "country": country}, timeout=30)
        if r.status_code == 429:
            raise _BraveQuotaExhausted()
        r.raise_for_status()
        items = r.json().get("web", {}).get("results", [])
        out += [{"title": i.get("title", ""), "href": i.get("url", ""), "body": i.get("description", "")} for i in items]
        if len(out) >= max_results or len(items) < 20:
            break
    return out[:max_results]


def _google_cse(query: str, region: str, max_results: int, api_key: str, cx: str) -> list[dict]:
    out = []
    country = None if region == "wt-wt" else region.split("-")[0].lower()
    for start in range(1, min(max_results, 100) + 1, 10):  # 10 results per request, 100 total max on this API
        params = {"key": api_key, "cx": cx, "q": query, "start": start, "num": min(10, max_results - len(out))}
        if country:
            params["cr"] = f"country{country.upper()}"
        r = requests.get(GOOGLE_CSE_URL, params=params, timeout=30)
        if r.status_code == 429:  # the free 100/day quota is used up for today
            raise requests.HTTPError("daily quota used up", response=r)
        r.raise_for_status()
        items = r.json().get("items", [])
        out += [{"title": i.get("title", ""), "href": i.get("link", ""), "body": i.get("snippet", "")} for i in items]
        if len(items) < 10 or len(out) >= max_results:
            break
    return out[:max_results]


# ---------------------------------------------------------------- other keyed search services with free plans
# Each person can add their own key for any of these in Settings -> Keys. They are tried after Brave and before the
# free engines (which cloud servers often get refused by). Free plans when this was written - they change, so each
# card in Settings links to the provider's own pricing page: Serper 2,500 one-time free searches; Tavily 1,000 free
# credits a month; Firecrawl a monthly free allowance of credits (2 per 10 search results, 1 per page scraped).
# Responses are read defensively: these APIs have shipped more than one shape over time, and a shape we don't
# recognise just gives no results (and the Settings "Test key" button shows that straight away).
SERPER_URL = "https://google.serper.dev/search"
TAVILY_URL = "https://api.tavily.com/search"
FIRECRAWL_SEARCH_URL = "https://api.firecrawl.dev/v2/search"
FIRECRAWL_SCRAPE_URL = "https://api.firecrawl.dev/v2/scrape"


class OutOfCredit(Exception):
    """The service answered 401/402/403/429: a wrong key, no credit left, or rate-limited."""


def _check(r: requests.Response) -> None:
    if r.status_code in (401, 402, 403, 429):
        raise OutOfCredit(f"HTTP {r.status_code}")
    r.raise_for_status()


def _serper(query: str, region: str, max_results: int, key: str) -> list[dict]:
    body = {"q": query, "num": min(max(max_results, 10), 100)}
    if region != "wt-wt":
        body["gl"] = region.split("-")[0].lower()
    r = requests.post(SERPER_URL, headers={"X-API-KEY": key, "Content-Type": "application/json"}, json=body, timeout=30)
    _check(r)
    items = r.json().get("organic") or []
    return [{"title": i.get("title", ""), "href": i.get("link", "") or i.get("url", ""),
             "body": i.get("snippet", "") or i.get("description", "")} for i in items if isinstance(i, dict)][:max_results]


def _tavily(query: str, region: str, max_results: int, key: str) -> list[dict]:
    body = {"query": query, "max_results": min(max_results, 20), "search_depth": "basic"}  # basic = 1 credit
    r = requests.post(TAVILY_URL, headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                      json=body, timeout=30)
    _check(r)
    items = r.json().get("results") or []
    return [{"title": i.get("title", ""), "href": i.get("url", ""), "body": i.get("content", "")}
            for i in items if isinstance(i, dict)]


def _firecrawl_search(query: str, region: str, max_results: int, key: str) -> list[dict]:
    body = {"query": query, "limit": min(max_results, 100)}
    if region != "wt-wt":
        body["country"] = region.split("-")[0].upper()
    r = requests.post(FIRECRAWL_SEARCH_URL, headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                      json=body, timeout=60)
    _check(r)
    data = r.json().get("data") or []
    items = (data.get("web") or []) if isinstance(data, dict) else data  # v2: {"data": {"web": [...]}}; v1: {"data": [...]}
    return [{"title": i.get("title", ""), "href": i.get("url", ""), "body": i.get("description", "") or i.get("snippet", "")}
            for i in items if isinstance(i, dict)]


def firecrawl_scrape(url: str, key: str, timeout: int = 60) -> str | None:
    """A page's HTML through Firecrawl, for sites that refuse a plain request (bot checks, pages built by JavaScript).
    1 credit per page. None when it can't be read; raises OutOfCredit when the key is refused or spent."""
    r = requests.post(FIRECRAWL_SCRAPE_URL, headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                      json={"url": url, "formats": ["html"]}, timeout=timeout)
    _check(r)
    data = r.json().get("data") or {}
    return (data.get("html") or data.get("rawHtml") or None) if isinstance(data, dict) else None


KEYED_SEARCH = {  # name -> (label, function), tried in KEYED_SEARCH_NAMES order after Brave
    "serper": ("Serper", _serper),
    "tavily": ("Tavily", _tavily),
    "firecrawl": ("Firecrawl", _firecrawl_search),
}


def test_keyed_search(name: str, key: str) -> int:
    """One real search with this key, for Settings' "Test key" button. Returns how many results came back."""
    _, fn = KEYED_SEARCH[name]
    try:
        return len(fn("digital marketing institutes in Hyderabad", "in-en", 5, key))
    except OutOfCredit as e:
        raise requests.HTTPError(f"the key was refused or is out of credit ({e})")


def _enabled(name: str) -> bool:
    try:
        from .config import is_enabled

        return is_enabled(name)
    except Exception:
        return True


def _cache_says_unhealthy(name: str) -> bool:
    from .connection_health import is_unhealthy

    return is_unhealthy(name)


def web_search(query: str, region: str, max_results: int, say, engines=None) -> list[dict]:
    """`engines` (optional) chooses which free engines to try, in order. The default order is FREE_ENGINES."""
    key = _brave_key()
    brave_allowed = _enabled("brave") and not _cache_says_unhealthy("brave")
    if not _enabled("brave"):
        say("  skipped Brave (disabled in Admin → Connections)")
    elif _cache_says_unhealthy("brave"):
        say("  skipped Brave (last health check failed)")
    if key and not brave_exhausted(key) and brave_allowed:
        try:
            hits = _brave(query, region, max_results, key)
            if hits:
                return hits
            say("  Brave API returned no results; trying other options")
        except _BraveQuotaExhausted:
            if _mark_exhausted(key):  # only the call that first discovers this says so - not every query after it
                say(f"  Brave's free search limit for this account has been used up for now. It should refresh "
                    f"around {_next_reset_guess()} (Brave doesn't publish the exact day). Continuing without it for "
                    f"the rest of this run, which will take longer than usual - nothing has failed.")
        except requests.RequestException as e:
            say(f"  Brave API failed ({e}); trying other options")
    elif key and brave_exhausted(key):
        say("  skipped Brave (quota exhausted earlier this run); trying other options")

    for name in KEYED_SEARCH_NAMES:
        label, fn = KEYED_SEARCH[name]
        k = _extra_key(name)
        if not k or brave_exhausted(k) or not _enabled(name):
            continue
        try:
            hits = fn(query, region, max_results, k)
            if hits:
                return hits
            say(f"  {label} returned no results; trying other options")
        except OutOfCredit as e:
            if _mark_exhausted(k):  # said once per run, not on every search
                say(f"  {label} refused the search ({e}): its key is wrong or its free credit is used up. Continuing "
                    f"without {label} for the rest of this run - check the key in Connections & settings → Keys.")
        except (requests.RequestException, ValueError) as e:
            say(f"  {label} failed ({type(e).__name__}); trying other options")

    g_allowed = _enabled("google_cse")
    if not g_allowed:
        say("  skipped Google CSE (disabled in Admin → Connections)")
    g_key, g_cx = os.environ.get("GOOGLE_CSE_KEY"), os.environ.get("GOOGLE_CSE_CX")
    if g_key and g_cx and g_allowed:
        try:
            hits = _google_cse(query, region, max_results, g_key, g_cx)
            if hits:
                return hits
            say("  Google search returned no results; trying free engines")
        except requests.RequestException as e:
            say(f"  Google search unavailable ({e}); trying free engines")

    # Any one free engine often returns far fewer than max_results for a specific query (sometimes under 10), even
    # when it isn't blocked at all - it simply doesn't have more to give for that query. Stopping at the first
    # engine that returned *anything* (the old behaviour) is why a "200 results" run could come back with 9 rows.
    # So: keep querying further engines and merge their results (by URL) until max_results is reached or every
    # engine has been tried.
    if not _enabled("ddgs"):
        say("  skipped free engines (disabled in Admin → Connections)")
        raise SearchBlocked("All search options are disabled. Enable Brave, Google CSE or free engines in Admin → Connections.")
    merged, seen_href, failed, used = [], set(), [], []
    try:
        from .proxy import account as _proxy_account
        from .proxy import proxy_for as _proxy_for

        _search_proxy = _proxy_for("search") or ""
    except Exception:
        _search_proxy, _proxy_account = "", lambda n: None
    if _search_proxy:
        say("  searching through the company proxy")
    with DDGS(timeout=FREE_ENGINE_TIMEOUT, **({"proxy": _search_proxy} if _search_proxy else {})) as ddgs:
        for i, engine in enumerate(engines or FREE_ENGINES):
            remaining = max_results - len(merged)
            if remaining <= 0:
                break
            if i:  # no pause before the very first engine - only between successive ones
                time.sleep(FREE_ENGINE_GAP)
            try:
                hits = ddgs.text(query, region=region, max_results=remaining, backend=engine) or []
                if _search_proxy:
                    try:
                        _proxy_account(len(json.dumps(hits, ensure_ascii=False).encode("utf-8")))
                    except Exception:
                        pass
            except Exception as e:  # each engine fails in its own way (blocked, rate limited, no results)
                failed.append(f"{engine}: no results" if "no results" in str(e).lower() else f"{engine}: {str(e)[:60]}")
                continue
            new = 0
            for h in hits:
                href = (h.get("href") or "").rstrip("/").lower()
                if href and href not in seen_href:
                    seen_href.add(href)
                    merged.append(h)
                    new += 1
            if new:
                used.append(f"{engine} ({new})")
            else:
                failed.append(f"{engine}: no results")
    if merged:
        if failed:
            say(f"  used {', '.join(used)} (nothing more from: {', '.join(f.split(':')[0] for f in failed)})")
        return merged
    # Every free engine came back with nothing. Two very different situations produce the exact same log lines
    # here: a genuinely obscure query (no engine anywhere has anything for it) and this IP being rate-limited (an
    # engine returns "no results" when it's actually refusing, same as a real empty answer) - so don't claim
    # either one confidently; say what was actually observed.
    if all(f.endswith("no results") for f in failed):
        say("  no results from any free search engine for this search (this can mean there's genuinely nothing "
            "to find, or that this server's searches are being rate-limited right now - a free Brave or Google "
            "search key removes the second possibility entirely; see the README)")
        return []
    say("  all search options refused: " + "; ".join(failed))
    if all(not f.endswith("no results") for f in failed):
        raise SearchBlocked("The search engines refused the request. Try again later, or ask your admin to add a "
                            "free Brave or Google search key (see the README).")
    return []
