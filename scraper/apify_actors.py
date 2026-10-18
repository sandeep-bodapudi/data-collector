"""Paid data through the person's own Apify account: Google Maps places and website contact details.

Phase 2 of docs/v2/free-paid-run-flow-plan.md. Two actors, each described once here (id, how to build its input, how to
turn its output into our columns, what it costs), so adding a third is one more entry, not new plumbing:

  places   compass/crawler-google-places   Google Maps listings: phone, website, address, hours, rating, optionally emails
  contacts vdrmota/contact-info-scraper     emails, phones and social links read from a company's own website

Input fields and prices below were read from each actor's Apify Store page on 2026-10-15 (pay-per-event, "FREE" plan
tier: $0.004 per place, +$0.002 per place with the contacts add-on; $0.002 per page for the contact scraper). Output
field names follow the actors' documented dataset items but were NOT confirmed against a live run - every normaliser
is therefore tolerant: a missing or oddly-typed field becomes an empty cell, never an error. Prices are env-overridable
because actors re-price; the estimate is a worst case (the FREE tier is the dearest).

Never logs or returns the token. Failure-tolerant: ActorError carries a message that is safe to show.
"""
from __future__ import annotations

import os
import re
from urllib.parse import urlparse

import requests

API = "https://api.apify.com/v2"
PLACES_ACTOR = os.environ.get("APIFY_PLACES_ACTOR", "compass/crawler-google-places")
CONTACTS_ACTOR = os.environ.get("APIFY_CONTACTS_ACTOR", "vdrmota/contact-info-scraper")

PLACE_USD = float(os.environ.get("APIFY_PLACE_USD", "0.004"))
PLACE_CONTACTS_USD = float(os.environ.get("APIFY_PLACE_CONTACTS_USD", "0.002"))
CONTACT_PAGE_USD = float(os.environ.get("APIFY_CONTACT_PAGE_USD", "0.002"))
START_USD = float(os.environ.get("APIFY_START_USD", "0.001"))  # per run, per GB of memory (rounded up to 1)

MAX_PLACES_PER_CALL = 500
MAX_PAID_URLS_PER_RUN = int(os.environ.get("APIFY_MAX_URLS_PER_RUN", "100"))
CONTACT_PAGES_PER_SITE = 5
CONTACT_BATCH = 20


class ActorError(Exception):
    """A problem running an actor, in words that are safe to show the person."""


def _clean(v) -> str:
    return re.sub(r"\s+", " ", str(v)).strip() if v not in (None, "") else ""


def _first(*vals) -> str:
    for v in vals:
        s = _clean(v)
        if s:
            return s
    return ""


def _as_list(v) -> list:
    if v is None or v == "":
        return []
    return v if isinstance(v, list) else [v]


# ---------------------------------------------------------------- the one HTTP call
def run_actor(actor: str, token: str, payload: dict, timeout: int = 120, _post=None) -> list[dict]:
    """Run an actor synchronously and return its dataset items. Raises ActorError, never leaks the token."""
    if not token or len(token) < 10:
        raise ActorError("No Apify token. Add yours in Settings.")
    post = _post or requests.post
    actor_id = actor.replace("/", "~")
    try:
        r = post(f"{API}/acts/{actor_id}/run-sync-get-dataset-items",
                 headers={"Authorization": f"Bearer {token}"},
                 params={"timeout": timeout}, json=payload, timeout=timeout + 15)
    except requests.Timeout:
        raise ActorError("Apify took too long to answer.")
    except requests.ConnectionError:
        raise ActorError("Could not reach Apify.")
    code = r.status_code
    if code in (401, 403):
        raise ActorError("Apify refused the token. Check it in Settings.")
    if code == 402:
        raise ActorError("Your Apify account is out of credit or at its usage limit.")
    if code == 404:
        raise ActorError(f"Apify has no actor called {actor}.")
    if code == 429:
        raise ActorError("Apify is rate limiting this token. Try again in a minute.")
    if code >= 400:
        raise ActorError(f"Apify answered {code}.")
    try:
        data = r.json()
    except ValueError:
        raise ActorError("Apify returned something that isn't data.")
    if not isinstance(data, list):
        raise ActorError("Apify returned an unexpected result.")
    return [x for x in data if isinstance(x, dict)]


# ---------------------------------------------------------------- places
def search_term(category: str, name_filter: str = "") -> str:
    """What to type into Google Maps for one of our categories."""
    term = re.sub(r"\s*&\s*", " and ", category or "").strip().lower()
    nf = (name_filter or "").strip()
    return (f"{nf} {term}" if nf else term).strip().lower()


def places_input(category: str, location: str, limit: int, name_filter: str = "", contacts: bool = True) -> dict:
    payload = {"searchStringsArray": [search_term(category, name_filter)], "locationQuery": location.strip(),
               "maxCrawledPlacesPerSearch": max(1, min(int(limit), MAX_PLACES_PER_CALL)), "language": "en"}
    if contacts:
        payload["scrapeContacts"] = True
    return payload


def _hours(v) -> str:
    out = []
    for d in _as_list(v):
        if isinstance(d, dict):
            day, hrs = _clean(d.get("day")), _clean(d.get("hours"))
            if day or hrs:
                out.append(f"{day}: {hrs}".strip(": "))
        elif _clean(d):
            out.append(_clean(d))
    return "; ".join(out)[:400]


