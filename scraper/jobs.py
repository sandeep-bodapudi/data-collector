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
from urllib.parse import urlparse

from . import ai_extract, apify_actors, discover, extract, listings, places, quality, search, sources
from . import topic as topic_words
from .excel import SECRET_KEYS
from .fetch import WORKERS, Fetcher, domain_of
from .search import SearchBlocked, web_search

REGIONS = {
    "wt-wt": "Worldwide", "in-en": "India", "us-en": "United States", "uk-en": "United Kingdom",
    "ae-en": "UAE", "au-en": "Australia", "ca-en": "Canada", "sg-en": "Singapore",
}

JOBS: dict[str, "Job"] = {}
RESULT_TTL = 2 * 3600  # seconds a finished job's rows are kept for the browser to collect
MAX_EXPANDED_QUERIES = 40  # cap on extra synonym searches added to one run, however many queries it has


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
        self.effective: dict = {}  # preflight result: what will actually be used
        self.remote = False  # True when a queue worker (not this process) runs the job

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
            "log": self.log[-60:], "error": self.error, "effective": self.effective,
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
    try:
        from . import job_store

        job_store.clean(RESULT_TTL)
    except Exception:
        pass


def _persist_job(job: "Job", status: str, finished: bool = True) -> None:
    """Save a job snapshot to disk (public spec only, never secrets).

    finished=False writes a running snapshot with no finish time, so the TTL purge
    (which falls back to file age) can never delete a still-running job's snapshot.
    """
    try:
        from . import job_store

        end = job.finished or datetime.now()
        try:
            stats = job.stats()
        except Exception:
            stats = {"with_email": 0, "with_phone": 0}
        job_store.persist(job.id, {
            "owner_id": job.owner_id,
            "mode": job.spec.get("mode", "web"),
            "spec_public": public_spec(job.spec),
            "effective": job.effective if isinstance(job.effective, dict) else {},
            "columns": job.columns,
            "rows": [{c: _text(r.get(c)) for c in job.columns} for r in job.rows],
            "status": status,
            "error": job.error,
            "log_tail": job.log[-60:],
            "phase": job.phase,
            "activity": job.activity,
            "total": job.total,
            "done": job.done,
            "elapsed": int((end - job.started).total_seconds()) if finished else 0,
            "with_email": stats.get("with_email", 0),
            "with_phone": stats.get("with_phone", 0),
            "finished_at": time.time() if finished else 0,
        })
    except Exception:
        pass


class StoredJob:
    """A finished job rehydrated from disk. Read-only: collect, then delete."""

    def __init__(self, job_id: str, payload: dict):
        import threading as _th

        self.id = job_id
        self.owner_id = payload.get("owner_id")
        self.status = payload.get("status", "done")
        self.mode = payload.get("mode", "web")
        self.columns = payload.get("columns", [])
        self._rows = payload.get("rows", [])
        self.error = payload.get("error")
        self.log = payload.get("log_tail", [])
        self.effective = payload.get("effective", {})
        self.elapsed = payload.get("elapsed", 0)
        self.with_email = payload.get("with_email", 0)
        self.with_phone = payload.get("with_phone", 0)
        self.total = payload.get("total", len(self._rows))
        self.done = payload.get("done", len(self._rows))
        try:
            self.total, self.done = int(self.total), int(self.done)
        except (TypeError, ValueError):
            self.total = self.done = len(self._rows)
        self.phase = payload.get("phase") or ("done" if self.status != "running" else "search")
        self.activity = payload.get("activity") or (
            {"done": "All done!",
             "cancelled": "Stopped. The rows collected so far are kept.",
             "error": "Something went wrong."}.get(self.status, "All done!")
            if self.status != "running" else "Still going…")
        self.cancelled = _th.Event()

    def to_dict(self, preview_rows=25):
        preview = [{c: _short(r.get(c)) for c in self.columns} for r in self._rows[-preview_rows:]]
        return {"id": self.id, "status": self.status, "total": self.total, "done": self.done,
                "phase": self.phase, "activity": self.activity,
                "mode": self.mode,
                "elapsed": self.elapsed, "with_email": self.with_email, "with_phone": self.with_phone,
                "count": len(self._rows), "columns": self.columns,
                "preview": preview, "log": self.log[-60:], "error": self.error,
                "effective": self.effective, "stored": True}

    def result(self) -> dict:
        return {"columns": self.columns,
                "rows": [{c: _text(r.get(c)) for c in self.columns} for r in self._rows]}


def load_stored_job(job_id: str, allow_running: bool = False):
    """Return a StoredJob from the store, or None. No secrets ever leave the payload."""
    try:
        from . import job_store

        payload = job_store.retrieve(job_id)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("status") in ("queued", "running") and not allow_running:
        return None
    try:
        return StoredJob(job_id, payload)
    except Exception:
        return None


def rehydrate_jobs() -> dict:
    """Boot recovery: running markers become error records; expired payloads purged."""
    converted, kept, now = 0, 0, time.time()
    try:
        from . import job_store

        for jid in job_store.all_ids():
            payload = job_store.retrieve(jid)
            if not isinstance(payload, dict):
                continue
            if payload.get("status") in ("queued", "running"):
                payload["status"] = "error"
                payload["error"] = ("Server restarted while this run was going. "
                                    "Start again — nothing was collected.")
                payload["finished_at"] = now
                job_store.persist(jid, payload)
                converted += 1
            else:
                kept += 1
        purged = job_store.clean(RESULT_TTL)
    except Exception:
        return {"converted": 0, "kept": 0, "purged": 0}
    return {"converted": converted, "kept": kept, "purged": purged}


def _watch_remote(job: Job, interval: float = 2.0, snapshot_every: float = 15.0) -> None:
    """Worker-side loop: honour the disk cancel flag and persist running snapshots
    so the web process (which only sees disk) can report progress. Exits on finish."""
    import time as _t

    try:
        from . import job_store

        job_store.clear_cancel(job.id)
    except Exception:
        pass
    elapsed = 0.0
    while not job.finished:
        _t.sleep(interval)
        elapsed += interval
        try:
            from . import job_store

            if job_store.cancel_requested(job.id):
                job.cancelled.set()
        except Exception:
            pass
        if elapsed >= snapshot_every and not job.finished:
            elapsed = 0.0
            _persist_job(job, "running", finished=False)
    try:
        from . import job_store

        job_store.cancel_clear(job.id)
    except Exception:
        pass


def run_queued_job(job_id: str, spec: dict, user_id: int) -> str:
    """RQ worker entrypoint. Must stay importable as 'scraper.jobs.run_queued_job'.
    Rebuilds the job with its original id, runs it, persists progress + result."""
    job = Job(spec, user_id)
    job.id = job_id
    job.remote = True
    JOBS[job_id] = job
    watcher = threading.Thread(target=_watch_remote, args=(job,), daemon=True)
    watcher.start()
    try:
        _run_with_context(job)
    finally:
        watcher.join(timeout=30)
    return job.status


