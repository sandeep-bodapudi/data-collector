"""Background scrape jobs: run in a thread and report progress.

Nothing is stored on the server. A finished job's rows stay in memory only until the person's browser has
collected them (it saves them in its own IndexedDB) or RESULT_TTL passes.
"""
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from . import ai_extract, discover, extract, listings, places
from .excel import SECRET_KEYS
from .fetch import WORKERS, Fetcher, domain_of
from .search import SearchBlocked, web_search

REGIONS = {
    "wt-wt": "Worldwide", "in-en": "India", "us-en": "United States", "uk-en": "United Kingdom",
    "ae-en": "UAE", "au-en": "Australia", "ca-en": "Canada", "sg-en": "Singapore",
}

JOBS: dict[str, "Job"] = {}
RESULT_TTL = 2 * 3600  # seconds a finished job's rows are kept for the browser to collect


def public_spec(spec: dict) -> dict:
    """The run settings without any secrets (safe to show in the UI or save in the browser)."""
    return {k: v for k, v in spec.items() if k not in SECRET_KEYS}


def _text(v) -> str:
    if isinstance(v, list):
        return "; ".join(map(str, v))
    return "" if v is None else str(v)


class Job:
    def __init__(self, spec: dict, owner_id: int | None = None):
        self.id = uuid.uuid4().hex[:10]
        self.spec = spec
        self.owner_id = owner_id
        self.status = "queued"
        self.total = 0
        self.done = 0
        self.rows: list[dict] = []
        self.columns: list[str] = []
        self.log: list[str] = []
        self.error = None
        self.started = datetime.now()
        self.finished = None
        self.phase = "search"  # search -> visit -> done
        self.activity = "Getting ready…"
        self.cancelled = threading.Event()
        self.problems: list[str] = []  # service errors; shown when a run ends with no rows

    def say(self, msg: str):
        self.log.append(f"{datetime.now():%H:%M:%S}  {msg}")
        self.log = self.log[-300:]

    def stats(self) -> dict:
        # Column-name detection rather than a fixed mode -> columns mapping, so this also works for "enrich" mode,
        # whose columns are whatever the sheet being filled in already had (Places-style singular Email/Phone,
        # web-search-style plural Emails/Phone Numbers, or none at all).
        email_col = next((c for c in ("Emails", "Email") if c in self.columns), None)
        phone_col = next((c for c in ("Phone Numbers", "Phone") if c in self.columns), None)
        return {
            "with_email": sum(1 for r in self.rows if email_col and r.get(email_col)),
            "with_phone": sum(1 for r in self.rows if phone_col and r.get(phone_col)),
        }

    def to_dict(self, preview_rows=25):
        end = self.finished or datetime.now()
        return {
            "id": self.id, "status": self.status, "total": self.total, "done": self.done,
            "phase": self.phase, "activity": self.activity, "mode": self.spec["mode"],
            "elapsed": int((end - self.started).total_seconds()), **self.stats(),
            "count": len(self.rows), "columns": self.columns,
            "preview": [{c: _short(r.get(c)) for c in self.columns} for r in self.rows[-preview_rows:]],
            "log": self.log[-60:], "error": self.error,
        }

    def result(self) -> dict:
        """Everything the browser needs to save this run as a sheet (cells are plain text)."""
        return {"columns": self.columns,
                "rows": [{c: _text(r.get(c)) for c in self.columns} for r in self.rows]}


def _short(v, n=120):
    v = _text(v)
    return v if len(v) <= n else v[:n] + "…"


def purge_jobs():
    now = datetime.now()
    for jid, j in list(JOBS.items()):
        if j.finished and (now - j.finished).total_seconds() > RESULT_TTL:
            JOBS.pop(jid, None)


def start_job(spec: dict, user_id: int) -> Job:
    purge_jobs()
    job = Job(spec, user_id)
    JOBS[job.id] = job
    threading.Thread(target=_run, args=(job,), daemon=True).start()
    return job


