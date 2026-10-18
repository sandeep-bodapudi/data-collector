"""How far to trust an email address: cheap checks that need no paid service, shown as a column.

Phase 5 of docs/v2/free-paid-run-flow-plan.md. ZoomInfo/Apollo's most common complaint is "verified" emails that
bounce. We do not claim verification we cannot do. We say what we actually checked:

  - is it a free-mail address (Gmail, Yahoo...) rather than the company's own?
  - does its domain match the company's website?
  - does the domain exist, and does it publish a mail server (MX record)?
  - is it a shared inbox (info@, admin@...)?

and turn that into High / Medium / Low with the reason in words. We never connect to a mail server (no SMTP probing:
it is slow, often blocked, and gets servers' attention), so "High" means "looks like the company's real address",
not "will definitely deliver".

DNS uses dnspython when installed (real MX lookup). Without it we can only tell that a domain resolves, and say so.
"""
from __future__ import annotations

import os
import re
import socket
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout, as_completed
from datetime import date
from urllib.parse import urlparse

try:  # optional: a real MX lookup. Without it the check degrades to "the domain resolves".
    import dns.exception
    import dns.resolver
    HAVE_DNS = True
except Exception:  # pragma: no cover - exercised by tests through the resolver hook
    HAVE_DNS = False

QUALITY_COLUMNS = ["Email Quality", "Last Checked"]
EMAIL_COLUMNS = ("Emails", "Email")
WEBSITE_COLUMN = "Website"

FREE_MAIL = frozenset({
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.in", "yahoo.co.in", "ymail.com", "hotmail.com", "outlook.com",
    "live.com", "msn.com", "rediffmail.com", "aol.com", "icloud.com", "me.com", "protonmail.com", "proton.me",
    "zoho.com", "mail.com", "gmx.com", "yandex.com",
})
ROLE_LOCALS = frozenset({
    "info", "admin", "administrator", "contact", "hello", "hi", "support", "sales", "office", "enquiry", "enquiries",
    "inquiry", "inquiries", "mail", "team", "help", "service", "services", "accounts", "hr", "careers", "admissions",
    "admission", "principal", "director", "webmaster", "postmaster", "noreply", "no-reply",
})
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,24}$")
_SECOND_LEVEL = frozenset({"co", "com", "ac", "edu", "org", "gov", "net", "nic"})
MAX_DOMAINS = 300
DNS_TIMEOUT = 3.0
DNS_BUDGET_SECONDS = 25
_RANK = {"High": 3, "Medium": 2, "Low": 1}

_CACHE: dict[str, str] = {}


def split_emails(value) -> list[str]:
    return [e.strip() for e in re.split(r"[;,\s]+", str(value or "")) if "@" in e]


def host_of(url: str) -> str:
    u = (url or "").strip()
    if not u:
        return ""
    try:
        h = urlparse(u if "//" in u else "//" + u).hostname or ""
    except ValueError:
        return ""
    return h.lower().removeprefix("www.")


def org_domain(host: str) -> str:
    """The part of a host that identifies the organisation: mail.college.ac.in -> college.ac.in."""
    parts = [p for p in (host or "").lower().removeprefix("www.").split(".") if p]
    if len(parts) <= 2:
        return ".".join(parts)
    if len(parts[-1]) == 2 and parts[-2] in _SECOND_LEVEL:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def domain_state(domain: str, resolver=None) -> str:
    """mx (publishes a mail server) | exists (resolves; MX could not be checked) | a (resolves, no MX record)
    | none (does not exist or refuses mail) | unknown (lookup failed). Cached for the life of the process."""
    d = (domain or "").lower().strip(".")
    if not d:
        return "none"
    if resolver is not None:
        return resolver(d)
    # QUALITY_DNS=0 turns lookups off (they are real network calls); the test suite sets SKIP_STARTUP_PROBES=1 for the
    # same reason, so that also counts. Addresses are then rated without the mail-server check.
    if os.environ.get("QUALITY_DNS") == "0" or os.environ.get("SKIP_STARTUP_PROBES") == "1":
        return "unknown"
    if d in _CACHE:
        return _CACHE[d]
    state = _lookup(d)
    if state != "unknown":
        _CACHE[d] = state
    return state


