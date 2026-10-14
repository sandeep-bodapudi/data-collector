"""Phase 2 admin/cascade integration tests. Run: python tests/test_phase2_admin_api.py (exit 1 on failure)."""
import os
import sys
import tempfile
import time

TMP = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(TMP, "t2.db").replace("\\", "/")
os.environ["JOB_STORE_DIR"] = os.path.join(TMP, "jobs")  # durable-job payloads stay in the throwaway dir
os.environ["APP_USER"] = "boss2"
os.environ["APP_PASSWORD"] = "adminpass123"
os.environ["SECRET_KEY"] = "test-secret-key-phase2-only"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as appmod  # noqa: E402
from scraper import jobs as J  # noqa: E402
from scraper import search as S  # noqa: E402
from scraper import places as P  # noqa: E402

app = appmod.app
failures = []


def ok(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        failures.append(name)


c = app.test_client()
ok("admin can sign in", c.post("/login", data={"username": "BOSS2", "password": "adminpass123"}).status_code == 302)
c.post("/api/admin/users", json={"username": "mem", "password": "memberpass1", "role": "member"})
mem = app.test_client()
mem.post("/login", data={"username": "mem", "password": "memberpass1"})

# ---- member-visible status (no secrets) ----
r = mem.get("/api/connections/status")
d = r.get_json()
ok("member can see connection status", r.status_code == 200 and "status_string" in d)
ok("status leaks no keys", "sk-" not in str(d) and "BSA-" not in str(d))
ok("signed-out cannot see status", app.test_client().get("/api/connections/status").status_code in (302, 401))

# ---- admin toggle ----
r = c.get("/api/admin/connections").get_json()
ok("admin connections lists registry", "ddgs" in r["services"] and "overpass" in r["services"])
t = c.post("/api/admin/connections/ddgs", json={"enabled": False}).get_json()
ok("toggle ddgs off", t.get("enabled") == "0")
g = c.get("/api/admin/connections").get_json()
ok("toggle reflected", g["services"]["ddgs"]["enabled"] is False)
c.post("/api/admin/connections/ddgs", json={"enabled": True})
ok("unknown service rejected", c.post("/api/admin/connections/nope", json={}).status_code == 400)

# ---- test endpoints ----
ok("test missing name rejected", c.post("/api/admin/connections/test", json={}).status_code == 400)
ok("test unknown service handled", c.post("/api/admin/connections/test", json={"name": "nope"}).get_json()["result"]["healthy"] is False)
ok("test-all starts background", c.post("/api/admin/connections/test-all").get_json().get("started") is True)

# ---- budgets ----
b = c.get("/api/admin/budgets").get_json()
ok("budgets readable", "global_budget" in b)
ok("budgets writable", c.post("/api/admin/budgets", json={"social_budget_inr": 3000}).get_json().get("ok") is True)

# ---- startup probes helper ----
ok("run_startup_probes exists", callable(getattr(appmod, "run_startup_probes", None)))

# ---- Brave 429 falls back to DDGS ----
orig_brave, orig_ddgs = S._brave, S.DDGS
calls = {"n": 0}


def boom_brave(*a, **k):
    calls["n"] += 1
    raise S._BraveQuotaExhausted()


class FakeDDGS:
    def __init__(self, timeout=None):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def text(self, *a, **k):
        return [{"title": "T", "href": "https://example.com/a", "body": "b"}]


msgs = []
try:
    S._brave = boom_brave
    S.DDGS = FakeDDGS
    with S.search_keys("BSA-test-key-123"):
        hits = S.web_search("engineering colleges Hyderabad", "in-en", 5, msgs.append)
    ok("brave 429 falls back to ddgs", len(hits) == 1 and any("used up" in m for m in msgs))
finally:
    S._brave, S.DDGS = orig_brave, orig_ddgs

# ---- places: overpass disabled -> photon tried ----
import scraper.config as cfg

orig_enabled = cfg.is_enabled
o_over, o_ph, o_no, o_wd = P._overpass, P._fallback_photon, P._fallback_nominatim, P._fallback_wikidata
try:
    cfg.is_enabled = lambda name, sg=None: (name != "overpass")
    P._overpass = lambda *a, **k: (_ for _ in ()).throw(P.PlaceError("overpass down"))
    P._fallback_photon = lambda *a, **k: [{"tags": {"name": "X College"}, "lat": 17.0, "lon": 78.0}]
    P._fallback_nominatim = lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not reach nominatim"))
    P._fallback_wikidata = lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not reach wikidata"))
    P.geocode = lambda loc: {"kind": "box", "bbox": (17, 78, 18, 79), "label": loc}
    rows = P.search_places("Colleges & universities", "Hyderabad", "", 10, info=lambda m: None)
    ok("photon fallback used when overpass disabled", any(r["Name"] == "X College" for r in rows))
finally:
    cfg.is_enabled = orig_enabled
    P._overpass, P._fallback_photon, P._fallback_nominatim, P._fallback_wikidata = o_over, o_ph, o_no, o_wd
    import importlib

    importlib.reload(P)

# ---- geocode respects flags ----
try:
    cfg.is_enabled = lambda name, sg=None: False
    try:
        P.geocode("Hyderabad, India")
        ok("geocode all-disabled raises", False)
    except P.PlaceError as e:
        ok("geocode all-disabled raises", "Admin" in str(e))
finally:
    cfg.is_enabled = orig_enabled

# ---- enrich continues with note when search unavailable ----
try:
    cfg.is_enabled = lambda name, sg=None: False
    job = J.Job({"mode": "enrich", "rows": [{"Name": "A"}], "columns": ["Name"],
                 "custom_fields": [], "brave_key": "", "ai": {}}, 1)
    J._run(job)
    ok("enrich continues without search", job.status == "done" and any("guess-only" in m for m in job.log))
finally:
    cfg.is_enabled = orig_enabled

print(f"\n{len(failures)} failure(s)" if failures else "\nAll tests passed")
sys.exit(1 if failures else 0)