def _run(job: Job):
    job.status = "running"
    try:
        if job.spec["mode"] == "places":
            _run_places(job)
        elif job.spec["mode"] == "enrich":
            _run_enrich(job)
        else:
            _run_web(job)
        final = "cancelled" if job.cancelled.is_set() else "done"
        if final == "done" and not job.rows and job.problems:
            job.error, final = job.problems[-1], "error"
    except Exception as e:  # report any failure to the UI instead of crashing the thread
        job.error = f"{type(e).__name__}: {e}"
        final = "error"
        job.say("ERROR: " + job.error)
    job.phase, job.finished = "done", datetime.now()
    job.activity = {"done": "All done!", "cancelled": "Stopped. The rows collected so far are kept.",
                    "error": "Something went wrong."}[final]
    job.status = final


# ---------------------------------------------------------------- web search mode

# Words too generic to tell a real match from an unrelated one - every query already contains most of these,
# so they can't be used to check whether a *result* actually matches what was asked for.
_GENERIC_QUERY_WORDS = {
    "engineering", "college", "colleges", "institute", "institutes", "institution", "institutions", "university",
    "universities", "school", "schools", "contact", "email", "emails", "phone", "phones", "mobile", "number",
    "numbers", "list", "lists", "website", "websites", "official", "site", "sites", "course", "courses",
    "admission", "admissions", "details", "information", "near", "with", "and", "for", "the", "best", "top",
}


def _signal_words(query: str) -> list[str]:
    """The distinctive words of a search query (mainly place names) - what a genuinely matching result ought to
    mention somewhere, even if only in the snippet. Strips a trailing "site:..." operator first."""
    base = re.sub(r"\s+site:\S+$", "", query)
    return [w for w in re.findall(r"[a-z0-9]+", base.lower()) if len(w) >= 4 and w not in _GENERIC_QUERY_WORDS]


def _on_topic(h: dict, url: str, signal: list[str]) -> bool:
    """A free search engine with nothing good to offer sometimes answers with loosely-related, generic pages
    instead of admitting it found nothing - e.g. asking for "engineering colleges in Bachupally, Hyderabad" can
    come back with Britannica's definition of engineering, or a US university's "What Do Engineers Do?" page.
    Neither mentions the place at all, which a real match almost always does (the search engine itself tends to
    bold/quote the place name in the snippet when a page is genuinely about it). So: if the query named anything
    distinctive, require it to show up somewhere in the result; if it didn't, there's nothing to check."""
    if not signal:
        return True
    text = f"{h.get('title', '')} {h.get('body', '')} {url}".lower()
    return any(s in text for s in signal)


# Domains under these suffixes are only issued to accredited Indian academic/government institutions (.ac.in and
# .edu.in registration both require UGC/AICTE-recognised status; .gov.in and .nic.in are government-only) - real
# evidence a result is a genuine Indian institution, independent of whether a short search snippet happens to
# literally repeat the queried place name. Real failure this fixes: "engineering colleges in Secunderabad, India"
# got back real college results whose SERP snippets just said the college name with no city/country mentioned at
# all (not every snippet repeats the obvious) - plain _on_topic rejected every one of them as "unrelated", and this
# query's result set happened to have no directory hit either, so there was nothing to confirm the area from.
_TRUSTED_TLDS = (".ac.in", ".edu.in", ".gov.in", ".nic.in")


def _is_trusted_domain(url: str) -> bool:
    return domain_of(url).endswith(_TRUSTED_TLDS)


