"""Rule-based extractors that work on any web page (no AI needed)."""
import json
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

# Field key -> label shown in the UI and used as the Excel column header.
STANDARD_FIELDS = {
    "title": "Page Title",
    "description": "Description",
    "emails": "Emails",
    "phones": "Phone Numbers",
    "address": "Address",
    "social": "Social Media Links",
    "contact_page": "Contact Page URL",
    "text_snippet": "Page Text (first 500 chars)",
}

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,24}")
# Obfuscated email patterns: "info [at] school.edu", "info(at)school.edu", "info AT domain DOT edu"
EMAIL_OBFUSCATED_RE = re.compile(
    r"([A-Za-z0-9._%+\-]+)\s*[\[\(]?\s*(?:at|AT|@)\s*[\]\)]?\s*([A-Za-z0-9.\-]+)"
    r"\s*[\[\(]?\s*(?:dot|DOT|\.)\s*[\]\)]?\s*([A-Za-z]{2,24})"
)
EMAIL_JUNK_TLDS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".css", ".js")
EMAIL_JUNK_DOMAINS = ("example.com", "domain.com", "email.com", "sentry.io", "wixpress.com",
                      "sentry-next.wixpress.com", "yourdomain.com", "company.com", "schema.org",
                      "w3.org", "googleapis.com", "gstatic.com")

# Phone numbers: optional +country code, then 8-12 digits with common separators.
PHONE_RE = re.compile(r"(?<![/\w])(\+?(?:91[\s\-.]?)?\(?\d{3,5}\)?[\s.\-]?\d{3,5}[\s.\-]?\d{3,5})(?![/\w])")
# Also catches: Tel: 040-12345678, Ph: 1800-222-333
PHONE_LABEL_RE = re.compile(
    r"(?:Tel|Ph|Phone|Mobile|Mob|Fax|Helpline|Toll[\s\-]?Free|Call us?|Contact)\s*[:\-\s]\s*"
    r"(\+?[\d\s.\-\(\)]{8,18})",
    re.I
)

SOCIAL_HOSTS = {
    "facebook.com": "Facebook", "instagram.com": "Instagram", "linkedin.com": "LinkedIn",
    "twitter.com": "Twitter/X", "x.com": "Twitter/X", "youtube.com": "YouTube",
    "wa.me": "WhatsApp", "t.me": "Telegram", "pinterest.com": "Pinterest",
    "threads.net": "Threads",
}

# Sub-pages that commonly carry contact info on institution sites.
# Tried in order; we stop after collecting enough data.
CONTACT_SUBPAGES = [
    "/contact", "/contact-us", "/contactus", "/contact_us",
    "/about", "/about-us", "/aboutus",
    "/reach-us", "/reach_us", "/reachout", "/get-in-touch",
    "/enquiry", "/enquire",
    "/staff", "/our-team", "/team", "/faculty", "/directory",
    "/administration", "/management",
]


def _clean_email(e: str) -> str | None:
    if not e:
        return None
    e = e.strip().strip(".").lower()
    if e.endswith(EMAIL_JUNK_TLDS):
        return None
    if any(e.endswith("@" + d) or e.endswith("." + d) for d in EMAIL_JUNK_DOMAINS):
        return None
    if re.search(r"@\d+x\.", e):  # retina image names like logo@2x.png
        return None
    if e.count("@") != 1:
        return None
    local, domain = e.rsplit("@", 1)
    if "." not in domain or len(local) < 1:
        return None
    return e


def _clean_phone(raw: str) -> str | None:
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    if not 8 <= len(digits) <= 15:
        return None
    if len(set(digits)) <= 2:  # 0000000000, 1111111111 ...
        return None
    if re.fullmatch(r"(19|20)\d{2}(19|20)\d{2}.*", digits):  # year ranges like 2019-2024
        return None
    # Reject pure date-like patterns (e.g. 01012024)
    if re.fullmatch(r"0[1-9]0[1-9]\d{4}", digits):
        return None
    return re.sub(r"\s+", " ", raw.strip())


