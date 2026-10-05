"""Background scrape jobs: run in a thread, report progress, save to Excel."""
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from . import ai_extract, extract, places
from .excel import SECRET_KEYS
from .fetch import WORKERS, Fetcher, domain_of
from .search import SearchBlocked, web_search


REGIONS = {
    "wt-wt": "Worldwide", "in-en": "India", "us-en": "United States", "uk-en": "United Kingdom",
    "ae-en": "UAE", "au-en": "Australia", "ca-en": "Canada", "sg-en": "Singapore",
}

JOBS: dict[str, "Job"] = {}


def public_spec(spec: dict) -> dict:
    """The run settings without any secrets (safe to store in the database or show in the UI)."""
    return {k: v for k, v in spec.items() if k not in SECRET_KEYS}


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
        self.file = None
        self.error = None
        self.started = datetime.now()
        self.finished = None
        self.phase = "search"  # search -> visit -> save -> done
        self.activity = "Getting ready…"
        self.cancelled = threading.Event()
        self.problems: list[str] = []  # service errors; shown when a run ends with no rows

    def say(self, msg: str):
        self.log.append(f"{datetime.now():%H:%M:%S}  {msg}")
        self.log = self.log[-300:]

    def stats(self) -> dict:
        email_col, phone_col = ("Email", "Phone") if self.spec["mode"] == "places" else ("Emails", "Phone Numbers")
        return {
            "with_email": sum(1 for r in self.rows if r.get(email_col)),
            "with_phone": sum(1 for r in self.rows if r.get(phone_col)),
        }

    def to_dict(self, preview_rows=25):
        end = self.finished or datetime.now()
        return {
            "id": self.id, "status": self.status, "total": self.total, "done": self.done,
            "phase": self.phase, "activity": self.activity, "mode": self.spec["mode"],
            "elapsed": int((end - self.started).total_seconds()), **self.stats(),
            "count": len(self.rows), "columns": self.columns,
            "preview": [{c: _short(r.get(c)) for c in self.columns} for r in self.rows[-preview_rows:]],
            "log": self.log[-60:], "file": os.path.basename(self.file) if self.file else None,
            "sheet_id": getattr(self, "sheet_id", None), "error": self.error,
        }


def _short(v, n=120):
    if isinstance(v, list):
        v = "; ".join(map(str, v))
    v = "" if v is None else str(v)
    return v if len(v) <= n else v[:n] + "…"


def _safe_name(s: str) -> str:
    s = re.sub(r"[^\w\- ]+", "", s).strip().replace(" ", "_")
    return s[:50] or "scrape"


def start_job(spec: dict, app, user_id: int) -> Job:
    job = Job(spec, user_id)
    JOBS[job.id] = job
    threading.Thread(target=_run, args=(job, app, user_id), daemon=True).start()
    return job


def _run(job: Job, app, user_id: int):
    from models import db, Run
    import json
    with app.app_context():
        title = job.spec.get("file_name") or (job.spec.get("queries") or [job.spec.get("category", "scrape")])[0]
        run = Run(id=job.id, name=title, connector=job.spec["mode"], spec_json=json.dumps(public_spec(job.spec)),
                  owner_id=user_id, status="running")
        db.session.add(run)
        db.session.commit()

        job.status = "running"
        try:
            if job.spec["mode"] == "places":
                _run_places(job)
            else:
                _run_web(job)
            final = "cancelled" if job.cancelled.is_set() else "done"
            if final == "done" and not job.rows and job.problems:
                job.error, final = job.problems[-1], "error"
        except Exception as e:  # report any failure to the UI instead of crashing the thread
            job.error = f"{type(e).__name__}: {e}"
            final = "error"
            job.say("ERROR: " + job.error)
        
        if job.rows:
            job.phase, job.activity = "save", "Saving your Excel file…"
            try:
                _save(job, user_id)
            except Exception as e:
                job.error, final = f"Could not save Excel: {e}", "error"
        
        job.phase, job.finished = "done", datetime.now()
        job.activity = {"done": "All done!", "cancelled": "Stopped. Your data so far was saved.",
                        "error": "Something went wrong."}[final]
        job.status = final
        
        # update run record
        run = Run.query.get(job.id)
        if run:
            run.status = final
            run.total_items = job.total
            run.done_items = job.done
            run.row_count = len(job.rows)
            run.error = job.error
            run.completed_at = job.finished
            db.session.commit()


def _save(job: Job, user_id: int):
    from . import sheets
    title = job.spec.get("file_name") or (job.spec.get("queries") or [job.spec.get("category", "scrape")])[0]
    name = f"{_safe_name(title)}_{datetime.now():%Y%m%d_%H%M%S}.xlsx"
    info = {k: v for k, v in public_spec(job.spec).items() if v not in (None, "", [], False)}
    sheet = sheets.save(name, job.columns, job.rows, user_id, source="run", info=info, run_id=job.id)
    job.file, job.sheet_id = name, sheet.id
    job.say(f"Saved {len(job.rows)} rows to {name}")


