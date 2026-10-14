"""Phase 4 durability tests. Run: python tests/test_phase4_durable.py (exit 1 on failure)."""
import gzip
import json
import os
import sys
import tempfile
import time

TMP = tempfile.mkdtemp()
STORE = os.path.join(TMP, "jobs")
os.environ["JOB_STORE_DIR"] = STORE
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(TMP, "t4.db").replace("\\", "/")
os.environ["APP_USER"] = "boss4"
os.environ["APP_PASSWORD"] = "adminpass123"
os.environ["SECRET_KEY"] = "test-secret-key-phase4-only"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scraper import job_store as JS  # noqa: E402
from scraper import jobs as J  # noqa: E402
import app as appmod  # noqa: E402

app = appmod.app
failures = []


def ok(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        failures.append(name)


J._run_web = lambda job: (setattr(job, "columns", ["Name", "Emails"]),
                          job.rows.append({"Name": "A", "Emails": "a@a.com"}))

c = app.test_client()
ok("admin sign in", c.post("/login", data={"username": "BOSS4", "password": "adminpass123"}).status_code == 302)

# ---- finished job persists and is collectible after memory drop ----
job = J.Job({"mode": "web", "queries": ["x"], "max_results": 1, "custom_fields": [],
             "fields": [], "platforms": ["web"], "ai": {}}, 1)
J.JOBS[job.id] = job
J._run(job)
ok("run finishes", job.status == "done" and len(job.rows) == 1)
ok("payload on disk", os.path.exists(os.path.join(STORE, job.id + ".json.gz")))
JOBS_POP = J.JOBS.pop(job.id)  # simulate restart: memory gone, disk remains
assert JOBS_POP is not None
st = c.get(f"/api/jobs/{job.id}").get_json()
ok("status served from disk", st["status"] == "done" and st["count"] == 1 and st.get("stored") is True)
res = c.get(f"/api/jobs/{job.id}/result").get_json()
ok("result served from disk", res["rows"] == [{"Name": "A", "Emails": "a@a.com"}])
ok("collect-then-delete removes file",
   c.delete(f"/api/jobs/{job.id}").status_code == 200
   and not os.path.exists(os.path.join(STORE, job.id + ".json.gz"))
   and c.get(f"/api/jobs/{job.id}").status_code == 404)

# ---- cancelled-with-rows persists ----
job2 = J.Job({"mode": "web", "queries": ["x"], "max_results": 1, "custom_fields": [],
              "fields": [], "platforms": ["web"], "ai": {}}, 1)
job2.columns = ["Name"]
job2.rows = [{"Name": "Half"}]
job2.status, job2.error = "cancelled", None
from datetime import datetime as _dt

job2.finished = _dt.now()
J._persist_job(job2, "cancelled")
J.JOBS.pop(job2.id, None)
ok("cancelled rows collectible", c.get(f"/api/jobs/{job2.id}/result").get_json()["rows"] == [{"Name": "Half"}])
c.delete(f"/api/jobs/{job2.id}")

# ---- no secrets on disk ----
spec = {"mode": "social", "items": [{"url": "https://linkedin.com/in/x"}],
        "brightdata_key": "test-bd-SECRET-123", "apify_key": "test-ap-SECRET-456",
        "custom_fields": [], "brave_key": "BSA-SECRET-789", "ai": {"key": "sk-SECRET"}}
job3 = J.Job(spec, 1)
job3.columns = ["Name"]
job3.rows = [{"Name": "X"}]
job3.status = "done"
J._persist_job(job3, "done")
with gzip.open(os.path.join(STORE, job3.id + ".json.gz"), "rb") as f:
    blob = f.read().decode("utf-8")
ok("no secrets on disk", "SECRET" not in blob and "BSA-SECRET" not in blob)
JS.remove(job3.id)

# ---- TTL purge ----
old = {"owner_id": 1, "mode": "web", "spec_public": {}, "columns": ["Name"], "rows": [{"Name": "O"}],
       "status": "done", "error": None, "log_tail": [], "finished_at": time.time() - 3 * 3600}
JS.save("abc123", old)
ok("expired purged", JS.purge(2 * 3600) >= 1 and JS.load("abc123") is None)

# ---- corrupt file handled ----
with open(os.path.join(STORE, "deadbeef.json.gz"), "wb") as f:
    f.write(b"not-gzip-at-all")
ok("corrupt file dropped", JS.load("deadbeef") is None and not os.path.exists(os.path.join(STORE, "deadbeef.json.gz")))

# ---- running marker becomes an error record on rehydrate ----
JS.save("cafe01", {"owner_id": 1, "mode": "web", "spec_public": {}, "columns": [],
                   "rows": [], "status": "running", "error": None, "log_tail": [],
                   "finished_at": 0})
rep = J.rehydrate_jobs()
rec = JS.load("cafe01")
ok("restart converts running to error", rep["converted"] >= 1 and rec["status"] == "error"
   and "restarted" in (rec["error"] or ""))
JS.remove("cafe01")

# ---- stored jobs report their real mode; running markers never expire by finished_at ----
job_m = J.Job({"mode": "social", "items": [], "custom_fields": [], "ai": {}}, 1)
job_m.columns, job_m.rows, job_m.status = ["Name"], [{"Name": "M"}], "done"
from datetime import datetime as _dt2

job_m.finished = _dt2.now()
J._persist_job(job_m, "done")
stored_m = J.load_stored_job(job_m.id)
ok("stored mode survives", stored_m is not None and stored_m.to_dict()["mode"] == "social")
JS.remove(job_m.id)
# start_job writes a running marker synchronously, with no finish time (so the TTL
# purge, which falls back to file age, can never delete a still-running job)
seen_marker = {}
_orig_save = JS.save


def _spy_save(jid, payload):
    seen_marker.setdefault("payload", payload)
    return _orig_save(jid, payload)


JS.save = _spy_save
try:
    J.start_job({"mode": "web", "queries": ["zzz-noop"], "max_results": 1, "custom_fields": [],
                 "fields": [], "platforms": ["web"], "ai": {}}, 1)
finally:
    JS.save = _orig_save
_marker = seen_marker.get("payload", {})
ok("running marker carries no finish time",
   _marker.get("status") == "running" and _marker.get("finished_at") == 0)
import glob as _glob

for p in _glob.glob(os.path.join(STORE, "*.json.gz")):
    os.remove(p)
for jid in list(J.JOBS):
    J.JOBS.pop(jid, None)

# ---- pacing presets ----
with app.app_context():
    from models import AppSetting

    AppSetting.set("pacing_preset", "fast")
    from models import db as _db

    _db.session.commit()
    fast = J._current_gap()
    AppSetting.set("pacing_preset", "conservative")
    _db.session.commit()
    slow = J._current_gap()
    AppSetting.set("pacing_preset", "standard")
    _db.session.commit()
ok("pacing preset changes gaps", fast <= slow and fast > 0)
ok("bad preset ignored", c.post("/api/admin/settings", json={"pacing_preset": "ludicrous"}).status_code == 200
   and c.get("/api/admin/settings").get_json()["pacing_preset"] == "standard")
ok("valid preset saved", c.post("/api/admin/settings", json={"pacing_preset": "fast"}).status_code == 200
   and c.get("/api/admin/settings").get_json()["pacing_preset"] == "fast")
with app.app_context():
    from models import AppSetting as _AS3, db as _db3

    _AS3.set("pacing_preset", "standard")
    _db3.session.commit()

# ---- stored jobs keep their stats (no zeroed run detail after a restart) ----
job_s = J.Job({"mode": "web", "queries": ["x"], "max_results": 1, "custom_fields": [],
               "fields": ["emails"], "platforms": ["web"], "ai": {}}, 1)
job_s.columns = ["Name", "Emails"]
job_s.rows = [{"Name": "S", "Emails": "s@s.com"}]
job_s.status = "done"
from datetime import datetime as _dt3

job_s.finished = _dt3.now()
J._persist_job(job_s, "done")
J.JOBS.pop(job_s.id, None)
st_s = c.get(f"/api/jobs/{job_s.id}").get_json()
ok("stored stats survive", st_s.get("with_email") == 1 and isinstance(st_s.get("elapsed"), int))
c.delete(f"/api/jobs/{job_s.id}")

# ---- boot wiring: a fresh process rehydrates running markers ----
import subprocess as _sp

BOOT_STORE = os.path.join(TMP, "bootjobs")
os.makedirs(BOOT_STORE, exist_ok=True)
import scraper.job_store as _JSB

os.environ["JOB_STORE_DIR"] = BOOT_STORE
_JSB.save("b00700", {"owner_id": 1, "mode": "web", "spec_public": {}, "columns": [],
                     "rows": [], "status": "running", "error": None, "log_tail": [],
                     "finished_at": 0})
env = dict(os.environ, DATABASE_URL="sqlite:///" + os.path.join(TMP, "boot.db"),
           APP_USER="bootboss", APP_PASSWORD="adminpass123",
           SECRET_KEY="test-secret-key-boot-only")
probe = _sp.run([sys.executable, "-c",
                 "import sys; sys.path.insert(0, '.');"
                 "from scraper import job_store as JSB;"
                 "import app as A;"
                 "print('booted', JSB.load('b00700')['status'])"],
                capture_output=True, text=True, timeout=120,
                cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))), env=env)
os.environ["JOB_STORE_DIR"] = STORE
ok("boot converts running markers", probe.returncode == 0 and "booted error" in (probe.stdout or ""),
   (probe.stderr or "")[-300:])

print(f"\n{len(failures)} failure(s)" if failures else "\nAll tests passed")
sys.exit(1 if failures else 0)