def _search(job: Job) -> tuple[list[dict], list[dict]]:
    spec = job.spec
    platforms = spec.get("platforms", ["web"])
    expanded_queries = []
    for q in spec["queries"]:
        if "web" in platforms:
            expanded_queries.append(q)
        for p in platforms:
            if p != "web":
                expanded_queries.append(f"{q} site:{p}")

    results, listing_hits, seen = [], [], set()
    for q in expanded_queries:
        if job.cancelled.is_set():
            break
        job.say(f'Searching: "{q}"')
        job.activity = f'Searching the internet for "{q}"'
        try:
            hits = web_search(q, spec.get("region", "wt-wt"), spec["max_results"], job.say)
        except SearchBlocked as e:
            job.problems.append(str(e))
            hits = []
        signal = _signal_words(q)
        # Real failure, found on live micro-neighbourhood queries (e.g. "engineering colleges in Tarnaka"): a
        # genuine official college site for the exact right place still got dropped by _on_topic, because its own
        # homepage describes its location as "Hyderabad - 500 007" (city + PIN code), never the neighbourhood name
        # a hyperlocal query used - real institutions don't repeat hyperlocal names, only directory/listing pages do.
        # So: check whether any DIRECTORY hit (one that will be filtered out below anyway) confirms the engine
        # understood the place - directories put the area name in their own title/URL as a matter of course, so
        # that's real evidence, not a guess. Confirmation must come from a directory hit specifically, never from
        # an official-candidate hit itself (otherwise one good match would "vouch" for every other result in the
        # same batch, including genuine padding junk like Britannica/Oregon State - that's the original bug, and
        # since none of those are directory hits either, they correctly provide no such confirmation).
        def _is_directory(h):
            return "site:" not in q and not discover._is_official_candidate(h.get("href", ""))
        area_confirmed = not signal or any(_on_topic(h, h.get("href", ""), signal) for h in hits if _is_directory(h))
        new, off_topic, directory, dup = 0, 0, 0, 0
        for h in hits:
            url = h.get("href", "")
            if not url:
                continue
            key = domain_of(url) if spec.get("one_per_site") else url.rstrip("/").lower()
            if key in seen:
                dup += 1  # this exact page/site already showed up for an earlier search in this same run
                continue
            seen.add(key)
            if "site:" not in q and not discover._is_official_candidate(url):
                directory += 1  # a directory, social or map page lists other places; it is not one itself
                # Most directories have no dedicated parser (see scraper/listings.py) and are just dropped, same as
                # before - but a few do, and one of THEIR pages is a real list of 20-30+ real names worth keeping,
                # not one row for the directory page itself.
                if domain_of(url) in listings.LISTING_PARSERS:
                    listing_hits.append({"url": url})
                continue
            if not area_confirmed and not _is_trusted_domain(url) and not _on_topic(h, url, signal):
                off_topic += 1
                continue
            results.append({"query": q, "title": h.get("title", ""), "url": url, "snippet": h.get("body", "")})
            new += 1
        skipped = [f"{n} {label}" for n, label in ((off_topic, "unrelated"), (directory, "directory/listing"), (dup, "already seen"))
                   if n]
        job.say(f"  {new} new result{'s' if new != 1 else ''}" + (f" ({', '.join(skipped)} skipped)" if skipped else ""))
        if q != expanded_queries[-1]:
            time.sleep(_current_gap())  # measured-safe pacing; shorter automatically once an official search key is set
    return results, listing_hits


def _expand_listings(job: Job, fetcher: Fetcher, listing_hits: list[dict], known_names: set) -> list[dict]:
    """Turn each listing-page hit into one row per name found on it, instead of one row for the page itself.
    Just the name, address (when the listing shows one) and the directory's own profile link for that entry -
    not yet verified as the institution's own official site. _enrich_listed_rows fills in the real website and
    contacts for each afterwards."""
    rows = []
    for hit in listing_hits:
        if job.cancelled.is_set():
            break
        html, note = fetcher.get_html(hit["url"])
        if not html:
            job.say(f"  could not read listing page {hit['url']}: {note}")
            continue
        items = listings.extract_listing(html, hit["url"])
        added = 0
        for it in items:
            key = it["name"].lower()
            if key in known_names:
                continue
            known_names.add(key)
            rows.append({"Name": it["name"], "Address": it.get("address", ""), "Website": it["href"]})
            added += 1
        if items:
            dupes = len(items) - added
            job.say(f"  {domain_of(hit['url'])}: {added} new name{'s' if added != 1 else ''} from its listing"
                    + (f" ({dupes} already found elsewhere)" if dupes else ""))
    return rows