def _lookup(d: str) -> str:
    if HAVE_DNS:
        res = dns.resolver.Resolver()
        res.lifetime = DNS_TIMEOUT
        try:
            answers = res.resolve(d, "MX")
            hosts = [str(r.exchange).rstrip(".") for r in answers]
            if hosts and all(h == "" for h in hosts):
                return "none"  # a "null MX" says the domain accepts no mail at all
            return "mx"
        except dns.resolver.NXDOMAIN:
            return "none"
        except dns.resolver.NoAnswer:
            try:
                res.resolve(d, "A")
                return "a"
            except dns.resolver.NXDOMAIN:
                return "none"
            except Exception:
                return "a" if _resolves(d) else "unknown"
        except Exception:
            return "unknown"
    return "exists" if _resolves(d) else "unknown"


def _resolves(d: str) -> bool:
    try:
        socket.getaddrinfo(d, None)
        return True
    except OSError:
        return False


def assess(email: str, site_host: str, state: str) -> tuple[str, str]:
    """(level, reason) for one address. `state` is domain_state() of its domain."""
    e = (email or "").strip()
    if not _EMAIL_RE.match(e):
        return "Low", "not a valid address"
    local, _, dom = e.lower().rpartition("@")
    if state == "none":
        return "Low", "its domain does not exist or accepts no mail"
    shared = " (shared inbox)" if local.split("+")[0] in ROLE_LOCALS else ""
    if dom in FREE_MAIL:
        return "Medium", "free mail address, not the company's own domain" + shared
    match = bool(site_host) and org_domain(dom) == org_domain(site_host)
    server = {"mx": "mail server found", "exists": "domain exists", "a": "domain has no mail server record",
              "unknown": "mail server not checked"}.get(state, "mail server not checked")
    if match and state in ("mx", "exists"):
        return "High", f"company's own domain, {server}" + shared
    if match:
        return "Medium", f"company's own domain, but {server}" + shared
    if not site_host:
        return "Medium", f"no website to compare with, {server}" + shared
    return "Medium", "different domain from the company's website" + shared


def _best(emails: list[str], site_host: str, states: dict[str, str]) -> tuple[str, str]:
    best = ("", "")
    for e in emails:
        dom = e.lower().rpartition("@")[2]
        level, why = assess(e, site_host, states.get(dom, "unknown"))
        if not best[0] or _RANK[level] > _RANK[best[0]]:
            best = (level, why)
    return best


def annotate(rows: list[dict], columns: list[str], resolver=None, today: date | None = None) -> list[str]:
    """Add 'Email Quality' and 'Last Checked' to rows that have an email. Returns the new column list (the same
    list when the sheet has no email column at all). Never raises for odd data; DNS is time-boxed."""
    email_cols = [c for c in EMAIL_COLUMNS if c in columns]
    if not email_cols:
        return list(columns)
    when = (today or date.today()).isoformat()
    per_row = []
    domains: list[str] = []
    for r in rows:
        emails = [e for c in email_cols for e in split_emails(r.get(c))]
        per_row.append(emails)
        for e in emails:
            d = e.lower().rpartition("@")[2]
            if d and d not in domains:
                domains.append(d)
    states = _states(domains[:MAX_DOMAINS], resolver)
    for r, emails in zip(rows, per_row):
        if not emails:
            continue
        level, why = _best(emails, host_of(r.get(WEBSITE_COLUMN, "")), states)
        r["Email Quality"] = f"{level} - {why}"
        r["Last Checked"] = when
    out = list(columns)
    for c in QUALITY_COLUMNS:
        if c not in out:
            out.append(c)
    return out


def _states(domains: list[str], resolver=None) -> dict[str, str]:
    if not domains:
        return {}
    out: dict[str, str] = {}
    with ThreadPoolExecutor(min(8, len(domains))) as pool:
        futs = {pool.submit(domain_state, d, resolver): d for d in domains}
        try:
            for f in as_completed(futs, timeout=DNS_BUDGET_SECONDS):
                try:
                    out[futs[f]] = f.result()
                except Exception:
                    out[futs[f]] = "unknown"
        except FutureTimeout:
            pass  # out of time: whatever is missing counts as "not checked"
        for f in futs:
            f.cancel()
    return out
