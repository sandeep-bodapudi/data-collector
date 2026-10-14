"""Social enrichment shared types. No network here; providers live in sibling modules."""
from __future__ import annotations

import re
from urllib.parse import urlparse

SOCIAL_COLUMNS = ["Name", "Headline", "Current Company", "Location", "About",
                  "Experience summary", "Website", "Profile URL", "Platform"]

PLATFORMS = ("linkedin.com", "instagram.com", "facebook.com")


class SocialError(Exception):
    pass


class BudgetBlocked(SocialError):
    pass


def blank_record(name: str = "", url: str = "", platform: str = "") -> dict:
    return {"Name": name, "Headline": "", "Current Company": "", "Location": "",
            "About": "", "Experience summary": "", "Website": "",
            "Profile URL": url, "Platform": platform}


def platform_of(url: str) -> str:
    host = (urlparse(url).netloc or "").lower().removeprefix("www.")
    for p in PLATFORMS:
        if host == p or host.endswith("." + p):
            return p
    return ""


def normalize_url(url: str) -> str:
    u = (url or "").strip()
    if not u:
        return ""
    if not re.match(r"^https?://", u, re.I):
        u = "https://" + u
    p = urlparse(u)
    host = p.netloc.lower().removeprefix("www.")
    path = p.path.rstrip("/") or "/"
    if host == "linkedin.com" and path.lower().startswith("/in/"):
        path = "/in/" + path[4:].split("/")[0].lower()
    return f"https://{host}{path}" if host else ""


def is_filled_enough(rec: dict) -> bool:
    """Free path counts as sufficient when it found a headline plus company or location."""
    return bool((rec.get("Headline") or "").strip() and
                ((rec.get("Current Company") or "").strip() or (rec.get("Location") or "").strip()))


def merge_records(base: dict, extra: dict) -> dict:
    """Fill blanks in base from extra; never overwrite good data with emptier data."""
    out = dict(base)
    for k in SOCIAL_COLUMNS:
        if not (out.get(k) or "").strip() and (extra.get(k) or "").strip():
            out[k] = extra[k]
    return out


def estimate_cost(n_items: int, providers: list[str]) -> tuple[float, float]:
    """Cheapest-enabled unit price x items. Returns (usd, inr). O(1)."""
    from .config import SOCIAL_UNIT_USD, USD_TO_INR

    unit = min([SOCIAL_UNIT_USD.get(p, 0.0) for p in providers] or [0.0])
    usd = round(n_items * unit, 4)
    return usd, round(usd * USD_TO_INR, 2)