def normalise_place(item: dict) -> dict | None:
    """One Google Maps dataset item -> one row in our Places columns. None when it has no usable name."""
    name = _first(item.get("title"), item.get("name"))
    if len(name) < 2 or item.get("permanentlyClosed") is True:
        return None
    loc = item.get("location") if isinstance(item.get("location"), dict) else {}
    emails = [_clean(e).lower() for e in _as_list(item.get("emails")) if "@" in _clean(e)]
    extra = []
    if item.get("totalScore") not in (None, ""):
        extra.append(f"Rating {item['totalScore']}" + (f" ({item['reviewsCount']} reviews)" if item.get("reviewsCount") else ""))
    if item.get("temporarilyClosed") is True:
        extra.append("Temporarily closed")
    return {
        "Name": name,
        "Category": _first(item.get("categoryName"), (_as_list(item.get("categories")) or [""])[0]),
        "Address": _first(item.get("address"), item.get("street")),
        "City": _clean(item.get("city")), "State": _clean(item.get("state")), "Postcode": _clean(item.get("postalCode")),
        "Phone": _first(item.get("phone"), item.get("phoneUnformatted")),
        "Email": "; ".join(dict.fromkeys(emails)),
        "Website": _clean(item.get("website")),
        "Opening Hours": _hours(item.get("openingHours")),
        "Latitude": loc.get("lat", ""), "Longitude": loc.get("lng", ""),
        "Google Maps Link": _clean(item.get("url")),
        "Other Details": "; ".join(extra),
        "Website Source": "Google Maps" if item.get("website") else "",
    }


def fetch_places(category: str, location: str, limit: int, token: str, name_filter: str = "",
                 contacts: bool = True, _post=None) -> list[dict]:
    items = run_actor(PLACES_ACTOR, token, places_input(category, location, limit, name_filter, contacts),
                      timeout=180, _post=_post)
    return [r for r in (normalise_place(i) for i in items) if r]


def estimate_places_usd(n_places: int, contacts: bool = True, calls: int = 1) -> float:
    """Worst-case cost: every requested place returned, FREE-plan prices."""
    return round(n_places * (PLACE_USD + (PLACE_CONTACTS_USD if contacts else 0.0)) + calls * START_USD, 4)


# ---------------------------------------------------------------- contacts from a company's own website
def contacts_input(urls: list[str]) -> dict:
    return {"startUrls": [{"url": u} for u in urls], "maxRequestsPerStartUrl": CONTACT_PAGES_PER_SITE,
            "maxDepth": 2, "sameDomain": True, "mergeContacts": True, "proxyConfig": {"useApifyProxy": True}}


def host_of(u: str) -> str:
    try:
        h = urlparse(u if "//" in u else "//" + u).hostname or ""
    except ValueError:
        return ""
    return h.lower().removeprefix("www.")


def normalise_contacts(items: list[dict]) -> dict[str, dict]:
    """Dataset items -> {website host: {"emails": [...], "phones": [...], "social": [...]}} (merged per host)."""
    out: dict[str, dict] = {}
    social_keys = ("linkedIns", "twitters", "instagrams", "facebooks", "youtubes", "tiktoks", "pinterests")
    for it in items:
        host = host_of(_first(it.get("url"), it.get("domain"), it.get("startUrl")))
        if not host:
            continue
        rec = out.setdefault(host, {"emails": [], "phones": [], "social": []})
        rec["emails"] += [_clean(e).lower() for e in _as_list(it.get("emails")) if "@" in _clean(e)]
        rec["phones"] += [_clean(p) for p in _as_list(it.get("phones")) + _as_list(it.get("phonesUncertain")) if _clean(p)]
        for k in social_keys:
            rec["social"] += [_clean(s) for s in _as_list(it.get(k)) if _clean(s)]
    for rec in out.values():
        for k in rec:
            rec[k] = list(dict.fromkeys(rec[k]))
    return out


def fetch_contacts(urls: list[str], token: str, _post=None) -> dict[str, dict]:
    urls = [u for u in dict.fromkeys(urls) if u][:CONTACT_BATCH]
    if not urls:
        return {}
    return normalise_contacts(run_actor(CONTACTS_ACTOR, token, contacts_input(urls), timeout=240, _post=_post))


def estimate_contacts_usd(n_sites: int, calls: int | None = None) -> float:
    calls = calls if calls is not None else max(1, -(-n_sites // CONTACT_BATCH))
    return round(n_sites * CONTACT_PAGES_PER_SITE * CONTACT_PAGE_USD + calls * START_USD, 4)


def places_cost_usd(n_returned: int, contacts: bool = True) -> float:
    """What a finished call actually cost, from the rows it returned (not the worst-case estimate)."""
    return round(n_returned * (PLACE_USD + (PLACE_CONTACTS_USD if contacts else 0.0)) + START_USD, 4)


def contacts_cost_usd(n_sites: int, n_pages: int | None = None) -> float:
    pages = n_pages if n_pages is not None else n_sites * CONTACT_PAGES_PER_SITE
    return round(pages * CONTACT_PAGE_USD + START_USD, 4)


__all__ = ["ActorError", "run_actor", "fetch_places", "fetch_contacts", "normalise_place", "normalise_contacts",
           "estimate_places_usd", "estimate_contacts_usd", "places_cost_usd", "contacts_cost_usd"]
