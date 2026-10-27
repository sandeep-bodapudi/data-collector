"""What a run can do right now: services, fields, setup gaps and whether a "Deep" run is possible.

Plan: docs/v2/free-paid-run-flow-plan.md (Phase 1). The new-run screen is built from run_options() so it only offers
what will work, and build_job_spec calls check_depth() so the server enforces the same rule the screen shows.

Nothing here makes a network call and nothing here returns a key - only whether one is present.

Vocabulary (see scraper/config.py SERVICES):
  Standard run - uses services whose cost is "free" or "key" (the person's own key, normally with a free quota).
  Deep run     - can additionally use a "paid" service (AI provider, Bright Data, Apify) the person has a key for.
"""
from __future__ import annotations

import os

from . import config
from .connection_health import is_checked, is_unhealthy

DEPTHS = ("standard", "deep")

# Services that make a run "Deep", per mode. Web, Places and Enrich have only the AI custom-fields step today; the
# PLANNED list names paid providers that are designed (plan section 4) but NOT built, so the screen can say
# "coming" without pretending they work.
DEEP_SERVICES = {
    "web": ["ai"],
    "places": ["apify_places", "ai"],
    "enrich": ["apify_contacts", "ai"],
    "social": ["brightdata", "apify"],
}
PLANNED = {
    "web": [{"id": "apify_crawler", "label": "Apify website crawler (for sites that block plain fetch)"}],
    "places": [],
    "enrich": [{"id": "apify_verify", "label": "Email verification"}],
    "social": [],
}

# Which vault entry proves a service has a key. Google CSE keeps its key in the environment, not the user vault.
VAULT_KEY = {"brave": "brave_api_key", "ai": "ai_api_key", "brightdata": "brightdata_api_key", "apify": "apify_api_key",
             "apify_places": "apify_api_key", "apify_contacts": "apify_api_key", "google_places": "google_places_api_key",
             "serper": "serper_api_key", "tavily": "tavily_api_key", "firecrawl": "firecrawl_api_key"}


def key_present(name: str, vault: dict) -> bool:
    """True when the person (or their org) has supplied what `name` needs. Never returns the key itself."""
    meta = config.SERVICES.get(name, {})
    if not meta.get("needs_key"):
        return True
    if name == "google_cse":
        return bool(os.environ.get("GOOGLE_CSE_KEY") and os.environ.get("GOOGLE_CSE_CX"))
    if name == "google_places":
        return bool(vault.get("google_places_api_key") or os.environ.get("GOOGLE_PLACES_API_KEY"))
    if name == "brave":
        if vault.get("brave_api_key"):
            return True
        try:
            from . import search
            return bool(search.brave_key_set())
        except Exception:
            return False
    if name == "ai":
        return bool(vault.get("ai_api_key")) or (vault.get("ai_provider") == "custom" and bool(vault.get("ai_base_url")))
    return bool(vault.get(VAULT_KEY.get(name, ""), ""))


def service_state(name: str, vault: dict) -> dict:
    """One service, one answer. state: disabled | no_key | unhealthy | unchecked | ready. 'usable' is what a run uses:
    unknown health counts as usable, a failed fresh check does not (preflight may still retry it as a last resort)."""
    meta = config.SERVICES[name]
    if not config.is_enabled(name):
        state, usable = "disabled", False
    elif meta.get("needs_key") and not key_present(name, vault):
        state, usable = "no_key", False
    elif is_unhealthy(meta.get("health_as", name)):
        state, usable = "unhealthy", False
    elif not is_checked(meta.get("health_as", name)):
        state, usable = "unchecked", True
    else:
        state, usable = "ready", True
    return {"id": name, "label": meta["label"], "kind": meta["kind"], "cost": meta.get("cost", "free"),
            "free": bool(meta.get("free")), "needs_key": bool(meta.get("needs_key")),
            "state": state, "usable": usable}


