"""Backend tests. Run:  python tests/test_api.py   (exit code 1 if anything fails). Uses a throwaway SQLite database."""
import base64
import io
import json
import os
import sys
import tempfile
import time

TMP = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(TMP, "t.db").replace("\\", "/")
os.environ["APP_USER"] = "boss"
os.environ["APP_PASSWORD"] = "adminpass123"
os.environ["SECRET_KEY"] = "test-secret-key-for-unit-tests-only"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from openpyxl import load_workbook  # noqa: E402

import app as appmod  # noqa: E402
from models import VaultCredential  # noqa: E402
from scraper import jobs as J  # noqa: E402

app = appmod.app
failures = []


def ok(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        failures.append(name)


c = app.test_client()

# ---- public PWA files ------------------------------------------------------------------------
m = c.get("/manifest.webmanifest")
mj = m.get_json(force=True)
ok("manifest is public and valid", m.status_code == 200 and mj["display"] == "standalone" and mj["start_url"] == "/")
sizes = {(i["sizes"], i.get("purpose")) for i in mj["icons"]}
ok("manifest has 192, 512 and maskable icons", {("192x192", "any"), ("512x512", "any"), ("512x512", "maskable")} <= sizes)
ok("every manifest icon file exists", all(c.get(i["src"]).status_code == 200 for i in mj["icons"]))
sw = c.get("/sw.js")
ok("service worker served from root, never cached", sw.status_code == 200 and "no-cache" in sw.headers["Cache-Control"]
   and sw.headers["Service-Worker-Allowed"] == "/" and "javascript" in sw.mimetype)
ok("service worker precaches the app files", b"/static/app.js?v=" in sw.data and b"/offline.html" in sw.data)
ok("service worker never caches the API", b'startsWith("/api/")' in sw.data)
ok("offline page is public", c.get("/offline.html").status_code == 200)
login_page = c.get("/login")
ok("login page links manifest and logo", b"/manifest.webmanifest" in login_page.data and b"logo.svg" in login_page.data)
ok("logo is a real SVG", b"<svg" in c.get("/static/logo.svg").data[:200])

# ---- access control --------------------------------------------------------------------------
ok("home redirects to login", c.get("/").status_code == 302)
ok("api gives 401 when signed out", c.get("/api/settings").status_code == 401)
ok("old server-side sheet routes are gone", c.get("/api/sheets").status_code in (401, 404))
r = c.post("/login", data={"username": "BOSS", "password": "adminpass123"})
ok("admin from APP_USER/APP_PASSWORD can sign in", r.status_code == 302)
home = c.get("/")
ok("app shell renders with PWA tags and logo", home.status_code == 200 and b'rel="manifest"' in home.data and b"brand-logo" in home.data
   and b"store.js?v=" in home.data and b"sheetops.js?v=" in home.data)
ok("app config includes the user id (used to name the browser database)", b'"id": 1' in home.data or b'"id":1' in home.data)

# ---- settings: keys encrypted, never returned ------------------------------------------------
c.post("/api/settings", json={"ai_provider": "openai", "ai_api_key": "sk-test-1234567890ABCD"})
s = c.get("/api/settings").get_json()
ok("saved key is masked in the API", s["ai_key_mask"].endswith("ABCD") and "sk-test" not in json.dumps(s))
with app.app_context():
    raw = VaultCredential.query.filter_by(name="ai_api_key").first().encrypted_value
ok("saved key is encrypted in the database", "sk-test" not in raw)
ok("cookies are ignored while restricted mode is off", c.post("/api/settings", json={"li_at_cookie": "x"}).status_code == 200
   and c.get("/api/settings").get_json()["li_cookie_mask"] == "")

# ---- runs: live on the server only until the browser collects them ---------------------------
def fake_web(job):
    job.columns = ["Name", "Emails"]
    job.rows = [{"Name": "A", "Emails": ["a@a.com", "b@b.com"]}, {"Name": "B", "Emails": []}]
    job.total = job.done = 2
J._run_web = fake_web
r = c.post("/api/jobs", json={"mode": "web", "queries": "schools", "fields": ["emails"]})
jid = r.get_json()["id"]
for _ in range(50):
    if c.get(f"/api/jobs/{jid}").get_json()["status"] not in ("queued", "running"):
        break
    time.sleep(0.1)
st = c.get(f"/api/jobs/{jid}").get_json()
ok("run finishes", st["status"] == "done" and st["count"] == 2, st["status"])
res = c.get(f"/api/jobs/{jid}/result").get_json()
ok("result gives plain-text cells (lists joined)", res["rows"][0]["Emails"] == "a@a.com; b@b.com" and res["columns"] == ["Name", "Emails"])
c.post("/api/admin/users", json={"username": "ravi", "password": "memberpass1", "role": "viewer"})
c.post("/api/admin/users", json={"username": "meena", "password": "memberpass2", "role": "member"})
meena = app.test_client(); meena.post("/login", data={"username": "meena", "password": "memberpass2"})
ok("another user cannot see or fetch my run", meena.get(f"/api/jobs/{jid}").status_code == 404 and meena.get(f"/api/jobs/{jid}/result").status_code == 404)
ok("another user cannot delete my run", meena.delete(f"/api/jobs/{jid}").status_code == 404)
ok("forgetting a run frees the server's copy", c.delete(f"/api/jobs/{jid}").status_code == 200 and c.get(f"/api/jobs/{jid}").status_code == 404)

# a run that is stopped while running
def slow_web(job):
    job.columns = ["Name"]
    for _ in range(100):
        if job.cancelled.is_set():
            return
        time.sleep(0.05)
J._run_web = slow_web
jid2 = c.post("/api/jobs", json={"mode": "web", "queries": "x", "fields": ["emails"]}).get_json()["id"]
ok("result of a running job is refused (409)", c.get(f"/api/jobs/{jid2}/result").status_code == 409)
ok("a running job cannot be forgotten (409)", c.delete(f"/api/jobs/{jid2}").status_code == 409)
c.post(f"/api/jobs/{jid2}/stop")
for _ in range(50):
    if c.get(f"/api/jobs/{jid2}").get_json()["status"] == "cancelled":
        break
    time.sleep(0.1)
ok("stopping a run works", c.get(f"/api/jobs/{jid2}").get_json()["status"] == "cancelled")

# outage => failed with the reason
def boom(*a, **k):
    raise J.places.PlaceError("OpenStreetMap servers are busy (HTTP 504). Please try again in a few minutes.")
J.places.search_places = boom
J.time.sleep = lambda s: None
job = J.Job({"mode": "places", "category": "Hindu temples", "locations": ["X"], "max_results": 5, "custom_fields": [], "file_name": "", "ai": {}}, 1)
J._run(job)
ok("a service outage shows as failed with the reason", job.status == "error" and "busy" in job.error)
old = J.Job({"mode": "web"}, 1); old.finished = J.datetime(2000, 1, 1); J.JOBS["old"] = old; J.purge_jobs()
ok("purge removes expired finished jobs", "old" not in J.JOBS)

# viewer cannot start runs
viewer = app.test_client(); viewer.post("/login", data={"username": "ravi", "password": "memberpass1"})
ok("viewer cannot start runs", viewer.post("/api/jobs", json={"mode": "web", "queries": "x", "fields": ["emails"]}).status_code == 403)
ok("viewer cannot use admin api", viewer.get("/api/admin/users").status_code == 403)
ok("viewer can still export a sheet they hold", viewer.post("/api/export/xlsx", json={"name": "n", "columns": ["a"], "rows": [{"a": 1}]}).status_code == 200)

# ---- stateless helpers -----------------------------------------------------------------------
csv_file = (io.BytesIO(b"Name,Phone\nA,123\nB,456\n"), "one.csv")
r = c.post("/api/parse", data={"file": csv_file}, content_type="multipart/form-data").get_json()
ok("parse reads a CSV", r["columns"] == ["Name", "Phone"] and len(r["rows"]) == 2 and r["name"] == "one")
ok("parse rejects other file types", c.post("/api/parse", data={"file": (io.BytesIO(b"x"), "a.exe")}, content_type="multipart/form-data").status_code == 400)
ok("parse rejects an empty request", c.post("/api/parse", data={}, content_type="multipart/form-data").status_code == 400)
ok("viewer cannot import", viewer.post("/api/parse", data={"file": (io.BytesIO(b"a\n1\n"), "a.csv")}, content_type="multipart/form-data").status_code == 403)

x = c.post("/api/export/xlsx", json={"name": "My sheet", "columns": ["Name", "Site"],
                                     "rows": [{"Name": "=HYPERLINK(\"http://evil\",\"x\")", "Site": "https://a.com"}]})
wb = load_workbook(io.BytesIO(x.data))
ok("xlsx export is a real workbook", x.data[:2] == b"PK" and wb.sheetnames == ["Data", "Run Info"])
ok("scraped text starting with = stays text", wb["Data"]["A2"].data_type == "s")
ok("xlsx export rejects an empty request", c.post("/api/export/xlsx", json={}).status_code == 400)

# ---- admin ----------------------------------------------------------------------------------
a = c.get("/api/admin/settings").get_json()
ok("admin sees database status", a["database"]["kind"] == "sqlite" and a["database"]["persistent"] is False)
ok("cannot demote the last admin", c.patch("/api/admin/users/1", json={"role": "member"}).status_code == 400)

# ---- database fallback: a dead database must not take the site down ---------------------------
os.environ["DATABASE_URL"] = "postgresql://user:pw@127.0.0.1:1/none"
t0 = time.time()
url, state = appmod._resolve_database()
ok("unreachable database falls back to SQLite", url.startswith("sqlite") and state["fallback"] and state["error"], f"{url} {state}")
ok("fallback is quick", time.time() - t0 < 15)
os.environ["DATABASE_URL"] = "postgres://u:p@host/db"
ok("Render-style postgres:// URLs use the psycopg driver", appmod._database_url().startswith("postgresql+psycopg://u:p@host/db"))

print(f"\n{len(failures)} failure(s)" if failures else "\nAll tests passed")
sys.exit(1 if failures else 0)
