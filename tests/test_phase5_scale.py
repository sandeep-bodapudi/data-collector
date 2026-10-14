"""Phase 5E scale tests (proxy caps, healthz, backups). Run: python tests/test_phase5_scale.py (exit 1 on failure)."""
import os
import sys
import tempfile

TMP = tempfile.mkdtemp()
os.environ["JOB_STORE_DIR"] = os.path.join(TMP, "jobs")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(TMP, "t5e.db").replace("\\", "/")
os.environ["APP_USER"] = "boss5e"
os.environ["APP_PASSWORD"] = "adminpass123"
os.environ["SECRET_KEY"] = "test-secret-key-phase5e-only"
os.environ["DDGS_PROXY"] = "http://127.0.0.1:9"
os.environ["HTTPS_PROXY"] = "http://127.0.0.1:9"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scraper import proxy as P  # noqa: E402
import app as appmod  # noqa: E402

app = appmod.app
failures = []


def ok(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        failures.append(name)


with app.app_context():
    from models import AppSetting as _AS
    from models import db as _db

    for k in ("proxy_enabled", "proxy_search", "proxy_fetch", "proxy_cap_mb"):
        _AS.set(k, "0")
    _db.session.commit()

    # ---- defaults: everything direct ----
    ok("proxy off by default", P.proxy_for("search") == "" and P.proxy_for("fetch") == "")
    ok("unknown connector direct", P.proxy_for("socks") == "")

    # ---- enabled without a cap stays off (same posture as social budgets) ----
    for k, v in (("proxy_enabled", "1"), ("proxy_search", "1"), ("proxy_fetch", "1")):
        _AS.set(k, v)
    _db.session.commit()
    ok("no cap means off", P.proxy_for("search") == "" and P.proxy_for("fetch") == "")

    # ---- enabled + cap + URL routes ----
    _AS.set("proxy_cap_mb", "10")
    _db.session.commit()
    ok("search proxied", P.proxy_for("search") == "http://127.0.0.1:9")
    ok("fetch proxied", P.proxy_for("fetch") == "http://127.0.0.1:9")

    # ---- meter accumulates; cap blocks ----
    before = P.used_mb()
    P.account(2 * 1024 * 1024)
    ok("meter accumulates", abs(P.used_mb() - before - 2.0) < 0.01)
    P.account(9 * 1024 * 1024)
    ok("cap blocks", P.proxy_for("search") == "" and P.status()["capped"] is True)
    st = P.status()
    ok("status math", st["cap_mb"] == 10.0 and st["pct"] >= 100)

    # ---- Fetcher routes + meters ----
    from scraper.fetch import Fetcher

    _AS.set("proxy_cap_mb", "100")
    _db.session.commit()
    P._used_mb = 0.0
    f = Fetcher({})
    ok("fetcher uses proxy", f.session.proxies.get("https") == "http://127.0.0.1:9")
    f._robots["http://example.com"] = None

    class _Raw:
        def read(self, amt, decode_content=True):
            return b"<html><body>Hi</body></html>" * 100

    class _Resp:
        status_code = 200
        headers = {"Content-Type": "text/html"}
        encoding = "utf-8"
        raw = _Raw()

    f.session.get = lambda *a, **k: _Resp()
    html, note = f.get_html("http://example.com/")
    ok("fetch works via proxy", html is not None and "Hi" in html and note == "ok")
    ok("fetch bytes metered", P.used_mb() > 0)

    # ---- search passes the proxy to DDGS ----
    from scraper import search as S

    seen = {}

    class _DDGS:
        def __init__(self, **kw):
            seen.update(kw)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def text(self, *a, **k):
            return [{"title": "T", "href": "https://example.com/a", "body": "b"}]

    S.DDGS = _DDGS
    try:
        with S.search_keys(""):
            S.web_search("hello world test query", "wt-wt", 3, lambda m: None)
        ok("ddgs gets the proxy", seen.get("proxy") == "http://127.0.0.1:9")
    finally:
        import ddgs as _real

        S.DDGS = _real.DDGS
    for k in ("proxy_enabled", "proxy_search", "proxy_fetch", "proxy_cap_mb"):
        _AS.set(k, "0")
    _db.session.commit()
    P._used_mb = 0.0

# ---- admin proxy settings ----
c = app.test_client()
ok("admin sign in", c.post("/login", data={"username": "BOSS5E", "password": "adminpass123"}).status_code == 302)
c.post("/api/admin/users", json={"username": "mem5e", "password": "memberpass1", "role": "member"})
mem = app.test_client()
mem.post("/login", data={"username": "mem5e", "password": "memberpass1"})
ok("member cannot save proxy", mem.post("/api/admin/settings", json={"proxy_cap_mb": 5}).status_code == 403)
ok("bad cap rejected", c.post("/api/admin/settings", json={"proxy_cap_mb": "lots"}).status_code == 400)
ok("proxy settings roundtrip",
   c.post("/api/admin/settings", json={"proxy_enabled": True, "proxy_search": True,
                                        "proxy_fetch": False, "proxy_cap_mb": 500}).status_code == 200
   and c.get("/api/admin/settings").get_json()["proxy_cap_mb"] == "500.0")
c.post("/api/admin/settings", json={"proxy_enabled": False, "proxy_search": False,
                                     "proxy_fetch": False, "proxy_cap_mb": 0})

# ---- healthz: public, secret-free ----
anon = app.test_client()
h = anon.get("/healthz")
hj = h.get_json()
ok("healthz public", h.status_code in (200, 503) and hj.get("database") == "ok" and hj.get("store") == "ok")
ok("healthz leaks nothing", set(hj) == {"ok", "database", "store", "redis"}
   and "SECRET" not in str(hj) and "key" not in str(hj).lower())

# ---- backups ----
import backup as _B

os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(TMP, "src.db")
with open(os.path.join(TMP, "src.db"), "wb") as f:
    f.write(b"fake-db-bytes")
os.environ["JOB_STORE_DIR"] = os.path.join(TMP, "srcjobs")
os.makedirs(os.environ["JOB_STORE_DIR"], exist_ok=True)
with open(os.path.join(os.environ["JOB_STORE_DIR"], "aa00bb11.json.gz"), "wb") as f:
    f.write(b"fake-payload")
plan = _B.plan_backup(os.path.join(TMP, "out"))
ok("backup plan describes", plan["url_kind"] == "sqlite" and any(s["kind"] == "sqlite_copy" for s in plan["steps"]))
man = _B.run_backup(os.path.join(TMP, "out"))
ok("backup copies db + jobs", "data.db" in man["copied"] and man["job_files"] == 1
   and os.path.exists(os.path.join(TMP, "out", "manifest.json")))

print(f"\n{len(failures)} failure(s)" if failures else "\nAll tests passed")
sys.exit(1 if failures else 0)