def _enrich_web_row(job: Job, fetcher: Fetcher, row: dict, spec: dict) -> dict:
    """Fill in contacts for a row that now has a verified website - the same contact-page crawl _process_page does
    for a normal search hit, just reused here for a row that started from a listing page instead."""
    url = row.get("Website", "")
    if url and not url.startswith("http"):
        url = "http://" + url
    if not url:
        return row
    html, note = fetcher.get_html(url)
    if not html:
        job.say(f"  could not read {url}: {note}")
        return row
    page = extract.parse(html, url)
    candidates = extract.candidate_contact_urls(url, page.get("contact_page", ""))
    visited = {url}
    for sub_url in candidates:
        if job.cancelled.is_set():
            break
        if sub_url in visited:
            continue
        if page["emails"] and page["phones"]:
            break
        visited.add(sub_url)
        sub_html, _ = fetcher.get_html(sub_url)
        if sub_html:
            _merge_pages(page, extract.parse(sub_html, sub_url))
    emails = _own_emails(page["emails"], url)
    if emails:
        existing = row["Emails"].split("; ") if row.get("Emails") else []
        row["Emails"] = "; ".join(dict.fromkeys(existing + emails))
    if not row.get("Phone Numbers") and page["phones"]:
        row["Phone Numbers"] = "; ".join(extract.dedupe_phones(page["phones"])[:3])
    if not row.get("Address") and page["address"]:
        row["Address"] = page["address"]
    if spec["custom_fields"]:
        row.update(ai_extract.extract_fields(spec["custom_fields"], page["_text"], url, row["Name"], spec["ai"]))
    return row


def _enrich_listed_rows(job: Job, fetcher: Fetcher, rows: list[dict], spec: dict):
    """A listing-expanded row starts with only a name (and maybe an address) - find each one's real website and
    read its contacts, the same two-step, search-engine-aware way Places mode already does: first the free guess
    (name.ac.in-style addresses, verified by reading the page - no search engine, no blocking risk), then a paced
    web search for whatever's left (capped at MAX_DISCOVER, same as Places, so one huge run can't hammer the free
    engines for hundreds of names at once)."""
    for r in rows:
        r["_listing_url"] = r.get("Website", "")  # the directory's own profile link - kept as a fallback only
        r["Website"], r["Search Location"] = "", r.get("Address", "") or "India"
    _guess_websites(job, fetcher, rows)
    if not job.cancelled.is_set():
        _discover_websites(job, rows)
    todo = [r for r in rows if r.get("Website")]
    job.phase, job.total, job.done = "visit", len(todo), 0
    job.say(f"Reading contact details from {len(todo)} verified website{'s' if len(todo) != 1 else ''}…")
    with ThreadPoolExecutor(WORKERS) as pool:
        futures = {pool.submit(_enrich_web_row, job, fetcher, r, spec): r for r in todo}
        for fut in as_completed(futures):
            if job.cancelled.is_set():
                pool.shutdown(wait=False, cancel_futures=True)
                break
            job.done += 1
            try:
                fut.result()
            except Exception as e:
                job.say(f"  failed: {e}")
    for r in rows:
        if not r.get("Website"):
            r["Website"] = r.pop("_listing_url", "")  # nothing verified - the directory link is the only lead left
        else:
            r.pop("_listing_url", None)
        r.pop("Search Location", None)  # not a web-search column; only used internally to steer the guess/search


def _merge_pages(base: dict, extra: dict):
    """Merge email/phone/social/address from an extra page into the base page dict."""
    for k in ("emails", "phones", "social"):
        base[k] = list(dict.fromkeys(base[k] + extra[k]))
    base["phones"] = extract.dedupe_phones(base["phones"])
    base["address"] = base["address"] or extra["address"]
    base["_text"] += "\n\n" + extra["_text"]


def _process_page(job: Job, fetcher: Fetcher, hit: dict) -> dict:
    spec = job.spec
    html, note = fetcher.get_html(hit["url"])
    if not html:
        job.say(f"  could not read {hit['url']}: {note}")
    page = extract.parse(html, hit["url"]) if html else None

    # Check contact/about/staff pages for more emails & phones (the "Contact Us pages" switch).
    if page is not None and spec.get("follow_contact", True):
        candidates = extract.candidate_contact_urls(hit["url"], page.get("contact_page", ""))
        visited = {hit["url"]}
        for sub_url in candidates:
            if job.cancelled.is_set():
                break
            if sub_url in visited:
                continue
            # We have enough data already; stop crawling sub-pages
            if page["emails"] and page["phones"]:
                break
            visited.add(sub_url)
            sub_html, _ = fetcher.get_html(sub_url)
            if sub_html:
                sub_page = extract.parse(sub_html, sub_url)
                _merge_pages(page, sub_page)

    # Only the real data: who it is, how to reach them, where the site is. Search/fetch details stay in the run log.
    row = {"Name": (page["name"] if page else "") or extract.clean_name(hit["title"])}
    for key in spec["fields"]:
        if key != "title":  # the name above already covers the page title
            row[extract.STANDARD_FIELDS[key]] = page[key] if page else ""
    row["Website"] = domain_of(hit["url"])
    if spec["custom_fields"]:
        text = page["_text"] if page else f'{hit["title"]}\n{hit["snippet"]}'
        row.update(ai_extract.extract_fields(spec["custom_fields"], text, hit["url"], hit["query"], spec["ai"]))
    return row


