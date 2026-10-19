"""Business-profile sheet + the single website reader with its browser fallback. No network (DNS and browser mocked).
Run: python tests/test_profile.py   (exit 1 on failure)"""
import os
import sys
import tempfile
import threading
from datetime import date

TMP = tempfile.mkdtemp()
os.environ["JOB_STORE_DIR"] = os.path.join(TMP, "jobs")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(TMP, "pr.db").replace("\\", "/")
os.environ["SECRET_KEY"] = "test-secret-key-profile-only"
os.environ["SKIP_STARTUP_PROBES"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scraper import browser as B, jobs as J, profile as P  # noqa: E402

failures = []


def ok(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{str(detail)[:400]}]" if detail and not cond else ""))
    if not cond:
        failures.append(name)


MX = {"nareshit.com": "mx", "gmail.com": "mx", "visionias.in": "mx", "allen.in": "mx", "gone.example": "none"}
resolver = lambda d: MX.get(d, "unknown")  # noqa: E731
TODAY = date(2026, 10, 19)

# ---- does the website belong to this business? ---------------------------------------------------------------------
for name, host, want in [("Naresh IT", "nareshit.com", True), ("Vision IAS Coaching Center", "visionias.in", True),
                         ("Gokaraju Rangaraju Institute of Engineering and Technology", "griet.ac.in", True),
                         ("Mahendras coaching center", "mahendras.org", True), ("Allen Career Institute", "allen.ac.in", True),
                         ("A.V College of Arts, Science and Commerce", "arts.com", False),
                         ("Top Coaching Center in Karimnagar", "aakash.ac.in", False),
                         ("Sri Chaitanya Academy", "digitalclassworld.com", False)]:
    ok(f"name/site match: {name} ~ {host} is {want}", P.name_matches_site(name, host) is want)

c = P.confidence("Naresh IT", "https://nareshit.com", "info@nareshit.com", False, True)
ok("own site + own-domain email = High, with reasons", c.startswith("High") and "website matches the name" in c and "own domain" in c, c)
c = P.confidence("A.V College", "https://arts.com", "x@arts.com", False, True)
ok("a website that does not match the name is never High", not c.startswith("High") and "differs" in c, c)
ok("on the map without a website = Medium", P.confidence("Temple", "", "", True, False).startswith("Medium"))
ok("nothing to back a web row up = Low", P.confidence("Some Page", "https://random.example", "", False, False).startswith("Low"))

# ---- web rows -> profile sheet ------------------------------------------------------------------------------------------
web_rows = [
    {"Name": "Naresh IT", "_query": "software training institute in Ameerpet, Hyderabad",
     "Emails": ["naresh.it.hyd@gmail.com", "info@nareshit.com"], "Phone Numbers": ["040 2374 6666", "+91 81796 19999"],
     "Address": "2nd Floor, Durga Bhavani Plaza, Ameerpet, Hyderabad 500016", "Website": "nareshit.com", "Description": "",
     "Social Media Links": ["https://facebook.com/nareshit"]},
    {"Name": "Naresh IT Training", "_query": "software training institute in Ameerpet, Hyderabad",
     "Emails": [], "Phone Numbers": ["+91 81796 19999"], "Address": "", "Website": "www.nareshit.com"},
    {"Name": "Ghost Academy", "_query": "software training institute in Ameerpet, Hyderabad", "Emails": ["a@gone.example"],
     "Phone Numbers": [], "Address": "", "Website": "randomblog.example"},
]
rows, cols = P.build(web_rows, ["Name", "Emails", "Phone Numbers", "Address", "Description", "Social Media Links", "Website"],
                     {"mode": "web", "queries": ["software training institute in Ameerpet, Hyderabad"], "custom_fields": []},
                     resolver=resolver, today=TODAY)
ok("one clear column set, core first", cols[:3] == ["Business Name", "Category", "Phone"] and cols[-3:] == ["Match Confidence", "Source", "Last Checked"], cols)
ok("an empty optional column is dropped (Description)", "Description" not in cols, cols)
ok("the same website found twice becomes one row", len(rows) == 2, [r["Business Name"] for r in rows])
n = rows[0]
ok("strongest row first", n["Business Name"] == "Naresh IT" and n["Match Confidence"].startswith("High"), n)
ok("primary email is the company's own address, the free-mail one moves to Other Emails",
   n["Email"] == "info@nareshit.com" and n["Other Emails"] == "naresh.it.hyd@gmail.com", n)
ok("primary phone is the mobile, the landline goes to Other Phones", n["Phone"].startswith("+91 81796") and "040" in n["Other Phones"], n)
ok("Category and Area come from the search", n["Category"] == "software training institute" and n["Area"] == "Ameerpet, Hyderabad", n)
ok("Pincode is read from the address", n["Pincode"] == "500016", n)
ok("Website is a full link", n["Website"] == "https://nareshit.com", n["Website"])
ok("Social links are kept", "facebook.com/nareshit" in n["Social Links"])
ok("Source says web search + company website", n["Source"] == "Web search + company website", n["Source"])
g = rows[1]
ok("a row whose website does not match and whose email domain is dead is Low and last",
   g["Business Name"] == "Ghost Academy" and g["Match Confidence"].startswith("Low") and g["Email Quality"].startswith("Low"), g)
ok("every row is dated", all(r["Last Checked"] == "2026-10-19" for r in rows))

# ---- places rows -> profile sheet ---------------------------------------------------------------------------------------
place_rows = [{"Name": "Sri Venkateswara Swamy Temple", "Category": "Hindu temples", "Address": "Road No 2", "City": "Hyderabad",
               "State": "Telangana", "Postcode": "500034", "Phone": "", "Email": "", "Website": "", "Opening Hours": "06:00-20:00",
               "Google Maps Link": "https://www.google.com/maps?q=17.4,78.4", "Search Location": "Banjara Hills, Hyderabad",
               "Data Source": "OpenStreetMap"}]
rows, cols = P.build(place_rows, list(place_rows[0]), {"mode": "places", "category": "Hindu temples", "custom_fields": []},
                     resolver=resolver, today=TODAY)
t = rows[0]
ok("places keep map details: City, State, Pincode, Opening Hours, Maps link", all(c in cols for c in ("City", "State", "Pincode", "Opening Hours", "Google Maps Link")), cols)
ok("a mapped place with no website is Medium, not Low", t["Match Confidence"].startswith("Medium"), t["Match Confidence"])
ok("Area is the searched place", t["Area"] == "Banjara Hills, Hyderabad")
ok("custom AI columns stay at the end",
   P.build([{"Name": "X", "Website": "x.com", "Emails": [], "Main deity": "Venkateswara"}], ["Name", "Website", "Emails", "Main deity"],
           {"mode": "web", "queries": ["temples in Pune"], "custom_fields": ["Main deity"]}, resolver=resolver)[1][-1] == "Main deity")

# ---- the one website reader + browser fallback ----------------------------------------------------------------------------
class FakeFetcher:
    def __init__(self, pages, allow=True):
        self.pages, self.allow, self.asked = pages, allow, []

    def get_html(self, url):
        self.asked.append(url)
        v = self.pages.get(url)
        if v is None or v.startswith("HTTP "):
            return None, v or "HTTP 404"
        return v, "ok"

    def allowed(self, url):
        return self.allow


class FJ:
    def __init__(self):
        self.lock, self.browser_used, self.browser_found, self.log = threading.Lock(), 0, 0, []
        self.cancelled = type("E", (), {"is_set": staticmethod(lambda: False)})

    def say(self, m):
        self.log.append(m)


HOME = "<html><head><title>Naresh IT</title></head><body><p>Call: +91 81796 19999</p></body></html>"
RENDERED = "<html><body><p>Email: info@nareshit.com</p></body></html>"
real_enabled, real_read = B.enabled, B.read_site
calls = []
B.read_site = lambda url, allowed=None: calls.append(url) or [(RENDERED, url)]

B.enabled = lambda: False
page, note = J._read_site(FJ(), FakeFetcher({"https://nareshit.com": HOME}), "https://nareshit.com")
ok("browser off: plain reader only", page["emails"] == [] and calls == [])

B.enabled = lambda: True
job = FJ()
page, note = J._read_site(job, FakeFetcher({"https://nareshit.com": HOME}), "https://nareshit.com")
ok("no email from the plain reader -> the browser reads the site and finds it", page["emails"] == ["info@nareshit.com"] and calls == ["https://nareshit.com"], page["emails"])
ok("the run counts what the browser recovered", job.browser_used == 1 and job.browser_found == 1)
ok("phones from the plain reader are kept", any("81796" in p for p in page["phones"]))

calls.clear()
page, note = J._read_site(FJ(), FakeFetcher({"https://x.com": "<p>mail: hi@x.com</p>"}), "https://x.com")
ok("an email found the plain way -> no browser", calls == [] and page["emails"] == ["hi@x.com"])

calls.clear()
page, note = J._read_site(FJ(), FakeFetcher({"https://blocked.com": "HTTP 403"}), "https://blocked.com")
ok("a site that refuses the plain reader (403) is tried in the browser", calls == ["https://blocked.com"] and page is not None, (calls, note))

calls.clear()
page, note = J._read_site(FJ(), FakeFetcher({"https://gone.com": "HTTP 404"}), "https://gone.com")
ok("a missing page (404) is not retried in a browser", calls == [] and page is None)

calls.clear()
J._read_site(FJ(), FakeFetcher({"https://nareshit.com": HOME}, allow=False), "https://nareshit.com")
ok("robots.txt says no -> no browser either", calls == [])

os.environ["BROWSER_MAX_SITES"] = "1"
calls.clear()
capped = FJ()
for u in ("https://a.com", "https://b.com"):
    J._read_site(capped, FakeFetcher({u: HOME}), u)
ok("BROWSER_MAX_SITES caps browser use per run", calls == ["https://a.com"] and capped.browser_used == 1, calls)
os.environ.pop("BROWSER_MAX_SITES")


def boom(url, allowed=None):
    raise TimeoutError("slow site")


B.read_site = boom
fj = FJ()
page, note = J._read_site(fj, FakeFetcher({"https://nareshit.com": HOME}), "https://nareshit.com")
ok("a browser failure keeps the plain result and logs it", page is not None and any("browser could not read" in m for m in fj.log), fj.log)
B.enabled, B.read_site = real_enabled, real_read

os.environ.pop("BROWSER_FALLBACK", None)
ok("the fallback is off unless BROWSER_FALLBACK=1", B.enabled() is False)

# ---- names and brands seen on a real run (2026-10-19) -------------------------------------------------------------------
ok("a page title is not a business name", P.clean_business_name("Digital Marketing Landing Page -", "digitalgurusacademy.co.in") == "Digitalgurusacademy")
ok("a real name only loses trailing punctuation", P.clean_business_name("APEC IT Training |", "apectraining.com") == "APEC IT Training")
from scraper import quality as Q  # noqa: E402
ok("skillfloor.in mail on skillfloor.com is the company's own domain", Q.same_org("skillfloor.in", "skillfloor.com")
   and Q.assess("support@skillfloor.in", "skillfloor.com", "mx")[0] == "High")
ok("different brands on the same TLD are not the same org", not Q.same_org("abc.com", "xyz.com"))

print(f"\n{len(failures)} failure(s)" if failures else "\nAll tests passed")
sys.exit(1 if failures else 0)
