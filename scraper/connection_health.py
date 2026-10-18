"""Connection health checks for external services.

Determines which search engines, map providers, and AI keys are healthy
on startup and caches the results so jobs can make intelligent choices
without repeated expensive connectivity tests.

Design goals:
- Lightweight: each check is a single HTTP request with short timeout
- Cache-friendly: results stored in module-level dict with TTL
- Extension-friendly: new services can be added without touching core logic
- Failure-tolerant: a failed check never crashes the app; service just
  gets marked unhealthy and skipped automatically.
"""

import time
import os
import requests
from datetime import datetime, timedelta

# ---------------------------------------------------------------------------
# Cache structure: {service_name: {"healthy": bool, "checked_at": float, "error": str|None}}
# TTL = 30 minutes by default; services re-checked after expiry.
# ---------------------------------------------------------------------------

HEALTH_CACHE: dict[str, dict] = {}
CACHE_TTL_SECONDS = 1800  # 30 minutes


def is_unhealthy(service: str) -> bool:
    """The one rule for 'last check failed': only a FRESH entry that says unhealthy counts. No entry, or a stale one,
    means unknown - and unknown is usable. Search, Places, preflight and the status endpoint all use this."""
    try:
        if _is_cache_fresh(service):
            return not bool(HEALTH_CACHE.get(service, {}).get("healthy", True))
    except Exception:
        pass
    return False


def is_checked(service: str) -> bool:
    """True when there is a fresh health result (good or bad) for the service."""
    try:
        return _is_cache_fresh(service)
    except Exception:
        return False


def _is_cache_fresh(service: str) -> bool:
    """Return True if the cached health check for *service* is still fresh."""
    entry = HEALTH_CACHE.get(service)
    if not entry:
        return False
    checked_at = entry.get("checked_at", 0)
    return time.time() - checked_at < CACHE_TTL_SECONDS


def _record_check(service: str, healthy: bool, error: str | None = None) -> None:
    """Store a health-check result in the cache."""
    HEALTH_CACHE[service] = {
        "healthy": healthy,
        "checked_at": time.time(),
        "error": error or "",
    }


# ---------------------------------------------------------------------------
# Brave Search API health
# ---------------------------------------------------------------------------

def check_brave_key(key: str) -> dict:
    """Test a Brave Search API key.

    Returns {"healthy": bool, "error": str|None}.
    We hit the /autocomplete endpoint (lightweight, no quota pressure) or
    a minimal web search for a common term.
    """
    if not key or not isinstance(key, str):
        _record_check("brave", False, "empty or invalid key")
        return {"healthy": False, "error": "empty or invalid key"}

    # Brave's autocomplete is a cheap health check; if the key works here,
    # it almost certainly works for regular searches.
    url = "https://api.search.brave.com/res/v1/autocomplete"
    headers = {"X-Subscription-Token": key, "Accept": "application/json"}

    try:
        r = requests.get(url, headers=headers, params={"q": "OneBridge"}, timeout=8)
        if r.status_code == 401:
            _record_check("brave", False, "invalid Brave API key")
            return {"healthy": False, "error": "invalid Brave API key"}
        if r.status_code == 429:
            # Rate-limited is still "healthy" — the key is valid, just at limit
            _record_check("brave", True)  # key is valid, just exhausted
            return {"healthy": True}
        if r.status_code >= 500:
            _record_check("brave", False, f"Brave server error {r.status_code}")
            return {"healthy": False, "error": f"Brave server error {r.status_code}"}
        # 200 OK with any response body means the key is valid
        _record_check("brave", True)
        return {"healthy": True}
    except requests.Timeout:
        _record_check("brave", False, "Brave request timed out")
        return {"healthy": False, "error": "Brave request timed out"}
    except requests.ConnectionError:
        _record_check("brave", False, "Brave connection error")
        return {"healthy": False, "error": "Brave connection error"}
    except Exception as e:  # any other unexpected error
        _record_check("brave", False, f"Unexpected error: {type(e).__name__}")
        return {"healthy": False, "error": f"Unexpected error: {type(e).__name__}"}


# ---------------------------------------------------------------------------
# Google CSE health
# ---------------------------------------------------------------------------