def _keep(spec: dict, row: dict) -> bool:
    need = spec.get("require")
    if need == "emails":
        return bool(row.get("Emails"))
    if need == "phones":
        return bool(row.get("Phone Numbers"))
    if need == "any_contact":
        return bool(row.get("Emails") or row.get("Phone Numbers"))
    return True


def _run_web(job: Job):
    spec = job.spec
    job.columns = (["Name"] + [extract.STANDARD_FIELDS[k] for k in spec["fields"] if k != "title"]
                   + ["Website"] + spec["custom_fields"])
    hits, listing_hits = _search(job)
    if spec.get("seed") in listings.KNOWN_SEEDS:
        seed = listings.KNOWN_SEEDS[spec["seed"]]
        job.say(f"Also pulling {seed['label']}…")
        listing_hits += [{"url": u} for u in seed["urls"]]
    job.total = len(hits) + len(listing_hits)
    job.phase, job.activity = "visit", f"Reading {len(hits)} websites and picking out the details…"
    job.say(f"Visiting {len(hits)} pages…")
    fetcher = Fetcher(spec)
    known_names = set()
    with ThreadPoolExecutor(WORKERS) as pool:
        futures = {pool.submit(_process_page, job, fetcher, h): h for h in hits}
        for fut in as_completed(futures):
            if job.cancelled.is_set():
                pool.shutdown(wait=False, cancel_futures=True)
                job.say("Stopped by user.")
                break
            job.done += 1
            try:
                row = fut.result()
            except Exception as e:
                job.say(f"  failed {futures[fut]['url']}: {e}")
                continue
            known_names.add(row["Name"].lower())
            if _keep(spec, row):
                job.rows.append(row)

    if listing_hits and not job.cancelled.is_set():
        job.activity = f"Reading {len(listing_hits)} listing page{'s' if len(listing_hits) != 1 else ''} for individual names…"
        job.say(f"Expanding {len(listing_hits)} listing page{'s' if len(listing_hits) != 1 else ''} into individual rows…")
        listed_rows = _expand_listings(job, fetcher, listing_hits, known_names)
        if listed_rows and not job.cancelled.is_set():
            _enrich_listed_rows(job, fetcher, listed_rows, spec)
        for row in listed_rows:
            if _keep(spec, row):
                job.rows.append(row)
        job.done = job.total