# ---------------------------------------------------------------- fields
# What each mode can fill and which source fills it. Written from the code: web fields come from scraper/extract.py
# (STANDARD_FIELDS, read from fetched pages), Places columns from scraper/places.py PLACE_COLUMNS. "page" means the
# field is read from a website the run fetched; map sources fill what OpenStreetMap-style data carries and only when
# the place is mapped with it.
_PAGE = ["page"]


def _fields_web() -> list[dict]:
    from .extract import STANDARD_FIELDS
    return [{"id": k, "label": v, "sources": _PAGE} for k, v in STANDARD_FIELDS.items()]


_MAP_BASIC = ["overpass", "photon", "nominatim"]
FIELDS_PLACES = [
    {"id": "name", "label": "Name", "sources": _MAP_BASIC + ["wikidata"]},
    {"id": "category", "label": "Category", "sources": ["overpass"]},
    {"id": "address", "label": "Address", "sources": _MAP_BASIC},
    {"id": "location", "label": "City, state, postcode, coordinates", "sources": _MAP_BASIC},
    {"id": "maps_link", "label": "Map link", "sources": _MAP_BASIC},
    {"id": "phone", "label": "Phone", "sources": ["overpass", "page", "apify_places", "google_places"]},
    {"id": "email", "label": "Email", "sources": ["page", "apify_places"]},
    {"id": "website", "label": "Website", "sources": ["overpass", "page", "apify_places", "google_places"]},
    {"id": "opening_hours", "label": "Opening hours", "sources": ["overpass", "apify_places", "google_places"]},
    {"id": "rating", "label": "Rating and review count", "sources": ["apify_places", "google_places"]},
]
FIELDS_ENRICH = [
    {"id": "website", "label": "Website", "sources": ["search", "page"]},
    {"id": "email", "label": "Email", "sources": ["page", "apify_contacts"]},
    {"id": "phone", "label": "Phone", "sources": ["page", "apify_contacts"]},
    {"id": "address", "label": "Address", "sources": ["page"]},
    {"id": "social", "label": "Social media links", "sources": ["page", "apify_contacts"]},
]
FIELDS_SOCIAL = [
    {"id": "profile", "label": "Profile details", "sources": ["brightdata", "apify"]},
]


def _field_available(f: dict, mode: str, usable: set[str], has_sources: bool) -> tuple[bool, str]:
    """Is at least one source for this field usable right now? Returns (available, reason when not)."""
    can_fetch = bool(usable & {"brave", "google_cse", "ddgs"}) or has_sources
    sources = []
    for s in f["sources"]:
        if s == "page":
            if (mode == "web" and can_fetch) or mode in ("places", "enrich"):
                # Places/Enrich reach a company's own site through a website lookup, which needs a search engine or a
                # guess from the name; both are part of the free chain.
                sources.append("page")
        elif s == "search":
            if usable & {"brave", "google_cse", "ddgs"} or mode == "enrich":
                sources.append("search")
        elif s in usable:
            sources.append(s)
    if sources:
        return True, ""
    if "page" in f["sources"] and mode == "web":
        return False, "needs a search engine or one of your directory sources"
    return False, "no usable source for this right now (check Connections)"


def fields_for(mode: str, usable: set[str], has_sources: bool = False) -> list[dict]:
    base = {"web": _fields_web, "places": lambda: FIELDS_PLACES, "enrich": lambda: FIELDS_ENRICH,
            "social": lambda: FIELDS_SOCIAL}.get(mode, lambda: [])()
    out = []
    for f in base:
        ok, why = _field_available(f, mode, usable, has_sources)
        out.append({"id": f["id"], "label": f["label"], "sources": f["sources"], "available": ok, "reason": why})
    return out


# ---------------------------------------------------------------- depth
def deep_providers(mode: str, vault: dict) -> list[dict]:
    """The paid services that could make a run of this mode Deep, each with its current state."""
    out = []
    for name in DEEP_SERVICES.get(mode, []):
        st = service_state(name, vault)
        if mode == "social" and not config.is_enabled("social"):
            st = {**st, "state": "disabled", "usable": False}
        out.append(st)
    return out