def start_job(spec: dict, user_id: int) -> Job:
    purge_jobs()
    job = Job(spec, user_id)
    job.remote = False
    JOBS[job.id] = job
    try:
        from . import job_store

        # Crash marker: finished_at stays 0 while running, so the TTL purge (which
        # falls back to file age) can never delete a still-running job's marker.
        job_store.persist(job.id, {"owner_id": user_id, "mode": spec.get("mode", "web"),
                                "spec_public": public_spec(spec), "effective": {},
                                "columns": [], "rows": [], "status": "running",
                                "error": None, "log_tail": [], "finished_at": 0,
                                "started_at": time.time()})
    except Exception:
        pass
    try:
        from . import queue as _queue

        if _queue.worker_mode() == "rq":
            if _queue.enqueue_job(job.id, spec, user_id) is not None:
                job.remote = True
                return job
    except Exception:
        pass
    threading.Thread(target=_run_with_context, args=(job,), daemon=True).start()
    return job


def _run_with_context(job: Job) -> None:
    """Run a job with a Flask app context so DB-backed helpers (toggles, budgets,
    spend tracking) work in the worker thread. Falls back to context-free run rather
    than killing the job if context setup itself fails."""
    try:
        import app as appmod  # deferred: app imports this module at load time

        with appmod.app.app_context():
            _run(job)
            return
    except Exception:
        pass
    _run(job)


def _keyed(job: Job, fn):
    """Worker threads don't inherit the job's Brave key, so each one sets it before it searches."""
    key = getattr(job, "spec", {}).get("brave_key", "")

    def run(*args):
        with search.search_keys(key):
            return fn(*args)
    return run


def preflight(spec: dict) -> dict:
    """Compute effective services for this job. Pure + fast. O(n) services.

    Returns {"search": [...], "places": [...], "ai_ok": bool, "notes": [...]dio}.
    Respects admin conn_* flags and cache health (fresh-unhealthy skipped).
    """
    from .config import FREE_PLACES_ORDER, FREE_SEARCH_ORDER, is_enabled

    def _unhealthy(name: str) -> bool:
        from .connection_health import is_unhealthy

        return is_unhealthy(name)

    notes: list[str] = []
    search_eff: list[str] = []
    search_flaky: list[str] = []   # enabled and usable, but the last health probe failed
    for name in FREE_SEARCH_ORDER:
        if not is_enabled(name):
            notes.append(f"skipped {name} (disabled)")
            continue
        if _unhealthy(name):
            notes.append(f"skipped {name} (last check failed)")
            if not (name == "brave" and not (spec.get("brave_key") or search.brave_key_set())) and not (
                    name == "google_cse" and not (os.environ.get("GOOGLE_CSE_KEY") and os.environ.get("GOOGLE_CSE_CX"))):
                search_flaky.append(name)
            continue
        if name == "brave" and not (spec.get("brave_key") or search.brave_key_set()):
            notes.append("skipped brave (no key)")
            continue
        if name == "google_cse" and not (os.environ.get("GOOGLE_CSE_KEY") and os.environ.get("GOOGLE_CSE_CX")):
            notes.append("skipped google_cse (no key)")
            continue
        search_eff.append(name)
    if not search_eff and search_flaky:
        # A failed probe is a hint, not proof: one blip must not leave a keyless install with no search for 30 minutes.
        search_eff = search_flaky
        notes.append("every search engine failed its last check; trying " + ", ".join(search_flaky) + " anyway")
    places_eff: list[str] = []
    places_flaky: list[str] = []
    for name in FREE_PLACES_ORDER:
        if not is_enabled(name):
            notes.append(f"skipped {name} (disabled)")
            continue
        if _unhealthy(name):
            notes.append(f"skipped {name} (last check failed)")
            places_flaky.append(name)
            continue
        places_eff.append(name)
    if not places_eff and places_flaky:
        places_eff = places_flaky
        notes.append("every map source failed its last check; trying " + ", ".join(places_flaky) + " anyway")
    ai_ok = True
    if spec.get("custom_fields"):
        try:
            from .ai_extract import ai_available

            ai_ok = ai_available(spec.get("ai", {}))
        except Exception:
            ai_ok = bool(spec.get("ai", {}).get("key"))
        if not ai_ok:
            notes.append("AI off (no key or disabled); continuing without custom fields")
    social_providers: list[str] = []
    estimate_usd, estimate_inr = 0.0, 0.0
    if spec.get("mode") == "social":
        from .config import SOCIAL_PROVIDER_ORDER

        if not is_enabled("social"):
            notes.append("skipped social (master disabled in Admin → Connections)")
        else:
            for name in SOCIAL_PROVIDER_ORDER:
                if not is_enabled(name):
                    notes.append(f"skipped {name} (disabled)")
                    continue
                if _unhealthy(name):
                    notes.append(f"skipped {name} (last check failed)")
                    continue
                if name == "brightdata" and not (spec.get("brightdata_key") or ""):
                    notes.append("skipped brightdata (no key)")
                    continue
                if name == "apify" and not (spec.get("apify_key") or ""):
                    notes.append("skipped apify (no key)")
                    continue
                social_providers.append(name)
        try:
            from .social_base import estimate_cost

            n_items = len(spec.get("items", [])) or 0
            estimate_usd, estimate_inr = estimate_cost(n_items, social_providers) if social_providers else (0.0, 0.0)
        except Exception:
            pass
        if social_providers and not spec.get("free_only"):
            lim_usd, lim_inr = _social_limits()
            if lim_usd <= 0 and lim_inr <= 0:
                notes.append("no monthly social budget is set; paid enrichment stays off until an admin sets one")
    # Paid Apify data for a Deep Places / Enrich run: only with the person's own token, the admin's switch on and the
    # Apify token not known-broken. Standard runs never get these.
    paid_places: list[str] = []
    paid_contacts: list[str] = []
    if spec.get("depth") == "deep" and spec.get("mode") in ("places", "enrich"):
        wanted, bucket = (("apify_places", paid_places) if spec["mode"] == "places" else ("apify_contacts", paid_contacts))
        if not is_enabled(wanted):
            notes.append(f"skipped {wanted} (disabled in Admin \u2192 Connections)")
        elif not (spec.get("apify_key") or ""):
            notes.append(f"skipped {wanted} (no Apify token)")
        elif _unhealthy("apify"):
            notes.append(f"skipped {wanted} (Apify's last check failed)")
        else:
            bucket.append(wanted)
    return {"search": search_eff, "places": places_eff, "ai_ok": ai_ok, "notes": notes,
            "social": social_providers, "estimate_usd": estimate_usd, "estimate_inr": estimate_inr,
            "paid_places": paid_places, "paid_contacts": paid_contacts}