def _run_enrich(job: Job):
    """"Fill missing details" on an existing sheet: look up a website and contacts only for rows that don't already
    have one, leave every other row exactly as it was, and give back the whole sheet (filled where possible, still
    blank where nothing was found) rather than a filtered subset. Capped at MAX_DISCOVER rows per run on purpose,
    same as Places' website lookup - saving the result and running this again on THAT sheet only has the remaining,
    still-blank rows left to do (an already-filled row is never looked up again), so a big sheet gets done in a few
    runs instead of one very long one that free search engines are more likely to start refusing partway through."""
    spec = job.spec
    rows, columns = spec["rows"], spec["columns"]
    job.columns = columns
    website_col = "Website" if "Website" in columns else None
    email_col = next((c for c in ("Emails", "Email") if c in columns), None)
    phone_col = next((c for c in ("Phone Numbers", "Phone") if c in columns), None)

    def has_contact(r):
        return (email_col and r.get(email_col)) or (phone_col and r.get(phone_col))

    todo = [r for r in rows if r.get("Name") and not has_contact(r)]
    batch = todo[:MAX_DISCOVER]
    job.say(f"{len(rows)} rows: {len(rows) - len(todo)} already have contacts, {len(todo)} need a lookup"
            + (f" - doing the first {len(batch)} this run" if len(todo) > len(batch) else "") + ".")

    if batch:
        work = [{"Name": r["Name"], "Website": (r.get(website_col, "") if website_col else ""),
                 "Address": r.get("Address", ""), "_orig": r} for r in batch]
        fetcher = Fetcher(spec)
        _enrich_listed_rows(job, fetcher, work, spec)
        for w in work:
            orig = w["_orig"]
            if website_col and w.get("Website"):
                orig[website_col] = w["Website"]
            if email_col and w.get("Emails"):
                orig[email_col] = w["Emails"]
            if phone_col and w.get("Phone Numbers"):
                orig[phone_col] = w["Phone Numbers"]
            if "Address" in columns and not orig.get("Address") and w.get("Address"):
                orig["Address"] = w["Address"]

    job.rows = rows  # every row, not just the ones looked up this run - nothing gets dropped from the sheet
    filled = sum(1 for r in batch if has_contact(r))
    job.say(f"Filled in details for {filled} of {len(batch)} rows looked up this run."
            + (f" {len(todo) - len(batch)} more still need a lookup - save this sheet and run "
               "‘Fill missing details’ again to continue with the rest." if len(todo) > len(batch) else ""))
    job.phase, job.total, job.done = "done", len(rows), len(rows)


# ---------------------------------------------------------------- places mode

FREE_MAIL = ("gmail.com", "yahoo.com", "yahoo.in", "outlook.com", "hotmail.com", "rediffmail.com")


def _own_emails(emails: list[str], site: str) -> list[str]:
    """A site lists emails of web designers, partners and ad networks too. Keep the ones on the place's own domain (and
    plain Gmail/Yahoo-style ones, which small institutions really use); only if there are none, fall back to the first two."""
    root = discover.root_domain(domain_of(site))
    own = [e for e in emails if e.rsplit("@", 1)[-1] == root or e.rsplit("@", 1)[-1].endswith("." + root)]
    free = [e for e in emails if e.rsplit("@", 1)[-1] in FREE_MAIL and e not in own]
    return (own + free)[:5] or emails[:2]


def _enrich_place(job: Job, fetcher: Fetcher, row: dict) -> dict:
    url = row.get("Website", "")
    if url and not url.startswith("http"):
        url = "http://" + url
    if not url:
        return row
    html, note = fetcher.get_html(url)
    row["Website Status"] = note
    if not html:
        job.say(f"  could not read {url}: {note}")
        return row
    page = extract.parse(html, url)

    # Visit up to 3 contact-type sub-pages automatically
    candidates = extract.candidate_contact_urls(url, page.get("contact_page", ""))
    visited = {url}
    for sub_url in candidates:
        if job.cancelled.is_set():
            break
        if sub_url in visited:
            continue
        if page["emails"] and page["phones"]:
            break
        visited.add(sub_url)
        sub_html, _ = fetcher.get_html(sub_url)
        if sub_html:
            _merge_pages(page, extract.parse(sub_html, sub_url))

    emails = _own_emails(page["emails"], url)
    if emails:
        row["Email"] = "; ".join(dict.fromkeys(([row["Email"]] if row["Email"] else []) + emails))
    if not row["Phone"] and page["phones"]:
        row["Phone"] = "; ".join(extract.dedupe_phones(page["phones"])[:3])  # the first few are the contact ones; the rest are page noise
    if not row.get("Address") and page["address"]:
        row["Address"] = page["address"]
    if job.spec["custom_fields"]:
        row.update(ai_extract.extract_fields(job.spec["custom_fields"], page["_text"], url, row["Name"], job.spec["ai"]))
    return row


