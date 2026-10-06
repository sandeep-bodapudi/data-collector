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

import requests
from ddgs import DDGS

BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"
GOOGLE_CSE_URL = "https://www.googleapis.com/customsearch/v1"
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

    failed = []
    with DDGS() as ddgs:
        for engine in (engines or FREE_ENGINES):
            try:
                hits = ddgs.text(query, region=region, max_results=max_results, backend=engine) or []
            except Exception as e:  # each engine fails in its own way (blocked, rate limited, no results)
                failed.append(f"{engine}: no results" if "no results" in str(e).lower() else f"{engine}: {str(e)[:60]}")
                continue
            if hits:
                if failed:
                    say(f"  used {engine} (no results from: {', '.join(f.split(':')[0] for f in failed)})")
                return hits
            failed.append(f"{engine}: no results")
    say("  all search options refused: " + "; ".join(failed))
    if all(not f.endswith("no results") for f in failed):
        raise SearchBlocked("The search engines refused the request. Try again later, or ask your admin to add a "
                            "free Brave or Google search key (see the README).")
    return []