def _uniq(items):
    seen, out = set(), []
    for i in items:
        if i and i not in seen:
            seen.add(i)
            out.append(i)
    return out


def dedupe_phones(phones):
    """Treat '+919581230786' and '+91 9581 230786' as the same number (compare last 10 digits)."""
    seen, out = set(), []
    for p in phones:
        if not p:
            continue
        key = re.sub(r"\D", "", p)[-10:]
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


def _jsonld_objects(soup):
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            obj = stack.pop()
            if isinstance(obj, dict):
                yield obj
                stack.extend(v for v in obj.values() if isinstance(v, (dict, list)))
            elif isinstance(obj, list):
                stack.extend(obj)


def _format_address(addr) -> str:
    if isinstance(addr, str):
        return addr
    if isinstance(addr, dict):
        parts = [addr.get(k) for k in ("streetAddress", "addressLocality", "addressRegion", "postalCode", "addressCountry")]
        parts = [p.get("name") if isinstance(p, dict) else p for p in parts]
        return ", ".join(str(p) for p in parts if p)
    return ""


def visible_text(soup) -> str:
    for t in soup(["script", "style", "noscript", "svg", "iframe", "template"]):
        t.decompose()
    text = soup.get_text(" ", strip=True)
    return re.sub(r"\s{2,}", " ", text)


def _extract_obfuscated_emails(text: str) -> list[str]:
    """Find emails written as 'info[at]school[dot]edu' or 'info AT school DOT edu'."""
    out = []
    for m in EMAIL_OBFUSCATED_RE.finditer(text):
        reconstructed = f"{m.group(1)}@{m.group(2)}.{m.group(3)}"
        cleaned = _clean_email(reconstructed)
        if cleaned:
            out.append(cleaned)
    return out


def _extract_itemprop(soup, emails: list, phones: list):
    """Pull emails/phones from microdata itemprop attributes."""
    for el in soup.find_all(attrs={"itemprop": True}):
        prop = el.get("itemprop", "")
        val = el.get("content") or el.get("href") or el.get_text(" ", strip=True)
        if not val:
            continue
        if prop in ("email",):
            val = val.replace("mailto:", "")
            cleaned = _clean_email(val.strip())
            if cleaned:
                emails.append(cleaned)
        elif prop in ("telephone", "faxNumber"):
            cleaned = _clean_phone(val.strip())
            if cleaned:
                phones.append(cleaned)


def _extract_data_attrs(soup, emails: list, phones: list):
    """Scan data-email / data-phone attributes (common pattern for JS-hidden contacts)."""
    for el in soup.find_all(True):
        for attr in el.attrs:
            if attr in ("data-email", "data-mail"):
                v = el.get(attr, "")
                c = _clean_email(v)
                if c:
                    emails.append(c)
            if attr in ("data-phone", "data-tel", "data-mobile"):
                v = el.get(attr, "")
                c = _clean_phone(v)
                if c:
                    phones.append(c)


def _extract_phones_labeled(text: str) -> list[str]:
    """Find phone numbers that are preceded by a label like 'Tel:', 'Phone:', 'Mobile:'."""
    out = []
    for m in PHONE_LABEL_RE.finditer(text):
        c = _clean_phone(m.group(1))
        if c:
            out.append(c)
    return out


def _extract_address(soup, text: str) -> str:
    # 1. itemprop="address" or "streetAddress"
    for prop in ("address", "streetAddress", "location"):
        el = soup.find(attrs={"itemprop": prop})
        if el:
            addr = el.get("content") or el.get_text(" ", strip=True)
            if addr and len(addr) > 10:
                return addr[:400]

    # 2. JSON-LD
    for obj in _jsonld_objects(soup):
        if obj.get("address"):
            a = _format_address(obj["address"])
            if a:
                return a[:400]

    # 3. <address> HTML tag
    tag = soup.find("address")
    if tag:
        addr = tag.get_text(" ", strip=True)
        if len(addr) > 5:
            return addr[:400]

    # 4. Footer: look for divs/sections labelled "address" or containing pin patterns
    for el in soup.find_all(["div", "p", "section", "footer", "span"], class_=re.compile(r"address|location|contact", re.I)):
        addr = el.get_text(" ", strip=True)
        if len(addr) > 15:
            return addr[:400]

    # 5. Text pattern: "Address:" label
    for pattern in [
        r"(?:Address|Location|Registered\s+Office)\s*[:\-]\s*(.{15,300}?)(?:\s(?:Phone|Tel|Email|Mobile|Call|Fax|Pin)\b|$)",
        r"(\d+[,\s]+[A-Za-z][^,\n]{5,50}(?:Road|Street|Nagar|Colony|Layout|Marg|Avenue|Sector|Block|Area|District|Dist|State|Pin)[^,\n]{0,80})",
    ]:
        m = re.search(pattern, text, re.I)
        if m:
            return m.group(1).strip()[:400]

    return ""


