"""Cheapest-reliable Apify actor social client. Failure-tolerant; never leaks the token."""
from __future__ import annotations

import time

import requests

from .social_base import SocialError, blank_record

LAST_SLOT: dict[str, float] = {}
MIN_INTERVAL = 1.1
DEFAULT_ACTOR = "apify/linkedin-profile-scraper"


def _pace(token: str) -> None:
    import hashlib

    kid = hashlib.sha256(token.encode()).hexdigest()
    now = time.monotonic()
    wait = LAST_SLOT.get(kid, 0.0) - now
    if wait > 0:
        time.sleep(wait)
    LAST_SLOT[kid] = max(now, LAST_SLOT.get(kid, 0.0)) + MIN_INTERVAL


def fill_profile(url: str, token: str, actor: str = "", timeout: int = 40, _post=None) -> dict:
    if not token or len(token) < 10:
        raise SocialError("missing Apify token")
    _pace(token)
    post = _post or requests.post
    actor_id = (actor or DEFAULT_ACTOR).replace("/", "~")
    try:
        r = post(f"https://api.apify.com/v2/acts/{actor_id}/run-sync-get-dataset-items",
                 headers={"Authorization": f"Bearer {token}"},
                 params={"timeout": timeout}, json={"profileUrls": [url]}, timeout=timeout + 10)
    except requests.Timeout:
        raise SocialError("Apify timed out")
    except requests.ConnectionError:
        raise SocialError("Apify connection failed")
    if r.status_code in (401, 403):
        raise SocialError("Apify refused the token")
    if r.status_code == 429:
        raise SocialError("Apify quota used up")
    if r.status_code not in (200, 201):
        raise SocialError(f"Apify HTTP {r.status_code}")
    try:
        items = r.json()
        data = items[0] if isinstance(items, list) and items else {}
    except (ValueError, IndexError):
        raise SocialError("Apify unreadable reply")
    rec = blank_record(url=url)
    rec.update({"Name": str(data.get("fullName", data.get("name", "")))[:150],
                "Headline": str(data.get("headline", ""))[:300],
                "Current Company": str(data.get("company", data.get("companyName", "")))[:150],
                "Location": str(data.get("location", ""))[:150],
                "About": str(data.get("about", data.get("summary", "")))[:2000],
                "Experience summary": str(data.get("experience", ""))[:2000],
                "Website": str(data.get("website", data.get("companyWebsite", "")))[:300]})
    return rec


def quota_check(token: str, timeout: int = 10, _get=None) -> dict:
    if not token or len(token) < 10:
        return {"healthy": False, "error": "invalid Apify token format"}
    get = _get or requests.get
    try:
        r = get("https://api.apify.com/v2/users/me", headers={"Authorization": f"Bearer {token}"}, timeout=timeout)
    except Exception as e:
        return {"healthy": False, "error": type(e).__name__}
    if r.status_code in (401, 403):
        return {"healthy": False, "error": "Apify refused the token"}
    return {"healthy": True} if r.status_code == 200 else {"healthy": False, "error": f"HTTP {r.status_code}"}
