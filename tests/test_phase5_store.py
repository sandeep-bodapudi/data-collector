"""Phase 5B shared-store tests. Run: python tests/test_phase5_store.py (exit 1 on failure)."""
import os
import sys
import tempfile
import time

TMP = tempfile.mkdtemp()
os.environ["JOB_STORE_DIR"] = os.path.join(TMP, "jobs")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(TMP, "t5b.db").replace("\\", "/")
os.environ["APP_USER"] = "boss5b"
os.environ["APP_PASSWORD"] = "adminpass123"
os.environ["SECRET_KEY"] = "test-secret-key-phase5b-only"
os.environ["JOB_STORE_BACKEND"] = "disk"
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


PAY = {"owner_id": 1, "mode": "web", "spec_public": {"mode": "web"}, "columns": ["Name"],
       "rows": [{"Name": "A"}], "status": "done", "error": None, "log_tail": [],
       "finished_at": time.time()}

ok("unknown backend falls back to disk", (os.environ.__setitem__("JOB_STORE_BACKEND", "weird"), JS.backend())[1] == "disk")
os.environ["JOB_STORE_BACKEND"] = "disk"

with app.app_context():
    # ---- db roundtrip, no disk file ----
    os.environ["JOB_STORE_BACKEND"] = "db"
    try:
        ok("db roundtrip", JS.persist("aa00bb11", PAY) is True
           and (JS.retrieve("aa00bb11") or {}).get("rows") == [{"Name": "A"}])
        ok("db mode writes no file", not os.path.exists(os.path.join(TMP, "jobs", "aa00bb11.json.gz")))
        # ---- cancel flags in db ----
        JS.cancel_request("aa00bb11")
        ok("db cancel set", JS.cancel_requested("aa00bb11") is True)
        JS.cancel_clear("aa00bb11")
        ok("db cancel cleared", JS.cancel_requested("aa00bb11") is False)
        # ---- secrets scan on the row ----
        import gzip as _gz

        from models import JobPayload as _JP

        blob = _gz.decompress(bytes(_JP.query.get("aa00bb11").data)).decode("utf-8")
        ok("row holds public payload", '"spec_public"' in blob and '"rows"' in blob)
        # ---- purge by TTL ----
        old = dict(PAY, status="done", finished_at=time.time() - 3 * 3600)
        JS.persist("bb11cc22", old)
        ok("db ttl purge", JS.clean(2 * 3600) >= 1 and JS.retrieve("bb11cc22") is None
           and JS.retrieve("aa00bb11") is not None)
        # ---- rehydrate converts db running rows ----
        JS.persist("cc22dd33", dict(PAY, status="running", finished_at=0, rows=[]))
        rep = J.rehydrate_jobs()
        rec = JS.retrieve("cc22dd33")
        ok("rehydrate covers db", rep["converted"] >= 1 and rec["status"] == "error")
        # ---- live snapshots immune to TTL; stale stop flags purged ----
        from datetime import datetime as _dt2
        from datetime import timedelta as _td2

        from models import JobCancel as _JC2
        from models import JobPayload as _JP3
        from models import db as _db3

        live = _JP3.query.get("cc22dd33")
        live.status, live.finished_at = "running", None
        live.updated_at = _dt2.now()
        _db3.session.commit()
        JS.persist("cc22dd33", dict(PAY, status="running", finished_at=0, rows=[]))
        ok("live snapshot immune", JS.clean(2 * 3600) == 0 and JS.retrieve("cc22dd33") is not None)
        _db3.session.add(_JC2(job_id="stale09"))
        _db3.session.query(_JC2).filter_by(job_id="stale09").update(
            {"created_at": _dt2.now() - _td2(hours=3)})
        _db3.session.commit()
        ok("stale db flags purged", JS.clean(2 * 3600) >= 1 and not JS.is_cancelled_db("stale09"))
        # ---- discard removes the row ----
        JS.discard("aa00bb11")
        JS.discard("cc22dd33")
        ok("discard clears db", JS.retrieve("aa00bb11") is None and JS.retrieve("cc22dd33") is None)
    finally:
        os.environ["JOB_STORE_BACKEND"] = "disk"

    # ---- dual: both land; db loss falls back to disk ----
    os.environ["JOB_STORE_BACKEND"] = "dual"
    try:
        JS.persist("dd33ee44", PAY)
        from models import JobPayload as _JP2

        ok("dual writes both", os.path.exists(os.path.join(TMP, "jobs", "dd33ee44.json.gz"))
           and _JP2.query.get("dd33ee44") is not None)
        from models import db as _db

        _db.session.delete(_JP2.query.get("dd33ee44"))
        _db.session.commit()
        ok("dual falls back to disk", (JS.retrieve("dd33ee44") or {}).get("rows") == [{"Name": "A"}])
        JS.discard("dd33ee44")
        ok("discard clears both", JS.retrieve("dd33ee44") is None
           and not os.path.exists(os.path.join(TMP, "jobs", "dd33ee44.json.gz")))
    finally:
        os.environ["JOB_STORE_BACKEND"] = "disk"

    # ---- migration moves disk files, skips present rows, reruns safely ----
    JS.save("ee44ff55", PAY)
    first = JS.migrate_disk_to_db()
    second = JS.migrate_disk_to_db()
    ok("migration moves then skips", first["moved"] >= 1 and second["moved"] == 0
       and JS.load_db("ee44ff55") is not None and JS.load("ee44ff55") is not None)
    JS.discard("ee44ff55")

print(f"\n{len(failures)} failure(s)" if failures else "\nAll tests passed")
sys.exit(1 if failures else 0)
