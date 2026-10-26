"""Google Maps (Places API + browser) and Google search in the browser. No network: HTTP and the browser are mocked; the
Google page fixtures in tests/fixtures/ were cut from real pages on 2026-10-26.
Run: python tests/test_google.py   (exit 1 on failure)"""
import os
import sys
import tempfile
import time

TMP = tempfile.mkdtemp()
os.environ["JOB_STORE_DIR"] = os.path.join(TMP, "jobs")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(TMP, "g.db").replace("\\", "/")
os.environ["APP_USER"] = "bossg"
os.environ["APP_PASSWORD"] = "adminpass123"
os.environ["SECRET_KEY"] = "test-secret-key-google-only"
os.environ["SKIP_STARTUP_PROBES"] = "1"
os.environ.pop("GOOGLE_PLACES_API_KEY", None)
os.environ.pop("BROWSER_FALLBACK", None)
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import app as appmod  # noqa: E402
from scraper import browser as B, capabilities as C, google_browser as G, google_places as GP, jobs as J  # noqa: E402

failures = []


def ok(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{str(detail)[:400]}]" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def fx(name):
    return open(os.path.join(HERE, "fixtures", name), encoding="utf-8").read()


# ---- Places API --------------------------------------------------------------------------------------------------------
PLACE = {"displayName": {"text": "Naresh i Technologies"}, "primaryTypeDisplayName": {"text": "Software Training Institute"},
         "formattedAddress": "2nd Floor, Durga Bhavani Plaza, Ameerpet, Hyderabad, Telangana 500016",
         "addressComponents": [{"longText": "Hyderabad", "types": ["locality"]}, {"longText": "Telangana", "types": ["administrative_area_level_1"]},
                               {"longText": "500016", "types": ["postal_code"]}],
         "internationalPhoneNumber": "+91 40 2374 6666", "websiteUri": "http://www.nareshit.com/",
         "location": {"latitude": 17.4373, "longitude": 78.4481}, "googleMapsUri": "https://maps.google.com/?cid=1",
         "regularOpeningHours": {"weekdayDescriptions": ["Monday: 7 AM - 9 PM", "Tuesday: 7 AM - 9 PM"]},
         "rating": 4.4, "userRatingCount": 7697, "businessStatus": "OPERATIONAL"}
row = GP.normalise(PLACE)
ok("an API place maps onto the Places columns", row["Name"] == "Naresh i Technologies" and row["City"] == "Hyderabad" and row["State"] == "Telangana"
   and row["Postcode"] == "500016" and row["Phone"] == "+91 40 2374 6666" and row["Website"] == "http://www.nareshit.com/", row)
ok("hours, rating and source are kept", row["Opening Hours"].startswith("Monday: 7 AM") and row["Other Details"] == "Rating 4.4 (7697 reviews)"
   and row["Data Source"] == "Google Maps (API)", row)
ok("a permanently closed place is dropped", GP.normalise({**PLACE, "businessStatus": "CLOSED_PERMANENTLY"}) is None)


class R:
    def __init__(self, code, data):
        self.status_code, self._d = code, data

    def json(self):
        return self._d


sent = []


def pages(*responses):
    it = iter(responses)

    def _post(url, headers=None, json=None, timeout=None):
        sent.append((headers, json))
        return next(it)
    return _post


rows = GP.search("coaching in Ameerpet", "AIza-test-key", 30, _post=pages(
    R(200, {"places": [PLACE] * 20, "nextPageToken": "t2"}), R(200, {"places": [PLACE] * 20, "nextPageToken": "t3"})))
ok("pages are followed until the limit, and the limit is respected", len(rows) == 30 and len(sent) == 2 and sent[1][1]["pageToken"] == "t2", len(rows))
ok("the field mask asks only for sheet fields and the key goes in a header", "places.websiteUri" in sent[0][0]["X-Goog-FieldMask"]
   and sent[0][0]["X-Goog-Api-Key"] == "AIza-test-key" and "key" not in str(sent[0][1]))
for code, msg, want in ((403, "Billing must be enabled", "billing"), (403, "Places API (New) has not been used in project", "not enabled"),
                        (403, "API key not valid", "refused this API key"), (429, "quota", "quota")):
    try:
        GP.search("x", "AIza-test-key", 5, _post=pages(R(code, {"error": {"message": msg}})))
        ok(f"HTTP {code} raises", False)
    except GP.GooglePlacesError as e:
        ok(f"HTTP {code} ({msg[:20]}) -> readable reason, no key", want in str(e) and "AIza" not in str(e), str(e))
ok("test_key is one cheap call", GP.test_key("AIza-test-key", _post=pages(R(200, {"places": [{"id": "x"}]}))) == (True, ""))

