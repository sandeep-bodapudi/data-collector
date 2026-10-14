"""preflight: when every enabled engine failed its last probe, try them anyway. Run: python tests/test_preflight_fallback.py"""
import os, sys, tempfile, time
TMP = tempfile.mkdtemp()
os.environ["JOB_STORE_DIR"] = os.path.join(TMP, "jobs")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(TMP, "pf.db").replace("\\", "/")
os.environ["APP_USER"] = "bosspf"
os.environ["APP_PASSWORD"] = "adminpass123"
os.environ["SECRET_KEY"] = "test-secret-key-pf-only"
os.environ["SKIP_STARTUP_PROBES"] = "1"
os.environ.pop("GOOGLE_CSE_KEY", None)
os.environ.pop("BRAVE_API_KEY", None)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as appmod  # noqa: E402
from scraper import jobs as J, connection_health as H  # noqa: E402

fails = []


def ok(n, c):
    print(("PASS " if c else "FAIL ") + n)
    if not c:
        fails.append(n)


with appmod.app.app_context():
    H.HEALTH_CACHE["ddgs"] = {"healthy": False, "checked_at": time.time()}
    pf = J.preflight({"mode": "web"})
    ok("keyless install still searches with ddgs after one failed probe", pf["search"] == ["ddgs"])
    ok("the run says it is trying anyway", any("anyway" in n for n in pf["notes"]))
    H.HEALTH_CACHE["ddgs"] = {"healthy": True, "checked_at": time.time()}
    ok("healthy ddgs is used normally", J.preflight({"mode": "web"})["search"] == ["ddgs"])
print(f"\n{len(fails)} failure(s)" if fails else "\nAll tests passed")
sys.exit(1 if fails else 0)