def _run(job: Job):
    job.status = "running"
    try:
        eff = preflight(job.spec)
        job.effective = eff
        for n in eff.get("notes", []):
            job.say(f"  {n}")
        mode = job.spec.get("mode", "web")
        if mode == "web" and not eff.get("search"):
            raise SearchBlocked("All search options are disabled or unavailable. "
                                "Enable Brave, Google CSE or free engines in Admin → Connections.")
        if mode == "places" and not eff.get("places"):
            from .places import PlaceError

            raise PlaceError("All map services are disabled. Enable Overpass, Photon, "
                             "Nominatim or Wikidata in Admin → Connections.")
        if mode == "enrich" and not eff.get("search"):
            job.say("  continuing with guess-only website lookup (web search disabled or unavailable); "
                    "website discovery may find fewer sites")
        if mode == "social" and not eff.get("social"):
            job.say("  social providers unavailable (disabled, no key, or last check failed); "
                    "running free lookup only")
        with search.search_keys(job.spec.get("brave_key", "")):
            if job.spec["mode"] == "places":
                _run_places(job)
            elif job.spec["mode"] == "enrich":
                _run_enrich(job)
            elif job.spec["mode"] == "social":
                _run_social(job)
            else:
                _run_web(job)
        _annotate_quality(job)
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
    if final in ("done", "cancelled", "error"):
        _persist_job(job, final)  # every terminal state lands on disk; empty ones too, so a
        # restart never turns a finished run into a mysterious 404
    try:
        from models import log_audit

        log_audit(job.owner_id, "job.finished",
                  f"{job.spec.get('mode', 'web')} run {job.id}: {final} ({len(job.rows)} rows)")
    except Exception:
        pass
    try:
        from . import job_store

        job_store.cancel_clear(job.id)  # a finished job must never look stoppable later
    except Exception:
        pass


def _annotate_quality(job: Job) -> None:
    """Add 'Email Quality' / 'Last Checked' (and, for Places, 'Data Source') to a finished collection. Free checks only,
    never raises: a failure here must not turn a good run into an error."""
    spec = job.spec
    if spec.get("mode") not in ("web", "places", "enrich") or spec.get("names_only") or not job.rows:
        return
    try:
        if spec["mode"] == "places":
            for r in job.rows:
                r["Data Source"] = "Google Maps (paid)" if r.get("_paid") else "OpenStreetMap"
            if "Data Source" not in job.columns:
                job.columns = job.columns + ["Data Source"]
        if not any(c in job.columns for c in quality.EMAIL_COLUMNS):
            return
        job.activity = "Checking email addresses\u2026"
        job.columns = quality.annotate(job.rows, job.columns)
        rated = [r for r in job.rows if r.get("Email Quality")]
        if rated:
            high = sum(1 for r in rated if r["Email Quality"].startswith("High"))
            job.say(f"Checked {len(rated)} email address{'es' if len(rated) != 1 else ''}: {high} look like the company's own "
                    f"working address. See the Email Quality column.")
    except Exception as e:  # noqa: BLE001
        job.say(f"  skipped the email check ({type(e).__name__})")


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


# Titles that say "dictionary definition", in English or Chinese - a whole class of padding junk a struggling
# engine returns when it has nothing real (seen live: baike.baidu.com, iciba.com, dictionary.cambridge.org and
# friends answering "digital marketing ... centers in Ameerpet, Hyderabad" with what the word "digital" means).
# These pages are never a company, school, hospital or place - unless the person was actually asking for one
# ("what does X mean"), in which case the query itself carries the same words and the filter stands down.
_DEF_TITLE_WORDS = ("definition", "define", "meaning", "translation", "translate", "dictionary", "thesaurus",
                    "encyclopedia", "synonym", "antonym", "意思", "翻译", "词典", "字典", "百科", "音标", "读音",
                    "释义", "发音", "例句", "用法")
_PLACE_PREP_RE = re.compile(r"\b(in|near|at|around|across)\b")


def _looks_like_definition(title: str, query: str) -> bool:
    low = (title or "").lower()
    if not any(w in low for w in _DEF_TITLE_WORDS):
        return False
    asked = (query or "").lower()
    return not any(w in asked for w in _DEF_TITLE_WORDS)


def _word_in(word: str, text: str) -> bool:
    """Whole-word match for Latin words (so "co" never matches "college"); plain
    substring for anything else (Chinese has no word boundaries)."""
    if re.fullmatch(r"[a-z0-9]+", word):
        return re.search(r"\b" + re.escape(word) + r"\b", text) is not None
    return word in text


def _topic_place_words(query: str) -> tuple[list[str], list[str]]:
    """Split a query into (topic, place) at the last in/near/at. Topic words describe
    *what* (kept generic-inclusive: "college" in a title is real evidence of kind);
    place words describe *where*. Short tokens are dropped - they match everything."""
    base = re.sub(r"\s+site:\S+$", "", query or "")
    parts = _PLACE_PREP_RE.split(base.lower())
    if len(parts) >= 3:
        before, after = "".join(parts[:-2]), parts[-1]
    else:
        before, after = base, ""
    topic = [w for w in re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]+", before.lower()) if len(w) >= 3]
    place = [w for w in re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]+", after.lower()) if len(w) >= 3]
    return topic, place


# URL shapes that say "this page lists many things" rather than "this page IS one
# company" - tag/topic hubs, on-site search results, top-N listicles and list indexes.
# Seen live: a GitHub Topics page (github.com/topics/digital-marketing) kept as a company
# row for a coaching-centers query, on the strength of one shared word in its title.
# These join the directory bucket: expanded when a parser exists for the domain,
# otherwise counted and dropped - never kept as a row. Kept conservative on purpose:
# /best-services style company pages (no number, no list noun) are NOT matched, and
# platform-targeted site: searches skip this entirely.
_AGGREGATOR_PATH_RE = re.compile(
    r"/(topics?|tags?|search|results?|explore)(/|$|\?)"
    r"|/(top|best)[-/]\d"
    r"|/(lists?|listings?|directories?|companies|businesses)[-/]"
    r"|[?&](q|s|search|query|keyword|keywords)=", re.I)


def _is_aggregator_page(url: str) -> bool:
    try:
        path = urlparse(url).path.lower()
        qs = urlparse(url).query.lower()
    except Exception:
        return False
    if re.search(r"[?&](q|s|search|query|keyword|keywords)=", "?" + qs):
        return True
    return _AGGREGATOR_PATH_RE.search(path) is not None


