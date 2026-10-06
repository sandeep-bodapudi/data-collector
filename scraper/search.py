"""Web search with fallbacks.

1. Brave Search API, if BRAVE_API_KEY is set: official and reliable from cloud servers
   (free tier: https://brave.com/search/api/).
2. Otherwise free search engines through the `ddgs` library, tried one by one. These work well
   from office PCs, but some engines refuse requests from cloud servers.
"""
import os

import requests
from ddgs import DDGS

BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"
FREE_ENGINES = ["duckduckgo", "bing", "brave", "mojeek", "yahoo", "startpage", "google"]


class SearchBlocked(Exception):
    """Every search engine refused the request (common from cloud servers)."""


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


def web_search(query: str, region: str, max_results: int, say, engines=None) -> list[dict]:
    """`engines` (optional) chooses which free engines to try, in order. The default order is FREE_ENGINES."""
    key = os.environ.get("BRAVE_API_KEY")
    if key:
        try:
            hits = _brave(query, region, max_results, key)
            if hits:
                return hits
            say("  Brave API returned no results; trying free engines")
        except requests.RequestException as e:
            say(f"  Brave API failed ({e}); trying free engines")

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
                    say(f"  used {engine} (blocked: {', '.join(f.split(':')[0] for f in failed)})")
                return hits
            failed.append(f"{engine}: no results")
    say("  all search engines refused: " + "; ".join(failed))
    if all(not f.endswith("no results") for f in failed):
        raise SearchBlocked("The search engines refused the request. Try again later, or ask your admin to add a Brave Search API key.")
    return []
