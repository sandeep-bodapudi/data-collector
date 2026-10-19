"""Phase 5: email quality ratings (no network - DNS is injected). Run: python tests/test_quality.py (exit 1 on failure)"""
import os
import sys
import tempfile
from datetime import date

TMP = tempfile.mkdtemp()
os.environ["JOB_STORE_DIR"] = os.path.join(TMP, "jobs")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(TMP, "q.db").replace("\\", "/")
os.environ["APP_USER"] = "bossq"
os.environ["APP_PASSWORD"] = "adminpass123"
os.environ["SECRET_KEY"] = "test-secret-key-quality-only"
os.environ["SKIP_STARTUP_PROBES"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scraper import quality as Q  # noqa: E402
from scraper import jobs as J  # noqa: E402

failures = []


def ok(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        failures.append(name)


# ---- helpers ---------------------------------------------------------------------------------------------------------
ok("host_of strips scheme, www and path", Q.host_of("https://www.Allen.in/contact?x=1") == "allen.in" and Q.host_of("allen.in") == "allen.in" and Q.host_of("") == "")
ok("org_domain keeps the organisation part", Q.org_domain("mail.college.ac.in") == "college.ac.in" and Q.org_domain("www.allen.in") == "allen.in"
   and Q.org_domain("a.b.example.com") == "example.com" and Q.org_domain("example.co.uk") == "example.co.uk")
ok("split_emails handles ; , and spaces", Q.split_emails("a@x.com; b@y.com, c@z.com  junk") == ["a@x.com", "b@y.com", "c@z.com"])

# ---- assess ------------------------------------------------------------------------------------------------------------
lv = lambda *a: Q.assess(*a)[0]  # noqa: E731
ok("own domain + mail server = High", lv("principal@college.ac.in", "college.ac.in", "mx") == "High")
ok("a subdomain of the company's domain still matches", lv("info@mail.college.ac.in", "www.college.ac.in", "mx") == "High")
ok("a shared inbox on its own domain is still High, and says so", Q.assess("info@allen.in", "allen.in", "mx") == ("High", "company's own domain, mail server found (shared inbox)"))
ok("free mail is Medium, never High", lv("allen.coaching@gmail.com", "allen.in", "mx") == "Medium" and "free mail" in Q.assess("x@gmail.com", "allen.in", "mx")[1])
ok("a different domain from the website is Medium", lv("a@other.com", "allen.in", "mx") == "Medium" and "different domain" in Q.assess("a@other.com", "allen.in", "mx")[1])
ok("a domain that does not exist is Low", lv("a@nonexistent-domain-xyz.com", "nonexistent-domain-xyz.com", "none") == "Low")
ok("a malformed address is Low", lv("not-an-email", "a.com", "mx") == "Low" and lv("a@b", "a.com", "mx") == "Low")
ok("own domain but no MX record is Medium", lv("a@allen.in", "allen.in", "a") == "Medium")
ok("own domain, lookup failed: Medium and honest about it", Q.assess("a@allen.in", "allen.in", "unknown") == ("Medium", "company's own domain, but mail server not checked"))
ok("without dnspython 'exists' is accepted for High, worded as such", Q.assess("a@allen.in", "allen.in", "exists") == ("High", "company's own domain, domain exists"))
ok("no website to compare with is Medium", lv("a@allen.in", "", "mx") == "Medium")

# ---- annotate ----------------------------------------------------------------------------------------------------------
fake = {"allen.in": "mx", "gmail.com": "mx", "gone.example": "none"}
resolver = lambda d: fake.get(d, "unknown")  # noqa: E731
rows = [
    {"Name": "Allen", "Emails": "info@allen.in; x@gmail.com", "Website": "https://allen.in"},
    {"Name": "Ghost", "Emails": "a@gone.example", "Website": "https://gone.example"},
    {"Name": "NoEmail", "Emails": "", "Website": "https://n.com"},
]
cols = Q.annotate(rows, ["Name", "Emails", "Website"], resolver=resolver, today=date(2026, 10, 18))
ok("two columns are added once", cols == ["Name", "Emails", "Website", "Email Quality", "Last Checked"], cols)
ok("a row with several emails takes its best one", rows[0]["Email Quality"].startswith("High - company's own domain"), rows[0])
ok("a dead domain row is Low", rows[1]["Email Quality"].startswith("Low"))
ok("a row with no email is left alone (no false check date)", "Email Quality" not in rows[2] and "Last Checked" not in rows[2])
ok("the check date is the day it was run", rows[0]["Last Checked"] == "2026-10-18")
ok("re-running does not duplicate columns", Q.annotate(rows, cols, resolver=resolver) == cols)
ok("a sheet with no email column is returned unchanged", Q.annotate([{"Name": "x"}], ["Name"], resolver=resolver) == ["Name"])
ok("'Email' (places) works as well as 'Emails' (web)", "Email Quality" in Q.annotate([{"Email": "a@allen.in", "Website": "allen.in"}], ["Email", "Website"], resolver=resolver))


def boom(d):
    raise RuntimeError("dns exploded")


r2 = [{"Emails": "a@allen.in", "Website": "allen.in"}]
Q.annotate(r2, ["Emails", "Website"], resolver=boom)
ok("a failing lookup never raises; the address is rated as not checked", r2[0]["Email Quality"].startswith("Medium - company's own domain, but mail server not checked"), r2)
ok("with lookups switched off every domain is 'unknown' (no network)", Q.domain_state("allen.in") == "unknown")

# ---- job integration ---------------------------------------------------------------------------------------------------
class FakeJob:
    def __init__(self, spec, rows, columns):
        self.spec, self.rows, self.columns = spec, rows, columns
        self.log, self.activity = [], ""

    def say(self, m):
        self.log.append(m)


j = FakeJob({"mode": "places"}, [{"Name": "A", "Email": "info@allen.in", "Website": "allen.in"}, {"Name": "B", "_paid": True, "Email": "", "Website": ""}],
            ["Name", "Email", "Website"])
J._annotate_quality(j)
ok("places runs gain Data Source, Email Quality, Last Checked", j.columns == ["Name", "Email", "Website", "Data Source", "Email Quality", "Last Checked"], j.columns)
ok("data source says where a row came from", j.rows[0]["Data Source"] == "OpenStreetMap" and j.rows[1]["Data Source"] == "Google Maps (paid)")
ok("the log summarises the check", any("Checked 1 email address" in m for m in j.log), j.log)
jn = FakeJob({"mode": "places", "names_only": True}, [{"Company Name": "A"}], ["Company Name"])
J._annotate_quality(jn)
ok("names-only runs are untouched", jn.columns == ["Company Name"])
js = FakeJob({"mode": "social"}, [{"Name": "A"}], ["Name"])
J._annotate_quality(js)
ok("social runs are untouched", js.columns == ["Name"])
jw = FakeJob({"mode": "web"}, [{"Name": "A", "Emails": "a@x.com", "Website": "x.com"}], ["Name", "Emails", "Website"])
J._annotate_quality(jw)
ok("web runs gain the quality columns but no Data Source", jw.columns[-2:] == ["Email Quality", "Last Checked"] and "Data Source" not in jw.columns)
jb = FakeJob({"mode": "web"}, "not rows", ["Emails"])
jb.rows = [object()]
J._annotate_quality(jb)
ok("a failure inside the check is logged, never raised", any("skipped the email check" in m for m in jb.log), jb.log)

# ---- web rows hold lists when they are rated (real run, 2026-10-18: every web email came out "not a valid address") --------
ok("a list cell is read as its addresses, not as the text of a Python list",
   Q.split_emails(["info@workdyn.in", "x@y.com"]) == ["info@workdyn.in", "x@y.com"] and Q.split_emails("['q@w.in']") == ["q@w.in"])
wl = [{"Emails": ["info@workdyn.in"], "Website": "workdyn.in"}]
Q.annotate(wl, ["Emails", "Website"], resolver=lambda d: "mx")
ok("a web row with a list of emails is rated High, not Low", wl[0]["Email Quality"].startswith("High"), wl[0])

# ---- template placeholders are not contacts (real web run, 2026-10-19) ----------------------------------------------------
from scraper import extract as X  # noqa: E402
html = "<html><body><p>Phone: 123-456-7890</p><p>Mobile: +91 81259 88743</p><p>Email: contact@mysite.com, info@workdyn.in</p></body></html>"
got = X.parse(html, "https://workdyn.in/contact")
ok("a template email (contact@mysite.com) is dropped, the real one kept", got["emails"] == ["info@workdyn.in"], got["emails"])
ok("a template phone (123-456-7890) is dropped, the real one kept",
   all("123" not in p for p in got["phones"]) and any("81259" in p for p in got["phones"]), got["phones"])

print(f"\n{len(failures)} failure(s)" if failures else "\nAll tests passed")
sys.exit(1 if failures else 0)
