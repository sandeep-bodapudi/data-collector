"""Two-stage discovery + fill tests — mocked, no network. Run: python tests/test_two_stage.py (exit 1 on failure)."""
import os
import tempfile

TMP = tempfile.mkdtemp()
os.environ["JOB_STORE_DIR"] = os.path.join(TMP, "jobs")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(TMP, "t2s.db").replace("\\", "/")
os.environ["APP_USER"] = "boss2s"
os.environ["APP_PASSWORD"] = "adminpass123"
os.environ["SECRET_KEY"] = "test-secret-key-two-stage-only"
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scraper import jobs as J  # noqa: E402
from scraper.config import DISCOVERY_COLUMNS  # noqa: E402
import app as appmod  # noqa: E402

app = appmod.app

failures = []


def ok(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        failures.append(name)


HITS = [
    {"title": "Gokaraju Rangaraju Institute of Engineering", "href": "https://www.griet.ac.in/",
     "body": "GRIET Hyderabad official site", "query": "engineering colleges Hyderabad"},
    {"title": "Engineering Colleges in Hyderabad", "href": "https://www.colleges9.in/Telangana/Hyderabad/Engineering-Colleges/",
     "body": "list of colleges", "query": "engineering colleges Hyderabad"},
]

LISTING_HTML = """
<html><body><table>
<tr><td><b>Alpha College of Engineering</b><br>Kukatpally, Hyderabad</td>
<td><a href="/colleges/alpha-college/EN001/">Profile</a></td></tr>
<tr><td><b>Beta Institute of Technology</b><br>Dilsukhnagar, Hyderabad</td>
<td><a href="/colleges/beta-institute/EN002/">Profile</a></td></tr>
</table></body></html>
"""


class FakeFetcher:
    calls = []

    def __init__(self, spec=None):
        pass

    def get_html(self, url):
        FakeFetcher.calls.append(url)
        if "colleges9.in" in url:
            return LISTING_HTML, "ok"
        return None, "not fetched in this test"


J.Fetcher = FakeFetcher


def web_spec(**kw):
    spec = {"mode": "web", "queries": ["engineering colleges Hyderabad"], "max_results": 30,
            "fields": [], "platforms": ["web"], "region": "in-en", "custom_fields": [],
            "follow_contact": False, "one_per_site": False, "require": "",
            "names_only": True, "ai": {}}
    spec.update(kw)
    return spec


# ---- 1. names-only web: searches run, zero page visits, four columns ----
FakeFetcher.calls = []
_orig_ws = J.web_search
J.web_search = lambda q, region, n, say: list(HITS)
job = J.Job(web_spec(), 1)
J._run_web(job)
ok("discovery columns", job.columns == DISCOVERY_COLUMNS, str(job.columns))
names = [r["Company Name"] for r in job.rows]
ok("names found", "Alpha College of Engineering" in names and "Beta Institute of Technology" in names
   and any("Gokaraju" in n or "GRIET" in n or "Rangaraju" in n for n in names), str(names))
ok("no page visits", all("griet.ac.in" not in u for u in FakeFetcher.calls), str(FakeFetcher.calls))
ok("all rows Name only", all(r["Status"] == "Name only" for r in job.rows) and len(job.rows) >= 3)
ok("source + location filled",
   all(r["Source"] and r["Location / Search"] for r in job.rows), str(job.rows))
J.web_search = _orig_ws

# ---- 2. names-only places: no website lookup of any kind ----
import scraper.discover as _discover

lookups = {"guess": 0, "find": 0}
_orig_guess = _discover.guess_website
_orig_find = _discover.find_website
_discover.guess_website = lambda *a, **k: (lookups.__setitem__("guess", lookups["guess"] + 1), None)[1]
_discover.find_website = lambda *a, **k: (lookups.__setitem__("find", lookups["find"] + 1), (None, 0))[1]
import scraper.places as _places

_orig_search_places = _places.search_places
_places.search_places = lambda *a, **k: [
    {"Name": "City Hospital", "Category": "Hospitals", "Address": "Banjara Hills",
     "City": "", "State": "", "Postcode": "", "Phone": "", "Email": "", "Website": "",
     "Opening Hours": "", "Latitude": 0, "Longitude": 0, "Google Maps Link": "", "Other Details": ""}]
job2 = J.Job({"mode": "places", "category": "Hospitals", "locations": ["Hyderabad"],
              "max_results": 5, "custom_fields": [], "names_only": True, "enrich": False,
              "find_websites": True, "ai": {}}, 1)
J._run_places(job2)
ok("places discovery columns", job2.columns == DISCOVERY_COLUMNS)
ok("places name saved", [r["Company Name"] for r in job2.rows] == ["City Hospital"])
ok("no website lookup", lookups == {"guess": 0, "find": 0}, str(lookups))
_discover.guess_website, _discover.find_website = _orig_guess, _orig_find
_places.search_places = _orig_search_places

# ---- 3-6. enrich on a Company Name sheet ----
ENRICHED = {
    "Acme Corp": {"Website": "https://acme.example/", "Emails": "info@acme.example",
                  "Phone Numbers": "+91 98480 12345", "Address": ""},
    "Beta LLC": {"Website": "https://beta.example/", "Emails": "", "Phone Numbers": "",
                 "Address": ""},
}


def fake_enrich_listed(job, fetcher, rows, spec):
    for r in rows:
        fill = ENRICHED.get(r["Name"], {})
        r["Website"] = fill.get("Website", "")
        r["Emails"] = fill.get("Emails", "")
        r["Phone Numbers"] = fill.get("Phone Numbers", "")
        r["Address"] = fill.get("Address", "")


J._enrich_listed_rows = fake_enrich_listed


def enrich_job(rows, columns, **kw):
    spec = {"mode": "enrich", "rows": [dict(r) for r in rows], "columns": list(columns),
            "custom_fields": [], "ai": {}}
    spec.update(kw)
    job = J.Job(spec, 1)
    J._run_enrich(job)
    return job


cols = ["Company Name", "Website", "Emails", "Phone Numbers", "Address"]
rows = [
    {"Company Name": "Acme Corp", "Website": "", "Emails": "", "Phone Numbers": "", "Address": ""},
    {"Company Name": "Beta LLC", "Website": "", "Emails": "", "Phone Numbers": "", "Address": ""},
    {"Company Name": "Gamma Inc", "Website": "", "Emails": "", "Phone Numbers": "", "Address": ""},
    {"Company Name": "Done Co", "Website": "https://done.example/", "Emails": "hi@done.example",
     "Phone Numbers": "", "Address": "", "Status": "Enriched"},
]
job3 = enrich_job(rows, cols)
by_name = {r["Company Name"]: r for r in job3.rows}
ok("company-name alias works", "Status" in job3.columns)
ok("enriched status", by_name["Acme Corp"]["Status"] == "Enriched"
   and by_name["Acme Corp"]["Emails"] == "info@acme.example")
ok("partial status", by_name["Beta LLC"]["Status"] == "Partial"
   and by_name["Beta LLC"]["Website"] == "https://beta.example/")
ok("no-website status", by_name["Gamma Inc"]["Status"] == "No website found")
ok("existing kept", by_name["Done Co"]["Emails"] == "hi@done.example"
   and by_name["Done Co"]["Status"] == "Enriched")

# ---- 4. row_ids subset: others byte-identical ----
rows4 = [dict(r) for r in rows]
job4 = enrich_job(rows4, cols, row_ids=[2])
r4 = {r["Company Name"]: r for r in job4.rows}
ok("subset only attempts listed rows",
   r4["Acme Corp"].get("Status", "") == "" and r4["Gamma Inc"]["Status"] == "No website found",
   str([(k, v.get("Status", "")) for k, v in r4.items()]))

# ---- 5. re-run safety ----
job5 = enrich_job([dict(r) for r in job3.rows], list(job3.columns))
ok("re-run changes nothing",
   [{c: r.get(c, "") for c in job3.columns} for r in job3.rows]
   == [{c: r.get(c, "") for c in job3.columns} for r in job5.rows])

# ---- 6. free-mail fallback is labeled on Source ----
rows6 = [{"Company Name": "Small Shop", "Website": "", "Emails": "", "Phone Numbers": "",
          "Address": "", "Source": ""}]
cols6 = ["Company Name", "Website", "Emails", "Phone Numbers", "Address", "Source"]
ENRICHED["Small Shop"] = {"Website": "https://smallshop.example/", "Emails": "smallshop@gmail.com",
                          "Phone Numbers": "", "Address": ""}


def fake_enrich_freemail(job, fetcher, rows, spec):
    for r in rows:
        fill = ENRICHED.get(r["Name"], {})
        for k in ("Website", "Emails", "Phone Numbers", "Address"):
            r[k] = fill.get(k, "")


J._enrich_listed_rows = fake_enrich_freemail
job6 = enrich_job(rows6, cols6)
ok("free-mail fallback labeled",
   "free-mail" in (job6.rows[0].get("Source") or ""), str(job6.rows[0]))

# ---- 7. imported names-only CSV enriches ----
import io as _io

from scraper import sheets as _sheets

columns7, rows7 = _sheets.parse_upload("names.csv", b"Company Name\nAcme Corp\n")
ok("csv import keeps Company Name", columns7 == ["Company Name"] and rows7[0]["Company Name"] == "Acme Corp")
J._enrich_listed_rows = fake_enrich_listed
job7 = enrich_job(rows7, columns7)
ok("imported names enrich", job7.rows[0].get("Status") == "Partial")

# ---- 8. job-spec validation: names_only skips the fields requirement ----
with app.app_context():
    vault = {}
    spec, err, _code = appmod.build_job_spec(
        {"mode": "web", "queries": "engineering colleges", "names_only": True}, vault, 1)
    ok("names-only needs no fields", err is None and spec["names_only"] is True)
    _spec2, err2, _code2 = appmod.build_job_spec(
        {"mode": "web", "queries": "engineering colleges"}, vault, 1)
    ok("full run still needs fields", err2 is not None)
    spec3, err3, _code3 = appmod.build_job_spec(
        {"mode": "places", "category": "Schools", "locations": "Hyd", "names_only": True,
         "enrich": True}, vault, 1)
    ok("names-only forces enrich off", err3 is None and spec3["enrich"] is False)
    _spec4, err4, _code4 = appmod.build_job_spec(
        {"mode": "enrich", "columns": ["Company Name"], "rows": [{"Company Name": "A"}]},
        vault, 1)
    ok("company-name alias validates",
       err4 is None and _spec4["name_column"] == "Company Name" and _spec4["status_column"] == "Status")
    _spec5, err5, _code5 = appmod.build_job_spec(
        {"mode": "enrich", "columns": ["Thing"], "rows": [{"Thing": "A"}]}, vault, 1)
    ok("missing name column refused", err5 is not None and "Company Name" in err5)
    spec6, _err6, _code6 = appmod.build_job_spec(
        {"mode": "enrich", "columns": ["Company Name"], "rows": [{"Company Name": "A"}],
         "row_ids": [0, 5, -1, "x"]}, vault, 1)
    ok("row ids sanitized", spec6["row_ids"] == [0])

# ---- 9. full HTTP path: enrich a Company Name sheet, read Status back ----
J._enrich_listed_rows = fake_enrich_listed
c = app.test_client()
ok("admin sign in", c.post("/login", data={"username": "BOSS2S", "password": "adminpass123"}).status_code == 302)
rid = c.post("/api/jobs", json={"mode": "enrich", "columns": cols,
                                "rows": [dict(r) for r in rows]}).get_json()["id"]
import time as _t

for _ in range(200):
    st = c.get(f"/api/jobs/{rid}").get_json()
    if st["status"] not in ("queued", "running"):
        break
    _t.sleep(0.05)
ok("enrich job finishes", st["status"] == "done", st["status"])
res = c.get(f"/api/jobs/{rid}/result").get_json()
ok("status column served",
   "Status" in res["columns"]
   and {r["Company Name"]: r["Status"] for r in res["rows"]}.get("Acme Corp") == "Enriched")
c.delete(f"/api/jobs/{rid}")

print(f"\n{len(failures)} failure(s)" if failures else "\nAll tests passed")
sys.exit(1 if failures else 0)