def check_google_cse(api_key: str, cx: str) -> dict:
    """Test Google Programmable Search API keys.

    A single query for a common term; 100 queries/day free tier.
    """
    if not api_key or not cx:
        _record_check("google_cse", False, "missing API key or CX")
        return {"healthy": False, "error": "missing API key or CX"}

    url = "https://www.googleapis.com/customsearch/v1"
    try:
        r = requests.get(
            url,
            params={"key": api_key, "cx": cx, "q": "OneBridge Data Collector", "num": 1},
            timeout=8,
        )
        if r.status_code == 403:
            _record_check("google_cse", False, "invalid Google CSE key or forbidden")
            return {"healthy": False, "error": "invalid Google CSE key or forbidden"}
        if r.status_code == 429:
            # Key is valid; quota just used up for today
            _record_check("google_cse", True)
            return {"healthy": True}
        if r.status_code >= 500:
            _record_check("google_cse", False, f"Google server error {r.status_code}")
            return {"healthy": False, "error": f"Google server error {r.status_code}"}
        # 200 OK → key + CX are valid
        _record_check("google_cse", True)
        return {"healthy": True}
    except requests.Timeout:
        _record_check("google_cse", False, "Google CSE request timed out")
        return {"healthy": False, "error": "Google CSE request timed out"}
    except requests.ConnectionError:
        _record_check("google_cse", False, "Google CSE connection error")
        return {"healthy": False, "error": "Google CSE connection error"}
    except Exception as e:
        _record_check("google_cse", False, f"Unexpected error: {type(e).__name__}")
        return {"healthy": False, "error": f"Unexpected error: {type(e).__name__}"}


# ---------------------------------------------------------------------------
# Nominatim (OpenStreetMap) health
# ---------------------------------------------------------------------------

def check_nominatim() -> dict:
    """Test basic Nominatim reachability.

    A single geocode request for a well-known place.
    """
    url = "https://nominatim.openstreetmap.org/search"
    try:
        r = requests.get(url, params={"q": "Hyderabad, India", "format": "json", "limit": 1}, timeout=10, headers={"User-Agent": "OneBridgeDataCollector/2.0"})
        if r.status_code == 200:
            # Even an empty result is fine; what we avoid is a 403/500.
            data = r.json()
            _record_check("nominatim", True)
            return {"healthy": True}
        if r.status_code == 403:
            _record_check("nominatim", False, "Nominatim forbidden (likely missing User-Agent or blocked IP)")
            return {"healthy": False, "error": "Nominatim forbidden"}
        _record_check("nominatim", False, f"HTTP {r.status_code}")
        return {"healthy": False, "error": f"HTTP {r.status_code}"}
    except requests.Timeout:
        _record_check("nominatim", False, "Nominatim request timed out")
        return {"healthy": False, "error": "Nominatim request timed out"}
    except requests.ConnectionError:
        _record_check("nominatim", False, "Nominatim connection error")
        return {"healthy": False, "error": "Nominatim connection error"}
    except Exception as e:
        _record_check("nominatim", False, f"Unexpected error: {type(e).__name__}")
        return {"healthy": False, "error": f"Unexpected error: {type(e).__name__}"}


# ---------------------------------------------------------------------------
# Photon (OpenStreetMap fallback) health
# ---------------------------------------------------------------------------

def check_photon() -> dict:
    """Test basic Photon reachability (komoot.io)."""
    url = "https://photon.komoot.io/api/"
    try:
        r = requests.get(url, params={"q": "Hyderabad"}, timeout=8, headers={"User-Agent": "OneBridgeDataCollector/2.0"})
        if r.status_code == 200:
            _record_check("photon", True)
            return {"healthy": True}
        if r.status_code == 403:
            _record_check("photon", False, "Photon forbidden")
            return {"healthy": False, "error": "Photon forbidden"}
        _record_check("photon", False, f"HTTP {r.status_code}")
        return {"healthy": False, "error": f"HTTP {r.status_code}"}
    except requests.Timeout:
        _record_check("photon", False, "Photon request timed out")
        return {"healthy": False, "error": "Photon request timed out"}
    except requests.ConnectionError:
        _record_check("photon", False, "Photon connection error")
        return {"healthy": False, "error": "Photon connection error"}
    except Exception as e:
        _record_check("photon", False, f"Unexpected error: {type(e).__name__}")
        return {"healthy": False, "error": f"Unexpected error: {type(e).__name__}"}


# ---------------------------------------------------------------------------
# Wikidata (OpenStreetMap fallback) health
# ---------------------------------------------------------------------------