def _keep_hit(h: dict, url: str, query: str, area_confirmed: bool, wide: bool = False) -> bool:
    """The strict keep-rule for one search hit. Trusted academic/government domains always
    pass. Dictionary-definition titles never pass (unless asked for). Everything else must
    show a topic word in its title or domain - snippets echo the query too often to trust -
    plus either a place word anywhere or a directory hit confirming the engine got the area.
    A query with no place part only needs the topic-in-title check.

    Topic words are compared stemmed (centres = centers, Coach = coaching, Institutes = institute) and
    filler words ("best", "contact", "phone"...) don't count as topic or place evidence - see scraper/topic.py.
    `wide` (set when only collecting names) also accepts the category's synonyms (training, academy, institute
    for a coaching query): a wrong extra name there is cheap to delete, a missed company is not coming back.
    Junk classes stay out either way: definition titles above, directory/aggregator pages in _search."""
    if _is_trusted_domain(url):
        return True
    title = h.get("title", "") or ""
    if _looks_like_definition(title, query):
        return False
    topic, place = _topic_place_words(query)
    if not topic and not place:
        return True  # nothing distinctive to check, same as _on_topic
    topic = topic_words.drop_noise(topic)                      # keeps them all if that would leave nothing
    place = topic_words.drop_noise(place, keep_if_empty=False)  # a place made only of filler is no place
    title_zone = f"{title} {domain_of(url)}"
    if not topic_words.topic_hit(list(dict.fromkeys(topic)), title_zone, wide=wide):
        return False
    if not place:
        return True
    if area_confirmed:
        return True
    everywhere = f"{title} {h.get('body', '') or ''} {url}".lower()
    return any(_word_in(p, everywhere) for p in place)


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

    wide = bool(spec.get("names_only"))
    if spec.get("expand_synonyms") and "web" in platforms:
        # Opt-in: also search the category word's usual equivalents ("training institutes" for "coaching
        # centers"). More searches means a longer run, which is why it is a toggle and not the default.
        base = list(expanded_queries)
        extra: list[str] = []
        for q in spec["queries"]:
            extra += topic_words.expand_query(q)
        extra = [q for q in dict.fromkeys(extra) if q not in base][:MAX_EXPANDED_QUERIES]
        if extra:
            expanded_queries = base + extra
            job.say(f"Also searching {len(extra)} similar wordings (training/institute/academy style synonyms)…")

    results, listing_hits, seen = [], [], set()
    searched, answered = 0, 0
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
        searched += 1
        answered += bool(hits)
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
        dropped_titles: list[str] = []  # names-only runs show a few, so a missed company is visible, not silent
        for h in hits:
            url = h.get("href", "")
            if not url:
                continue
            key = domain_of(url) if spec.get("one_per_site") else url.rstrip("/").lower()
            if key in seen:
                dup += 1  # this exact page/site already showed up for an earlier search in this same run
                continue
            seen.add(key)
            if "site:" not in q and (not discover._is_official_candidate(url) or _is_aggregator_page(url)):
                directory += 1  # a directory, social, map, tag-hub or search-results page lists other places;
                # it is not one itself. Aggregator-shaped pages join this bucket even on otherwise-"official"
                # domains, so a GitHub Topics hub or a "top 10" listicle can never become a company row.
                # Most directories have no dedicated parser (see scraper/listings.py) and are just dropped, same as
                # before - but a few do, and one of THEIR pages is a real list of 20-30+ real names worth keeping,
                # not one row for the directory page itself.
                if domain_of(url) in listings.LISTING_PARSERS:
                    listing_hits.append({"url": url})
                continue
            if not _keep_hit(h, url, q, area_confirmed, wide=wide):
                off_topic += 1
                if wide and len(dropped_titles) < 5:
                    dropped_titles.append(f"“{(h.get('title', '') or '').strip()[:60]}” ({domain_of(url)})")
                continue
            results.append({"query": q, "title": h.get("title", ""), "url": url, "snippet": h.get("body", "")})
            new += 1
        skipped = [f"{n} {label}" for n, label in ((off_topic, "unrelated"), (directory, "directory/listing"), (dup, "already seen"))
                   if n]
        job.say(f"  {new} new result{'s' if new != 1 else ''}" + (f" ({', '.join(skipped)} skipped)" if skipped else ""))
        if dropped_titles:
            job.say("  skipped as unrelated: " + "; ".join(dropped_titles) + (" …" if off_topic > len(dropped_titles) else ""))
        if q != expanded_queries[-1]:
            time.sleep(_current_gap())  # measured-safe pacing; shorter automatically once an official search key is set
    if searched and not answered and not job.cancelled.is_set() and not (spec.get("brave_key") or search.brave_key_set()):
        # Seen in production: every free engine refuses a cloud server's address, so even "IT companies in Hyderabad"
        # came back empty and the run used to end as a quiet "done, 0 rows". Say what actually happened instead.
        job.problems.append("No search engine answered any of these searches. Free engines usually refuse requests "
                            "from cloud servers, so on the hosted app a search key is needed: add a free Brave "
                            "Search key in Connections & settings \u2192 Keys, or ask your admin to share one.")
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
        recipe = hit.get("recipe")
        items = sources.extract_items(html, hit["url"], recipe) if recipe else listings.extract_listing(html, hit["url"])
        added = 0
        for it in items:
            key = it["name"].lower()
            if key in known_names:
                continue
            known_names.add(key)
            row = {"Name": it["name"], "Address": it.get("address", ""), "Website": it["href"]}
            if recipe and recipe.get("profile"):
                row["_profile_ok"] = True  # the person's source asked for each entry's own page to be read
            rows.append(row)
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


def _read_profiles(job: Job, fetcher: Fetcher, rows: list[dict]):
    """Read each row's own profile page on its directory (e.g. colleges9.in's per-college page) for its phone number,
    email, address and official website. Plain page fetches, not search, so they run in parallel. A row whose profile
    gives an official website skips the free guess and the search entirely."""
    todo = [r for r in rows if r.get("_listing_url") and (r.get("_profile_ok") or listings.is_profile_url(r["_listing_url"]))]
    if not todo:
        return
    job.phase, job.total, job.done = "visit", len(todo), 0
    job.say(f"Reading the contact details on {len(todo)} college profile pages…")
    job.activity = f"Reading contact details for {len(todo)} colleges from their directory pages…"

    def one(r):
        html, _ = fetcher.get_html(r["_listing_url"])
        return listings.extract_profile(html, r["_listing_url"]) if html else None

    with ThreadPoolExecutor(WORKERS) as pool:
        futures = {pool.submit(_keyed(job, one), r): r for r in todo}
        for fut in as_completed(futures):
            if job.cancelled.is_set():
                pool.shutdown(wait=False, cancel_futures=True)
                break
            job.done += 1
            r = futures[fut]
            try:
                p = fut.result()
            except Exception:
                continue
            if not p:
                continue
            if p["phones"] and not r.get("Phone Numbers"):
                r["Phone Numbers"] = "; ".join(p["phones"])
            if p["emails"] and not r.get("Emails"):
                r["Emails"] = "; ".join(p["emails"])
            if p["address"] and not r.get("Address"):
                r["Address"] = p["address"]
            if p["website"]:
                r["Website"] = p["website"]


def _enrich_listed_rows(job: Job, fetcher: Fetcher, rows: list[dict], spec: dict):
    """A listing-expanded row starts with only a name (and maybe an address) - find each one's real website and
    read its contacts, the same two-step, search-engine-aware way Places mode already does: first the free guess
    (name.ac.in-style addresses, verified by reading the page - no search engine, no blocking risk), then a paced
    web search for whatever's left (capped at MAX_DISCOVER, same as Places, so one huge run can't hammer the free
    engines for hundreds of names at once)."""
    for r in rows:
        r["_listing_url"] = r.get("Website", "")  # the directory's own profile link - kept as a fallback only
        r["Website"], r["Search Location"] = "", r.get("Address", "") or "India"
    _read_profiles(job, fetcher, rows)  # the directory's own profile pages come first: they carry the phone/email
    _guess_websites(job, fetcher, rows)
    if not job.cancelled.is_set():
        _discover_websites(job, rows)
    todo = [r for r in rows if r.get("Website")]
    job.phase, job.total, job.done = "visit", len(todo), 0
    job.say(f"Reading contact details from {len(todo)} verified website{'s' if len(todo) != 1 else ''}…")
    with ThreadPoolExecutor(WORKERS) as pool:
        futures = {pool.submit(_keyed(job, _enrich_web_row), job, fetcher, r, spec): r for r in todo}
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


