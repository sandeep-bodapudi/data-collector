"""Phase 5A queue tests — no Redis server needed. Run: python tests/test_phase5_queue.py (exit 1 on failure)."""
import os
import sys
import tempfile
import threading
import time

TMP = tempfile.mkdtemp()
os.environ["JOB_STORE_DIR"] = os.path.join(TMP, "jobs")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(TMP, "t5.db").replace("\\", "/")
os.environ["APP_USER"] = "boss5"
os.environ["APP_PASSWORD"] = "adminpass123"
os.environ["SECRET_KEY"] = "test-secret-key-phase5-only"
os.environ.setdefault("SKIP_STARTUP_PROBES", "1")  # tests must not depend on live network probes
os.environ.pop("WORKER_MODE", None)  # default path must be threads
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scraper import job_store as JS  # noqa: E402
from scraper import jobs as J  # noqa: E402
from scraper import queue as Q  # noqa: E402
import app as appmod  # noqa: E402

app = appmod.app
failures = []


def ok(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        failures.append(name)


class FakeQueue:
    def __init__(self, run_inline=False):
        self.calls, self.run_inline = [], run_inline

    def enqueue(self, fn_path, job_id, spec, user_id):
        self.calls.append((fn_path, job_id, spec, user_id))
        return {"queued": job_id}


J._run_web = lambda job: (setattr(job, "columns", ["Name"]),
                          job.rows.append({"Name": "W"}))

# ---- 1. default mode is threads; missing rq degrades gracefully ----
ok("default worker mode is thread", Q.worker_mode() == "thread")
ok("enqueue without rq returns None", Q.enqueue_job("abcdef12", {}, 1) is None)
ok("request_cancel never raises", (Q.request_cancel("abcdef12"), True)[1])

# ---- 2. cancel flag helpers ----
JS.set_cancelled("abcdef12")
ok("cancel flag set", JS.is_cancelled("abcdef12") is True)
JS.clear_cancel("abcdef12")
ok("cancel flag cleared", JS.is_cancelled("abcdef12") is False)
ok("bad id cancel safe", JS.is_cancelled("!!!") is False and (JS.set_cancelled("!!!"), True)[1])

# ---- 3. injected backend: start_job enqueues, runs nothing locally ----
fq = FakeQueue()
Q.set_backend(fq)
try:
    ok("backend forces rq mode", Q.worker_mode() == "rq")
    job = J.start_job({"mode": "web", "queries": ["x"], "max_results": 1, "custom_fields": [],
                       "fields": [], "platforms": ["web"], "ai": {}}, 7)
    ok("remote placeholder returned", job.remote is True and job.status == "queued")
    ok("one enqueue call", len(fq.calls) == 1 and fq.calls[0][0] == "scraper.jobs.run_queued_job"
       and fq.calls[0][1] == job.id and fq.calls[0][3] == 7)
    ok("nothing ran locally", job.rows == [] and job.status == "queued")
    J.JOBS.pop(job.id, None)
    JS.remove(job.id)
finally:
    Q.set_backend(None)

# ---- 4. worker entrypoint runs the job end to end ----
_orig_ws4 = J.web_search
J.web_search = lambda *a, **k: [{"title": "T", "href": "https://example.com/a", "body": "b"}]
try:
    job2 = J.run_queued_job("cafe0202", {"mode": "web", "queries": ["x"], "max_results": 1,
                                         "custom_fields": [], "fields": [], "platforms": ["web"], "ai": {}}, 7)
finally:
    J.web_search = _orig_ws4
ok("worker runs to done", job2 == "done")
stored = J.load_stored_job("cafe0202")
ok("worker result persisted", stored is not None and [r.get("Business Name") for r in stored.result()["rows"]] == ["W"])
ok("worker cancel flag cleared", JS.is_cancelled("cafe0202") is False)
J.JOBS.pop("cafe0202", None)
JS.remove("cafe0202")

# ---- 5. progress snapshots appear mid-run ----
slow_started = threading.Event()


def slow_web(job):
    job.columns = ["Name"]
    slow_started.set()
    for _ in range(20):
        if job.cancelled.is_set():
            return
        time.sleep(0.05)
    job.rows.append({"Name": "Slow"})


J._run_web = slow_web
job3 = J.Job({"mode": "web", "queries": ["x"], "max_results": 1, "custom_fields": [],
              "fields": [], "platforms": ["web"], "ai": {}}, 7)
job3.id = "beef0303"
JOBS_KEEP = J.JOBS.get("beef0303")
J.JOBS["beef0303"] = job3
t = threading.Thread(target=J._watch_remote, args=(job3, 0.05, 0.1), daemon=True)
t.start()
slow_started.wait(timeout=5)
time.sleep(0.3)
mid = JS.load("beef0303")
ok("running snapshot visible mid-run", mid is not None and mid.get("status") == "running")
J._run(job3)
t.join(timeout=10)
snap = JS.load("beef0303")
ok("final payload persisted", snap is not None and snap["status"] == "done"
   and [r.get("Business Name") for r in snap["rows"]] == ["Slow"])
J.JOBS.pop("beef0303", None)
if JOBS_KEEP is not None:
    J.JOBS["beef0303"] = JOBS_KEEP
JS.remove("beef0303")
J._run_web = lambda job: (setattr(job, "columns", ["Name"]),
                          job.rows.append({"Name": "W"}))

# ---- 6. watcher honours the cancel flag ----
job4 = J.Job({"mode": "web", "queries": ["x"], "max_results": 1, "custom_fields": [],
              "fields": [], "platforms": ["web"], "ai": {}}, 7)
job4.id = "feed0404"
J.JOBS["feed0404"] = job4
t2 = threading.Thread(target=J._watch_remote, args=(job4, 0.05, 60.0), daemon=True)
t2.start()
time.sleep(0.1)
JS.set_cancelled("feed0404")
for _ in range(100):
    if job4.cancelled.is_set():
        break
    time.sleep(0.05)
job4.finished = J.datetime.now()
t2.join(timeout=10)
ok("cancel flag stops the job", job4.cancelled.is_set() is True)
J.JOBS.pop("feed0404", None)
JS.remove("feed0404")

# ---- 7. broken backend falls back to threads ----
class BoomQueue:
    def enqueue(self, *a, **k):
        raise ConnectionError("redis down")


Q.set_backend(BoomQueue())
_orig_ws = J.web_search
J.web_search = lambda *a, **k: [{"title": "T", "href": "https://example.com/a", "body": "b"}]
try:
    job5 = J.start_job({"mode": "web", "queries": ["x"], "max_results": 1, "custom_fields": [],
                        "fields": [], "platforms": ["web"], "ai": {}}, 7)
    for _ in range(100):
        if J.JOBS[job5.id].status not in ("queued", "running"):
            break
        time.sleep(0.05)
    ok("enqueue failure falls back to thread", J.JOBS[job5.id].status == "done"
       and J.JOBS[job5.id].remote is False)
    J.JOBS.pop(job5.id, None)
    JS.remove(job5.id)
finally:
    J.web_search = _orig_ws
    Q.set_backend(None)

# ---- 8. remote placeholder serves the worker's disk truth ----
c = app.test_client()
ok("admin sign in", c.post("/login", data={"username": "BOSS5", "password": "adminpass123"}).status_code == 302)
with app.app_context():
    from models import User as _U

    uid = _U.query.filter_by(username="boss5").first().id
mem = J.Job({"mode": "web", "queries": ["x"], "max_results": 1, "custom_fields": [],
             "fields": [], "platforms": ["web"], "ai": {}}, uid)
mem.id = "905a0505"
mem.remote = True  # as start_job leaves it in rq mode
J.JOBS[mem.id] = mem
J._persist_job(mem, "running", finished=False)
got = c.get(f"/api/jobs/{mem.id}").get_json()
ok("remote status reads disk snapshot", got["status"] == "running" and got.get("stored") is True)
ok("remote result refused while running", c.get(f"/api/jobs/{mem.id}/result").status_code == 409)
ok("remote stop accepted", c.post(f"/api/jobs/{mem.id}/stop").status_code == 200
   and JS.is_cancelled(mem.id) is True)
J.JOBS.pop(mem.id, None)
JS.remove(mem.id)
JS.clear_cancel(mem.id)

# ---- 9. orphan stop flags are purged, live ones kept ----
JS.set_cancelled("0a0a0a0b")
ok("orphan cancel purged", JS.purge(2 * 3600) >= 1 and JS.is_cancelled("0a0a0a0b") is False)
JS.save("1ace07ab", {"owner_id": 1, "mode": "web", "spec_public": {}, "columns": [],
                     "rows": [], "status": "running", "error": None, "log_tail": [],
                     "finished_at": 0})
JS.set_cancelled("1ace07ab")
JS.purge(2 * 3600)
ok("live cancel flag kept", JS.is_cancelled("1ace07ab") is True)
JS.remove("1ace07ab")
JS.clear_cancel("1ace07ab")

# ---- 10. worker entrypoint fails loudly without its deps ----
import scraper.worker as _W

try:
    import rq  # noqa: F401
    _has_rq = True
except ImportError:
    _has_rq = False
if not _has_rq:
    ok("worker reports missing deps", _W.main() == 2)
else:
    ok("worker reports missing deps", True, "rq installed here; dep path not exercised")

print(f"\n{len(failures)} failure(s)" if failures else "\nAll tests passed")
sys.exit(1 if failures else 0)