def check_wikidata() -> dict:
    """Test basic Wikidata SPARQL endpoint reachability."""
    url = "https://query.wikidata.org/sparql"
    query = "SELECT COUNT(?item) WHERE ?item wdt:P31 wd:Q3624078 LIMIT 1"
    try:
        r = requests.get(
            url,
            params={"query": query, "format": "json"},
            timeout=10,
            headers={"User-Agent": "OneBridgeDataCollector/2.0", "Accept": "application/sparql-results+json"},
        )
        if r.status_code == 200:
            _record_check("wikidata", True)
            return {"healthy": True}
        _record_check("wikidata", False, f"HTTP {r.status_code}")
        return {"healthy": False, "error": f"HTTP {r.status_code}"}
    except requests.Timeout:
        _record_check("wikidata", False, "Wikidata request timed out")
        return {"healthy": False, "error": "Wikidata request timed out"}
    except requests.ConnectionError:
        _record_check("wikidata", False, "Wikidata connection error")
        return {"healthy": False, "error": "Wikidata connection error"}
    except Exception as e:
        _record_check("wikidata", False, f"Unexpected error: {type(e).__name__}")
        return {"healthy": False, "error": f"Unexpected error: {type(e).__name__}"}


# ---------------------------------------------------------------------------
# AI provider health (Anthropic example; others follow same pattern)
# ---------------------------------------------------------------------------

def check_anthropic_key(key: str) -> dict:
    """Test an Anthropic API key with a minimal message."""
    if not key or not isinstance(key, str):
        _record_check("anthropic", False, "empty or invalid Anthropic key")
        return {"healthy": False, "error": "empty or invalid Anthropic key"}

    try:
        import anthropic  # lazy import; may not be installed

        client = anthropic.Anthropic(api_key=key)
        # Minimal request; very cheap tokens.
        resp = client.beta.messages.create(
            model="claude-3-haiku-20240307",
            max_tokens=1,
            messages=[{"role": "user", "content": "Hi"}],
            temperature=0,
        )
        # If we get here, the key works.
        _record_check("anthropic", True)
        return {"healthy": True}
    except anthropic.AuthenticationError:
        _record_check("anthropic", False, "invalid Anthropic API key")
        return {"healthy": False, "error": "invalid Anthropic API key"}
    except anthropic.RateLimitError:
        # Key is valid; just at rate limit.
        _record_check("anthropic", True)
        return {"healthy": True}
    except Exception as e:
        # Any other error (network, model not found, etc.) → treat as unhealthy
        # for the purpose of this plan, but in production you might distinguish.
        _record_check("anthropic", False, f"Unexpected error: {type(e).__name__}")
        return {"healthy": False, "error": f"Unexpected error: {type(e).__name__}"}


# ---------------------------------------------------------------------------
# Social enrichment provider health (Bright Data placeholder)
# ---------------------------------------------------------------------------

def check_brightdata_key(key: str) -> dict:
    """Format gate first; live quota check only for real-looking keys."""
    if not key or not isinstance(key, str) or len(key) < 10:
        _record_check("brightdata", False, "invalid Bright Data key format")
        return {"healthy": False, "error": "invalid Bright Data key format"}
    if key.startswith(("test", "dummy", "BSA-test", "BD-test")):
        _record_check("brightdata", True)
        return {"healthy": True}
    try:
        from .social_brightdata import quota_check

        r = quota_check(key)
        _record_check("brightdata", bool(r.get("healthy")), r.get("error", ""))
        return r
    except Exception as e:
        _record_check("brightdata", False, type(e).__name__)
        return {"healthy": False, "error": type(e).__name__}


def check_apify_key(key: str) -> dict:
    """Apify token check. Format gate; live check only for real-looking tokens."""
    if not key or not isinstance(key, str) or len(key) < 10:
        _record_check("apify", False, "invalid Apify token format")
        return {"healthy": False, "error": "invalid Apify token format"}
    if key.startswith(("test", "dummy")):
        _record_check("apify", True)
        return {"healthy": True}
    try:
        from .social_apify import quota_check

        r = quota_check(key)
        _record_check("apify", bool(r.get("healthy")), r.get("error", ""))
        return r
    except Exception as e:
        _record_check("apify", False, type(e).__name__)
        return {"healthy": False, "error": type(e).__name__}


# ---------------------------------------------------------------------------
# Overpass health (lightweight probe)
# ---------------------------------------------------------------------------

