"""Phase 1 of the free/paid run flow: one health rule, registry meaning, capability map, depth gate.
Run: python tests/test_capabilities.py   (exit 1 on failure)"""
import os
import sys
import tempfile
import time

TMP = tempfile.mkdtemp()
os.environ["JOB_STORE_DIR"] = os.path.join(TMP, "jobs")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(TMP, "cap.db").replace("\\", "/")
os.environ["APP_USER"] = "bosscap"
os.environ["APP_PASSWORD"] = "adminpass123"
os.environ["SECRET_KEY"] = "test-secret-key-capabilities-only"
os.environ["SKIP_STARTUP_PROBES"] = "1"
os.environ.pop("GOOGLE_CSE_KEY", None)
os.environ.pop("GOOGLE_CSE_CX", None)
os.environ.pop("BRAVE_API_KEY", None)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as appmod  # noqa: E402
from scraper import capabilities as C, config, connection_health as H  # noqa: E402

failures = []


def ok(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        failures.append(name)


app = appmod.app
AI_KEY = "sk-test-1234567890ABCD"

# ---- 1. one health rule ---------------------------------------------------------------------------------------------
H.HEALTH_CACHE.clear()
ok("no health result = usable, and reported as not checked", not H.is_unhealthy("ddgs") and not H.is_checked("ddgs"))
H.HEALTH_CACHE["ddgs"] = {"healthy": False, "checked_at": time.time()}
ok("a fresh failed check = unhealthy", H.is_unhealthy("ddgs") and H.is_checked("ddgs"))
H.HEALTH_CACHE["ddgs"] = {"healthy": False, "checked_at": time.time() - H.CACHE_TTL_SECONDS - 5}
ok("a stale failed check is ignored (unknown again)", not H.is_unhealthy("ddgs") and not H.is_checked("ddgs"))
H.HEALTH_CACHE["ddgs"] = {"healthy": True, "checked_at": time.time()}
ok("a fresh good check = healthy", not H.is_unhealthy("ddgs") and H.is_checked("ddgs"))
H.HEALTH_CACHE.clear()
ok("status_string says 'not checked yet', not FAIL, for an unprobed service",
   "Brave not checked yet" in H.status_string({"brave": {"healthy": True, "error": "", "checked": False}}))

# ---- 2. registry meaning --------------------------------------------------------------------------------------------
S = config.SERVICES
ok("every service declares a cost class", all(m.get("cost") in ("free", "key", "paid") for m in S.values()))
ok("free means no account at all: only keyless services",
   all(m["free"] == (not m["needs_key"] and m["cost"] == "free") for m in S.values()))
ok("Bright Data and Apify are paid (Bright Data used to be marked free)",
   S["brightdata"]["cost"] == "paid" and S["apify"]["cost"] == "paid" and not S["brightdata"]["free"])
ok("search keys are 'key' class (own key, free quota), not paid", S["brave"]["cost"] == "key" and S["google_cse"]["cost"] == "key")

# ---- 3. service state and the depth gate ------------------------------------------------------------------------------
V0 = {}
ok("keyless engine is usable with no key and no check",
   C.service_state("ddgs", V0)["usable"] and C.service_state("ddgs", V0)["state"] == "unchecked")
ok("a keyed service without a key is not usable (no_key)",
   C.service_state("brave", V0)["state"] == "no_key" and not C.service_state("brave", V0)["usable"])
ok("... and becomes usable once the vault has the key", C.service_state("brave", {"brave_api_key": "BSA-test-key-123456"})["usable"])
H.HEALTH_CACHE["overpass"] = {"healthy": False, "checked_at": time.time()}
ok("a fresh failed check shows as unhealthy", C.service_state("overpass", V0)["state"] == "unhealthy")
H.HEALTH_CACHE.clear()

ok("standard is always possible", C.check_depth("places", "standard", V0)[0] and C.check_depth("social", "standard", V0)[0])
r = C.check_depth("web", "deep", V0)
ok("deep is refused with no paid service, and says why", (not r[0]) and "Deep run needs a paid service" in r[1] and "no key added" in r[1], r)
ok("deep is possible once an AI key is present", C.check_depth("web", "deep", {"ai_api_key": AI_KEY})[0])
ok("custom AI endpoint without a key also counts",
   C.check_depth("places", "deep", {"ai_provider": "custom", "ai_base_url": "http://localhost:11434/v1"})[0])
ok("social deep needs a Bright Data or Apify key, not an AI key", not C.check_depth("social", "deep", {"ai_api_key": AI_KEY})[0])

# ---- 4. fields ---------------------------------------------------------------------------------------------------------
fw = {f["id"]: f for f in C.fields_for("web", {"ddgs"}, False)}
ok("web page fields are available when a search engine is usable", fw["emails"]["available"] and fw["phones"]["available"])
fw0 = {f["id"]: f for f in C.fields_for("web", set(), False)}
ok("... and greyed with a reason when there is no engine and no directory source",
   not fw0["emails"]["available"] and fw0["emails"]["reason"])
ok("... but a directory source alone is enough", {f["id"]: f for f in C.fields_for("web", set(), True)}["emails"]["available"])
fp = {f["id"]: f for f in C.fields_for("places", {"photon"}, False)}
ok("places: a fallback map source fills name/address but not opening hours",
   fp["name"]["available"] and fp["address"]["available"] and not fp["opening_hours"]["available"] and fp["opening_hours"]["reason"])
fp2 = {f["id"]: f for f in C.fields_for("places", {"overpass"}, False)}
ok("places: Overpass fills opening hours", fp2["opening_hours"]["available"])

# ---- 5. the whole map + the API -----------------------------------------------------------------------------------------
c = app.test_client()
ok("signed-out cannot read run options", c.get("/api/run-options?mode=web").status_code in (302, 401))
ok("admin signs in", c.post("/login", data={"username": "BOSSCAP", "password": "adminpass123"}).status_code == 302)
r = c.get("/api/run-options?mode=places&depth=standard")
d = r.get_json()
ok("places options answer", r.status_code == 200 and d["mode"] == "places" and d["depth"] == "standard", str(d)[:200])
ok("places options list categories and regions", "Hindu temples" in d["categories"] and any(x["id"] == "in-en" for x in d["regions"]))
ok("places chain is the free map chain", d["chain"]["places"] == ["overpass", "photon", "nominatim", "wikidata"], d["chain"])
ok("a keyless install can start a standard places run", d["can_start"] is True)
ok("deep is reported impossible with no paid key", d["deep"]["possible"] is False and d["deep"]["reason"])
dw = c.get("/api/run-options?mode=web&depth=standard").get_json()
ok("planned (not built) paid providers are listed, flagged by id", dw["deep"]["planned"] and all("id" in p for p in dw["deep"]["planned"]))
deep0 = c.get("/api/run-options?mode=places&depth=deep").get_json()
ok("AI shows as optional in a standard run, missing in a deep one",
   {s["id"]: s["status"] for s in d["setup"]}["ai"] == "optional" and {s["id"]: s["status"] for s in deep0["setup"]}["ai"] == "missing")
ok("a deep run is not startable without a paid service", deep0["can_start"] is False)
ok("bad mode / depth are rejected",
   c.get("/api/run-options?mode=nope").status_code == 400 and c.get("/api/run-options?depth=nope").status_code == 400)

c.post("/api/settings", json={"ai_provider": "openai", "ai_api_key": AI_KEY})
d2 = c.get("/api/run-options?mode=web&depth=deep").get_json()
ok("with an AI key a deep web run is possible and startable", d2["deep"]["possible"] and d2["can_start"], str(d2["deep"])[:200])
ok("options never contain a key value", "1234567890ABCD" not in str(d2))
ok("unprobed services are reported as not broken",
   all(s["state"] in ("unchecked", "ready", "no_key", "disabled") for s in d2["services"]),
   [(s["id"], s["state"]) for s in d2["services"]])

st = c.get("/api/connections/status").get_json()
ok("status endpoint: unprobed service is usable and flagged unchecked",
   st["services"]["ddgs"]["healthy"] is True and st["services"]["ddgs"]["checked"] is False)
H.HEALTH_CACHE["ddgs"] = {"healthy": False, "checked_at": time.time(), "error": "blocked"}
st = c.get("/api/connections/status").get_json()
ok("status endpoint: a fresh failed check shows unhealthy with its error",
   st["services"]["ddgs"]["healthy"] is False and st["services"]["ddgs"]["error"] == "blocked")
ad = c.get("/api/admin/connections").get_json()["services"]["overpass"]
ok("admin connections table: an unprobed service is usable and flagged unchecked (no false FAIL)", ad["healthy"] is True and ad["checked"] is False)
H.HEALTH_CACHE.clear()

# ---- 6. build_job_spec carries and enforces depth ----------------------------------------------------------------------
PLACES = {"mode": "places", "category": "Hindu temples", "locations": "Pune"}
with app.app_context():
    s, e, code = appmod.build_job_spec(dict(PLACES), {}, 1)
    ok("depth defaults to standard", e is None and s["depth"] == "standard")
    s, e, code = appmod.build_job_spec({**PLACES, "depth": "deep"}, {}, 1)
    ok("deep with no paid service is refused with 400 and a reason",
       s is None and code == 400 and "Deep run needs a paid service" in e, (e, code))
    s, e, code = appmod.build_job_spec({**PLACES, "depth": "deep"}, {"ai_api_key": AI_KEY, "ai_provider": "openai"}, 1)
    ok("deep with an AI key is accepted and recorded", e is None and s["depth"] == "deep", e)
    s, e, code = appmod.build_job_spec({**PLACES, "depth": "turbo"}, {}, 1)
    ok("an unknown depth is rejected", s is None and code == 400)
    s, e, code = appmod.build_job_spec({"mode": "web", "queries": "schools in Pune", "fields": ["emails"]}, {}, 1)
    ok("web spec carries depth too", e is None and s["depth"] == "standard", e)

# ---- 7. blockers, pricing and budget in run-options ----------------------------------------------------------------------
c2 = app.test_client()
c2.post("/login", data={"username": "BOSSCAP", "password": "adminpass123"})
o = c2.get("/api/run-options?mode=places&depth=standard").get_json()
ok("standard places: no blockers; pricing and budget are reported",
   o["blockers"] == [] and o["pricing"]["place_usd"] > 0 and o["budget"]["set"] is False, o["blockers"])
ok("standard never reports a paid run", o["paid_will_run"] is False)
o = c2.get("/api/run-options?mode=places&depth=deep").get_json()
ok("deep with AI only: startable, no Apify so no budget needed", o["can_start"] is True and o["paid_will_run"] is False, o["blockers"])
with app.app_context():
    from models import AppSetting, db
    AppSetting.set("conn_apify_places", "1")
    db.session.commit()
c2.post("/api/settings", json={"apify_api_key": "apify_api_TESTTOKEN1234567890"})
o = c2.get("/api/run-options?mode=places&depth=deep").get_json()
ok("deep with an Apify token and no budget: blocked with a clear reason",
   o["paid_will_run"] is True and o["can_start"] is False and any("monthly budget" in b for b in o["blockers"]), o["blockers"])
ok("... and the checklist has a budget row marked as needing setup", {r["id"]: r["status"] for r in o["setup"]}.get("budget") == "missing")
with app.app_context():
    AppSetting.set("social_budget_usd", "5")
    db.session.commit()
o = c2.get("/api/run-options?mode=places&depth=deep").get_json()
ok("with a budget the same run can start and shows what is left",
   o["can_start"] is True and o["budget"]["set"] and any("left" in r["why"] for r in o["setup"] if r["id"] == "budget"), o["blockers"])

# ---- 8. testing a saved paid-data key ----------------------------------------------------------------------------------
from scraper import connection_health as CH  # noqa: E402
c3 = app.test_client()
ok("signed-out cannot test keys", c3.post("/api/settings/key-test", json={"name": "apify"}).status_code in (302, 401))
c3.post("/login", data={"username": "BOSSCAP", "password": "adminpass123"})
ok("unknown connector is rejected", c3.post("/api/settings/key-test", json={"name": "nope"}).status_code == 400)
c3.post("/api/settings", json={"apify_api_key": ""})
r = c3.post("/api/settings/key-test", json={"name": "apify"}).get_json()
ok("no saved key -> asks to save one first", r["ok"] is False and "Save your Apify key first" in r["error"], r)
c3.post("/api/settings", json={"apify_api_key": "apify_api_TESTTOKEN1234567890", "brightdata_api_key": "bd-key-1234567890-abcdef"})
real_a, real_b = CH.check_apify_key, CH.check_brightdata_key
CH.check_apify_key = lambda key: {"healthy": True}
CH.check_brightdata_key = lambda key: {"healthy": False, "error": "quota exhausted"}
r = c3.post("/api/settings/key-test", json={"name": "apify"}).get_json()
ok("a working token reports ok", r == {"ok": True}, r)
r = c3.post("/api/settings/key-test", json={"name": "brightdata"}).get_json()
ok("a refused key reports the reason, never the key", r["ok"] is False and r["error"] == "quota exhausted" and "bd-key" not in str(r), r)


def boom(key):
    raise RuntimeError("secret apify_api_TESTTOKEN1234567890 leaked?")


CH.check_apify_key = boom
r = c3.post("/api/settings/key-test", json={"name": "apify"}).get_json()
ok("an unexpected failure is a readable message that does not echo the key", r["ok"] is False and "TESTTOKEN" not in str(r), r)
CH.check_apify_key, CH.check_brightdata_key = real_a, real_b
st = c3.get("/api/settings").get_json()
ok("settings expose masks for the new key boxes, not the keys", st.get("apify_key_mask") and "TESTTOKEN123456" not in str(st), st.get("apify_key_mask"))
opts = c3.get("/api/run-options?mode=web&depth=standard").get_json()
ok("setup checklist links into the right Settings section",
   any(r["href"] == "#/settings/keys" for r in opts["setup"]) and all(r["href"].startswith("#/") for r in opts["setup"] if r["href"]),
   [r["href"] for r in opts["setup"]])

# ---- 9. a single search / location keeps its commas (production bug, 2026-10-18) ------------------------------------------
with app.app_context():
    s, e, code = appmod.build_job_spec({"mode": "places", "category": "Hindu temples", "locations": "Ameerpet, Hyderabad"}, {}, 1)
    ok("one location with a comma stays one location", e is None and s["locations"] == ["Ameerpet, Hyderabad"], s and s["locations"])
    s, e, code = appmod.build_job_spec({"mode": "places", "category": "Hindu temples", "locations": "Ameerpet, Hyderabad\nKukatpally, Hyderabad"}, {}, 1)
    ok("several locations are split on lines only", e is None and s["locations"] == ["Ameerpet, Hyderabad", "Kukatpally, Hyderabad"], s and s["locations"])
    s, e, code = appmod.build_job_spec({"mode": "web", "queries": "coaching centers in Ameerpet, Hyderabad", "fields": ["emails"]}, {}, 1)
    ok("one search with a comma stays one search", e is None and s["queries"] == ["coaching centers in Ameerpet, Hyderabad"], s and s["queries"])
    s, e, code = appmod.build_job_spec({"mode": "web", "queries": ["a, b", "c"], "fields": ["emails"]}, {}, 1)
    ok("a list from a schedule or API caller is taken as given", e is None and s["queries"] == ["a, b", "c"])
    s, e, code = appmod.build_job_spec({"mode": "places", "category": "Hindu temples", "locations": "Pune", "custom_fields": "main deity, timings"},
                                       {"ai_api_key": AI_KEY, "ai_provider": "openai"}, 1)
    ok("custom AI fields still split on commas", e is None and s["custom_fields"] == ["main deity", "timings"], s and s["custom_fields"])

# ---- 10. web search that nobody answers (production, 2026-10-18) -------------------------------------------------------
from scraper import jobs as JJ  # noqa: E402


class _FJ:
    def __init__(self, spec):
        self.spec, self.problems, self.log, self.activity = spec, [], [], ""
        self.cancelled = type("E", (), {"is_set": staticmethod(lambda: False)})

    def say(self, m):
        self.log.append(m)


_real_ws, _real_sleep = JJ.web_search, JJ.time.sleep
JJ.time.sleep = lambda s: None
JJ.web_search = lambda q, region, n, say, *a, **k: []
fj = _FJ({"queries": ["IT companies in Hyderabad contact", "schools in Pune"], "platforms": ["web"], "max_results": 10, "region": "in-en"})
JJ._search(fj)
ok("every search empty and no key -> a clear problem, not a silent empty run", any("Free engines usually refuse" in p for p in fj.problems), fj.problems)
JJ.web_search = lambda q, region, n, say, *a, **k: [] if "Pune" in q else [{"title": "x", "href": "https://x.example.com", "body": ""}]
fj2 = _FJ({"queries": ["IT companies in Hyderabad contact", "schools in Pune"], "platforms": ["web"], "max_results": 10, "region": "in-en"})
JJ._search(fj2)
ok("one search answered -> no such problem (nothing found is then a real answer)", not fj2.problems, fj2.problems)
fj3 = _FJ({"queries": ["a"], "platforms": ["web"], "max_results": 10, "region": "in-en", "brave_key": "BSA-test-key-123456"})
JJ.web_search = lambda q, region, n, say, *a, **k: []
JJ._search(fj3)
ok("with a search key an empty answer is not blamed on the server", not fj3.problems)
JJ.web_search, JJ.time.sleep = _real_ws, _real_sleep

os.environ["RENDER"] = "true"
r10 = {x["id"]: x for x in C.run_options("web", "standard", {})["setup"]}
ok("on the hosted app a missing Brave key is 'recommended', with the reason", r10["brave"]["status"] == "recommended" and "cloud servers" in r10["brave"]["why"], r10.get("brave"))
os.environ.pop("RENDER")
r11 = {x["id"]: x for x in C.run_options("web", "standard", {})["setup"]}
ok("on a local machine it stays optional", r11["brave"]["status"] == "optional")

print(f"\n{len(failures)} failure(s)" if failures else "\nAll tests passed")
sys.exit(1 if failures else 0)
