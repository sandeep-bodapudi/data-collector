"""Web search with fallbacks, cheapest/most reliable first.

1. Brave Search API, if BRAVE_API_KEY is set (free tier: 2,000 queries/month, https://brave.com/search/api/).
2. Google Programmable Search, if GOOGLE_CSE_KEY + GOOGLE_CSE_CX are set (free tier: 100 queries/DAY,
   https://programmablesearchengine.google.com/ - create an engine, "Search the entire web", then
   https://developers.google.com/custom-search/v1/introduction for the API key). Good as a reliable top-up once
   Brave's monthly quota or the free engines below run dry for the day.
3. Otherwise free search engines through the `ddgs` library, tried one by one. These work well from office PCs,
   but datacenter IPs (cloud servers) get blocked often and unpredictably - which engine works varies run to run.
   "bing" is deliberately not in this list: this library doesn't actually have a Bing backend, and passing that
   name silently falls back to trying everything at once, which wastes requests and makes the log lie about
   which engine actually answered.
"""
import os
import time

import requests
from ddgs import DDGS

BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"
GOOGLE_CSE_URL = "https://www.googleapis.com/customsearch/v1"
# A short pause between trying successive free engines for the SAME query. Querying one engine right after another
# with no gap at all, on a cloud server's shared IP, is the same kind of rapid-fire request pattern that makes a
# single engine start refusing (see scraper/jobs.py's _current_gap/SEARCH_GAP, measured for *between* searches);
# trying every free engine back-to-back within one search is the same risk, just compressed into a few seconds.
FREE_ENGINE_GAP = float(os.environ.get("FREE_ENGINE_GAP_SECONDS", "1.5"))
FREE_ENGINES = ["duckduckgo", "yahoo", "brave", "google", "mojeek", "startpage"]


class SearchBlocked(Exception):
    """Every search option refused the request (common from cloud servers without a paid key)."""


def _brave(query: str, region: str, max_results: int, key: str) -> list[dict]:
    country = "ALL" if region == "wt-wt" else region.split("-")[0].upper()
    out = []
    for offset in range(0, 10):  # Brave allows up to 10 pages of 20 results
        r = requests.get(BRAVE_URL, headers={"X-Subscription-Token": key, "Accept": "application/json"},
                         params={"q": query, "count": 20, "offset": offset, "country": country}, timeout=30)
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


def web_search(query: str, region: str, max_results: int, say, engines=None) -> list[dict]:
    """`engines` (optional) chooses which free engines to try, in order. The default order is FREE_ENGINES."""
    key = os.environ.get("BRAVE_API_KEY")
    if key:
        try:
            hits = _brave(query, region, max_results, key)
            if hits:
                return hits
            say("  Brave API returned no results; trying other options")
        except requests.RequestException as e:
            say(f"  Brave API failed ({e}); trying other options")

    g_key, g_cx = os.environ.get("GOOGLE_CSE_KEY"), os.environ.get("GOOGLE_CSE_CX")
    if g_key and g_cx:
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
    merged, seen_href, failed, used = [], set(), [], []
    with DDGS() as ddgs:
        for i, engine in enumerate(engines or FREE_ENGINES):
            remaining = max_results - len(merged)
            if remaining <= 0:
                break
            if i:  # no pause before the very first engine - only between successive ones
                time.sleep(FREE_ENGINE_GAP)
            try:
                hits = ddgs.text(query, region=region, max_results=remaining, backend=engine) or []
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