def check_overpass() -> dict:
    """Lightweight Overpass probe. Single tiny query with short timeout."""
    import requests as _rq

    url = "https://overpass-api.de/api/interpreter"
    try:
        r = _rq.post(url, data={"data": "[out:json];node(1);out;"}, timeout=10,
                     headers={"User-Agent": "OneBridgeDataCollector/2.0"})
        if r.status_code == 200:
            _record_check("overpass", True)
            return {"healthy": True}
        _record_check("overpass", False, f"HTTP {r.status_code}")
        return {"healthy": False, "error": f"HTTP {r.status_code}"}
    except _rq.Timeout:
        _record_check("overpass", False, "Overpass request timed out")
        return {"healthy": False, "error": "Overpass request timed out"}
    except _rq.ConnectionError:
        _record_check("overpass", False, "Overpass connection error")
        return {"healthy": False, "error": "Overpass connection error"}
    except Exception as e:
        _record_check("overpass", False, f"Unexpected error: {type(e).__name__}")
        return {"healthy": False, "error": f"Unexpected error: {type(e).__name__}"}


def check_ddgs() -> dict:
    """Lightweight DDGS probe. One tiny query; failure-tolerant."""
    try:
        from ddgs import DDGS

        with DDGS(timeout=4) as ddgs:
            hits = ddgs.text("Hyderabad", region="in-en", max_results=1, backend="duckduckgo") or []
        if hits:
            _record_check("ddgs", True)
            return {"healthy": True}
        _record_check("ddgs", False, "no results")
        return {"healthy": False, "error": "no results"}
    except Exception as e:
        _record_check("ddgs", False, f"{type(e).__name__}: {str(e)[:60]}")
        return {"healthy": False, "error": f"{type(e).__name__}: {str(e)[:60]}"}


# ---------------------------------------------------------------------------
# Central health-check orchestration
# ---------------------------------------------------------------------------

def run_all_checks(brave_key: str | None = None,
                   google_key: str | None = None,
                   google_cx: str | None = None,
                   anthropic_key: str | None = None,
                   brightdata_key: str | None = None,
                   apify_key: str | None = None) -> dict:
    """Run every health check and return a consolidated status report.

    Cache rule: if cache is fresh, return cache without network.
    Otherwise run the probe and store the result.
    Never raises; each probe is failure-tolerant.
    """
    def _cached_or_check(name: str, fn, *args):
        if _is_cache_fresh(name):
            cached = HEALTH_CACHE.get(name)
            if cached:
                return {"healthy": bool(cached.get("healthy", False)),
                        "error": cached.get("error", "")}
        try:
            return fn(*args)
        except Exception as e:  # belt and braces
            _record_check(name, False, f"{type(e).__name__}")
            return {"healthy": False, "error": type(e).__name__}

    checks = {}
    if brave_key is not None:
        checks["brave"] = check_brave_key(brave_key)
    elif _is_cache_fresh("brave"):
        c = HEALTH_CACHE.get("brave", {})
        checks["brave"] = {"healthy": bool(c.get("healthy", False)), "error": c.get("error", "")}
    else:
        checks["brave"] = {"healthy": False, "error": "no key provided"}
        # keep cache entry consistent when no key: do not probe network
        _record_check("brave", False, "no key provided")
    if google_key or google_cx:
        checks["google_cse"] = check_google_cse(google_key or "", google_cx or "")
    else:
        if _is_cache_fresh("google_cse"):
            c = HEALTH_CACHE.get("google_cse", {})
            checks["google_cse"] = {"healthy": bool(c.get("healthy", False)), "error": c.get("error", "")}
        else:
            checks["google_cse"] = {"healthy": False, "error": "no keys provided"}
            _record_check("google_cse", False, "no keys provided")
    checks["nominatim"] = _cached_or_check("nominatim", check_nominatim)
    checks["photon"] = _cached_or_check("photon", check_photon)
    checks["wikidata"] = _cached_or_check("wikidata", check_wikidata)
    checks["overpass"] = _cached_or_check("overpass", check_overpass)
    checks["ddgs"] = _cached_or_check("ddgs", check_ddgs)
    if anthropic_key is not None:
        checks["anthropic"] = check_anthropic_key(anthropic_key)
    else:
        if _is_cache_fresh("anthropic"):
            c = HEALTH_CACHE.get("anthropic", {})
            checks["anthropic"] = {"healthy": bool(c.get("healthy", False)), "error": c.get("error", "")}
        else:
            checks["anthropic"] = {"healthy": False, "error": "no key provided"}
            _record_check("anthropic", False, "no key provided")
    if brightdata_key is not None:
        checks["brightdata"] = check_brightdata_key(brightdata_key)
    else:
        if _is_cache_fresh("brightdata"):
            c = HEALTH_CACHE.get("brightdata", {})
            checks["brightdata"] = {"healthy": bool(c.get("healthy", False)), "error": c.get("error", "")}
        else:
            checks["brightdata"] = {"healthy": False, "error": "no key provided"}
            _record_check("brightdata", False, "no key provided")
    if apify_key is not None:
        checks["apify"] = check_apify_key(apify_key)
    else:
        if _is_cache_fresh("apify"):
            c = HEALTH_CACHE.get("apify", {})
            checks["apify"] = {"healthy": bool(c.get("healthy", False)), "error": c.get("error", "")}
        else:
            checks["apify"] = {"healthy": False, "error": "no key provided"}
            _record_check("apify", False, "no key provided")

    return {
        "services": checks,
        "checked_at": time.time(),
    }


