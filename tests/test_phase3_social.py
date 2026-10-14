"""Phase 3 social tests — mocked, no network, no spend. Run: python tests/test_phase3_social.py (exit 1 on failure)."""
import os
import sys
import tempfile

TMP = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(TMP, "t3.db").replace("\\", "/")
os.environ["JOB_STORE_DIR"] = os.path.join(TMP, "jobs")  # durable-job payloads stay in the throwaway dir
os.environ["APP_USER"] = "boss3"
os.environ["APP_PASSWORD"] = "adminpass123"
os.environ["SECRET_KEY"] = "test-secret-key-phase3-only"
os.environ.setdefault("SKIP_STARTUP_PROBES", "1")  # tests must not depend on live network probes
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scraper.social_base import (  # noqa: E402
    blank_record, estimate_cost, is_filled_enough, merge_records, normalize_url, platform_of,
)
from scraper import jobs as J  # noqa: E402
import scraper.config as cfg  # noqa: E402
import app as appmod  # noqa: E402

app = appmod.app
failures = []


def ok(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        failures.append(name)


# ---- unit ----
ok("platform_of linkedin", platform_of("https://www.linkedin.com/in/abc") == "linkedin.com")
ok("platform_of unknown empty", platform_of("https://example.com/x") == "")
ok("normalize dedupes case/slash", normalize_url("HTTPS://WWW.LinkedIn.com/in/ABC/") == normalize_url("https://linkedin.com/in/abc"))
ok("blank record columns", set(blank_record("N", "u", "linkedin.com")) >= {"Name", "Profile URL", "Platform"})
ok("filled needs headline+company", is_filled_enough({"Headline": "H", "Current Company": "C"}) and not is_filled_enough({"Headline": "H"}))
ok("merge never overwrites", merge_records({"Name": "A", "Headline": ""}, {"Name": "B", "Headline": "H"}) == {"Name": "A", "Headline": "H"})
usd, inr = estimate_cost(1000, ["apify"])
ok("estimate math sane", usd > 0 and inr == round(usd * 96.0, 2))

orig_enabled = cfg.is_enabled
cfg.is_enabled = lambda name, sg=None: True
try:
    from scraper.connection_health import HEALTH_CACHE

    for k in ("social", "brightdata", "apify"):
        HEALTH_CACHE.pop(k, None)

    def run_social(items, hybrid=True, free_rec=None, bd=None, ap=None, keys=None, spend=None):
        keys = keys or {"brightdata_key": "test-key-1234567890", "apify_key": "test-token-1234567890"}
        spec = {"mode": "social", "items": items, "hybrid": hybrid, "platforms": ["linkedin.com"],
                "custom_fields": [], "brave_key": "", "ai": {}, **keys}
        eff = J.preflight(spec)
        job = J.Job(spec, 1)
        job.effective = eff
        J._run_social(job,
                      free_lookup=lambda it: dict(free_rec) if free_rec else {"Name": it.get("name", "N"), "Profile URL": it.get("url", ""), "Platform": "linkedin.com"},
                      bd_fill=bd, ap_fill=ap,
                      spend_tracker=spend if spend is not None else {"spent": (0.0, 0)})
        return job

    calls = {"bd": 0, "ap": 0}
    # free hit skips paid
    job = run_social([{"url": "https://linkedin.com/in/a", "name": "A"}],
                     free_rec={"Name": "A", "Headline": "H", "Current Company": "C", "Profile URL": "https://linkedin.com/in/a", "Platform": "linkedin.com"},
                     bd=lambda u, k: (calls.__setitem__("bd", calls["bd"] + 1), {"Headline": "X"})[1],
                     ap=lambda u, k, a="": (calls.__setitem__("ap", calls["ap"] + 1), {"Headline": "X"})[1])
    ok("free hit skips paid", calls == {"bd": 0, "ap": 0} and len(job.rows) == 1)

    # free miss -> brightdata
    calls = {"bd": 0, "ap": 0}
    spend = {"spent": (0.0, 0)}
    job = run_social([{"url": "https://linkedin.com/in/b", "name": "B"}],
                     free_rec={"Name": "B", "Profile URL": "https://linkedin.com/in/b", "Platform": "linkedin.com"},
                     bd=lambda u, k: (calls.__setitem__("bd", calls["bd"] + 1) or {"Name": "B", "Headline": "H", "Current Company": "C"}),
                     ap=lambda u, k, a="": (calls.__setitem__("ap", calls["ap"] + 1) or {"Headline": "X"}),
                     spend=spend)
    ok("free miss calls brightdata", calls["bd"] == 1 and job.rows[0]["Headline"] == "H")

    # brightdata down -> apify
    calls = {"bd": 0, "ap": 0}

    def boom_bd(u, k):
        calls["bd"] += 1
        raise Exception("down")

    def good_ap(u, k, a=""):
        calls["ap"] += 1
        return {"Name": "C", "Headline": "H", "Current Company": "C"}

    job = run_social([{"url": "https://linkedin.com/in/c", "name": "C"}],
                     free_rec={"Name": "C", "Profile URL": "https://linkedin.com/in/c", "Platform": "linkedin.com"},
                     bd=boom_bd, ap=good_ap)
    ok("brightdata failure falls to apify", calls == {"bd": 1, "ap": 1} and job.rows[0]["Current Company"] == "C")

    # budget at 100% -> no paid calls
    import scraper.jobs as JJ

    orig_lim = JJ._social_limits
    JJ._social_limits = lambda: (1.0, 96.0)
    try:
        calls = {"bd": 0, "ap": 0}
        job = run_social([{"url": "https://linkedin.com/in/d", "name": "D"}],
                         free_rec={"Name": "D", "Profile URL": "https://linkedin.com/in/d", "Platform": "linkedin.com"},
                         bd=lambda u, k: (calls.__setitem__("bd", calls["bd"] + 1) or {}),
                         ap=lambda u, k, a="": (calls.__setitem__("ap", calls["ap"] + 1) or {}),
                         spend={"spent": (1.0, 96.0)})
        ok("budget cap blocks paid", calls == {"bd": 0, "ap": 0} and any("budget" in m for m in job.log))
    finally:
        JJ._social_limits = orig_lim

    # dedupe by URL
    job = run_social([{"url": "https://linkedin.com/in/e/", "name": "E"}, {"url": "https://LINKEDIN.com/in/e", "name": "E2"}],
                     free_rec={"Name": "E", "Headline": "H", "Current Company": "C", "Profile URL": "x", "Platform": "linkedin.com"})
    ok("dedupe by normalized url", len(job.rows) == 1)

    # no budget set + no tracker -> paid stays off with a clear note
    calls = {"bd": 0, "ap": 0}
    spec = {"mode": "social", "items": [{"url": "https://linkedin.com/in/f", "name": "F"}],
            "hybrid": True, "platforms": ["linkedin.com"], "custom_fields": [],
            "brightdata_key": "test-key-1234567890", "apify_key": "", "brave_key": "", "ai": {}}
    eff = J.preflight(spec)
    job = J.Job(spec, 1)
    job.effective = eff
    J._run_social(job,
                  free_lookup=lambda it: {"Name": "F", "Profile URL": "https://linkedin.com/in/f", "Platform": "linkedin.com"},
                  bd_fill=lambda u, k: (calls.__setitem__("bd", calls["bd"] + 1) or {"Headline": "H"}),
                  ap_fill=lambda u, k, a="": (calls.__setitem__("ap", calls["ap"] + 1) or {"Headline": "H"}))
    ok("no budget means paid stays off", calls == {"bd": 0, "ap": 0} and any("no monthly social budget" in m for m in job.log))

    # 80% warning fires when spend crosses the line mid-run
    calls = {"ap": 0}

    def ap_paid(u, k, a=""):
        calls["ap"] += 1
        return {"Name": "G", "Headline": "H", "Current Company": "C"}

    job2 = J.Job({"mode": "social", "items": [{"url": "https://linkedin.com/in/g", "name": "G"}],
                          "hybrid": True, "platforms": ["linkedin.com"], "custom_fields": [],
                          "brightdata_key": "", "apify_key": "test-token-1234567890",
                          "brave_key": "", "ai": {}}, 1)
    eff2 = J.preflight({"mode": "social", "items": job2.spec["items"], "custom_fields": [],
                        "brightdata_key": "", "apify_key": "test-token-1234567890",
                        "brave_key": "", "ai": {}})
    job2.effective = eff2
    J._run_social(job2,
                  free_lookup=lambda it: {"Name": "G", "Profile URL": "https://linkedin.com/in/g", "Platform": "linkedin.com"},
                  ap_fill=ap_paid,
                  spend_tracker={"spent": (0.007, 0.67), "limits": (0.01, 0.96)})
    ok("80% warning fires mid-run", calls["ap"] == 1 and any("80%" in m for m in job2.log))

    # empty provider reply counts no spend
    spend3 = {"spent": (0.0, 0), "limits": (10.0, 960.0)}
    job3 = J.Job({"mode": "social", "items": [{"url": "https://linkedin.com/in/h", "name": "H"}],
                          "hybrid": True, "platforms": ["linkedin.com"], "custom_fields": [],
                          "brightdata_key": "test-key-1234567890", "apify_key": "",
                          "brave_key": "", "ai": {}}, 1)
    job3.effective = J.preflight(job3.spec)
    J._run_social(job3,
                  free_lookup=lambda it: {"Name": "H", "Profile URL": "https://linkedin.com/in/h", "Platform": "linkedin.com"},
                  bd_fill=lambda u, k: {},
                  spend_tracker=spend3)
    ok("empty reply counts no spend", spend3["spent"] == (0.0, 0) and any("not counted as spend" in m for m in job3.log))

    # name-only item with no link skips paid lookup
    calls = {"bd": 0}
    job4 = J.Job({"mode": "social", "items": [{"url": "", "name": "No Link", "platform": ""}],
                          "hybrid": True, "platforms": ["linkedin.com"], "custom_fields": [],
                          "brightdata_key": "test-key-1234567890", "apify_key": "",
                          "brave_key": "", "ai": {}}, 1)
    job4.effective = J.preflight(job4.spec)
    J._run_social(job4,
                  free_lookup=lambda it: {"Name": "No Link", "Profile URL": "", "Platform": ""},
                  bd_fill=lambda u, k: (calls.__setitem__("bd", calls["bd"] + 1) or {"Headline": "H"}),
                  spend_tracker={"spent": (0.0, 0), "limits": (10.0, 960.0)})
    ok("linkless item skips paid", calls["bd"] == 0 and any("no profile link" in m for m in job4.log))
finally:
    cfg.is_enabled = orig_enabled

# social defaults to OFF (fresh DB has no flags, registry default is "0")
with app.app_context():
    from scraper.connection_health import HEALTH_CACHE as _HC

    for k in ("social", "brightdata", "apify"):
        _HC.pop(k, None)
    eff0 = J.preflight({"mode": "social", "items": [{"url": "https://linkedin.com/in/z", "name": "Z"}],
                        "custom_fields": [], "brightdata_key": "test-key-1234567890",
                        "apify_key": "", "brave_key": "", "ai": {}})
    ok("social off by default", eff0.get("social") == [] and any("master disabled" in m for m in eff0["notes"]))

# ---- API ----
c = app.test_client()
ok("admin sign in", c.post("/login", data={"username": "BOSS3", "password": "adminpass123"}).status_code == 302)
c.post("/api/settings", json={"brightdata_api_key": "test-bd-key-1234567890", "apify_api_key": "test-ap-token-1234567890"})
s = c.get("/api/settings").get_json()
ok("social keys masked", s.get("brightdata_key_mask", "").endswith("7890") and "test-bd-key" not in str(s))
ok("social empty rejected", c.post("/api/jobs", json={"mode": "social", "profiles": ""}).status_code == 400)
big = "\n".join(f"https://linkedin.com/in/p{i}" for i in range(60))
r = c.post("/api/jobs", json={"mode": "social", "profiles": big})
ok("social per-run cap enforced", r.status_code == 400 and "capped" in r.get_json().get("error", ""))
r = c.post("/api/jobs", json={"mode": "social", "profiles": "https://linkedin.com/in/anna", "free_only": True})
ok("social free-only accepted", r.status_code == 200 and "id" in r.get_json())
st = c.get("/api/connections/status").get_json()
ok("status gains social entries", "social" in st["services"] and "brightdata" in st["services"] and "apify" in st["services"])
ok("status carries social spend", isinstance(st.get("social_spend"), dict))
ok("status leaks no keys", "test-bd-key" not in str(st) and "test-ap-token" not in str(st))
e = c.post("/api/social/estimate", json={"profiles": "https://linkedin.com/in/a\nhttps://linkedin.com/in/b"}).get_json()
ok("estimate endpoint answers", "estimate_usd" in e and e.get("items") == 2 and "providers" in e)
ok("estimate empty rejected", c.post("/api/social/estimate", json={"profiles": ""}).status_code == 400)

# paid blocked until an admin sets a budget (social enabled, keys present, no budget)
with app.app_context():
    from models import AppSetting as _AS2, db as _db2

    for k, v in (("conn_social", "1"), ("conn_brightdata", "1"), ("conn_apify", "0"),
                 ("social_budget_inr", "0"), ("social_budget_usd", "0")):
        _AS2.set(k, v)
    _db2.session.commit()
r = c.post("/api/jobs", json={"mode": "social", "profiles": "https://linkedin.com/in/anna"})
ok("paid blocked with no budget", r.status_code == 402 and "budget" in r.get_json().get("error", ""))
with app.app_context():
    _AS2.set("social_budget_inr", "3000")
    _db2.session.commit()
r = c.post("/api/jobs", json={"mode": "social", "profiles": "https://linkedin.com/in/anna"})
ok("paid accepted once budgeted", r.status_code == 200 and "id" in r.get_json())

# worker threads run with an app context (spend persists, toggles apply)
import time as _t

J._run_web = lambda job: (setattr(job, "columns", ["Name"]), job.rows.append({"Name": "Ctx"}))
job = J.start_job({"mode": "web", "queries": ["x"], "max_results": 1, "custom_fields": [],
                   "fields": [], "platforms": ["web"], "ai": {}}, 1)
for _ in range(100):
    if J.JOBS[job.id].status not in ("queued", "running"):
        break
    _t.sleep(0.05)
ok("threaded job finishes with context", J.JOBS[job.id].status == "done" and len(J.JOBS[job.id].rows) == 1)

print(f"\n{len(failures)} failure(s)" if failures else "\nAll tests passed")
sys.exit(1 if failures else 0)