# ---------------------------------------------------------------- web search mode

def _search(job: Job) -> list[dict]:
    spec = job.spec
    platforms = spec.get("platforms", ["web"])
    expanded_queries = []
    for q in spec["queries"]:
        if "web" in platforms:
            expanded_queries.append(q)
        for p in platforms:
            if p != "web":
                expanded_queries.append(f"{q} site:{p}")
                
    results, seen = [], set()
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
        new = 0
        for h in hits:
            url = h.get("href", "")
            key = url.rstrip("/").lower()
            if spec.get("one_per_site"):
                key = domain_of(url)
            if not url or key in seen:
                continue
            seen.add(key)
            results.append({"query": q, "title": h.get("title", ""), "url": url, "snippet": h.get("body", "")})
            new += 1
        job.say(f"  {new} new results")
        time.sleep(1)
    return results


def _merge_pages(base: dict, extra: dict):
    """Merge email/phone/social/address from an extra page into the base page dict."""
    for k in ("emails", "phones", "social"):
        base[k] = list(dict.fromkeys(base[k] + extra[k]))
    base["phones"] = extract.dedupe_phones(base["phones"])
    base["address"] = base["address"] or extra["address"]
    base["_text"] += "\n\n" + extra["_text"]


def _process_page(job: Job, fetcher: Fetcher, hit: dict) -> dict:
    spec = job.spec
    row = {
        "Search Query": hit["query"], "Result Title": hit["title"], "Source URL": hit["url"],
        "Website": domain_of(hit["url"]), "Search Snippet": hit["snippet"],
    }
    html, note = fetcher.get_html(hit["url"])
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

    for key in spec["fields"]:
        row[extract.STANDARD_FIELDS[key]] = page[key] if page else ""
    if spec["custom_fields"]:
        text = page["_text"] if page else f'{hit["title"]}\n{hit["snippet"]}'
        row.update(ai_extract.extract_fields(spec["custom_fields"], text, hit["url"], hit["query"], spec["ai"]))
    row["Fetch Status"] = note
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
    job.columns = (["Search Query", "Result Title", "Source URL", "Website", "Search Snippet"]
                   + [extract.STANDARD_FIELDS[k] for k in spec["fields"]]
                   + spec["custom_fields"] + ["Fetch Status"])
    hits = _search(job)
    job.total = len(hits)
    job.phase, job.activity = "visit", f"Reading {len(hits)} websites and picking out the details…"
    job.say(f"Visiting {len(hits)} pages…")
    fetcher = Fetcher(spec)
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
            if _keep(spec, row):
                job.rows.append(row)


# ---------------------------------------------------------------- places mode

def _enrich_place(job: Job, fetcher: Fetcher, row: dict) -> dict:
    url = row.get("Website", "")
    if url and not url.startswith("http"):
        url = "http://" + url
    if not url:
        return row
    html, note = fetcher.get_html(url)
    row["Website Status"] = note
    if not html:
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

    if page["emails"]:
        row["Email"] = "; ".join(dict.fromkeys(([row["Email"]] if row["Email"] else []) + page["emails"]))
    if not row["Phone"] and page["phones"]:
        row["Phone"] = "; ".join(extract.dedupe_phones(page["phones"]))
    if job.spec["custom_fields"]:
        row.update(ai_extract.extract_fields(job.spec["custom_fields"], page["_text"], url, row["Name"], job.spec["ai"]))
    return row


def _run_places(job: Job):
    spec = job.spec
    job.columns = list(places.PLACE_COLUMNS)
    if spec.get("enrich"):
        job.columns += spec["custom_fields"] + ["Website Status"]
    all_rows = []
    job.total = len(spec["locations"])
    for loc in spec["locations"]:
        if job.cancelled.is_set():
            break
        job.say(f'Finding "{spec["category"]}" in {loc}…')
        job.activity = f'Looking up {spec["category"].lower()} in {loc}…'
        try:
            found = places.search_places(spec["category"], loc, spec.get("name_filter", ""), spec["max_results"],
                                         notify=lambda msg: setattr(job, "activity", msg))
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
    if "Search Location" not in job.columns:
        job.columns.insert(0, "Search Location")

    if not spec.get("enrich"):
        job.rows = all_rows
        return

    with_site = [r for r in all_rows if r.get("Website")]
    job.rows = [r for r in all_rows if not r.get("Website")]
    job.total, job.done = len(with_site), 0
    job.phase, job.activity = "visit", f"Visiting {len(with_site)} websites to find emails and phone numbers…"
    job.say(f"Visiting {len(with_site)} websites for emails/phones…")
    fetcher = Fetcher(spec)
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