def _discovery_row(name: str, source: str, location: str) -> dict:
    from .config import DISCOVERY_COLUMNS, NAME_ONLY_STATUS

    row = {"Company Name": (name or "").strip()[:150], "Source": (source or "")[:200],
           "Location / Search": (location or "")[:200], "Status": NAME_ONLY_STATUS}
    return {c: row[c] for c in DISCOVERY_COLUMNS}


def _run_web_names(job: Job):
    """Stage 1 discovery for web search: names only, no page visits.

    Search results and listing pages are read for names, and every name found is
    kept immediately in job.rows (the browser saves them into a sheet as they
    arrive, so stopping early loses nothing). Directory sources work the same way.
    """
    from .config import DISCOVERY_COLUMNS

    spec = job.spec
    job.columns = list(DISCOVERY_COLUMNS)
    job.say("Saving company names as we find them… You can stop anytime and continue later.")
    job.activity = "Saving company names as we find them…"
    hits, listing_hits = _search(job)
    for src in spec.get("sources") or []:
        job.say(f"Also pulling {src['name']}…")
        listing_hits += [{"url": u, "recipe": src["config"]} for u in sources.list_urls(src["config"])]
    job.total = len(hits) + len(listing_hits)
    job.phase = "search"
    known: set[str] = set()

    def keep(name: str, source: str, location: str):
        name = (name or "").strip()
        if len(name) < 2 or name.lower() in known:
            return
        known.add(name.lower())
        job.rows.append(_discovery_row(name, source, location))
        job.done += 1

    fetcher = Fetcher(spec)
    for h in hits:
        if job.cancelled.is_set():
            break
        url = h.get("url", "")  # _search() normalizes hits to {query, title, url, snippet}
        if not url:
            continue
        if "site:" not in h.get("query", "") and not discover._is_official_candidate(url):
            if domain_of(url) in listings.LISTING_PARSERS:
                listing_hits.append({"url": url})
            continue
        keep(extract.clean_name(h.get("title", "")), domain_of(url), h.get("query", ""))
    for hit in listing_hits:
        if job.cancelled.is_set():
            break
        html, note = fetcher.get_html(hit["url"])
        if not html:
            job.say(f"  could not read listing page {hit['url']}: {note}")
            job.done += 1
            continue
        recipe = hit.get("recipe")
        try:
            items = sources.extract_items(html, hit["url"], recipe) if recipe \
                else listings.extract_listing(html, hit["url"])
        except Exception:
            items = []
        origin = domain_of(hit["url"])
        added = 0
        for it in items:
            before = len(known)
            keep(it["name"], origin, it.get("address", ""))
            added += len(known) - before
        job.say(f"  {origin}: {added} new name{'s' if added != 1 else ''} from its listing")
        job.done += 1
    if job.cancelled.is_set():
        job.say("Stopped by user. The names saved so far are kept.")
    else:
        job.say(f"Saved {len(job.rows)} company names. Open the sheet and use ‘Fill missing details’ when ready.")