def check_depth(mode: str, depth: str, vault: dict) -> tuple[bool, str]:
    """(ok, reason). Standard is always possible. Deep needs at least one paid service that is enabled, keyed and not
    known-broken - otherwise the run would silently be a Standard run, which is the wrong thing to promise."""
    if depth != "deep":
        return True, ""
    provs = deep_providers(mode, vault)
    if any(p["usable"] for p in provs):
        return True, ""
    if not provs:
        return False, "Deep runs aren't available for this kind of run yet."
    reasons = {"disabled": "turned off by your admin", "no_key": "no key added", "unhealthy": "its last check failed"}
    why = "; ".join(f"{p['label']}: {reasons.get(p['state'], p['state'])}" for p in provs)
    return False, f"A Deep run needs a paid service. {why}."


# ---------------------------------------------------------------- the whole picture
def run_options(mode: str, depth: str, vault: dict, has_sources: bool = False, budget: dict | None = None) -> dict:
    """Everything the new-run screen needs for (mode, depth). Pure: reads settings and the health cache only.
    `budget` (optional) = {"set": bool, "limit_usd": float, "spent_usd": float}, the person's monthly paid-spend pot."""
    from . import apify_actors, jobs, places

    mode = mode if mode in DEEP_SERVICES else "web"
    depth = depth if depth in DEPTHS else "standard"
    names_by_kind = {k: [n for n, m in config.SERVICES.items() if m["kind"] == k]
                     for k in ("search", "places", "enrich", "ai", "social")}
    relevant = {"web": ["search", "ai"], "places": ["places", "search", "ai"], "enrich": ["search", "enrich", "ai"],
                "social": ["social", "ai"]}[mode]
    services = [service_state(n, vault) for k in relevant for n in names_by_kind[k]]
    usable = {s["id"] for s in services if s["usable"]}

    probe = {"mode": mode, "brave_key": vault.get("brave_api_key", ""),
             "search_keys": {n: vault.get(VAULT_KEY[n], "") for n in ("serper", "tavily", "firecrawl") if vault.get(VAULT_KEY[n])},
             "brightdata_key": vault.get("brightdata_api_key", ""), "apify_key": vault.get("apify_api_key", ""),
             "ai": {"provider": vault.get("ai_provider", ""), "key": vault.get("ai_api_key", ""),
                    "base_url": vault.get("ai_base_url", "")}, "items": []}
    chain = jobs.preflight(probe)
    deep_ok, deep_reason = check_depth(mode, "deep", vault)

    setup = []

    def row(id_, label, status, why="", href=""):
        setup.append({"id": id_, "label": label, "status": status, "why": why, "href": href})

    if mode in ("web", "enrich"):
        ok = bool(chain["search"])
        row("search", "Web search", "ready" if ok else "missing",
            "Using: " + ", ".join(chain["search"]) if ok else "Every search option is off or unavailable (Admin → Connections).",
            "" if ok else "#/settings/connections")
        if not key_present("brave", vault):
            if os.environ.get("RENDER"):
                # On the hosted app the free engines usually refuse the server's address outright (checked in
                # production: 0 results for every search), so the key is not just "faster" there.
                row("brave", "Brave Search key (needed on the hosted app)", "recommended",
                    "Free engines usually refuse cloud servers, so web searches here often find nothing without it. "
                    "Free to get.", "#/settings/keys")
            else:
                row("brave", "Brave Search key (faster, optional)", "optional",
                    "Free to get. Without it the run uses free engines, which are slower.", "#/settings/keys")
    if mode == "places" and not key_present("google_places", vault):
        row("google_places", "Google Maps API key (best map data)", "optional",
            "Official Google Maps listings with phone, website, hours and rating. Google gives a free monthly allowance.",
            "#/settings/keys")
    if mode == "places":
        ok = bool(chain["places"])
        row("places", "Map sources", "ready" if ok else "missing",
            "Using: " + ", ".join(chain["places"]) if ok else "Every map source is off or unavailable (Admin → Connections).")
    if mode == "social":
        ok = bool(chain.get("social"))
        row("social", "Social providers", "ready" if ok else ("missing" if depth == "deep" else "optional"),
            "Using: " + ", ".join(chain["social"]) if ok else "Free public lookup only until a Bright Data or Apify key is added.",
            "" if ok else "#/settings/keys")
    if mode != "social":
        ai_ok = key_present("ai", vault) and config.is_enabled("ai")
        row("ai", "AI connector", "ready" if ai_ok else ("missing" if depth == "deep" and not deep_ok else "optional"),
            "Fills any custom detail you type." if ai_ok else "Add your own AI key to fill custom columns.",
            "" if ai_ok else "#/settings/ai")

    # Deep: one row per paid provider other than AI (AI has its own row above), so "Deep needs a paid service" is never
    # a mystery - each provider says what it is missing and where to fix it.
    if depth == "deep":
        provs = deep_providers(mode, vault)
        any_ok = any(p["usable"] for p in provs)
        why_not = {"disabled": ("Turned off by your admin (Admin \u2192 Connections).", "#/admin"),
                   "no_key": ("Add your own token in Settings.", "#/settings/keys"),
                   "unhealthy": ("Its last check failed; try again shortly.", "")}
        for p in provs:
            if p["id"] == "ai" or (mode == "social" and p["id"] in ("brightdata", "apify")):
                continue
            if p["usable"]:
                row(p["id"], p["label"], "ready", "Will be used for this run.")
            else:
                why, href = why_not.get(p["state"], ("Not available right now.", ""))
                row(p["id"], p["label"], "optional" if any_ok else "missing", why, href)

    # Will this run buy data from Apify? Then it needs the monthly budget, exactly as build_job_spec enforces.
    apify_ids = [p["id"] for p in deep_providers(mode, vault) if p["usable"] and p["id"].startswith("apify_")]
    paid_will_run = depth == "deep" and bool(apify_ids)
    budget = budget or {}
    if depth == "deep" and apify_ids:
        left = max(float(budget.get("limit_usd", 0)) - float(budget.get("spent_usd", 0)), 0.0)
        row("budget", "Monthly budget for paid data", "ready" if budget.get("set") else "missing",
            f"${left:.2f} left this month." if budget.get("set") else "Ask your admin to set one in Admin \u2192 Connections.",
            "" if budget.get("set") else "#/admin")
    blockers = []
    if depth == "deep" and not deep_ok:
        blockers.append(deep_reason)
    if mode == "web" and not chain["search"]:
        blockers.append("Every search option is off or unavailable.")
    if mode == "places" and not chain["places"]:
        blockers.append("Every map source is off or unavailable.")
    if paid_will_run and not budget.get("set"):
        blockers.append("Paid data needs a monthly budget first.")

    out = {
        "mode": mode, "depth": depth,
        "blockers": blockers,
        "paid_will_run": paid_will_run,
        "pricing": {"place_usd": apify_actors.PLACE_USD, "place_contacts_usd": apify_actors.PLACE_CONTACTS_USD,
                    "start_usd": apify_actors.START_USD, "contact_page_usd": apify_actors.CONTACT_PAGE_USD,
                    "contact_pages": apify_actors.CONTACT_PAGES_PER_SITE, "max_places_per_call": apify_actors.MAX_PLACES_PER_CALL},
        "budget": {"set": bool(budget.get("set")), "limit_usd": budget.get("limit_usd", 0), "spent_usd": budget.get("spent_usd", 0)},
        "services": services,
        "chain": {"search": chain["search"], "places": chain["places"], "social": chain.get("social", [])},
        "fields": fields_for(mode, usable, has_sources),
        "setup": setup,
        "deep": {"possible": deep_ok, "reason": "" if deep_ok else deep_reason,
                 "providers": deep_providers(mode, vault), "planned": PLANNED.get(mode, [])},
        "can_start": not blockers,
        "notes": chain.get("notes", []),
    }
    if mode == "places":
        out["categories"] = list(places.CATEGORIES)
    out["regions"] = [{"id": k, "label": v} for k, v in jobs.REGIONS.items()]
    return out
