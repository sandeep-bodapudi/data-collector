"""'Read list pages with AI' tests - mocked AI and pages, no network. Run: python tests/test_ai_lists.py (exit 1 on failure)."""
import os
import tempfile

TMP = tempfile.mkdtemp()
os.environ["JOB_STORE_DIR"] = os.path.join(TMP, "jobs")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(TMP, "ailists.db").replace("\\", "/")
os.environ["APP_USER"] = "bossai"
os.environ["APP_PASSWORD"] = "adminpass123"
os.environ["SECRET_KEY"] = "test-secret-key-ai-lists-only"
os.environ.setdefault("SKIP_STARTUP_PROBES", "1")
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scraper import ai_extract as A  # noqa: E402
from scraper import jobs as J  # noqa: E402
import app as appmod  # noqa: E402,F401  (sets up the database the config switches live in)

failures = []


def ok(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        failures.append(name)


J.time.sleep = lambda s: None
AI = {"provider": "anthropic", "key": "test-key", "model": "", "base_url": ""}

# A real-shaped "top 10" article: two real institutes with contacts, plus navigation and the publisher itself.
PAGE = """Home | Courses | Blog | Contact
Top 10 Digital Marketing Institutes in Hyderabad (2026)
1. Digital Medha - Amrutha Arcade, Ameerpet, Hyderabad. Call +91 81061-87989, email info@digitalmedha.com,
website www.digitalmedha.com
2. Lemonoids - Mehdipatnam Main Road. Phone 9700157246.
Published by Example Blog. Subscribe to our newsletter."""

# ---- 1. the page is the proof: nothing the AI says survives unless it's written on the page -------------------
items = A._verify_items([
    {"name": "Digital Medha", "address": "Amrutha Arcade, Ameerpet, Hyderabad", "phone": "8106187989",
     "email": "info@digitalmedha.com", "website": "https://www.digitalmedha.com"},
    {"name": "Lemonoids", "address": "", "phone": "9700157246", "email": "hello@lemonoids.com", "website": "lemonoids.com"},
    {"name": "Invented Academy", "address": "", "phone": "9999988888", "email": "", "website": ""},
    {"name": "digital  medha", "address": "", "phone": "", "email": "", "website": ""},  # same name twice
], PAGE)
names = [i["name"] for i in items]
ok("a name that isn't on the page is dropped (hallucination guard)", "Invented Academy" not in names, str(names))
ok("real names are kept, once each", names == ["Digital Medha", "Lemonoids"], str(names))
medha, lemon = items
ok("a phone written with spaces/dashes on the page still verifies", medha["phone"] == "8106187989", str(medha))
ok("an email on the page is kept", medha["email"] == "info@digitalmedha.com")
ok("a website on the page is kept", medha["website"] == "https://www.digitalmedha.com", medha["website"])
ok("an address on the page is kept", medha["address"] == "Amrutha Arcade, Ameerpet, Hyderabad")
ok("an email NOT on the page is blanked, but the real name stays", lemon["email"] == "" and lemon["phone"] == "9700157246", str(lemon))
ok("a website NOT on the page is blanked", lemon["website"] == "", lemon["website"])
ok("garbage input never raises", A._verify_items("not a list", PAGE) == [] and A._verify_items([None, 5, {}], PAGE) == [])

# ---- 2. extract_list: no AI set up -> nothing, quietly; a key problem is raised once; a blip is skipped -------
ok("no AI key: returns nothing, makes no call", A.extract_list(PAGE, "https://blog.example/top-10", "x", {}) == [])
_orig_call = A.call
A.call = lambda *a, **k: {"items": [{"name": "Lemonoids", "address": "", "phone": "9700157246", "email": "", "website": ""}]}
got = A.extract_list(PAGE, "https://blog.example/top-10", "digital marketing institutes", AI)
ok("extract_list returns the verified items", [g["name"] for g in got] == ["Lemonoids"], str(got))


def bad_key(*a, **k):
    raise A.AIError("invalid API key")


A.call = bad_key
try:
    A.extract_list(PAGE, "u", "t", AI)
    ok("a bad key is raised so the run can say so", False)
except A.AIError:
    ok("a bad key is raised so the run can say so", True)


def blip(*a, **k):
    raise A.AIError("rate limited - try fewer pages")


A.call = blip
ok("a passing rate-limit just skips the page", A.extract_list(PAGE, "u", "t", AI) == [])
A.call = _orig_call

# ---- 3. _search routes on-topic list pages to the AI only when switched on, within the per-run cap ----------
HITS = [
    {"title": "Digital Medha - Digital Marketing Course in Hyderabad", "href": "https://digitalmedha.com/", "body": ""},
    {"title": "Top 10 Digital Marketing Institutes in Hyderabad", "href": "https://blog.example/top-10-dm/", "body": ""},
    {"title": "Best Digital Marketing Training Institutes in Hyderabad", "href": "https://other.example/best-dm/", "body": ""},
    {"title": "Top 10 Hotels in Hyderabad", "href": "https://travel.example/top-hotels/", "body": ""},
    {"title": "Digital marketing institutes in Hyderabad", "href": "https://www.facebook.com/groups/dm", "body": ""},
]
_orig_ws = J.web_search
J.web_search = lambda q, region, n, say: [dict(h) for h in HITS]


def web_spec(**kw):
    spec = {"mode": "web", "queries": ["digital marketing institutes in Hyderabad"], "max_results": 30,
            "fields": ["emails", "phones"], "platforms": ["web"], "region": "in-en", "custom_fields": [],
            "follow_contact": False, "one_per_site": False, "require": "", "names_only": False, "ai": AI}
    spec.update(kw)
    return spec


job = J.Job(web_spec(), 1)
_, lh = J._search(job)
ok("switched off: list pages are not sent to AI (old behaviour)", not any(h.get("ai") for h in lh), str(lh))

job = J.Job(web_spec(ai_lists=True), 1)
_, lh = J._search(job)
ai_urls = [h["url"] for h in lh if h.get("ai")]
ok("switched on: on-topic list pages are read with AI",
   "https://blog.example/top-10-dm/" in ai_urls and "https://other.example/best-dm/" in ai_urls, str(ai_urls))
ok("an off-topic list page (hotels) costs no AI call", "https://travel.example/top-hotels/" not in ai_urls)
ok("a social/login site is never sent", not any("facebook.com" in u for u in ai_urls))

J.MAX_AI_LIST_PAGES = 1
job = J.Job(web_spec(ai_lists=True), 1)
_, lh = J._search(job)
ok("the per-run cap is respected and logged", sum(1 for h in lh if h.get("ai")) == 1
   and any("per-run limit" in m for m in job.log), str(job.log[-3:]))
J.MAX_AI_LIST_PAGES = 40

job = J.Job(web_spec(ai_lists=True, ai={}), 1)
_, lh = J._search(job)
ok("switched on without an AI key: skipped, and the log says why",
   not any(h.get("ai") for h in lh) and any("no AI key" in m for m in job.log))


# ---- 4. listing rows: AI details are pre-filled; a stated website is used, not guessed -------------------------
class FakeFetcher:
    def __init__(self, spec=None):
        pass

    def get_html(self, url):
        return (f"<html><body><pre>{PAGE}</pre></body></html>", "ok") if "example" in url else (None, "skip")


A.call = lambda *a, **k: {"items": [
    {"name": "Digital Medha", "address": "", "phone": "8106187989", "email": "info@digitalmedha.com", "website": "www.digitalmedha.com"},
    {"name": "Lemonoids", "address": "", "phone": "9700157246", "email": "", "website": ""}]}
job = J.Job(web_spec(ai_lists=True), 1)
rows = J._expand_listings(job, FakeFetcher(), [{"url": "https://blog.example/top-10-dm/", "query": "dm", "ai": True}], set())
by = {r["Name"]: r for r in rows}
ok("one row per business on the list page", set(by) == {"Digital Medha", "Lemonoids"}, str(list(by)))
ok("the page's phone and email are pre-filled",
   by["Digital Medha"]["Phone Numbers"] == "8106187989" and by["Digital Medha"]["Emails"] == "info@digitalmedha.com")

guessed = []
_orig_guess, _orig_disc, _orig_enrich = J._guess_websites, J._discover_websites, J._enrich_web_row
J._guess_websites = lambda job, f, rs: guessed.extend(r["Name"] for r in rs if not r.get("Website"))
J._discover_websites = lambda job, rs: None
J._enrich_web_row = lambda job, f, r, spec: r
J._enrich_listed_rows(job, FakeFetcher(), rows, job.spec)
ok("a website the list page names is used directly, not guessed",
   by["Digital Medha"]["Website"] == "https://www.digitalmedha.com" and "Digital Medha" not in guessed, str(guessed))
ok("a business with no website on the page still goes to the normal lookup", "Lemonoids" in guessed)
ok("no internal fields leak into the sheet", not any(k.startswith("_site") for r in rows for k in r))
J._guess_websites, J._discover_websites, J._enrich_web_row = _orig_guess, _orig_disc, _orig_enrich

# ---- 5. a bad AI key stops AI reading for the rest of the run, with one clear message ------------------------
A.call = bad_key
job = J.Job(web_spec(ai_lists=True), 1)
hits = [{"url": "https://blog.example/a", "query": "dm", "ai": True}, {"url": "https://blog.example/b", "query": "dm", "ai": True}]
J._expand_listings(job, FakeFetcher(), hits, set())
ok("a bad key is reported once, and AI reading switches off for the run",
   sum("Reading list pages with AI stopped" in p for p in job.problems) == 1 and job.spec["ai_lists"] is False, str(job.problems))

# ---- 6. names-only runs read AI list pages too ---------------------------------------------------------------
A.call = lambda *a, **k: {"items": [{"name": "Lemonoids", "address": "", "phone": "", "email": "", "website": ""}]}
J.Fetcher = FakeFetcher
job = J.Job(web_spec(ai_lists=True, names_only=True, fields=[]), 1)
J._run_web(job)
names = [r["Company Name"] for r in job.rows]
ok("names-only: businesses named on a list page are saved", "Lemonoids" in names, str(names))

A.call = _orig_call
J.web_search = _orig_ws
print(f"\n{len(failures)} failure(s)" if failures else "\nAll tests passed")
sys.exit(1 if failures else 0)