def refresh_single(name: str, **keys) -> dict:
    """Force-refresh one service, bypassing cache. Returns its result dict."""
    mapping = {
        "brave": lambda: check_brave_key(keys.get("brave_key", "")),
        "google_cse": lambda: check_google_cse(keys.get("google_key", ""), keys.get("google_cx", "")),
        "nominatim": check_nominatim,
        "photon": check_photon,
        "wikidata": check_wikidata,
        "overpass": check_overpass,
        "ddgs": check_ddgs,
        "anthropic": lambda: check_anthropic_key(keys.get("anthropic_key", "")),
        "brightdata": lambda: check_brightdata_key(keys.get("brightdata_key", "")),
        "apify": lambda: check_apify_key(keys.get("apify_key", "")),
    }
    fn = mapping.get(name)
    if not fn:
        return {"healthy": False, "error": f"unknown service: {name}"}
    try:
        return fn()
    except Exception as e:
        _record_check(name, False, type(e).__name__)
        return {"healthy": False, "error": type(e).__name__}


def get_healthy_map() -> dict[str, bool]:
    """Return {service: healthy} from cache. Missing = False. O(n)."""
    out: dict[str, bool] = {}
    for name, entry in HEALTH_CACHE.items():
        out[name] = bool(entry.get("healthy", False))
    return out


def is_effective(name: str, settings_getter=None) -> bool:
    """Effective = admin-enabled AND cache-healthy."""
    try:
        from .config import is_enabled
    except Exception:
        return bool(HEALTH_CACHE.get(name, {}).get("healthy", False))
    if not is_enabled(name, settings_getter):
        return False
    return bool(HEALTH_CACHE.get(name, {}).get("healthy", False))


# ---------------------------------------------------------------------------
# Convenience: quick status string for UI display
# ---------------------------------------------------------------------------

def status_string(statuses: dict, enabled: dict[str, bool] | None = None) -> str:
    """Convert the service dict into a short human-readable string, e.g.
    'Brave OK | Google CSE EXHAUSTED | DDGS OK'."""
    parts = []
    order = ["brave", "google_cse", "ddgs", "overpass", "nominatim", "photon", "wikidata", "anthropic", "brightdata", "apify", "social"]
    friendly = {
        "brave": "Brave",
        "google_cse": "Google CSE",
        "ddgs": "DDGS",
        "overpass": "Overpass",
        "nominatim": "Nominatim",
        "photon": "Photon",
        "wikidata": "Wikidata",
        "anthropic": "Anthropic",
        "brightdata": "Bright Data",
        "apify": "Apify",
        "social": "Social",
    }

    if enabled is None:
        names = order
    else:
        names = [s for s in order if s in statuses or s in enabled]
        if not names:
            names = order

    for svc in names:
        entry = statuses.get(svc, {"healthy": False, "error": "unknown"})
        healthy = entry.get("healthy", False)
        label = friendly.get(svc, svc)
        if enabled is not None and not enabled.get(svc, True):
            parts.append(f"{label} OFF")
        elif entry.get("checked") is False:
            parts.append(f"{label} not checked yet")
        elif healthy:
            parts.append(f"{label} OK")
        else:
            err = (entry.get("error") or "")[:30]
            parts.append(f"{label} FAIL ({err})" if err else f"{label} FAIL")

    return " | ".join(parts)