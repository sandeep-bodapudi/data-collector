"""Bright Data free-tier social client. Failure-tolerant; never leaks the key."""
from __future__ import annotations

import time

import requests

from .social_base import SocialError, blank_record

LAST_SLOT: dict[str, float] = {}
MIN_INTERVAL = 1.1


def _pace(key: str) -> None:
    import hashlib

    kid = hashlib.sha256(key.encode()).hexdigest()
    now = time.monotonic()
    wait = LAST_SLOT.get(kid, 0.0) - now
    if wait > 0:
        time.sleep(wait)
    LAST_SLOT[kid] = max(now, LAST_SLOT.get(kid, 0.0)) + MIN_INTERVAL


def fill_profile(url: str, key: str, timeout: int = 25, _post=None) -> dict:
    """Fill one public profile. `_post` injects a fake HTTP layer for tests."""
    if not key or len(key) < 10:
        raise SocialError("missing Bright Data key")
    _pace(key)
    post = _post or requests.post
    try:
        r = post("https://api.brightdata.com/datasets/v3/trigger",
                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                 params={"dataset": "social_profile", "url": url}, timeout=timeout)
    except requests.Timeout:
        raise SocialError("Bright Data timed out")
    except requests.ConnectionError:
        raise SocialError("Bright Data connection failed")
    if r.status_code in (401, 403):
        raise SocialError("Bright Data refused the key")
    if r.status_code == 429:
        raise SocialError("Bright Data quota used up")
    if r.status_code != 200:
        raise SocialError(f"Bright Data HTTP {r.status_code}")
    try:
        data = r.json()
    except ValueError:
        raise SocialError("Bright Data unreadable reply")
    rec = blank_record(url=url)
    get = data.get if isinstance(data, dict) else (lambda _k, _d="": _d)
    rec.update({"Name": str(get("name", ""))[:150], "Headline": str(get("headline", ""))[:300],
                "Current Company": str(get("company", ""))[:150], "Location": str(get("location", ""))[:150],
                "About": str(get("about", ""))[:2000], "Experience summary": str(get("experience", ""))[:2000],
                "Website": str(get("website", ""))[:300]})
    return rec


def quota_check(key: str, timeout: int = 10, _get=None) -> dict:
    """Cheap key check used by the admin Test button. Never a full scrape."""
    if not key or len(key) < 10:
        return {"healthy": False, "error": "invalid Bright Data key format"}
    get = _get or requests.get
    try:
        r = get("https://api.brightdata.com/account", headers={"Authorization": f"Bearer {key}"}, timeout=timeout)
    except Exception as e:
        return {"healthy": False, "error": type(e).__name__}
    if r.status_code in (401, 403):
        return {"healthy": False, "error": "Bright Data refused the key"}
    return {"healthy": True} if r.status_code == 200 else {"healthy": False, "error": f"HTTP {r.status_code}"}
