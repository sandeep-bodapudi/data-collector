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
EMAIL_JUNK_TLDS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".css", ".js")
EMAIL_JUNK_DOMAINS = ("example.com", "domain.com", "email.com", "sentry.io", "wixpress.com",
                      "sentry-next.wixpress.com", "yourdomain.com", "company.com")

# Phone numbers: optional +country code, then 8-12 digits with common separators.
PHONE_RE = re.compile(r"(?<![\w/])(\+?\d{1,3}[\s.\-]?)?(\(?\d{2,5}\)?[\s.\-]?)\d{3,5}[\s.\-]?\d{3,5}(?![\w/])")

SOCIAL_HOSTS = {
    "facebook.com": "Facebook", "instagram.com": "Instagram", "linkedin.com": "LinkedIn",
    "twitter.com": "Twitter/X", "x.com": "Twitter/X", "youtube.com": "YouTube",
    "wa.me": "WhatsApp", "t.me": "Telegram", "pinterest.com": "Pinterest",
}


def _clean_email(e: str) -> str | None:
    e = e.strip().strip(".").lower()
    if e.endswith(EMAIL_JUNK_TLDS) or any(e.endswith("@" + d) or e.endswith("." + d) for d in EMAIL_JUNK_DOMAINS):
        return None
    if re.search(r"@\d+x\.", e):  # retina image names like logo@2x.png
        return None
    return e


def _clean_phone(raw: str) -> str | None:
    digits = re.sub(r"\D", "", raw)
    if not 10 <= len(digits) <= 13:
        return None
    if len(set(digits)) <= 2:  # 0000000000, 1111111111 ...
        return None
    if re.fullmatch(r"(19|20)\d{2}(19|20)\d{2}.*", digits):  # year ranges like 2019-2024
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


def parse(html: str, url: str) -> dict:
    """Extract every standard field from one page. Returns lists for multi-value fields."""
    soup = BeautifulSoup(html, "lxml")
    out = {k: [] if k in ("emails", "phones", "social") else "" for k in STANDARD_FIELDS}

    if soup.title and soup.title.string:
        out["title"] = soup.title.string.strip()
    meta = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", attrs={"property": "og:description"})
    if meta and meta.get("content"):
        out["description"] = meta["content"].strip()

    emails, phones, social, contact_page = [], [], [], ""
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        low = href.lower()
        if low.startswith("mailto:"):
            emails.append(_clean_email(href[7:].split("?")[0]))
        elif low.startswith("tel:"):
            phones.append(_clean_phone(href[4:]))
        else:
            absu = urljoin(url, href)
            host = urlparse(absu).netloc.lower().removeprefix("www.")
            for sh, name in SOCIAL_HOSTS.items():
                if host == sh or host.endswith("." + sh):
                    social.append(f"{name}: {absu}")
            label = (a.get_text(" ", strip=True) or "").lower()
            if not contact_page and ("contact" in low or "contact" in label) and urlparse(absu).netloc == urlparse(url).netloc:
                contact_page = absu

    address = ""
    for obj in _jsonld_objects(soup):
        if obj.get("email"):
            emails.append(_clean_email(str(obj["email"]).replace("mailto:", "")))
        if obj.get("telephone"):
            phones.append(_clean_phone(str(obj["telephone"])))
        if not address and obj.get("address"):
            address = _format_address(obj["address"])

    text = visible_text(soup)
    emails += [_clean_email(e) for e in EMAIL_RE.findall(text)]
    phones += [_clean_phone(m.group(0)) for m in PHONE_RE.finditer(text)]

    if not address:
        tag = soup.find("address")
        if tag:
            address = tag.get_text(" ", strip=True)
    if not address:
        m = re.search(r"(?:Address|Location)\s*[:\-]\s*(.{10,200}?)(?:\s(?:Phone|Tel|Email|Mobile|Call)\b|$)", text, re.I)
        if m:
            address = m.group(1).strip()

    out.update(
        emails=_uniq(emails),
        phones=dedupe_phones(phones)[:10],
        social=_uniq(social),
        address=address[:300],
        contact_page=contact_page,
        text_snippet=text[:500],
    )
    out["_text"] = text  # full text, used for AI extraction (not exported)
    return out
