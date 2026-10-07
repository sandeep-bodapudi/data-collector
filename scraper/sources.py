"""User-defined directory sources: a list page and a way to read each entry on it, saved as data rather than code.

A source (saved per person in the app) says where the listing pages are and how to read them:

  list_urls   1-10 page addresses (http or https)
  pages       how many pages of each list to read (1-30); page N adds ?<page_param>=N to the address
  page_param  the query name used for pages (default "page")
  mode        "jsonld": read the schema.org list the page embeds (ItemList of places with name, url and address).
                        Many listing sites publish this, and it doesn't depend on how the page is laid out.
              "css":    read each entry with CSS selectors: item_selector (one per entry), name_selector,
                        and optionally link_selector and address_selector.
  profile     true: open each entry's own page and read its phone, email and website from it.

Nothing about a particular site is written in code. Presets (scraper/source_presets.json) are only starting points
that get copied into a person's own sources when they choose one.
"""
import json
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from .fetch import domain_of

MAX_PAGES = 30
MAX_LIST_URLS = 10
MAX_SOURCES_PER_USER = 50
PHONE_RE = re.compile(r"(?<![\w/])(\+?\d[\d\s-]{7,}\d)(?![\w/])")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
SITE_RE = re.compile(r"\b(?:Website|Web ?site|Web|Site)\s*:?\s*((?:https?://)?(?:www\.)?[\w-]+(?:\.[\w-]+)+(?:/\S*)?)", re.I)
PHONE_LABEL_RE = re.compile(r"\b(?:Phone|Telephone|Tel|Mobile|Contact(?: No)?)\b[^\d+]{0,20}?((?:\+?\d[\d\s,/()-]{6,}))", re.I)
JUNK_EMAIL_DOMAINS = ("example.", "sentry", "wixpress", "domain.", "email.com", "yourdomain")


import os as _os

PRESETS_PATH = _os.path.join(_os.path.dirname(__file__), "source_presets.json")