# Tunable without a code change (e.g. on Render: Environment tab). SEARCH_GAP paces BOTH "Search the web" queries
# (_search, above) and the Places website lookup (_discover_websites, below) - they go through the same
# scraper/search.py and hit the same free engines, so the same safe pacing applies to both.
# Defaults are measured against the free DuckDuckGo/Yahoo/Brave/Google/Mojeek/Startpage scraping fallback with no
# paid key: pushing the gap below ~5s reliably made every engine start refusing after just a handful of requests in
# testing (not a guess - see docs/PRD-status.md and README.md "Scaling this up"). A BRAVE_API_KEY or
# GOOGLE_CSE_KEY/GOOGLE_CSE_CX (see .env.example) removes this limit almost entirely: those are official APIs with
# their own daily quota instead of being rate-limited by IP, so this pacing is skipped whenever they're used.
MAX_DISCOVER = int(os.environ.get("DISCOVER_MAX_PER_RUN", "80"))      # places looked up per Places run
SEARCH_GAP = float(os.environ.get("DISCOVER_GAP_SECONDS", "5.0"))     # seconds between searches with no paid key
THROTTLE_WAIT = float(os.environ.get("DISCOVER_RETRY_WAIT", "25.0"))  # pause before the one retry on an empty result
# With an official key, Brave/Google enforce their own (generous) per-day quota rather than guessing at scripted use
# by IP, so the defensive gap above is unnecessary - a short, fixed pause is kept only to stay comfortably under
# the still-real per-second rate limit most API tiers apply (not a documented number to rely on exactly; lower it
# with DISCOVER_GAP_SECONDS once you know your plan's actual limit).
KEYED_GAP = float(os.environ.get("DISCOVER_KEYED_GAP_SECONDS", "1.1"))


def _current_gap() -> float:
    if os.environ.get("BRAVE_API_KEY") or (os.environ.get("GOOGLE_CSE_KEY") and os.environ.get("GOOGLE_CSE_CX")):
        return min(SEARCH_GAP, KEYED_GAP)  # never slower than the no-key pacing, only ever faster
    return SEARCH_GAP


def _guess_websites(job: Job, fetcher: Fetcher, rows: list[dict]):
    """First, with no search engine at all: try the address a college would normally have (initials + .ac.in etc.) and
    keep it only if the page really is that college's. Fast, and not subject to search-engine blocking."""
    todo = [r for r in rows if not r.get("Website") and r.get("Name")]
    if not todo:
        return
    job.phase, job.total, job.done = "visit", len(todo), 0
    job.say(f"Checking the likely website addresses of {len(todo)} places…")
    job.activity = f"Checking likely website addresses for {len(todo)} places…"

    def one(r):
        if job.cancelled.is_set():
            return None
        try:
            return discover.guess_website(fetcher, r["Name"], r.get("Search Location", ""), r.get("_aliases", []))
        except Exception:  # a guess that fails must never stop the run
            return None

    with ThreadPoolExecutor(WORKERS) as pool:
        for r, url in zip(todo, pool.map(one, todo)):
            job.done += 1
            if url:
                r["Website"], r["Website Source"] = url, "Guessed from the name and verified"
                job.say(f"  {r['Name']}: {url}")
    job.say(f"  found {sum(1 for r in todo if r.get('Website'))} of {len(todo)} that way.")


def _discover_websites(job: Job, rows: list[dict]):
    """The map rarely lists a website. Search the web for each such place's own site, so its contact details can be read."""
    todo = [r for r in rows if not r.get("Website") and r.get("Name")]
    if not todo:
        return
    batch = todo[:MAX_DISCOVER]
    gap = _current_gap()
    job.phase, job.total, job.done = "visit", len(batch), 0
    job.say(f"Looking for the websites of {len(batch)} places that the map doesn't give one for "
            f"(about {gap:g} seconds each{', using your search key' if gap < SEARCH_GAP else ', to keep the search engines happy'})…")
    seen: dict[str, str | None] = {}  # the same name (several branches) is searched once
    for r in batch:
        if job.cancelled.is_set():
            return
        key = r["Name"].lower().strip()
        job.activity = f"Looking for the website of {r['Name']}…"
        if key not in seen:
            try:
                url, n_hits = discover.find_website(r["Name"], r.get("Search Location", ""), job.say, r.get("_aliases", []), gap)
                # Zero results is only treated as throttling (worth a wait-and-retry) on the free, unpaced-by-quota
                # path. With an official key, a quota problem already surfaced as an error inside web_search() and
                # fell through to the free engines, so zero hits here just means this place genuinely had none.
                if n_hits == 0 and gap >= SEARCH_GAP:
                    job.activity = "The search engines are limiting requests, waiting a moment…"
                    job.say(f"  no search results for {r['Name']}; waiting {THROTTLE_WAIT:g} s and trying once more")
                    time.sleep(THROTTLE_WAIT)
                    url, n_hits = discover.find_website(r["Name"], r.get("Search Location", ""), job.say, r.get("_aliases", []), gap)
                    if n_hits == 0:
                        job.say("  The search engines are still limiting requests, so the remaining websites were not "
                                "looked up. Try again later, or ask your admin to add a Brave Search key.")
                        return
                seen[key] = url
            except discover.SearchBlocked:
                job.say("  Web search refused the requests, so the remaining websites were not looked up. "
                        "Try again later, or ask your admin to add a Brave Search key.")
                return
            except Exception as e:  # one failed lookup must not stop the rest
                job.say(f"  lookup failed for {r['Name']}: {type(e).__name__}")
                seen[key] = None
            time.sleep(gap)
        url = seen[key]
        if url:
            r["Website"], r["Website Source"] = url, "Found by web search"
            job.say(f"  {r['Name']}: {url}")
        else:
            job.say(f"  {r['Name']}: no official website found")
        job.done += 1
    if len(todo) > len(batch):
        job.say(f"  Looked up the first {len(batch)} of {len(todo)} places. Run again with a smaller area for the rest.")


