"""Dedicated parsers for specific directory/listing sites - e.g. "Engineering Colleges in Hyderabad" pages that
list 20-30 institutions on one page. These are site-specific on purpose: a generic "find the list" heuristic was
tried and tested against real pages (colleges9.in, sulekha.com, collegedunia.com) and produced unreliable, mixed
results - real college names tangled up with site navigation, course-category links, and (on sulekha.com, a general
local-business directory) individual people's profiles. A parser matched to one site's actual markup is reliable;
a universal one isn't. Add a new site here only after inspecting its real markup the same way - don't guess.

Each parser returns real rows (Name, Address when available, and the directory's own profile-page link for that
listing - not yet verified as the institution's own official site, since that needs a further lookup/enrichment
pass). A parser breaks if the site redesigns its page; that's an acceptable, visible failure (it'll just stop
matching and return nothing), not a silent wrong answer."""
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .fetch import domain_of


def _parse_colleges9(soup: BeautifulSoup, url: str) -> list[dict]:
    """colleges9.in's category pages (e.g. /Telangana/Hyderabad/Engineering-Colleges/) list colleges one per table
    row: <td><b>NAME</b><br>address</td> next to a link to /colleges/<SLUG>/<CODE>/, that college's own profile
    page on colleges9.in (not its official site)."""
    rows = []
    for a in soup.find_all("a", href=True):
        if "/colleges/" not in a["href"]:
            continue
        tr = a.find_parent("tr")
        if not tr:
            continue
        b = tr.find("b")
        if not b:
            continue
        name = b.get_text(" ", strip=True)
        if not name or len(name) < 4:
            continue
        td = b.find_parent("td")
        address = ""
        if td:
            full = td.get_text(" ", strip=True)
            address = full[len(name):].strip(" ,")  # whatever follows the <b>name</b> in the same cell
        rows.append({"name": name[:150], "address": address[:300], "href": urljoin(url, a["href"])})
    # Several links per row (profile + "Courses & Cutoff Ranks") point at the same college - keep the first.
    seen, out = set(), []
    for r in rows:
        if r["name"].lower() in seen:
            continue
        seen.add(r["name"].lower())
        out.append(r)
    return out


LISTING_PARSERS = {
    "colleges9.in": _parse_colleges9,
}


# ---------------------------------------------------------------- college profile pages (the contact details)
# Each college on colleges9.in has its own profile page (the link a listing row points at). That page is where the
# phone number, email and official website are - the category listing page only has the name and address. Labels
# were read from a real profile page (AAR Mahaveer Engineering College, /colleges/.../EN728/): "Phone No.", "Mobile:",
# "Email", "Website", "Address Details", "Affliated to:", "Established on:". Matched on those labels, not on layout.
import re as _re

_PROFILE_PHONE = _re.compile(r"Phone No\.?\s*:?\s*([+\d][\d\s,/()-]{5,}?)\s*(?=Head|Mobile|Email|Website|Hostel|$)")
_PROFILE_MOBILE = _re.compile(r"Mobile\s*:?\s*([+\d][\d\s,/()-]{5,}?)\s*(?=Email|Website|Hostel|$)")
_PROFILE_EMAIL = _re.compile(r"\bEmail\s*:?\s*([\w.+-]+@[\w-]+(?:\.[\w-]+)+)")
_PROFILE_WEBSITE = _re.compile(r"\bWebsite\s*:?\s*((?:https?://)?(?:www\.)?[\w-]+(?:\.[\w-]+)+)")
_PROFILE_ADDRESS = _re.compile(r"Address Details\s+Address\s*(.+?)\s+District")


def _parse_colleges9_profile(text: str) -> dict:
    out = {"phones": [], "emails": [], "website": "", "address": ""}
    for pat in (_PROFILE_PHONE, _PROFILE_MOBILE):
        m = pat.search(text)
        if m:
            digits = re.sub(r"[^\d+]", "", m.group(1))
            if len(re.sub(r"\D", "", digits)) >= 6:
                out["phones"].append(m.group(1).strip(" ,/"))
    m = _PROFILE_EMAIL.search(text)
    if m:
        out["emails"].append(m.group(1).rstrip("."))
    m = _PROFILE_WEBSITE.search(text)
    if m and "colleges9" not in m.group(1):
        site = m.group(1).rstrip(".")
        out["website"] = site if site.startswith("http") else "https://" + site
    m = _PROFILE_ADDRESS.search(text)
    if m:
        out["address"] = re.sub(r"\s+", " ", m.group(1)).strip(" ,")
    return out


PROFILE_PARSERS = {"colleges9.in": _parse_colleges9_profile}


def is_profile_url(url: str) -> bool:
    """A link to a college's own profile page on a site we have a contact-details parser for."""
    return domain_of(url or "") in PROFILE_PARSERS and "/colleges/" in (url or "")


def extract_profile(html: str, url: str) -> dict:
    """Phone numbers, emails, official website and address from a college's profile page; empty fields if the
    page doesn't have them or the site isn't registered. Never raises - a changed page just yields less."""
    parser = PROFILE_PARSERS.get(domain_of(url))
    if not parser:
        return {"phones": [], "emails": [], "website": "", "address": ""}
    try:
        soup = BeautifulSoup(html, "lxml")
        for tag in soup(["script", "style"]):
            tag.decompose()
        return parser(re.sub(r"\s+", " ", soup.get_text(" ")))
    except Exception:
        return {"phones": [], "emails": [], "website": "", "address": ""}


def extract_listing(html: str, url: str) -> list[dict]:
    """Real rows from a listing page, using the parser matched to this exact site - empty if none is registered
    for this domain (most directories aren't covered yet; see the module docstring for why that's deliberate)."""
    parser = LISTING_PARSERS.get(domain_of(url))
    if not parser:
        return []
    try:
        return parser(BeautifulSoup(html, "lxml"), url)
    except Exception:  # a parser written against one page layout must never break the run if the site changed it
        return []


# ---------------------------------------------------------------- known directory URLs (no search engine needed)
# colleges9.in organises every Telangana district's colleges under a fixed, discoverable URL shape - no search
# required to find these pages at all, which means this data source has zero exposure to free-engine blocking or
# pacing. Verified live, real counts per district (engineering colleges): Adilabad 2, Hyderabad 38, Karimnagar 19,
# Khammam 25, Mahaboobnagar 10, Medak 28, Nalgonda 40, Nizamabad 13, Ranga-Reddy 165, Warangal 30 - 370 real colleges
# total, one search-free run. The district list itself was read live from https://www.colleges9.in/Telangana/ and
# is reasonably stable (it's a fixed set of districts, not something that gets reshuffled), but if colleges9.in adds
# or renames a district this list needs a manual refresh - it's not re-discovered automatically every run, to avoid
# depending on a live page fetch succeeding just to know what to fetch next.
TELANGANA_DISTRICTS = (
    "Adilabad", "Hyderabad", "Karimnagar", "Khammam", "Mahaboobnagar", "Medak",
    "Nalgonda", "Nizamabad", "Ranga-Reddy", "Warangal",
)

KNOWN_SEEDS = {
    "telangana_engineering_colleges": {
        "label": "Telangana engineering colleges (colleges9.in, ~370 real colleges, no search engine needed)",
        "urls": [f"https://www.colleges9.in/Telangana/{d}/Engineering-Colleges/" for d in TELANGANA_DISTRICTS],
    },
}
