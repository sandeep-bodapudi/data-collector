"""A deploy/restart while a run is going: rows found so far are kept; admins are told about risky hosting settings.
Run: python tests/test_restart_keeps_rows.py   (exit 1 on failure)"""
import os
import sys
import tempfile
import time

TMP = tempfile.mkdtemp()
os.environ["JOB_STORE_DIR"] = os.path.join(TMP, "jobs")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(TMP, "rs.db").replace("\\", "/")
os.environ["APP_USER"] = "bossrs"
os.environ["APP_PASSWORD"] = "adminpass123"
os.environ["SECRET_KEY"] = "test-secret-key-restart-only"
os.environ["SKIP_STARTUP_PROBES"] = "1"
os.environ["RUN_SNAPSHOT_SECONDS"] = "0.2"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as appmod  # noqa: E402
from scraper import job_store, jobs as J  # noqa: E402

failures = []


def ok(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        failures.append(name)


app = appmod.app
J.SNAPSHOT_EVERY = 0.2

# ---- a running run saves its rows while it goes ----------------------------------------------------------------------
with app.app_context():
    job = J.Job({"mode": "web", "queries": ["x"], "max_results": 1, "custom_fields": [], "fields": [], "platforms": ["web"]}, 1)
    job.columns = ["Name", "Emails"]
    J.JOBS[job.id] = job
    import threading
    t = threading.Thread(target=J._snapshot_while_running, args=(job,), daemon=True)
    t.start()
    job.rows.append({"Name": "Allen Coaching", "Emails": "info@allen.in"})
    time.sleep(0.7)
    snap = job_store.retrieve(job.id)
    ok("a running run's rows are snapshotted while it goes", snap and snap["status"] == "running" and len(snap["rows"]) == 1, snap and snap.get("status"))
    job.rows.append({"Name": "Naresh IT", "Emails": "info@nareshit.com"})
    time.sleep(0.7)
    ok("the snapshot follows new rows", len(job_store.retrieve(job.id)["rows"]) == 2)
    job.finished = True
    t.join(2)
    ok("the snapshot loop stops when the run ends", not t.is_alive())

    # ---- restart: the thread is gone, the snapshot is not -----------------------------------------------------------
    J.JOBS.pop(job.id, None)
    rep = J.rehydrate_jobs()
    rec = job_store.retrieve(job.id)
    ok("after a restart the run is 'cancelled' with its rows, not an empty error",
       rec["status"] == "cancelled" and len(rec["rows"]) == 2 and "2 rows collected before that are kept" in rec["error"], rec.get("error"))
    c = app.test_client()
    c.post("/login", data={"username": "BOSSRS", "password": "adminpass123"})
    st = c.get(f"/api/jobs/{job.id}").get_json()
    ok("the browser sees a finished run with rows to save", st["status"] == "cancelled" and st["count"] == 2, st)
    res = c.get(f"/api/jobs/{job.id}/result").get_json()
    ok("and can collect those rows", [r["Name"] for r in res["rows"]] == ["Allen Coaching", "Naresh IT"], res.get("rows"))

    # a run that had found nothing yet still ends clearly
    job_store.persist("abc123def4", {"owner_id": 1, "mode": "web", "spec_public": {}, "effective": {}, "columns": [], "rows": [],
                                     "status": "running", "error": None, "log_tail": [], "finished_at": 0, "started_at": time.time()})
    J.rehydrate_jobs()
    rec2 = job_store.retrieve("abc123def4")
    ok("a restarted run with no rows yet is an error that says what happened", rec2["status"] == "error" and "before any rows were found" in rec2["error"])

    # ---- admin warnings about hosting settings ------------------------------------------------------------------------
    real_on, real_from = appmod.ON_SERVER, appmod.SECRET_FROM_ENV
    appmod.ON_SERVER, appmod.SECRET_FROM_ENV = True, False
    w = c.get("/api/admin/connections").get_json()["warnings"]
    ok("on a server without SECRET_KEY the admin is warned", any("SECRET_KEY is not set" in x for x in w), w)
    ok("on a server with the disk job store the admin is warned", any("JOB_STORE_BACKEND=db" in x for x in w), w)
    os.environ["JOB_STORE_BACKEND"] = "db"
    appmod.SECRET_FROM_ENV = True
    ok("with both set there are no warnings", c.get("/api/admin/connections").get_json()["warnings"] == [])
    os.environ.pop("JOB_STORE_BACKEND")
    appmod.ON_SERVER = False
    appmod.SECRET_FROM_ENV = False
    ok("a local machine gets no hosting warnings", c.get("/api/admin/connections").get_json()["warnings"] == [])
    appmod.ON_SERVER, appmod.SECRET_FROM_ENV = real_on, real_from

print(f"\n{len(failures)} failure(s)" if failures else "\nAll tests passed")
sys.exit(1 if failures else 0)
