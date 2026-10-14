"""Central connection registry — single source of truth for Phase 2.

No other module hardcodes service names. Admin enable flags live in
AppSetting as conn_<name> = "1" / "0" (default "1").
Health (network reality) lives in scraper/connection_health.py cache.
Effective = enabled AND healthy.
"""
from __future__ import annotations

SERVICES: dict[str, dict] = {
    "brave": {"label": "Brave Search", "kind": "search", "needs_key": True, "free": True, "default": "1"},
    "google_cse": {"label": "Google CSE", "kind": "search", "needs_key": True, "free": True, "default": "1"},
    "ddgs": {"label": "Free engines (DDGS)", "kind": "search", "needs_key": False, "free": True, "default": "1"},
    "overpass": {"label": "Overpass", "kind": "places", "needs_key": False, "free": True, "default": "1"},
    "photon": {"label": "Photon", "kind": "places", "needs_key": False, "free": True, "default": "1"},
    "nominatim": {"label": "Nominatim", "kind": "places", "needs_key": False, "free": True, "default": "1"},
    "wikidata": {"label": "Wikidata", "kind": "places", "needs_key": False, "free": True, "default": "1"},
    "ai": {"label": "AI custom fields", "kind": "ai", "needs_key": True, "free": False, "default": "1"},
    "social": {"label": "Social enrichment (master)", "kind": "social", "needs_key": False, "free": False, "default": "0"},
    "brightdata": {"label": "Bright Data (social)", "kind": "social", "needs_key": True, "free": True, "default": "0"},
    "apify": {"label": "Apify actors (social)", "kind": "social", "needs_key": True, "free": False, "default": "0"},
}

FREE_SEARCH_ORDER = ["brave", "google_cse", "ddgs"]
FREE_PLACES_ORDER = ["overpass", "photon", "nominatim", "wikidata"]
SOCIAL_PROVIDER_ORDER = ["brightdata", "apify"]

# Unit economics (USD per successfully filled paid profile). Env-overridable.
SOCIAL_UNIT_USD = {
    "brightdata": float(__import__("os").environ.get("SOCIAL_BRIGHTDATA_USD_PER_PROFILE", "0.0")),
    "apify": float(__import__("os").environ.get("SOCIAL_APIFY_USD_PER_PROFILE", "0.0035")),
}
USD_TO_INR = float(__import__("os").environ.get("USD_TO_INR", "96.0"))
MAX_SOCIAL_PER_RUN = int(__import__("os").environ.get("SOCIAL_MAX_PER_RUN", "50"))


def is_enabled(name: str, settings_getter=None) -> bool:
    """Return True if admin has enabled `name`. Default True.

    settings_getter: optional callable(key, default) — defaults to AppSetting.get
    lazily to avoid import cycles at module load.
    """
    if name not in SERVICES:
        return False
    default = SERVICES[name].get("default", "1")
    if settings_getter is None:
        try:
            from models import AppSetting

            settings_getter = AppSetting.get
        except Exception:
            return default == "1"
    try:
        return settings_getter(f"conn_{name}", default) == "1"
    except Exception:
        return default == "1"


def effective_services(names: list[str], healthy: dict[str, bool], settings_getter=None) -> list[str]:
    """Filter ordered `names` to enabled AND healthy. O(n)."""
    out = []
    for n in names:
        if n not in SERVICES:
            continue
        if not is_enabled(n, settings_getter):
            continue
        if healthy.get(n) is False:
            continue
        out.append(n)
    return out