# ---- Google pages read in the browser ---------------------------------------------------------------------------------------
place_url = fx("gmaps_place_url.txt").strip()
p = G.parse_place_html(fx("gmaps_place.html"), place_url)
ok("a real Google Maps place page reads into a row", p and p["Name"] == "Perfect Academy" and p["Category"] == "Coaching Center"
   and p["Phone"] == "+91 89853 13100" and p["Other Details"] == "Rating 4.9", p)
ok("the address loses its 'Address:' label and the position comes from the link",
   p["Address"].startswith("B-4, First floor") and abs(p["Latitude"] - 17.4320063) < 1e-6 and p["Data Source"] == "Google Maps (browser)", p)
links = G.maps_links(fx("gmaps_list.html"))
ok("results list: place links in order, the Sponsored one skipped", links and "Perfect+Academy" in links[0] and not any("Datamites" in x for x in links), links)
ok("Google's 'unusual traffic' page is recognised", G.looks_blocked("https://www.google.com/sorry/index?continue=x", fx("google_sorry.html"))
   and G.looks_blocked("", fx("google_sorry.html")))
ok("a normal place page is not mistaken for a block", not G.looks_blocked(place_url, fx("gmaps_place.html")))
SERP = ('<div id="search"><div class="g"><a href="https://www.digitalvidya.com/"><h3>Digital Vidya - Digital Marketing Courses</h3></a>'
        '<div class="VwiC3b">Hyderabad centre in Ameerpet. Call +91 80100 33033</div></div>'
        '<div class="g"><a href="/url?q=https://apectraining.com/&sa=U"><h3>APEC IT Training</h3></a></div>'
        '<div class="g"><a href="https://maps.google.com/x"><h3>Map</h3></a></div></div>')
res = G.parse_search_html(SERP)
ok("search results: title, real link (unwrapped), snippet; Google's own links dropped",
   [r["href"] for r in res] == ["https://www.digitalvidya.com/", "https://apectraining.com/"] and res[0]["body"].startswith("Hyderabad centre"), res)

# ---- the per-run session: pacing, budget, stop after a block ---------------------------------------------------------------
real_run, real_sleep = B.run, G.time.sleep
G.time.sleep = lambda s: None
calls = []
B.run = lambda task, *a, **k: calls.append(a) or [{"title": "t", "href": "https://x.com", "body": ""}]
os.environ["GOOGLE_BROWSER_MAX_SEARCHES"] = "2"
s = G.Session()
got = [len(s.search("q1", 5)), len(s.search("q2", 5)), len(s.search("q3", 5))]
ok("GOOGLE_BROWSER_MAX_SEARCHES caps searches per run", got == [1, 1, 0] and len(calls) == 2, got)
os.environ.pop("GOOGLE_BROWSER_MAX_SEARCHES")


def blocked(task, *a, **k):
    raise G.GoogleBlocked("captcha")


B.run = blocked
log = []
s2 = G.Session(log.append)
ok("a block stops Google for the rest of the run", s2.search("q", 5) == [] and s2.blocked and s2.maps("q", 5) == [] and s2.used == 1, s2.used)
ok("...and says so, without trying to get around it", any("never try to get around" in m for m in log), log)
B.run, G.time.sleep = real_run, real_sleep

# ---- runs: which Google route is used ----------------------------------------------------------------------------------------
class FJ:
    def __init__(self, spec):
        self.spec, self.log, self.problems, self.activity, self.gplaces_used, self.google = spec, [], [], "", 0, None
        self.cancelled = type("E", (), {"is_set": staticmethod(lambda: False)})

    def say(self, m):
        self.log.append(m)


