"""Google Maps data through the official Places API (New) - the dependable route for map listings.

Option (c) of the plan the owner chose on 2026-10-26: the official API wherever a key is set (it works on the hosted
server too: no browser, no blocking), and the Playwright Google Maps reader (scraper/google_browser.py) only as a
fallback on a PC when there is no key.

One Text Search call ("coaching centres in Ameerpet, Hyderabad") returns up to 20 places; up to 3 pages are read.
The field mask asks only for what goes in the sheet. Google bills Text Search per call by the most expensive field
requested (phone, website, hours and rating are in the higher tier) and gives a free monthly allowance per tier -
check the current Places API pricing page, and set a daily quota in Google Cloud so a big run can never surprise you.
GOOGLE_PLACES_MAX_PER_RUN (default 300 places) caps one run.

Key: the person's own (Settings -> Keys), an org-shared one, or GOOGLE_PLACES_API_KEY in the server environment.
Never logged, never returned.
"""
from __future__ import annotations

import os

import requests

URL = "https://places.googleapis.com/v1/places:searchText"
FIELDS = ",".join([
    "places.id", "places.displayName", "places.formattedAddress", "places.addressComponents", "places.location",
    "places.googleMapsUri", "places.nationalPhoneNumber", "places.internationalPhoneNumber", "places.websiteUri",
    "places.regularOpeningHours.weekdayDescriptions", "places.rating", "places.userRatingCount",
    "places.businessStatus", "places.primaryTypeDisplayName", "nextPageToken",
])
PAGE_SIZE = 20
MAX_PAGES = 3


class GooglePlacesError(Exception):
    """A readable reason the API refused; never contains the key."""


def max_per_run() -> int:
    try:
        return max(0, int(os.environ.get("GOOGLE_PLACES_MAX_PER_RUN", "300")))
    except ValueError:
        return 300


def key_from(vault: dict) -> str:
    return (vault or {}).get("google_places_api_key") or os.environ.get("GOOGLE_PLACES_API_KEY", "")


def _component(p: dict, kind: str) -> str:
    for c in p.get("addressComponents") or []:
        if kind in (c.get("types") or []):
            return c.get("longText") or c.get("shortText") or ""
    return ""


def normalise(p: dict) -> dict | None:
    """One API place -> one row in our Places columns. None for a closed or nameless place."""
    name = ((p.get("displayName") or {}).get("text") or "").strip()
    if len(name) < 2 or p.get("businessStatus") == "CLOSED_PERMANENTLY":
        return None
    loc = p.get("location") or {}
    hours = (p.get("regularOpeningHours") or {}).get("weekdayDescriptions") or []
    details = []
    if p.get("rating"):
        details.append(f"Rating {p['rating']}" + (f" ({p['userRatingCount']} reviews)" if p.get("userRatingCount") else ""))
    if p.get("businessStatus") == "CLOSED_TEMPORARILY":
        details.append("Temporarily closed")
    return {
        "Name": name,
        "Category": ((p.get("primaryTypeDisplayName") or {}).get("text") or ""),
        "Address": p.get("formattedAddress") or "",
        "City": _component(p, "locality"), "State": _component(p, "administrative_area_level_1"),
        "Postcode": _component(p, "postal_code"),
        "Phone": p.get("internationalPhoneNumber") or p.get("nationalPhoneNumber") or "",
        "Email": "",
        "Website": p.get("websiteUri") or "",
        "Opening Hours": "; ".join(hours)[:400],
        "Latitude": loc.get("latitude", ""), "Longitude": loc.get("longitude", ""),
        "Google Maps Link": p.get("googleMapsUri") or "",
        "Other Details": "; ".join(details),
        "Data Source": "Google Maps (API)",
    }


def _error(r) -> GooglePlacesError:
    try:
        msg = ((r.json() or {}).get("error") or {}).get("message", "")
    except ValueError:
        msg = ""
    low = msg.lower()
    if r.status_code in (401, 403):
        if "billing" in low:
            return GooglePlacesError("Google needs billing turned on for this project before the Places API answers.")
        if "not been used" in low or "disabled" in low or "not enabled" in low:
            return GooglePlacesError("The Places API (New) is not enabled for this key's Google Cloud project.")
        return GooglePlacesError("Google refused this API key. Check it, and that it may call the Places API (New).")
    if r.status_code == 429:
        return GooglePlacesError("Google Places quota reached for now (the daily or per-minute limit on this key).")
    if r.status_code == 400:
        return GooglePlacesError(f"Google did not accept the search ({msg[:120] or 'bad request'}).")
    return GooglePlacesError(f"Google Places answered {r.status_code}.")


def search(query: str, key: str, limit: int, region: str = "IN", _post=None) -> list[dict]:
    """Up to `limit` places for a text query (max 60, Google's own ceiling for one search)."""
    if not key:
        raise GooglePlacesError("No Google Places API key.")
    post = _post or requests.post
    headers = {"Content-Type": "application/json", "X-Goog-Api-Key": key, "X-Goog-FieldMask": FIELDS}
    rows, token = [], None
    for _ in range(MAX_PAGES):
        body = {"textQuery": query, "pageSize": min(PAGE_SIZE, max(1, limit - len(rows))), "languageCode": "en",
                "regionCode": region}
        if token:
            body["pageToken"] = token
        try:
            r = post(URL, headers=headers, json=body, timeout=30)
        except requests.RequestException as e:
            raise GooglePlacesError(f"Could not reach Google ({type(e).__name__}).")
        if r.status_code != 200:
            raise _error(r)
        data = r.json() or {}
        rows += [x for x in (normalise(p) for p in data.get("places") or []) if x]
        token = data.get("nextPageToken")
        if not token or len(rows) >= limit:
            break
    return rows[:limit]


def test_key(key: str, _post=None) -> tuple[bool, str]:
    """Cheapest possible check: one result, id only."""
    post = _post or requests.post
    try:
        r = post(URL, headers={"Content-Type": "application/json", "X-Goog-Api-Key": key, "X-Goog-FieldMask": "places.id"},
                 json={"textQuery": "temple in Hyderabad", "pageSize": 1}, timeout=20)
    except requests.RequestException as e:
        return False, f"Could not reach Google ({type(e).__name__})."
    if r.status_code == 200:
        return True, ""
    return False, str(_error(r))
