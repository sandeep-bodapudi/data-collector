"""One clear business-profile sheet for every web and Places run (plan: docs/v2/business-profiles-plan.md).

Before this, each mode made its own sheet (web: Name/Emails/Phone Numbers..., Places: Name/Phone/Email...), cells held
every email a page mentioned with the best one buried, empty columns were kept, and nothing said whether a row was a
solid match or a guess. build() turns a finished run's rows into one shape:

  Business Name, Category, Phone, Other Phones, Email, Other Emails, Email Quality, Website, Address, Area, City, State,
  Pincode, Opening Hours, Google Maps Link, Social Links, Match Confidence, Source, Last Checked  (+ AI columns)

- one primary phone (a mobile first) and one primary email (the best-rated, the company's own domain first); the rest
  stay available in Other Phones / Other Emails
- Match Confidence says how sure we are that the website and contacts belong to this business, and why
- a column with nothing in it is dropped (the core columns always stay), rows are sorted strongest first, and the same
  business found twice (same website, or same name in the same area) becomes one row
"""
from __future__ import annotations

import re
from datetime import date

from . import discover, extract, quality

CORE = ["Business Name", "Phone", "Email", "Email Quality", "Website", "Address", "Match Confidence", "Source", "Last Checked"]
ORDER = ["Business Name", "Category", "Phone", "Other Phones", "Email", "Other Emails", "Email Quality", "Website", "Address",
         "Area", "City", "State", "Pincode", "Opening Hours", "Google Maps Link", "Social Links", "Description",
         "Contact Page", "Match Confidence", "Source", "Last Checked"]
_PIN = re.compile(r"(?<!\d)([1-9]\d{2})\s?(\d{3})(?!\d)")
_WORD = re.compile(r"[a-z0-9]+")
_PREP = re.compile(r"\b(in|near|at|around|across)\b", re.I)
_RANK = {"High": 3, "Medium": 2, "Low": 1, "": 0}
_STOP = set(discover.GENERIC_WORDS) | {"pvt", "private", "limited", "ltd", "llp", "inc", "co", "com", "in", "www", "org",
                                       "net", "edu", "ac", "training", "coaching", "classes", "centre", "center",
                                       "digital", "marketing", "solutions", "services", "academy", "institute",
                                       # subject words: "A.V College of Arts" must not match arts.com
                                       "arts", "art", "science", "sciences", "commerce", "degree", "management",
                                       "computer", "computers", "software", "systems", "global", "info", "infotech",
                                       "technologies", "learning", "education", "study", "studies", "career", "careers",
                                       # honorifics and filler that start thousands of names ("Sri Ram ..." is not srichaitanya.com)
                                       "sri", "shri", "sree", "shree", "smt", "new", "the", "om", "maa", "jai", "my"}


def _as_list(v) -> list[str]:
    if isinstance(v, (list, tuple, set)):
        items = [str(x) for x in v]
    else:
        items = re.split(r"\s*;\s*", str(v or ""))
    return [x.strip() for x in items if x and x.strip()]


_TRAILING = re.compile(r"[\s\-|:\u2013\u2014,.]+$")
_PAGE_NAME = re.compile(r"\b(landing page|home ?page|official (web)?site|welcome|index|untitled)\b", re.I)


def clean_business_name(name: str, website: str) -> str:
    """'Digital Marketing Landing Page -' is a page title, not a business. Strip trailing punctuation and page-ish
    words; if nothing distinctive is left, name the business after its website ('digitalgurusacademy.co.in' ->
    'Digitalgurusacademy') rather than keep a description."""
    n = _TRAILING.sub("", _PAGE_NAME.sub("", name or "")).strip()
    n = re.sub(r"\s{2,}", " ", n)
    words = [w for w in _WORD.findall(n.lower()) if w not in _STOP and len(w) >= 3]
    if not words:
        host = quality.host_of(website)
        label = quality.org_domain(host).split(".")[0] if host else ""
        if label:
            return label.capitalize()
    return n or (name or "").strip()


def _split_query(q: str) -> tuple[str, str]:
    """('digital marketing institute', 'Ameerpet, Hyderabad') from 'digital marketing institute in Ameerpet, Hyderabad'."""
    q = re.sub(r"\s+site:\S+", "", q or "").strip()
    last = None
    for m in _PREP.finditer(q):
        last = m
    if not last:
        return q, ""
    return q[:last.start()].strip(" ,"), q[last.end():].strip(" ,")


# Hosts that are not a business's own website, though Google Maps and directories often list them as "Website":
# forms, link-in-bio pages, chat links and social profiles (seen live: a forms.gle link as an institute's website).
NOT_A_WEBSITE = ("forms.gle", "docs.google.com", "sites.google.com", "linktr.ee", "wa.me", "api.whatsapp.com",
                 "facebook.com", "fb.com", "instagram.com", "linkedin.com", "twitter.com", "x.com", "youtube.com",
                 "t.me", "bit.ly", "g.page", "business.site")


