"""Phase 5D org-keys + audit tests. Run: python tests/test_phase5_org_audit.py (exit 1 on failure)."""
import os
import sys
import tempfile

TMP = tempfile.mkdtemp()
os.environ["JOB_STORE_DIR"] = os.path.join(TMP, "jobs")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(TMP, "t5d.db").replace("\\", "/")
os.environ["APP_USER"] = "boss5d"
os.environ["APP_PASSWORD"] = "adminpass123"
os.environ["SECRET_KEY"] = "test-secret-key-phase5d-only"
os.environ.setdefault("SKIP_STARTUP_PROBES", "1")  # tests must not depend on live network probes
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as appmod  # noqa: E402
from models import log_audit  # noqa: E402

app = appmod.app
failures = []


def ok(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        failures.append(name)


c = app.test_client()
ok("admin sign in", c.post("/login", data={"username": "BOSS5D", "password": "adminpass123"}).status_code == 302)
c.post("/api/admin/users", json={"username": "mem5d", "password": "memberpass1", "role": "member"})
c.post("/api/admin/users", json={"username": "vw5d", "password": "viewerpass1", "role": "viewer"})
mem = app.test_client()
mem.post("/login", data={"username": "mem5d", "password": "memberpass1"})

# ---- admin API guards ----
ok("member cannot list org keys", mem.get("/api/admin/org-keys").status_code == 403)
ok("member cannot list audit", mem.get("/api/admin/audit").status_code == 403)
ok("only shareable names", c.post("/api/admin/org-keys",
                                  json={"name": "li_at_cookie", "value": "x" * 20}).status_code == 400)
ok("roles required", c.post("/api/admin/org-keys",
                            json={"name": "brave_api_key", "value": "x" * 20, "roles": []}).status_code == 400)
ok("unknown org key 404", c.delete("/api/admin/org-keys/nope").status_code == 404)

# ---- fallback: member without a personal key uses the org key ----
r = c.post("/api/admin/org-keys", json={"name": "brave_api_key", "value": "BSA-ORG-KEY-1234567890"})
ok("org key saved", r.status_code == 200 and r.get_json()["key"]["masked"].endswith("7890"))
lst = c.get("/api/admin/org-keys").get_json()
ok("org values never listed", "BSA-ORG-KEY" not in str(lst) and lst["keys"][0]["name"] == "brave_api_key")
mine = mem.get("/api/settings").get_json()
ok("member sees the name only", "brave_api_key" in mine.get("org_keys", []) and "BSA-ORG-KEY" not in str(mine))

from scraper import jobs as J

with app.app_context():
    from models import User as _U

    mem_id = _U.query.filter_by(username="mem5d").first().id
    vault = appmod._effective_vault(mem_id, "member")
    ok("org key fills the gap", vault.get("brave_api_key") == "BSA-ORG-KEY-1234567890")
    spec, err, _code = appmod.build_job_spec(
        {"mode": "web", "queries": "x", "fields": ["emails"]}, vault, mem_id)
    ok("spec built on org key", err is None and spec["brave_key"] == "BSA-ORG-KEY-1234567890")

# ---- personal wins ----
with app.app_context():
    from models import VaultCredential as _VC
    from models import db as _db
    from models import encrypt as _enc

    _db.session.add(_VC(owner_id=mem_id, name="brave_api_key", platform="search",
                        credential_type="api_key", encrypted_value=_enc("BSA-MINE-KEY-0987654321")))
    _db.session.commit()
    vault2 = appmod._effective_vault(mem_id, "member")
    ok("personal key wins", vault2.get("brave_api_key") == "BSA-MINE-KEY-0987654321")

# ---- role gate: viewer-only key is invisible to members ----
c.post("/api/admin/org-keys", json={"name": "ai_api_key", "value": "sk-ORG-VIEWER-ONLY-12345", "roles": ["viewer"]})
with app.app_context():
    vault3 = appmod._effective_vault(mem_id, "member")
    ok("role gate holds", vault3.get("ai_api_key", "") == "")
    vault4 = appmod._effective_vault(mem_id, "viewer")
    ok("viewer role sees it", vault4.get("ai_api_key") == "sk-ORG-VIEWER-ONLY-12345")

# ---- audit trail ----
with app.app_context():
    from models import AuditLog as _AL

    n0 = _AL.query.count()
    log_audit(None, "test.ping", "hello")
with app.app_context():
    from models import AuditLog as _AL2

    ok("audit writes", _AL2.query.count() == n0 + 1)
    ok("audit never raises", (log_audit(None, None, None), True)[1])
jr = mem.post("/api/jobs", json={"mode": "web", "queries": "quiet test", "fields": ["emails"]})
ok("job create audited", jr.status_code == 200)
with app.app_context():
    from models import AuditLog as _AL3

    last = _AL3.query.order_by(_AL3.id.desc()).first()
    ok("audit names the actor", last.username == "mem5d" and last.action == "job.started")
sh = mem.post("/api/shares", json={"name": "s", "columns": ["Name"], "rows": [{"Name": "A"}],
                                   "link_access": True, "expires_days": 1})
with app.app_context():
    from models import AuditLog as _AL4

    acts = [a.action for a in _AL4.query.order_by(_AL4.id.desc()).limit(5).all()]
    ok("share create audited", "share.created" in acts)
mem.delete(f"/api/shares/{sh.get_json()['id']}")
with app.app_context():
    from models import AuditLog as _AL5

    acts2 = [a.action for a in _AL5.query.order_by(_AL5.id.desc()).limit(5).all()]
    ok("share stop audited", "share.stopped" in acts2)
aud = c.get("/api/admin/audit?limit=5").get_json()
ok("admin audit reads newest-first", isinstance(aud, list) and len(aud) > 0
   and aud[0]["id"] >= aud[-1]["id"] and "user" in aud[0])
ok("audit limit capped", len(c.get("/api/admin/audit?limit=9999").get_json()) <= 500)

# ---- delete reverts to personal/empty ----
c.delete("/api/admin/org-keys/ai_api_key")
c.delete("/api/admin/org-keys/brave_api_key")
with app.app_context():
    from models import VaultCredential as _VC2
    from models import db as _db2

    _db2.session.delete(_VC2.query.filter_by(owner_id=mem_id, name="brave_api_key").first())
    _db2.session.commit()
    ok("delete reverts cleanly", appmod._effective_vault(mem_id, "member").get("brave_api_key", "") == "")

print(f"\n{len(failures)} failure(s)" if failures else "\nAll tests passed")
sys.exit(1 if failures else 0)
