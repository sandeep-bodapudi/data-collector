"""Phase 5C schedule tests. Run: python tests/test_phase5_schedule.py (exit 1 on failure)."""
import os
import sys
import tempfile
from datetime import datetime

TMP = tempfile.mkdtemp()
os.environ["JOB_STORE_DIR"] = os.path.join(TMP, "jobs")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(TMP, "t5c.db").replace("\\", "/")
os.environ["APP_USER"] = "boss5c"
os.environ["APP_PASSWORD"] = "adminpass123"
os.environ["SECRET_KEY"] = "test-secret-key-phase5c-only"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scraper.scheduler import (  # noqa: E402
    CronError, check_schedules, cron_due, describe_cron, parse_cron,
)
from scraper import jobs as J  # noqa: E402
import app as appmod  # noqa: E402

app = appmod.app
failures = []


def ok(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        failures.append(name)


# ---- cron parsing ----
ok("preset daily", parse_cron("daily") is not None)
try:
    parse_cron("not a cron")
    ok("bad cron rejected", False)
except CronError:
    ok("bad cron rejected", True)
try:
    parse_cron("0 25 * * *")
    ok("range enforced", False)
except CronError:
    ok("range enforced", True)
ok("step syntax", 0 in parse_cron("*/15 * * * *")[0] and 30 in parse_cron("*/15 * * * *")[0])

# Monday 2026-10-12 09:00 local
MON_9 = datetime(2026, 10, 12, 9, 0)
ok("daily fires at 9", cron_due("0 9 * * *", MON_9) is True)
ok("daily silent at 10", cron_due("0 9 * * *", datetime(2026, 10, 12, 10, 0)) is False)
ok("weekly monday fires", cron_due("0 9 * * 1", MON_9) is True)
ok("weekday 7 means sunday", cron_due("0 9 * * 7", datetime(2026, 10, 11, 9, 0)) is True)
ok("weekly skips tuesday", cron_due("0 9 * * 1", datetime(2026, 10, 13, 9, 0)) is False)
ok("dom-or-dow fires on either", cron_due("0 9 12 * 2", MON_9) is True)  # 12th AND Monday
ok("dom-or-dow silent on neither",
   cron_due("0 9 13 * 2", datetime(2026, 10, 14, 9, 0)) is False)  # Wed 14th, not Tue
ok("describe daily", "09:00" in describe_cron("0 9 * * *"))
ok("describe bad cron explains", "5 fields" in describe_cron("nope"))

# ---- API ----
c = app.test_client()
ok("admin sign in", c.post("/login", data={"username": "BOSS5C", "password": "adminpass123"}).status_code == 302)
c.post("/api/admin/users", json={"username": "mem5c", "password": "memberpass1", "role": "member"})
c.post("/api/admin/users", json={"username": "vw5c", "password": "viewerpass1", "role": "viewer"})
mem = app.test_client()
mem.post("/login", data={"username": "mem5c", "password": "memberpass1"})
viewer = app.test_client()
viewer.post("/login", data={"username": "vw5c", "password": "viewerpass1"})

ok("viewer cannot save schedules", viewer.post("/api/schedules", json={}).status_code == 403)
ok("name required", c.post("/api/schedules", json={"cron": "daily", "body": {"mode": "web"}}).status_code == 400)
ok("bad cron rejected", c.post("/api/schedules",
                               json={"name": "x", "cron": "never", "body": {"mode": "web"}}).status_code == 400)
ok("bad body rejected", c.post("/api/schedules",
                               json={"name": "x", "cron": "daily", "body": {"mode": "web"}}).status_code == 400)

body = {"mode": "web", "queries": "test schools", "fields": ["emails"], "platforms": ["web"]}
r = c.post("/api/schedules", json={"name": "Morning schools", "cron": "* * * * *", "body": body})
sched = r.get_json()
ok("schedule created", r.status_code == 200 and sched["name"] == "Morning schools"
   and "when" in sched and sched["enabled"] is True)

ids = [sched["id"]]
for i in range(19):
    rr = c.post("/api/schedules", json={"name": f"filler {i}", "cron": "daily", "body": body})
    ids.append(rr.get_json()["id"])
ok("schedule cap enforced", c.post("/api/schedules",
                                   json={"name": "one too many", "cron": "daily",
                                         "body": body}).status_code == 400)
for sid in ids[1:]:
    c.delete(f"/api/schedules/{sid}")
ids = ids[:1]

# ownership isolation
ok("member sees only own", mem.get("/api/schedules").get_json() == {"schedules": []})
ok("member cannot touch it", mem.delete(f"/api/schedules/{sched['id']}").status_code == 404)
ok("toggle works", c.post("/api/schedules", json={"id": sched["id"], "enabled": False}).get_json()["enabled"] is False)
c.post("/api/schedules", json={"id": sched["id"], "enabled": True})

# ---- triggering uses the same gate ----
started = []


def fake_start(spec, uid):
    started.append((spec, uid))

    class _Fake:
        id = "fake1"

    return _Fake()


orig_start = J.start_job
J.start_job = fake_start
try:
    with app.app_context():
        from models import Schedule as _S
        from models import db as _db

        row = _S.query.get(sched["id"])
        row.last_triggered_at = None
        _db.session.commit()
        out = check_schedules(datetime(2026, 10, 12, 9, 1))
        mine = [s for s in started if s[0].get("schedule_name") == "Morning schools"]
        ok("due schedule fires", out["fired"] >= 1 and len(mine) == 1
           and mine[0][0]["queries"] == ["test schools"] and mine[0][1] == row.owner_id)
        n_before = len(started)
        out2 = check_schedules(datetime(2026, 10, 12, 9, 1, 30))
        ok("no double fire in a minute", out2["fired"] == 0 and len(started) == n_before)
finally:
    J.start_job = orig_start

# budget breach is recorded, never charged
with app.app_context():
    from models import AppSetting as _AS2
    from models import db as _db2

    _AS2.set("conn_social", "1")
    _AS2.set("conn_brightdata", "1")
    _db2.session.commit()
c.post("/api/settings", json={"brightdata_api_key": "test-bd-key-1234567890"})
c.post("/api/schedules", json={"id": sched["id"], "enabled": False})  # isolate: only the social one due
sb = c.post("/api/schedules", json={"name": "Social sweep", "cron": "* * * * *",
                                    "body": {"mode": "social",
                                             "profiles": "https://linkedin.com/in/anna"}}).get_json()
ok("social schedule saved", "id" in sb)
n_before = len(started)
J.start_job = fake_start
try:
    with app.app_context():
        out3 = check_schedules(datetime(2026, 10, 12, 9, 2))
finally:
    J.start_job = orig_start
with app.app_context():
    from models import Schedule as _S2

    row2 = _S2.query.get(sb["id"])
    ok("budget breach skips loudly", out3["skipped"] >= 1 and len(started) == n_before
       and "budget" in (row2.last_status or "").lower())
c.delete(f"/api/schedules/{sb['id']}")
for sid in ids:
    c.delete(f"/api/schedules/{sid}")

ok("unknown schedule 404", c.delete("/api/schedules/999999").status_code == 404)
ok("schedules empty at end", c.get("/api/schedules").get_json() == {"schedules": []})

print(f"\n{len(failures)} failure(s)" if failures else "\nAll tests passed")
sys.exit(1 if failures else 0)
