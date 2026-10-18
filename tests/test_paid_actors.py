"""Phase 2: paid Apify data for Deep Places / Enrich runs - client, normalisers, gating, spend. Mocked HTTP only.
Run: python tests/test_paid_actors.py   (exit 1 on failure)"""
import os
import sys
import tempfile
import threading

import requests

TMP = tempfile.mkdtemp()
os.environ["JOB_STORE_DIR"] = os.path.join(TMP, "jobs")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(TMP, "paid.db").replace("\\", "/")
os.environ["APP_USER"] = "bosspaid"
os.environ["APP_PASSWORD"] = "adminpass123"
os.environ["SECRET_KEY"] = "test-secret-key-paid-actors-only"
os.environ["SKIP_STARTUP_PROBES"] = "1"
os.environ.pop("GOOGLE_CSE_KEY", None)
os.environ.pop("BRAVE_API_KEY", None)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as appmod  # noqa: E402
from scraper import apify_actors as A, capabilities as C, connection_health as H, jobs as J  # noqa: E402
from models import AppSetting, db  # noqa: E402

failures = []
TOKEN = "apify_api_TESTTOKEN1234567890"


def ok(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        failures.append(name)


class Resp:
    def __init__(self, code=200, data=None, bad_json=False):
        self.status_code, self._d, self._bad = code, data, bad_json

    def json(self):
        if self._bad:
            raise ValueError("no json")
        return self._d


def post_returning(resp):
    calls = []

    def _post(url, **kw):
        calls.append((url, kw))
        if isinstance(resp, Exception):
            raise resp
        return resp
    _post.calls = calls
    return _post


# ---- 1. the HTTP call and its errors ---------------------------------------------------------------------------------
p = post_returning(Resp(200, [{"title": "A"}, "junk", {"title": "B"}]))
ok("run_actor returns only the dict items", A.run_actor("a/b", TOKEN, {"x": 1}, _post=p) == [{"title": "A"}, {"title": "B"}])
ok("it calls the sync endpoint with ~ in the actor id and a bearer header",
   "/acts/a~b/run-sync-get-dataset-items" in p.calls[0][0] and p.calls[0][1]["headers"]["Authorization"] == f"Bearer {TOKEN}")
for code, word in ((401, "refused the token"), (403, "refused the token"), (402, "out of credit"), (404, "no actor"),
                   (429, "rate limiting"), (500, "answered 500")):
    try:
        A.run_actor("a/b", TOKEN, {}, _post=post_returning(Resp(code)))
        ok(f"HTTP {code} raises", False)
    except A.ActorError as e:
        ok(f"HTTP {code} -> readable error without the token", word in str(e) and TOKEN not in str(e), str(e))
for exc, word in ((requests.Timeout(), "too long"), (requests.ConnectionError(), "Could not reach")):
    try:
        A.run_actor("a/b", TOKEN, {}, _post=post_returning(exc))
        ok("network error raises", False)
    except A.ActorError as e:
        ok(f"{type(exc).__name__} -> readable error", word in str(e))
for resp in (Resp(200, bad_json=True), Resp(200, {"not": "a list"})):
    try:
        A.run_actor("a/b", TOKEN, {}, _post=post_returning(resp))
        ok("bad body raises", False)
    except A.ActorError:
        ok("a non-list / non-JSON body raises ActorError", True)
try:
    A.run_actor("a/b", "", {}, _post=post_returning(Resp(200, [])))
    ok("empty token raises", False)
except A.ActorError as e:
    ok("a missing token is refused before any request", "token" in str(e).lower())

# ---- 2. places input and output ---------------------------------------------------------------------------------------
inp = A.places_input("Coaching & training centers", "Ameerpet, Hyderabad", 80, "ABC")
ok("search term is plain words", inp["searchStringsArray"] == ["abc coaching and training centers"], inp)
ok("one location, limit and contacts add-on are set", inp["locationQuery"] == "Ameerpet, Hyderabad" and inp["maxCrawledPlacesPerSearch"] == 80 and inp["scrapeContacts"] is True)
ok("limit is clamped to the per-call cap", A.places_input("Hospitals", "Pune", 99999)["maxCrawledPlacesPerSearch"] == A.MAX_PLACES_PER_CALL)
ok("names-only can switch the paid contacts add-on off", "scrapeContacts" not in A.places_input("Hospitals", "Pune", 10, contacts=False))

item = {"title": "  Allen   Coaching ", "categoryName": "Coaching center", "address": "Ameerpet, Hyderabad 500016",
        "city": "Hyderabad", "state": "Telangana", "postalCode": "500016", "phone": "+91 40 1234 5678",
        "emails": ["Info@Allen.in", "info@allen.in"], "website": "https://allen.in", "totalScore": 4.5, "reviewsCount": 120,
        "location": {"lat": 17.43, "lng": 78.44}, "url": "https://maps.google.com/?cid=1",
        "openingHours": [{"day": "Monday", "hours": "9 AM to 6 PM"}, {"day": "Tuesday", "hours": "9 AM to 6 PM"}]}
row = A.normalise_place(item)
ok("a full item maps onto our Places columns",
   row["Name"] == "Allen Coaching" and row["Phone"] == "+91 40 1234 5678" and row["Website"] == "https://allen.in"
   and row["Latitude"] == 17.43 and row["Longitude"] == 78.44 and row["Postcode"] == "500016", row)
ok("duplicate emails collapse to one", row["Email"].lower().count("info@allen.in") == 1, row["Email"])
ok("hours and rating land in Opening Hours / Other Details",
   row["Opening Hours"].startswith("Monday: 9 AM to 6 PM; Tuesday") and "Rating 4.5 (120 reviews)" in row["Other Details"])
ok("a permanently closed place is dropped", A.normalise_place({"title": "Gone Academy", "permanentlyClosed": True}) is None)
ok("a nameless item is dropped", A.normalise_place({"phone": "1"}) is None and A.normalise_place({"title": " "}) is None)
weird = A.normalise_place({"title": "Odd Place", "location": None, "emails": "a@b.com", "openingHours": "Mon-Fri", "phone": 12345,
                           "categories": ["School"], "totalScore": 0})
ok("odd types become cells, never an error", weird["Email"] == "a@b.com" and weird["Opening Hours"] == "Mon-Fri" and weird["Category"] == "School" and weird["Latitude"] == "", weird)

fp = post_returning(Resp(200, [item, {"title": "Gone", "permanentlyClosed": True}]))
rows = A.fetch_places("Coaching & training centers", "Ameerpet", 50, TOKEN, _post=fp)
ok("fetch_places returns normalised, non-closed rows", [r["Name"] for r in rows] == ["Allen Coaching"])
ok("the payload that was sent is the documented input", fp.calls[0][1]["json"]["searchStringsArray"] == ["coaching and training centers"])

# ---- 3. contacts ------------------------------------------------------------------------------------------------------
items = [{"url": "https://www.allen.in/contact", "emails": ["Info@Allen.in", "info@allen.in", "not-an-email"], "phones": ["+91 1"],
          "facebooks": ["https://facebook.com/allen"]},
         {"url": "https://allen.in/about", "emails": ["admissions@allen.in"], "phonesUncertain": ["+91 2"], "linkedIns": ["https://linkedin.com/c/allen"]},
         {"url": "", "emails": ["x@y.com"]}]
nc = A.normalise_contacts(items)
ok("contacts merge per host (www stripped) and lower-case emails", list(nc) == ["allen.in"] and nc["allen.in"]["emails"] == ["info@allen.in", "admissions@allen.in"], nc)
ok("phones (incl. uncertain) and social links are collected", nc["allen.in"]["phones"] == ["+91 1", "+91 2"] and len(nc["allen.in"]["social"]) == 2)
ok("an item with no URL is ignored", "y.com" not in nc)
ci = A.contacts_input(["https://a.com", "https://b.com"])
ok("contact input follows the documented fields", ci["startUrls"] == [{"url": "https://a.com"}, {"url": "https://b.com"}] and ci["mergeContacts"] is True and ci["sameDomain"] is True and ci["proxyConfig"] == {"useApifyProxy": True})
ok("estimates: 100 places with contacts ~ $0.60, contacts for 20 sites ~ $0.21",
   abs(A.estimate_places_usd(100, True) - 0.601) < 0.01 and abs(A.estimate_contacts_usd(20) - 0.201) < 0.01,
   (A.estimate_places_usd(100, True), A.estimate_contacts_usd(20)))
ok("actual cost is computed from the rows that came back", A.places_cost_usd(10, True) < A.estimate_places_usd(100, True))

# ---- 4. registry, capabilities ----------------------------------------------------------------------------------------
from scraper import config  # noqa: E402
S = config.SERVICES
ok("registry: apify_places / apify_contacts are paid, need a key, default off, share Apify's health",
   all(S[n]["cost"] == "paid" and S[n]["needs_key"] and S[n]["default"] == "0" and S[n]["health_as"] == "apify"
       for n in ("apify_places", "apify_contacts")))
ok("a Deep places/enrich run can now use them", "apify_places" in C.DEEP_SERVICES["places"] and "apify_contacts" in C.DEEP_SERVICES["enrich"])
ok("both are in the places-mode and enrich-mode service lists",
   any(s["id"] == "apify_places" for s in C.run_options("places", "deep", {})["services"])
   and any(s["id"] == "apify_contacts" for s in C.run_options("enrich", "deep", {})["services"]))

app = appmod.app
VAULT = {"apify_api_key": TOKEN}
with app.app_context():
    db.create_all()
    ok("deep places is not possible with a token while the admin switch is off",
       not C.check_depth("places", "deep", VAULT)[0] and "turned off by your admin" in C.check_depth("places", "deep", VAULT)[1], C.check_depth("places", "deep", VAULT))
    AppSetting.set("conn_apify_places", "1")
    AppSetting.set("conn_apify_contacts", "1")
    db.session.commit()
    ok("... and possible once the switch is on", C.check_depth("places", "deep", VAULT)[0] and C.check_depth("enrich", "deep", VAULT)[0])
    ok("Google-Maps-only fields (rating) become available", {f["id"]: f for f in C.fields_for("places", {"apify_places"})}["rating"]["available"]
       and not {f["id"]: f for f in C.fields_for("places", {"overpass"})}["rating"]["available"])
    H.HEALTH_CACHE.clear()

    # ---- 5. preflight ------------------------------------------------------------------------------------------------
    base = {"mode": "places", "depth": "deep", "apify_key": TOKEN}
    ok("preflight: deep places + token + switch on -> paid_places", J.preflight(base)["paid_places"] == ["apify_places"])
    ok("preflight: standard never gets paid data", J.preflight({**base, "depth": "standard"})["paid_places"] == [])
    ok("preflight: no token -> none, with a note", J.preflight({**base, "apify_key": ""})["paid_places"] == []
       and any("no Apify token" in n for n in J.preflight({**base, "apify_key": ""})["notes"]))
    H.HEALTH_CACHE["apify"] = {"healthy": False, "checked_at": __import__("time").time()}
    ok("preflight: Apify's failed check turns it off", J.preflight(base)["paid_places"] == [])
    H.HEALTH_CACHE.clear()
    ok("preflight: enrich gets paid_contacts, places does not", J.preflight({**base, "mode": "enrich"})["paid_contacts"] == ["apify_contacts"]
       and J.preflight({**base, "mode": "enrich"})["paid_places"] == [])

    # ---- 6. build_job_spec gate -------------------------------------------------------------------------------------
    PL = {"mode": "places", "category": "Hindu temples", "locations": "Pune", "depth": "deep", "max_results": 10}
    s, e, code = appmod.build_job_spec(dict(PL), VAULT, 1)
    ok("deep paid run with no budget set -> 402 asking for a budget", s is None and code == 402 and "monthly budget" in e, (e, code))
    AppSetting.set("social_budget_usd", "5")
    AppSetting.set("social_budget_inr", "0")
    db.session.commit()
    s, e, code = appmod.build_job_spec(dict(PL), VAULT, 1)
    ok("with a budget it is accepted and carries the token", e is None and s["apify_key"] == TOKEN and s["depth"] == "deep", (e, code))
    ok("the token never reaches the public spec", "apify_key" not in J.public_spec(s))
    s, e, code = appmod.build_job_spec({**PL, "max_results": 500, "locations": "\n".join(f"City{i}" for i in range(30))}, VAULT, 1)
    ok("a run that could cost more than the budget is refused with the amounts", s is None and code == 402 and "could cost up to $" in e, (e, code))
    s, e, code = appmod.build_job_spec({**PL, "depth": "standard"}, VAULT, 1)
    ok("standard run: no token in the spec, no gate", e is None and "apify_key" not in s)
    s, e, code = appmod.build_job_spec(dict(PL), {"ai_api_key": "sk-test-1234567890ABCD", "ai_provider": "openai"}, 1)
    ok("deep with AI only (no Apify token): accepted, nothing paid so no budget gate", e is None and s["apify_key"] == "")
    ENR = {"mode": "enrich", "depth": "deep", "columns": ["Name", "Website", "Email"],
           "rows": [{"Name": "A", "Website": "https://a.com", "Email": ""}], "name_column": "Name"}
    s, e, code = appmod.build_job_spec(ENR, VAULT, 1)
    ok("deep enrich with a budget is accepted and carries the token", e is None and s["apify_key"] == TOKEN, (e, code))
    ok("enforce_budget=False skips the gate (scheduler/estimate paths)", appmod.build_job_spec(dict(PL), VAULT, 1, enforce_budget=False)[1] is None)

    # ---- 7. the run itself ---------------------------------------------------------------------------------------------
    def fresh_job(spec_extra=None, eff_paid=True, **kw):
        spec = {"mode": "places", "category": "Coaching & training centers", "locations": ["Ameerpet"], "max_results": 5,
                "apify_key": TOKEN, "names_only": False, "enrich": False, **(spec_extra or {})}
        j = J.Job(spec, owner_id=1)
        j.effective = {"paid_places": ["apify_places"] if eff_paid else [], "paid_contacts": ["apify_contacts"] if eff_paid else []}
        return j

    osm_row = {"Name": "Allen Coaching School", "Phone": "", "Email": "", "Website": "", "Address": "Ameerpet", "Google Maps Link": "",
               "Latitude": "17.4301", "Longitude": "78.4401", "Other Details": ""}
    real = (J.places.search_places, J.apify_actors.fetch_places, J.time.sleep)
    J.places.search_places = lambda *a, **k: [dict(osm_row)]
    calls = []

    def fake_fetch(category, loc, n, token, name_filter="", contacts=True, _post=None):
        calls.append((category, loc, n, contacts))
        return [dict(A.normalise_place(item)), dict(A.normalise_place({"title": "Zoom Technologies", "latitude": 1, "location": {"lat": 17.5, "lng": 78.5}, "phone": "9"}))]
    J.apify_actors.fetch_places = fake_fetch
    J.time.sleep = lambda s: None

    j = fresh_job()
    J._run_places(j)
    names = sorted(r["Name"] for r in j.rows)
    ok("paid rows are added to the free map rows", "Zoom Technologies" in names and any("Allen" in n for n in names), names)
    ok("the same place found by both sources is merged into one row", sum("allen" in n.lower() for n in names) == 1, names)
    merged = next(r for r in j.rows if "Allen" in r["Name"])
    ok("... and the merged row keeps the phone/website only the paid source had", merged["Phone"] and merged["Website"] == "https://allen.in", merged)
    ok("one paid call per location, asking for the run's limit", calls == [("Coaching & training centers", "Ameerpet", 5, True)], calls)
    spent = J._social_spend(1)[0]
    ok("spend was recorded against the monthly budget", spent > 0, spent)
    ok("the log says what was used", any("paid places" in m for m in j.log) and any("places from Google Maps (paid)" in m for m in j.log), j.log)

    before = J._social_spend(1)[0]
    calls.clear()
    jn = fresh_job({"names_only": True})
    J._run_places_names(jn)
    ok("names-only: paid call asks for names without the contacts add-on", calls and calls[0][3] is False, calls)
    ok("names-only: paid names are labelled Google Maps, free ones Map",
       {r["Company Name"]: r["Source"] for r in jn.rows}.get("Zoom Technologies") == "Google Maps"
       and {r["Company Name"]: r["Source"] for r in jn.rows}.get("Allen Coaching School") == "Map", jn.rows)
    ok("names-only: spend is lower than a full run's", J._social_spend(1)[0] - before < spent)

    calls.clear()
    jf = fresh_job(eff_paid=False)
    J._run_places(jf)
    ok("a run whose preflight found no paid service makes no paid call", calls == [] and len(jf.rows) == 1)

    AppSetting.set("social_budget_usd", "0.001")
    db.session.commit()
    jb = fresh_job()
    J._run_places(jb)
    ok("when the remaining budget is too small the paid call is skipped, the free rows still come back",
       calls == [] and len(jb.rows) == 1 and any("skipped" in m for m in jb.log), jb.log)
    AppSetting.set("social_budget_usd", "100")
    db.session.commit()

    def boom(*a, **k):
        raise A.ActorError("Your Apify account is out of credit or at its usage limit.")
    J.apify_actors.fetch_places = boom
    jx = fresh_job()
    J._run_places(jx)
    ok("an Apify failure is reported and the free rows are kept", len(jx.rows) == 1 and any("out of credit" in p_ for p_ in jx.problems), jx.problems)
    J.places.search_places, J.apify_actors.fetch_places, J.time.sleep = real

    # ---- 8. enrich: paid contacts only for the gaps ----------------------------------------------------------------------
    real_fc = J.apify_actors.fetch_contacts
    asked = []

    def fake_contacts(urls, token, _post=None):
        asked.append(list(urls))
        return {"b.com": {"emails": ["hello@b.com"], "phones": ["+91 99"], "social": []}}
    J.apify_actors.fetch_contacts = fake_contacts
    work = [{"Name": "A", "Website": "https://a.com", "Emails": "x@a.com", "Phone Numbers": ""},
            {"Name": "B", "Website": "https://www.b.com/", "Emails": "", "Phone Numbers": ""},
            {"Name": "C", "Website": "", "Emails": "", "Phone Numbers": ""}]
    je = fresh_job({"mode": "enrich"})
    J._paid_contacts(je, work)
    ok("only rows with a website and no contact are sent", asked == [["https://www.b.com/"]], asked)
    ok("their contacts are filled from the actor result", work[1]["Emails"] == "hello@b.com" and work[1]["Phone Numbers"] == "+91 99")
    ok("rows that already had a contact are untouched", work[0]["Emails"] == "x@a.com" and work[2]["Emails"] == "")
    asked.clear()
    J._paid_contacts(fresh_job({"mode": "enrich"}, eff_paid=False), [dict(work[1], Emails="")])
    ok("no paid_contacts in preflight -> no call", asked == [])
    J.apify_actors.fetch_contacts = real_fc

print(f"\n{len(failures)} failure(s)" if failures else "\nAll tests passed")
sys.exit(1 if failures else 0)