def _website(v: str) -> str:
    v = (v or "").strip()
    if not v:
        return ""
    host = quality.host_of(v)
    if not host:
        return ""
    return v if v.startswith("http") else f"https://{host}"


def _not_a_website(url: str) -> bool:
    host = quality.host_of(url)
    return bool(host) and any(host == h or host.endswith("." + h) for h in NOT_A_WEBSITE)


INDIA_WORDS = ("india", "telangana", "andhra", "hyderabad", "secunderabad", "bengaluru", "bangalore", "chennai", "mumbai",
               "delhi", "pune", "kolkata", "vijayawada", "visakhapatnam", "warangal", "karimnagar", "tamil nadu",
               "karnataka", "maharashtra", "kerala", "gujarat", "ameerpet", "kukatpally", "madhapur", "gachibowli")


def indian_context(spec: dict) -> bool:
    """Is this run about places in India? Then a phone with another country code means the website we matched
    probably belongs to someone else (seen live: 'Race institute' in Ameerpet matched race.edu, a US college)."""
    if spec.get("region") == "in-en":
        return True
    text = " ".join([*(spec.get("locations") or []), *(spec.get("queries") or [])]).lower()
    return any(w in text for w in INDIA_WORDS)


def _foreign_phone(phone: str) -> bool:
    p = (phone or "").strip()
    digits = re.sub(r"\D", "", p)
    if p.startswith("+"):
        return not p.startswith("+91")
    return len(digits) == 11 and digits.startswith("1")  # "1 416 725-5646" style North American numbers


def _phones(values) -> tuple[str, list[str]]:
    phones = extract.dedupe_phones(_as_list(values))
    mobiles = [p for p in phones if re.sub(r"\D", "", p)[-10:][:1] in "6789" and len(re.sub(r"\D", "", p)) >= 10]
    ordered = mobiles + [p for p in phones if p not in mobiles]
    return (ordered[0] if ordered else ""), ordered[1:6]


def name_matches_site(name: str, host: str) -> bool:
    """Does the website look like this business's own? 'Naresh IT' ~ nareshit.com, 'Vision IAS' ~ visionias.in,
    'Gokaraju Rangaraju Institute of Engineering' ~ griet.ac.in (initials). 'A.V College' ~ arts.com is no."""
    label = quality.org_domain(host).split(".")[0] if host else ""
    if not label or len(label) < 3:
        return False
    words = [w for w in _WORD.findall((name or "").lower())]
    distinctive = [w for w in words if w not in _STOP and len(w) >= 3]
    squashed = "".join(words)
    if any(w in label for w in distinctive if len(w) >= 4):
        return True
    if len(label) >= 5 and label in squashed:
        return True
    initials = "".join(w[0] for w in words if w not in {"of", "and", "the", "for", "&"})
    return len(initials) >= 3 and (label.startswith(initials) or initials.startswith(label))


def confidence(name: str, site: str, email: str, on_map: bool, has_contact: bool) -> str:
    host = quality.host_of(site)
    match = name_matches_site(name, host) if host else False
    own = bool(email and host and quality.same_org(email.rpartition("@")[2], host))
    reasons = []
    if host:
        reasons.append("website matches the name" if match else "website name differs from the business name")
    if own:
        reasons.append("email on the website's own domain")
    if on_map:
        reasons.append("listed on a map")
    if not has_contact:
        reasons.append("no phone or email found")
    if host and match and (own or on_map or has_contact):
        level = "High"
    elif (host and match) or own or (on_map and has_contact):
        level = "Medium"
    elif on_map and not host:
        level = "Medium"
    else:
        level = "Low"
    return f"{level} - " + ", ".join(reasons) if reasons else level