SPEC = {"mode": "places", "category": "Coaching & training centers", "max_results": 20}
with appmod.app.app_context():
    real_search, real_enabled = GP.search, B.enabled
    asked = []
    GP.search = lambda q, key, n, **k: asked.append((q, n)) or [dict(row)]
    j = FJ({**SPEC, "google_places_key": "AIza-test-key"})
    out = J._google_maps_rows(j, "Ameerpet, Hyderabad")
    ok("with a key the official API is used", out and out[0]["Data Source"] == "Google Maps (API)" and asked == [("coaching and training centers in Ameerpet, Hyderabad", 20)], asked)
    os.environ["GOOGLE_PLACES_MAX_PER_RUN"] = "1"
    asked.clear()
    j2 = FJ({**SPEC, "google_places_key": "AIza-test-key"})
    J._google_maps_rows(j2, "A")
    J._google_maps_rows(j2, "B")
    ok("GOOGLE_PLACES_MAX_PER_RUN caps a run", len(asked) == 1 and any("already used" in m for m in j2.log), j2.log)
    os.environ.pop("GOOGLE_PLACES_MAX_PER_RUN")

    def fail(*a, **k):
        raise GP.GooglePlacesError("Google needs billing turned on for this project before the Places API answers.")
    GP.search = fail
    B.enabled = lambda: True
    G_real_maps = G.Session.maps
    G.Session.maps = lambda self, q, n: [{"Name": "Perfect Academy", "Data Source": "Google Maps (browser)"}]
    j3 = FJ({**SPEC, "google_places_key": "AIza-test-key"})
    out = J._google_maps_rows(j3, "Ameerpet")
    ok("an API failure is reported and the browser takes over on a PC", out and out[0]["Data Source"] == "Google Maps (browser)"
       and any("billing" in p_ for p_ in j3.problems), (out, j3.problems))
    B.enabled = lambda: False
    ok("no key and no browser: nothing from Google, no error", J._google_maps_rows(FJ(dict(SPEC)), "Ameerpet") == [])
    G.Session.maps = G_real_maps
    GP.search, B.enabled = real_search, real_enabled

    # the source label survives into the sheet
    jq = type("Q", (), {})()
    jq.spec, jq.log, jq.activity = {"mode": "places", "category": "Coaching & training centers", "custom_fields": []}, [], ""
    jq.say = jq.log.append
    jq.rows = [dict(row), {"Name": "Sri Ram Classes", "Phone": "", "Email": "", "Website": ""}]
    jq.columns = ["Name", "Phone", "Email", "Website", "Data Source"]
    J._annotate_quality(jq)
    srcs = {r["Business Name"]: r["Source"] for r in jq.rows}
    ok("Google rows keep their source label; map rows say OpenStreetMap",
       srcs.get("Naresh i Technologies", "").startswith("Google Maps (API)") and srcs.get("Sri Ram Classes") == "OpenStreetMap", srcs)

    # web: Google in the browser only when the free engines found nothing, and only without a Brave key
    real_ws, real_jsleep = J.web_search, J.time.sleep
    J.web_search, J.time.sleep = (lambda q, region, n, say, *a, **k: []), (lambda s: None)
    B.enabled = lambda: True
    G.Session.search = lambda self, q, n: [{"title": "Digital Vidya - Digital Marketing Institute in Ameerpet", "href": "https://www.digitalvidya.com/",
                                           "body": "Ameerpet Hyderabad"}]
    jw = FJ({"queries": ["digital marketing institute in Ameerpet, Hyderabad"], "platforms": ["web"], "max_results": 10, "region": "in-en"})
    hits, _ = J._search(jw)
    ok("web: when the free engines find nothing, Google in the browser is asked", [h["url"] for h in hits] == ["https://www.digitalvidya.com/"], hits)
    jk = FJ({"queries": ["x in y"], "platforms": ["web"], "max_results": 10, "region": "in-en", "brave_key": "BSA-test-key-123"})
    ok("web: with a Brave key the browser is never used for search", J._search(jk)[0] == [] and jk.google is None)
    J.web_search, J.time.sleep = real_ws, real_jsleep
    B.enabled = real_enabled

    # ---- registry, setup checklist, settings and spec ------------------------------------------------------------------
    st = {x["id"]: x for x in C.run_options("places", "standard", {})["setup"]}
    ok("places setup suggests the Google Maps key when there is none", st.get("google_places", {}).get("status") == "optional")
    ok("...and not once a key is set", "google_places" not in {x["id"] for x in C.run_options("places", "standard", {"google_places_api_key": "k"})["setup"]})
    s, e, code = appmod.build_job_spec({"mode": "places", "category": "Hindu temples", "locations": "Pune"}, {"google_places_api_key": "AIza-user"}, 1)
    ok("a places spec carries the person's key", e is None and s["google_places_key"] == "AIza-user")
    ok("...which never reaches the stored public spec", "google_places_key" not in J.public_spec(s))
    os.environ["GOOGLE_PLACES_API_KEY"] = "AIza-server"
    s, e, code = appmod.build_job_spec({"mode": "places", "category": "Hindu temples", "locations": "Pune"}, {}, 1)
    ok("without a personal key the server's key is used", s["google_places_key"] == "AIza-server")
    os.environ.pop("GOOGLE_PLACES_API_KEY")

c = appmod.app.test_client()
c.post("/login", data={"username": "BOSSG", "password": "adminpass123"})
c.post("/api/settings", json={"google_places_api_key": "AIza-saved-key-123456"})
st = c.get("/api/settings").get_json()
ok("settings show the Google key only as a mask", st["google_places_key_mask"] and "saved-key" not in str(st))
real_tk = GP.test_key
GP.test_key = lambda key, **k: (False, "Google needs billing turned on for this project before the Places API answers.")
r = c.post("/api/settings/key-test", json={"name": "google_places"}).get_json()
ok("the key test reports Google's reason", r["ok"] is False and "billing" in r["error"] and "AIza" not in str(r), r)
GP.test_key = real_tk

print(f"\n{len(failures)} failure(s)" if failures else "\nAll tests passed")
sys.exit(1 if failures else 0)