def _run_places(job: Job):
    spec = job.spec
    job.columns = list(places.OUTPUT_COLUMNS) + (spec["custom_fields"] if spec.get("enrich") else [])
    all_rows = []
    job.total = len(spec["locations"])
    for loc in spec["locations"]:
        if job.cancelled.is_set():
            break
        job.say(f'Finding "{spec["category"]}" in {loc}…')
        job.activity = f'Looking up {spec["category"].lower()} in {loc}…'
        try:
            found = places.search_places(spec["category"], loc, spec.get("name_filter", ""), spec["max_results"],
                                         notify=lambda msg: setattr(job, "activity", msg), info=job.say)
        except places.PlaceError as e:
            job.say(f"  {e}")
            job.problems.append(str(e))
            found = []
        except Exception as e:  # network problems with one location shouldn't stop the others
            job.say(f"  failed: {type(e).__name__}: {e}")
            job.problems.append(f"Could not reach the map service ({type(e).__name__}). Please try again.")
            found = []
        for r in found:
            r["Search Location"] = loc
        job.say(f"  {len(found)} places found")
        all_rows += found
        job.done += 1
        time.sleep(1)

    all_rows, merged = discover.merge_duplicates(all_rows)  # one place listed twice on the map under similar names
    if merged:
        job.say(f"Merged {merged} duplicate map listing{'s' if merged != 1 else ''} of the same place.")

    if not spec.get("enrich"):
        job.rows = all_rows
        return

    for r in all_rows:
        if r.get("Website"):
            r["Website Source"] = "Map"
    fetcher = Fetcher(spec)
    if spec.get("find_websites", True):
        _guess_websites(job, fetcher, all_rows)
        if job.cancelled.is_set():
            return
        _discover_websites(job, all_rows)
        if not job.cancelled.is_set():
            all_rows, merged = discover.merge_duplicates(all_rows)  # now also by shared website
            if merged:
                job.say(f"Merged {merged} more duplicate listing{'s' if merged != 1 else ''} (same website, close together).")

    with_site = [r for r in all_rows if r.get("Website")]
    job.rows = [r for r in all_rows if not r.get("Website")]
    job.total, job.done = len(with_site), 0
    job.phase, job.activity = "visit", f"Visiting {len(with_site)} websites to find emails and phone numbers…"
    job.say(f"Visiting {len(with_site)} websites for emails/phones…")
    with ThreadPoolExecutor(WORKERS) as pool:
        futures = {pool.submit(_enrich_place, job, fetcher, r): r for r in with_site}
        for fut in as_completed(futures):
            if job.cancelled.is_set():
                pool.shutdown(wait=False, cancel_futures=True)
                job.say("Stopped by user.")
                break
            job.done += 1
            try:
                job.rows.append(fut.result())
            except Exception as e:
                job.say(f"  website failed: {e}")
                job.rows.append(futures[fut])
    job.rows.sort(key=lambda r: not (r.get("Email") or r.get("Phone")))  # places with contact details first