def candidate_contact_urls(base_url: str, found_contact: str) -> list[str]:
    """Return a prioritised list of sub-page URLs to check for contact info."""
    parsed = urlparse(base_url)
    root = f"{parsed.scheme}://{parsed.netloc}"
    urls = []
    if found_contact and found_contact != base_url:
        urls.append(found_contact)
    for path in CONTACT_SUBPAGES:
        candidate = root + path
        if candidate != base_url and candidate not in urls:
            urls.append(candidate)
    return urls[:6]  # never check more than 6 sub-pages per site


def parse(html: str, url: str) -> dict:
    """Extract every standard field from one page. Returns lists for multi-value fields."""
    soup = BeautifulSoup(html, "lxml")
    out = {k: [] if k in ("emails", "phones", "social") else "" for k in STANDARD_FIELDS}

    if soup.title and soup.title.string:
        out["title"] = soup.title.string.strip()
    meta = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", attrs={"property": "og:description"})
    if meta and meta.get("content"):
        out["description"] = meta["content"].strip()

    emails, phones, social = [], [], []
    contact_page = ""

    # --- anchor tags ---
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        low = href.lower()
        if low.startswith("mailto:"):
            emails.append(_clean_email(href[7:].split("?")[0]))
        elif low.startswith("tel:"):
            phones.append(_clean_phone(re.sub(r"tel:\+?", "", href, flags=re.I)))
        else:
            absu = urljoin(url, href)
            host = urlparse(absu).netloc.lower().removeprefix("www.")
            for sh, name in SOCIAL_HOSTS.items():
                if host == sh or host.endswith("." + sh):
                    social.append(f"{name}: {absu}")
            label = (a.get_text(" ", strip=True) or "").lower()
            if not contact_page and (
                any(kw in low for kw in ("contact", "reach", "enquir", "get-in-touch")) or
                any(kw in label for kw in ("contact", "reach us", "enquiry", "get in touch"))
            ) and urlparse(absu).netloc == urlparse(url).netloc:
                contact_page = absu

    # --- itemprop microdata ---
    _extract_itemprop(soup, emails, phones)

    # --- data-* attributes ---
    _extract_data_attrs(soup, emails, phones)

    # --- JSON-LD ---
    for obj in _jsonld_objects(soup):
        if obj.get("email"):
            emails.append(_clean_email(str(obj["email"]).replace("mailto:", "")))
        if obj.get("telephone"):
            phones.append(_clean_phone(str(obj["telephone"])))

    # --- full visible text ---
    text = visible_text(soup)

    # Standard regex emails from text
    emails += [_clean_email(e) for e in EMAIL_RE.findall(text)]
    # Obfuscated emails
    emails += _extract_obfuscated_emails(text)

    # Standard phone regex
    phones += [_clean_phone(m.group(0)) for m in PHONE_RE.finditer(text)]
    # Label-anchored phones (Tel:, Phone: etc.)
    phones += _extract_phones_labeled(text)

    address = _extract_address(soup, text)

    out.update(
        emails=_uniq(emails),
        phones=dedupe_phones(phones)[:20],
        social=_uniq(social),
        address=address,
        contact_page=contact_page,
        text_snippet=text[:500],
    )
    out["_text"] = text  # full text, used for AI extraction (not exported)
    return out