def build(rows: list[dict], columns: list[str], spec: dict, resolver=None, today: date | None = None) -> tuple[list[dict], list[str]]:
    """Profile rows + columns for a finished web or Places run. Pure apart from the DNS lookups for Email Quality
    (injectable through `resolver`)."""
    mode = spec.get("mode", "web")
    custom = [c for c in (spec.get("custom_fields") or []) if c in columns]
    when = (today or date.today()).isoformat()
    queries = spec.get("queries") or []
    out: list[dict] = []
    all_emails: list[list[str]] = []
    for r in rows:
        name = str(r.get("Name") or r.get("Company Name") or "").strip()
        if not name:
            continue
        # Listing/source rows carry the search that found them; without one, only a single-search run can say.
        q = r["_query"] if r.get("_query") else (queries[0] if len(queries) == 1 else "")
        topic, area = _split_query(q) if mode == "web" else ("", "")
        emails = list(dict.fromkeys(e.lower() for e in quality.split_emails(r.get("Emails") or r.get("Email"))))
        phone, other_phones = _phones(r.get("Phone Numbers") or r.get("Phone"))
        address = str(r.get("Address") or "").strip()
        pin = r.get("Postcode") or ""
        if not pin:
            m = _PIN.search(address)
            pin = f"{m.group(1)}{m.group(2)}" if m else ""
        on_map = mode == "places"
        source = (r.get("Data Source") or "OpenStreetMap") if on_map else "Web search"
        social = _as_list(r.get("Social Media Links"))
        site = str(r.get("Website") or "")
        if _not_a_website(site):  # a form / social / chat link listed as the website: keep it, but as a link
            social = [site] + social
            site = ""
        name = clean_business_name(name, str(r.get("Website") or ""))
        prof = {
            "Business Name": name[:150],
            "Category": r.get("Category") or (spec.get("category") if on_map else topic),
            "Phone": phone, "Other Phones": "; ".join(other_phones),
            "_emails": emails,
            "Website": _website(site),
            "Address": address, "Area": r.get("Search Location") or area,
            "City": r.get("City") or "", "State": r.get("State") or "", "Pincode": pin,
            "Opening Hours": r.get("Opening Hours") or "", "Google Maps Link": r.get("Google Maps Link") or "",
            "Social Links": "; ".join(social[:6]),
            "Description": str(r.get("Description") or "")[:400], "Contact Page": r.get("Contact Page URL") or "",
            # "+ company website" only when the website was actually read (web runs always read it; Places runs
            # record a Website Status when they did), not merely because the map listed one.
            "Source": source + (" + company website" if (emails or phone) and r.get("Website")
                                and (not on_map or r.get("Website Status")) else ""),
            "_on_map": on_map,
        }
        for c in custom:
            prof[c] = r.get(c, "")
        out.append(prof)
        all_emails.append(emails)

    # Rate every email once (Email Quality's free checks), then pick each row's best one as its primary email.
    india = indian_context(spec)
    domains = list(dict.fromkeys(e.rpartition("@")[2] for es in all_emails for e in es))[:quality.MAX_DOMAINS]
    states = quality._states(domains, resolver)
    for p in out:
        host = quality.host_of(p["Website"])
        rated = []
        for e in p.pop("_emails"):
            level, why = quality.assess(e, host, states.get(e.rpartition("@")[2], "unknown"))
            own = bool(host) and quality.same_org(e.rpartition("@")[2], host)
            rated.append((_RANK[level], own, e, f"{level} - {why}"))
        rated.sort(key=lambda t: (-t[0], not t[1]))
        p["Email"] = rated[0][2] if rated else ""
        p["Email Quality"] = rated[0][3] if rated else ""
        p["Other Emails"] = "; ".join(t[2] for t in rated[1:6])
        p["Match Confidence"] = confidence(p["Business Name"], p["Website"], p["Email"], p.pop("_on_map"),
                                           bool(p["Email"] or p["Phone"]))
        if india and p["Phone"] and _foreign_phone(p["Phone"]):
            level, _, why = p["Match Confidence"].partition(" - ")
            lower = {"High": "Medium", "Medium": "Low"}.get(level, level)
            p["Match Confidence"] = f"{lower} - " + ", ".join(x for x in (why, "phone is not an Indian number") if x)
        p["Last Checked"] = when

    out = _dedupe(out)
    out.sort(key=lambda p: (-_RANK[p["Match Confidence"].split(" - ")[0]], not (p["Email"] or p["Phone"]),
                            p["Business Name"].lower()))
    cols = [c for c in ORDER if c in CORE or any(p.get(c) for p in out)] + custom
    return [{c: p.get(c, "") for c in cols} for p in out], cols


def _dedupe(rows: list[dict]) -> list[dict]:
    """Same website (organisation domain), or same name in the same area: one row, keeping what either had."""
    kept: list[dict] = []
    by_key: dict[str, dict] = {}
    for p in rows:
        host = quality.host_of(p["Website"])
        keys = [f"site:{quality.org_domain(host)}"] if host else []
        keys.append(f"name:{p['Business Name'].lower()}|{(p.get('Area') or '').lower()}")
        twin = next((by_key[k] for k in keys if k in by_key), None)
        if twin is not None and host and twin.get("Website") and not quality.same_org(host, quality.host_of(twin["Website"])):
            twin = None  # same name, different websites: two businesses (branches, franchisees), not one
        if twin is None:
            kept.append(p)
            for k in keys:
                by_key[k] = p
            continue
        for c, v in p.items():
            if v and not twin.get(c):
                twin[c] = v
        if _RANK[p["Match Confidence"].split(" - ")[0]] > _RANK[twin["Match Confidence"].split(" - ")[0]]:
            twin["Match Confidence"] = p["Match Confidence"]
        for k in keys:
            by_key.setdefault(k, twin)
    return kept