def _run_web(job: Job):
    spec = job.spec
    if spec.get("names_only"):
        return _run_web_names(job)
    job.columns = (["Name"] + [extract.STANDARD_FIELDS[k] for k in spec["fields"] if k != "title"]
                   + ["Website"] + spec["custom_fields"])
    hits, listing_hits = _search(job)
    for src in spec.get("sources") or []:
        job.say(f"Also pulling {src['name']}…")
        listing_hits += [{"url": u, "recipe": src["config"]} for u in sources.list_urls(src["config"])]
    job.total = len(hits) + len(listing_hits)
    job.phase, job.activity = "visit", f"Reading {len(hits)} websites and picking out the details…"
    job.say(f"Visiting {len(hits)} pages…")
    fetcher = Fetcher(spec)
    known_names = set()
    with ThreadPoolExecutor(WORKERS) as pool:
        futures = {pool.submit(_keyed(job, _process_page), job, fetcher, h): h for h in hits}
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
    runs instead of one very long one that free search engines are more likely to start refusing partway through.

    Two-stage sheets may call their name column "Company Name" (see NAME_COLUMN_ALIASES);
    attempted rows get a Status of Enriched / Partial / No website found."""
    from .config import (DEFAULT_STATUS_COLUMN, NAME_COLUMN_ALIASES, STATUS_ENRICHED,
                         STATUS_NO_WEBSITE, STATUS_PARTIAL)

    spec = job.spec
    rows, columns = spec["rows"], spec["columns"]
    name_col = spec.get("name_column") or next((c for c in NAME_COLUMN_ALIASES if c in columns), None)
    if not name_col or name_col not in columns:
        raise ValueError("This sheet has no Name or Company Name column, so there's nothing to look up by.")
    status_col = (spec.get("status_column") or DEFAULT_STATUS_COLUMN)[:200] or DEFAULT_STATUS_COLUMN
    if status_col not in columns:
        columns = columns + [status_col]
    job.columns = columns
    website_col = "Website" if "Website" in columns else None
    email_col = next((c for c in ("Emails", "Email") if c in columns), None)
    phone_col = next((c for c in ("Phone Numbers", "Phone") if c in columns), None)

    def has_contact(r):
        return (email_col and r.get(email_col)) or (phone_col and r.get(phone_col))

    def needs_work(r):
        if not (r.get(name_col) or "").strip():
            return False
        if website_col and not (r.get(website_col) or "").strip():
            return True
        return not has_contact(r)

    wanted = spec.get("row_ids")
    if isinstance(wanted, list) and wanted:
        wanted_idx = {i for i in wanted if isinstance(i, int) and 0 <= i < len(rows)}
        todo = [rows[i] for i in sorted(wanted_idx) if needs_work(rows[i])]
        job.say(f"{len(wanted_idx)} selected rows: {len(todo)} still need details.")
    else:
        todo = [r for r in rows if needs_work(r)]
    batch = todo[:MAX_DISCOVER]
    job.say(f"{len(rows)} rows: {len(rows) - len(todo)} already complete, {len(todo)} need a lookup"
            + (f" - doing the first {len(batch)} this run" if len(todo) > len(batch) else "") + ".")
    job.activity = (f"Looking for websites and contacts… {len(todo)} companies still need details"
                    if todo else "Checking the sheet…")

    def mark_status(orig, site: str):
        contact = has_contact(orig)
        if site and contact:
            orig[status_col] = STATUS_ENRICHED
        elif site or contact:
            orig[status_col] = STATUS_PARTIAL
        else:
            orig[status_col] = STATUS_NO_WEBSITE

    if batch:
        work = [{"Name": r[name_col], "Website": (r.get(website_col, "") if website_col else ""),
                 "Address": r.get("Address", ""), "_orig": r} for r in batch]
        fetcher = Fetcher(spec)
        _enrich_listed_rows(job, fetcher, work, spec)
        _paid_contacts(job, work)
        for w in work:
            orig = w["_orig"]
            had_emails = {e.strip() for e in str(orig.get(email_col, "") or "").split(";") if e.strip()} if email_col else set()
            if website_col and w.get("Website"):
                orig[website_col] = w["Website"]
            if email_col and w.get("Emails"):
                orig[email_col] = w["Emails"]
            if phone_col and w.get("Phone Numbers"):
                orig[phone_col] = w["Phone Numbers"]
            if "Address" in columns and not orig.get("Address") and w.get("Address"):
                orig["Address"] = w["Address"]
            site = (orig.get(website_col, "") if website_col else "") or w.get("Website", "")
            mark_status(orig, site)
            # Company-only rule, stated on the row: free-mail addresses are kept only when
            # the company's own site publishes no company-domain address. The note goes on
            # Source only when that column exists (discovery sheets always have it).
            if email_col and "Source" in columns and not (orig.get("Source") or "").strip():
                final_emails = [e.strip() for e in str(orig.get(email_col, "") or "").split(";") if e.strip()]
                added = [e for e in final_emails if e not in had_emails]
                if added and not any(e.rsplit("@", 1)[-1] not in FREE_MAIL for e in final_emails if "@" in e):
                    domain = domain_of(site) if site else ""
                    orig["Source"] = (f"Company website ({domain}) — free-mail address, no company email published"
                                      if domain else "Company website — free-mail address, no company email published")[:200]

    job.rows = rows  # every row, not just the ones looked up this run - nothing gets dropped from the sheet
    filled = sum(1 for r in batch if has_contact(r))
    job.say(f"Filled in details for {filled} of {len(batch)} rows looked up this run."
            + (f" {len(todo) - len(batch)} more still need a lookup - save this sheet and run "
               "‘Fill missing details’ again to continue with the rest." if len(todo) > len(batch) else ""))
    job.say(f"All done – {sum(1 for r in rows if has_contact(r))} companies now have contact details.")
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


PACING_PRESETS = {"conservative": (8.0, 2.0), "standard": None, "fast": (2.0, 0.6)}


def _gaps() -> tuple[float, float]:
    """(search_gap, keyed_gap). Admin preset wins when set to conservative/fast;
    standard (default) keeps the env-derived constants, so untouched deployments
    behave exactly as before."""
    try:
        from models import AppSetting

        preset = (AppSetting.get("pacing_preset", "standard") or "standard").lower()
    except Exception:
        preset = "standard"
    pair = PACING_PRESETS.get(preset)
    if pair is None:
        return SEARCH_GAP, KEYED_GAP
    return pair


def _current_gap() -> float:
    # Dynamic: short gap when an official keyed path is usable, long gap for free engines.
    search_gap, keyed_gap = _gaps()
    try:
        from .config import is_enabled

        brave_ok = is_enabled("brave") and (search.brave_key_set())
        cse_ok = is_enabled("google_cse") and (os.environ.get("GOOGLE_CSE_KEY") and os.environ.get("GOOGLE_CSE_CX"))
        if brave_ok or cse_ok:
            return min(search_gap, keyed_gap)  # never slower than the no-key pacing, only ever faster
    except Exception:
        pass
    if search.brave_key_set() or (os.environ.get("GOOGLE_CSE_KEY") and os.environ.get("GOOGLE_CSE_CX")):
        return min(search_gap, keyed_gap)  # never slower than the no-key pacing, only ever faster
    return search_gap


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


SEARCH_WORKERS = int(os.environ.get("SEARCH_WORKERS", "20"))


def _discover_parallel(job: Job, batch: list[dict]):
    """With the person's own Brave key, each place is searched on its own worker thread (up to SEARCH_WORKERS at once).
    Brave's per-second limit is enforced per key in scraper/search.py, so the workers queue behind that limit rather
    than breaking it. Without a key, _discover_websites keeps its one-at-a-time pacing for the free engines."""
    names: dict[str, dict] = {}
    for r in batch:
        names.setdefault(r["Name"].lower().strip(), r)
    job.phase, job.total, job.done = "visit", len(names), 0
    job.say(f"Searching for the websites of {len(names)} places, up to {SEARCH_WORKERS} at a time "
            "(Brave allows about one search per second on a key, so the speed-up is limited by that).")

    def find(r):
        return discover.find_website(r["Name"], r.get("Search Location", ""), job.say, r.get("_aliases", []), 0)[0]

    found: dict[str, str | None] = {}
    with ThreadPoolExecutor(SEARCH_WORKERS) as pool:
        futures = {pool.submit(_keyed(job, find), r): key for key, r in names.items()}
        for fut in as_completed(futures):
            if job.cancelled.is_set():
                pool.shutdown(wait=False, cancel_futures=True)
                break
            key = futures[fut]
            job.done += 1
            try:
                found[key] = fut.result()
            except discover.SearchBlocked:
                found[key] = None
                job.say("  Brave refused the searches. Check your Brave key and its monthly quota in Settings.")
            except Exception as e:  # one failed lookup must not stop the rest
                found[key] = None
                job.say(f"  lookup failed for {names[key]['Name']}: {type(e).__name__}")
    for r in batch:
        url = found.get(r["Name"].lower().strip())
        if url:
            r["Website"], r["Website Source"] = url, "Found by web search"
            job.say(f"  {r['Name']}: {url}")
        else:
            job.say(f"  {r['Name']}: no official website found")


def _discover_websites(job: Job, rows: list[dict]):
    """The map rarely lists a website. Search the web for each such place's own site, so its contact details can be read."""
    todo = [r for r in rows if not r.get("Website") and r.get("Name")]
    if not todo:
        return
    batch = todo[:MAX_DISCOVER]
    if search.brave_key_set():
        return _discover_parallel(job, batch)
    gap = _current_gap()
    base_gap, _ = _gaps()
    job.phase, job.total, job.done = "visit", len(batch), 0
    job.say(f"Looking for the websites of {len(batch)} places that the map doesn't give one for "
            f"(about {gap:g} seconds each{', using your search key' if gap < base_gap else ', to keep the search engines happy'})…")
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
                if n_hits == 0 and gap >= base_gap:
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


# ---------------------------------------------------------------- paid Apify data (Deep Places / Enrich runs)
# One monthly pot covers all paid actor spend: the same budget the admin already sets for social enrichment.

def _paid_budget_left(job: Job) -> float:
    """USD still spendable this month; 0 when no budget is set (paid calls stay off until an admin sets one)."""
    from .config import USD_TO_INR

    lim_usd, lim_inr = _social_limits()
    if lim_usd <= 0 and lim_inr <= 0:
        return 0.0
    sp_usd, sp_inr = _social_spend(job.owner_id)
    left = []
    if lim_usd > 0:
        left.append(lim_usd - sp_usd)
    if lim_inr > 0:
        left.append((lim_inr - sp_inr) / USD_TO_INR)
    return max(min(left), 0.0)


def _charge_paid(job: Job, usd: float, what: str) -> None:
    from .config import USD_TO_INR

    _add_social_spend(job.owner_id, usd, usd * USD_TO_INR, label=f"paid.{what}")
    job.say(f"  paid {what}: about ${usd:.3f} / \u20b9{usd * USD_TO_INR:.1f} used")