def _load_presets() -> list[dict]:
    try:
        with open(PRESETS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


PRESETS = _load_presets()  # starting points only; a person's own copy is what actually gets used


class SourceError(ValueError):
    """A source that can't be used, with a message a person can act on."""


def validate(config: dict) -> dict:
    """Return a clean copy of a source's settings, or raise SourceError saying what to fix."""
    if not isinstance(config, dict):
        raise SourceError("The source settings are missing.")
    urls = config.get("list_urls")
    if isinstance(urls, str):
        urls = [u.strip() for u in urls.splitlines() if u.strip()]
    if not isinstance(urls, list) or not urls:
        raise SourceError("Add at least one list page address.")
    clean_urls = []
    for u in urls[:MAX_LIST_URLS]:
        u = str(u).strip()
        parsed = urlparse(u)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise SourceError(f"This is not a web address: {u[:80]}")
        clean_urls.append(u)
    try:
        pages = int(config.get("pages") or 1)
    except (TypeError, ValueError):
        raise SourceError("Pages must be a number.")
    if not 1 <= pages <= MAX_PAGES:
        raise SourceError(f"Pages must be between 1 and {MAX_PAGES}.")
    page_param = re.sub(r"[^\w-]", "", str(config.get("page_param") or "page"))[:30] or "page"
    mode = config.get("mode")
    if mode not in ("jsonld", "css"):
        raise SourceError("Choose how to read the list: structured data, or CSS selectors.")
    clean = {"list_urls": clean_urls, "pages": pages, "page_param": page_param, "mode": mode,
             "profile": bool(config.get("profile"))}
    if mode == "css":
        for key in ("item_selector", "name_selector"):
            sel = str(config.get(key) or "").strip()
            if not sel:
                raise SourceError("CSS mode needs the item and name selectors.")
            try:
                BeautifulSoup("", "lxml").select(sel)
            except Exception:
                raise SourceError(f"This selector is not valid CSS: {sel[:80]}")
            clean[key] = sel[:300]
        for key in ("link_selector", "address_selector"):
            sel = str(config.get(key) or "").strip()[:300]
            if sel:
                try:
                    BeautifulSoup("", "lxml").select(sel)
                except Exception:
                    raise SourceError(f"This selector is not valid CSS: {sel[:80]}")
            clean[key] = sel
    return clean


def list_urls(config: dict) -> list[str]:
    """Every page to read: each list address, with its page numbers added."""
    out = []
    for base in config["list_urls"]:
        for n in range(1, config["pages"] + 1):
            if n == 1:
                out.append(base)
            else:
                sep = "&" if "?" in base else "?"
                out.append(f"{base}{sep}{config['page_param']}={n}")
    return out


def _address_text(addr) -> str:
    if isinstance(addr, str):
        return addr.strip()
    if isinstance(addr, dict):
        parts = [addr.get(k, "") for k in ("streetAddress", "addressLocality", "addressRegion", "postalCode")]
        return ", ".join(str(p).strip() for p in parts if p and str(p).strip())
    return ""


def _walk(obj, found: list):
    if isinstance(obj, dict):
        if isinstance(obj.get("itemListElement"), list):
            found.extend(obj["itemListElement"])
        for v in obj.values():
            _walk(v, found)
    elif isinstance(obj, list):
        for v in obj:
            _walk(v, found)


def extract_items(html: str, base_url: str, config: dict) -> list[dict]:
    """Entries on one list page, as {name, address, href}. Never raises: a page that doesn't match gives nothing."""
    out, seen = [], set()

    def add(name, address, href):
        name = re.sub(r"\s+", " ", name or "").strip()[:150]
        if len(name) < 3 or name.lower() in seen:
            return
        seen.add(name.lower())
        out.append({"name": name, "address": (address or "").strip()[:300], "href": href or ""})

    try:
        if config.get("mode") == "jsonld":
            soup = BeautifulSoup(html, "lxml")
            found = []
            for tag in soup.find_all("script", type="application/ld+json"):
                try:
                    _walk(json.loads(tag.string or ""), found)
                except (ValueError, TypeError):
                    continue
            for el in found:
                item = el.get("item", el) if isinstance(el, dict) else {}
                if isinstance(item, dict):
                    add(item.get("name", ""), _address_text(item.get("address")),
                        urljoin(base_url, item.get("url", "")) if item.get("url") else "")
        else:
            soup = BeautifulSoup(html, "lxml")
            for el in soup.select(config["item_selector"]):
                name_el = el.select_one(config["name_selector"])
                if not name_el:
                    continue
                name = name_el.get_text(" ", strip=True)
                link_el = el.select_one(config["link_selector"]) if config.get("link_selector") else el.select_one("a[href]")
                href = urljoin(base_url, link_el["href"]) if link_el and link_el.get("href") else ""
                address = ""
                if config.get("address_selector"):
                    addr_el = el.select_one(config["address_selector"])
                    address = addr_el.get_text(" ", strip=True) if addr_el else ""
                    if address.startswith(name):  # the address cell often starts with the name itself
                        address = address[len(name):].strip(" ,")
                add(name, address, href)
    except Exception:  # a page that changed shape yields less, never an error for the whole run
        return out
    return out


def extract_profile_generic(html: str, url: str) -> dict:
    """Phone, email, website and address from any entry's own page: the page's structured data first, then the
    visible text around labels such as Phone, Mobile, Email and Website."""
    phones, emails, website, address = [], [], "", ""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except (ValueError, TypeError):
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                stack.extend(node.values())
                if node.get("telephone"):
                    phones.append(str(node["telephone"]))
                if node.get("email"):
                    emails.append(str(node["email"]).replace("mailto:", ""))
                if not address and node.get("address"):
                    address = _address_text(node["address"])
    for tag in soup(["script", "style"]):
        tag.decompose()
    text = re.sub(r"\s+", " ", soup.get_text(" "))
    phones += [m.group(1).strip(" ,/") for m in PHONE_LABEL_RE.finditer(text)]
    emails += EMAIL_RE.findall(text)
    # The directory's own address (for its newsletter, support, etc.) is not the entry's contact.
    own = domain_of(url).replace("www.", "")
    emails = [e.rstrip(".") for e in emails
              if not any(j in e.lower() for j in JUNK_EMAIL_DOMAINS) and not e.rsplit("@", 1)[-1].endswith(own)]
    m = SITE_RE.search(text)
    if m:
        site = m.group(1).rstrip(".")
        website = site if site.startswith("http") else "https://" + site
    # A link to the directory itself is not the entry's own website.
    if website and domain_of(website) == domain_of(url):
        website = ""
    digits_ok = [p for p in dict.fromkeys(phones) if len(re.sub(r"\D", "", p)) >= 7]
    return {"phones": digits_ok[:3], "emails": list(dict.fromkeys(emails))[:3], "website": website, "address": address}