def _paid_places(job: Job, loc: str) -> list[dict]:
    """Google Maps rows for one location, through the person's Apify account. [] (with a log line) when it can't run."""
    eff = job.effective if isinstance(job.effective, dict) else {}
    if not eff.get("paid_places"):
        return []
    spec = job.spec
    n = min(int(spec.get("max_results") or 30), apify_actors.MAX_PLACES_PER_CALL)
    contacts = not spec.get("names_only")
    est, left = apify_actors.estimate_places_usd(n, contacts), _paid_budget_left(job)
    if est > left:
        job.say(f"  Google Maps (paid) skipped for {loc}: up to ${est:.2f} but ${left:.2f} of the monthly budget is left")
        return []
    job.activity = f"Asking Google Maps (paid) about {loc}\u2026"
    try:
        rows = apify_actors.fetch_places(spec["category"], loc, n, spec.get("apify_key", ""),
                                         spec.get("name_filter", ""), contacts)
    except apify_actors.ActorError as e:
        job.say(f"  Google Maps (paid) failed: {e}")
        job.problems.append(str(e))
        return []
    _charge_paid(job, apify_actors.places_cost_usd(len(rows), contacts), "places")
    for r in rows:
        r["_paid"] = True
    job.say(f"  {len(rows)} places from Google Maps (paid)")
    return rows


def _paid_contacts(job: Job, work: list[dict]) -> None:
    """Fill emails/phones for rows that have a website but still no contact after the free crawl. Hybrid on purpose:
    the free path goes first, the paid actor only pays for the gaps."""
    eff = job.effective if isinstance(job.effective, dict) else {}
    if not eff.get("paid_contacts") or job.cancelled.is_set():
        return
    todo = [w for w in work if (w.get("Website") or "").strip() and not (w.get("Emails") or w.get("Phone Numbers"))]
    todo = todo[:apify_actors.MAX_PAID_URLS_PER_RUN]
    if not todo:
        return
    job.say(f"Asking Apify (paid) for contacts on {len(todo)} websites the free lookup could not read\u2026")
    filled = 0
    for i in range(0, len(todo), apify_actors.CONTACT_BATCH):
        batch = todo[i:i + apify_actors.CONTACT_BATCH]
        est, left = apify_actors.estimate_contacts_usd(len(batch), 1), _paid_budget_left(job)
        if est > left:
            job.say(f"  stopped: next batch could cost ${est:.2f} but ${left:.2f} of the monthly budget is left")
            break
        job.activity = f"Reading {len(batch)} websites for contacts (paid)\u2026"
        try:
            found = apify_actors.fetch_contacts([w["Website"] for w in batch], job.spec.get("apify_key", ""))
        except apify_actors.ActorError as e:
            job.say(f"  Apify contacts failed: {e}")
            job.problems.append(str(e))
            break
        _charge_paid(job, apify_actors.contacts_cost_usd(len(batch)), "contacts")
        for w in batch:
            rec = found.get(apify_actors.host_of(w["Website"]))
            if not rec:
                continue
            if rec["emails"]:
                w["Emails"] = "; ".join(rec["emails"][:5])
            if rec["phones"]:
                w["Phone Numbers"] = "; ".join(rec["phones"][:5])
            filled += bool(rec["emails"] or rec["phones"])
    job.say(f"  {filled} more website{'s' if filled != 1 else ''} gave contact details")


def _run_places_names(job: Job):
    """Stage 1 discovery for Places: map names only, no website lookup of any kind."""
    from .config import DISCOVERY_COLUMNS

    spec = job.spec
    job.columns = list(DISCOVERY_COLUMNS)
    job.say("Saving company names as we find them… You can stop anytime and continue later.")
    job.activity = "Saving company names as we find them…"
    job.total = len(spec["locations"])
    known: set[str] = set()
    for loc in spec["locations"]:
        if job.cancelled.is_set():
            break
        job.say(f'Finding "{spec["category"]}" in {loc}…')
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
        found = found + _paid_places(job, loc)
        added = 0
        for r in found:
            name = (r.get("Name", "") or "").strip()
            if len(name) < 2 or name.lower() in known:
                continue
            known.add(name.lower())
            job.rows.append(_discovery_row(name, "Google Maps" if r.get("_paid") else "Map", loc))
            added += 1
        job.say(f"  {added} new company name{'s' if added != 1 else ''} saved")
        job.done += 1
        time.sleep(1)
    if job.cancelled.is_set():
        job.say("Stopped by user. The names saved so far are kept.")
    else:
        job.say(f"Saved {len(job.rows)} company names. Open the sheet and use ‘Fill missing details’ when ready.")


def _run_places(job: Job):
    spec = job.spec
    if spec.get("names_only"):
        return _run_places_names(job)
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
        found = found + _paid_places(job, loc)
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
        if r.get("Website") and not r.get("Website Source"):
            r["Website Source"] = "Map"  # paid Google Maps rows already say so
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
        futures = {pool.submit(_keyed(job, _enrich_place), job, fetcher, r): r for r in with_site}
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


# ---------------------------------------------------------------- social mode (Phase 3, controlled spend)

def _budget_pct(spent_usd: float, spent_inr: float, limit_usd: float, limit_inr: float) -> float:
    """Highest consumed fraction across both currencies. 0 when no budget set."""
    pct = 0.0
    if limit_usd > 0:
        pct = max(pct, spent_usd / limit_usd)
    if limit_inr > 0:
        pct = max(pct, spent_inr / limit_inr)
    return pct


def _budget_over(spent_usd: float, spent_inr: float, limit_usd: float, limit_inr: float) -> bool:
    return (limit_usd > 0 and spent_usd >= limit_usd) or (limit_inr > 0 and spent_inr >= limit_inr)


def _added_data(before: dict, after: dict) -> bool:
    """True if the merge filled at least one previously blank data column."""
    for k in ("Name", "Headline", "Current Company", "Location", "About",
              "Experience summary", "Website"):
        if not (before.get(k) or "").strip() and (after.get(k) or "").strip():
            return True
    return False


def _social_limits() -> tuple[float, float]:
    """Return (limit_usd, limit_inr) for social. (0,0) means no budget set: paid calls stay blocked."""
    try:
        from models import AppSetting

        inr = float(AppSetting.get("social_budget_inr", "0") or 0)
        usd = float(AppSetting.get("social_budget_usd", "0") or 0)
        if inr <= 0 and usd <= 0:
            return (0.0, 0.0)  # 0 = no budget set: paid enrichment stays off until an admin sets one
        from .config import USD_TO_INR

        if inr <= 0:
            inr = round(usd * USD_TO_INR, 2)
        if usd <= 0:
            usd = round(inr / USD_TO_INR, 4)
        return (usd, inr)
    except Exception:
        return (0.0, 0.0)


def _social_spend(owner_id: int | None) -> tuple[float, float]:
    """Return (spent_usd, spent_inr), resetting on month rollover. (0,0) when DB unavailable."""
    try:
        from models import MonthlyBudget, db

        row = MonthlyBudget.query.filter_by(user_id=owner_id, service="social").first()
        if not row:
            return (0.0, 0.0)
        start = row.month_start
        now = datetime.now()
        if start and (start.year, start.month) != (now.year, now.month):
            row.spent_inr, row.spent_usd, row.month_start = 0, 0.0, now
            db.session.commit()
            return (0.0, 0.0)
        return (float(row.spent_usd or 0.0), int(row.spent_inr or 0))
    except Exception:
        return (0.0, 0.0)


def _add_social_spend(owner_id: int | None, usd: float, inr: float, label: str = "social.spend") -> None:
    try:
        from models import MonthlyBudget, db

        row = MonthlyBudget.query.filter_by(user_id=owner_id, service="social").first()
        if not row:
            row = MonthlyBudget(user_id=owner_id, service="social")
            db.session.add(row)
        row.spent_usd = float(row.spent_usd or 0.0) + float(usd)
        row.spent_inr = int(row.spent_inr or 0) + int(round(inr))
        db.session.commit()
        if usd or inr:
            from models import log_audit

            log_audit(owner_id, label, f"+${float(usd):.4f} / ₹{float(inr):.2f}")
    except Exception:
        try:
            from models import db as _db

            _db.session.rollback()
        except Exception:
            pass


def _free_social_lookup(item: dict, say) -> dict:
    """Best-effort ₹0 lookup: public search snippet only, never login-walled fetch."""
    from .social_base import blank_record

    url, name, platform = item.get("url", ""), item.get("name", ""), item.get("platform", "")
    rec = blank_record(name=name, url=url, platform=platform)
    query = url if url else f"{name} site:{platform or 'linkedin.com/in'}"
    try:
        hits = web_search(query, "wt-wt", 3, lambda m: None)
    except Exception:
        return rec
    if not hits:
        return rec
    top = hits[0]
    rec["Name"] = rec["Name"] or top.get("title", "")[:150]
    rec["About"] = (top.get("body", "") or "")[:2000]
    rec["Profile URL"] = rec["Profile URL"] or top.get("href", "")
    return rec


def _run_social(job: Job, free_lookup=None, bd_fill=None, ap_fill=None, spend_tracker=None):
    """Enrich social profiles. Hybrid ON: paid only for free misses, within budget."""
    from .config import MAX_SOCIAL_PER_RUN, SOCIAL_UNIT_USD
    from .social_base import SOCIAL_COLUMNS, is_filled_enough, merge_records, normalize_url

    spec = job.spec
    job.columns = list(SOCIAL_COLUMNS)
    items = (spec.get("items") or [])[:MAX_SOCIAL_PER_RUN]
    if len(spec.get("items") or []) > len(items):
        job.say(f"  capped at the first {len(items)} profiles this run (monthly guardrail)")
    providers = (job.effective.get("social") if isinstance(job.effective, dict) else None) or []
    hybrid = spec.get("hybrid", True)
    limit_usd, limit_inr = _social_limits()
    if spend_tracker is not None:
        spent_usd, spent_inr = spend_tracker.get("spent", (0.0, 0))
        tracker_limits = spend_tracker.get("limits")
        if tracker_limits is not None:
            limit_usd, limit_inr = tracker_limits
    else:
        spent_usd, spent_inr = _social_spend(job.owner_id)
    budget_set = limit_usd > 0 or limit_inr > 0
    if not budget_set and providers and spend_tracker is None:
        job.say("  no monthly social budget is set; paid enrichment stays off until an admin sets one "
                "(free lookup only)")
        providers = []
    if budget_set:
        pct = _budget_pct(spent_usd, spent_inr, limit_usd, limit_inr)
        job.say(f"  social budget: ${spent_usd:.2f} / ${limit_usd:.2f} spent "
                f"(₹{spent_inr} / ₹{limit_inr}, {pct * 100:.0f}% used)")
        if pct >= 0.8 and not _budget_over(spent_usd, spent_inr, limit_usd, limit_inr):
            job.say("  monthly social budget is over 80% used; remaining paid fills are limited")
    free_fn = free_lookup or (lambda it: _free_social_lookup(it, job.say))
    job.total, job.done = len(items), 0
    job.phase, job.activity = "visit", f"Enriching {len(items)} profiles (public fields only)…"
    seen: set[str] = set()
    paid_fills, free_fills, warned_80 = 0, 0, False
    for it in items:
        if job.cancelled.is_set():
            job.say("Stopped by user.")
            break
        url = normalize_url(it.get("url", ""))
        key = url or (it.get("name", "").strip().lower() + "|" + it.get("platform", ""))
        if key in seen:
            job.done += 1
            continue
        seen.add(key)
        rec = free_fn({**it, "url": url})
        rec["Profile URL"] = rec.get("Profile URL") or url
        if hybrid and is_filled_enough(rec):
            job.say(f"  {rec.get('Name') or url}: free lookup sufficient, no paid call")
            free_fills += 1
        elif providers:
            if not (url or rec.get("Profile URL", "")):
                job.say(f"  {rec.get('Name') or 'profile'}: no profile link to enrich; paid lookup skipped")
                free_fills += 1
            elif _budget_over(spent_usd, spent_inr, limit_usd, limit_inr):
                job.say("  monthly social budget reached; remaining profiles use free lookup only")
                providers = []
                free_fills += 1
            else:
                row_paid = False
                for p in list(providers):
                    try:
                        if p == "brightdata":
                            fill = (bd_fill or __import__("scraper.social_brightdata",
                                                          fromlist=["fill_profile"]).fill_profile)
                            got = fill(url or rec.get("Profile URL", ""), spec.get("brightdata_key", ""))
                        elif p == "apify":
                            fill = (ap_fill or __import__("scraper.social_apify",
                                                          fromlist=["fill_profile"]).fill_profile)
                            got = fill(url or rec.get("Profile URL", ""), spec.get("apify_key", ""),
                                       spec.get("apify_actor", ""))
                        else:
                            continue
                    except Exception as e:
                        job.say(f"  {p} failed for {url or rec.get('Name')}: {type(e).__name__}")
                        continue
                    before = dict(rec)
                    rec = merge_records(rec, got)
                    if _added_data(before, rec):
                        row_paid = True
                        paid_fills += 1
                        unit_usd = SOCIAL_UNIT_USD.get(p, 0.0)
                        from .config import USD_TO_INR

                        unit_inr = round(unit_usd * USD_TO_INR, 2)
                        if spend_tracker is not None:
                            s_usd, s_inr = spend_tracker.get("spent", (0.0, 0))
                            spend_tracker["spent"] = (s_usd + unit_usd, s_inr + unit_inr)
                            spent_usd, spent_inr = spend_tracker["spent"]
                        else:
                            _add_social_spend(job.owner_id, unit_usd, unit_inr)
                            spent_usd, spent_inr = _social_spend(job.owner_id)
                        job.say(f"  {rec.get('Name') or url}: filled via {p} "
                                f"(+${unit_usd:.4f} / ₹{unit_inr:.2f})")
                        if budget_set and not warned_80 and _budget_pct(spent_usd, spent_inr, limit_usd, limit_inr) >= 0.8:
                            warned_80 = True
                            job.say("  monthly social budget is now over 80% used")
                    else:
                        job.say(f"  {p} returned no new data for {url or rec.get('Name')}; not counted as spend")
                    if is_filled_enough(rec):
                        break
                if not row_paid:
                    free_fills += 1
        else:
            free_fills += 1
        row = {c: _text(rec.get(c)) for c in SOCIAL_COLUMNS}
        job.rows.append(row)
        job.done += 1
    job.say(f"Filled {len(job.rows)} profiles ({free_fills} free, {paid_fills} paid fills). "
            f"Public fields only; coverage varies, no guaranteed contacts.")
