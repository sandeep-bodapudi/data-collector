"""Place search tests. Run:  python tests/test_places.py   (no network needed; exit code 1 if anything fails)."""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scraper import discover, fetch, jobs, places  # noqa: E402

failures = []


def ok(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        failures.append(name)


places.time.sleep = lambda s: None
discover.time.sleep = lambda s: None
jobs.time.sleep = lambda s: None

# ---- what counts as an area ----------------------------------------------------------------------
poi = {"class": "office", "type": "educational_institution", "osm_type": "node", "osm_id": 1, "lat": "17.5366", "lon": "78.3644",
       "boundingbox": ["17.536598", "17.536698", "78.3644236", "78.3645236"], "display_name": "akashra junior college, bachupally"}
mandal = {"class": "boundary", "type": "administrative", "osm_type": "relation", "osm_id": 9821936, "lat": "17.53", "lon": "78.37",
          "boundingbox": ["17.5078", "17.5537", "78.3479", "78.3990"], "display_name": "Bachupally mandal, Medchal-Malkajgiri, Telangana, India"}
suburb = {"class": "place", "type": "suburb", "osm_type": "node", "osm_id": 5, "lat": "17.49", "lon": "78.39",
          "boundingbox": ["17.4899", "17.4901", "78.3899", "78.3901"], "display_name": "Kukatpally, Hyderabad"}
city = {"class": "place", "type": "city", "osm_type": "relation", "osm_id": 7, "lat": "17.38", "lon": "78.48",
        "boundingbox": ["17.2", "17.6", "78.2", "78.7"], "display_name": "Hyderabad, Telangana"}
ok("a single business is not an area", places._as_area(poi) is None)
ok("an administrative boundary is an area", places._as_area(mandal) == {"kind": "area", "osm_type": "relation", "osm_id": 9821936})
a = places._as_area(suburb)
ok("a suburb that is only a point gets a search radius", a["kind"] == "around" and a["radius"] == 2500)
ok("a big place uses its bounding box", places._as_area(city)["kind"] == "box")

# ---- the Bachupally case: the full text matches only a business, the first word matches the area ----
calls = []
def fake_nominatim(q, limit=8):
    calls.append(q)
    return {"Bachupally, Hyderabad": [poi], "Bachupally": [mandal]}.get(q, [])
places._nominatim = fake_nominatim
g = places.geocode("Bachupally, Hyderabad")
ok("'Bachupally, Hyderabad' resolves to the whole area, not the business", g["kind"] == "area" and g["osm_id"] == 9821936, str(g))
ok("it records that it matched from the first word", g["fallback"] is True)
ok("the description shows the matched area and how", "Bachupally mandal" in places.describe_location(g, "Bachupally, Hyderabad")
   and "matched from" in places.describe_location(g, "Bachupally, Hyderabad"))
ok("both attempts were made in order", calls == ["Bachupally, Hyderabad", "Bachupally"])

places._nominatim = lambda q, limit=8: [mandal]
g = places.geocode("Bachupally")
ok("an exact area name needs no fallback", g["kind"] == "area" and g["fallback"] is False)

places._nominatim = lambda q, limit=8: [poi]
g = places.geocode("Some Shop, Hyderabad")
ok("only a business found: search around it and say so", g["kind"] == "around" and g["radius"] == 3000 and g["approx"] is True)
ok("the approximate case is described honestly", "Couldn't find an area" in places.describe_location(g, "Some Shop, Hyderabad"))

places._nominatim = lambda q, limit=8: []
places._geocode_photon = lambda q: {"kind": "box", "bbox": (1, 2, 3, 4), "label": "Backup Town"}
ok("the backup geocoder is used when the main one knows nothing", places.geocode("Nowhere")["label"] == "Backup Town")
places._geocode_photon = lambda q: None
try:
    places.geocode("Nowhere")
    ok("an unknown place raises a helpful error", False)
except places.PlaceError as e:
    ok("an unknown place raises a helpful error", "Location not found" in str(e) and "Bachupally, Hyderabad, India" in str(e))

# ---- the map query ---------------------------------------------------------------------------------
ok("area query", places._area_clause({"kind": "area", "osm_type": "relation", "osm_id": 9821936}) == ("area(3609821936)->.a;", "(area.a)"))
ok("way area query", places._area_clause({"kind": "area", "osm_type": "way", "osm_id": 5})[0] == "area(2400000005)->.a;")
ok("box query", places._area_clause({"kind": "box", "bbox": (1.0, 2.0, 3.0, 4.0)}) == ("", "(1.0,2.0,3.0,4.0)"))
ok("radius query", places._area_clause({"kind": "around", "radius": 2500, "lat": 17.5, "lon": 78.4}) == ("", "(around:2500,17.5,78.4)"))

sent = {}
def fake_overpass(query, notify=None):
    sent["q"] = query
    return [{"type": "way", "lat": None, "center": {"lat": 17.5399, "lon": 78.3860}, "tags": {"name": "vnr vjiet college", "amenity": "university"}}]
places._overpass = fake_overpass
places.geocode = lambda loc: {"kind": "area", "osm_type": "relation", "osm_id": 9821936, "label": "Bachupally mandal", "fallback": True}
log = []
rows = places.search_places("Engineering colleges", "Bachupally, Hyderabad", "", 100, info=log.append)
ok("the engineering category searches by name inside the area", "area(3609821936)" in sent["q"] and "engineer|tech" in sent["q"] and "vjiet" in sent["q"])
ok("the search logs where it looked", log and log[0].startswith("Searching inside: Bachupally mandal") and len(rows) == 1)
places._overpass = lambda q, notify=None: []
log.clear()
none = places.search_places("Engineering colleges", "Bachupally, Hyderabad", "", 100, info=log.append)
ok("zero results come with advice", none == [] and any("Nothing in the map data" in m and "Web search" in m for m in log))

# ---- finding a website ------------------------------------------------------------------------------
H = lambda url, title="": {"href": url, "title": title, "body": ""}
ok("distinctive words ignore 'college', 'engineering', 'women'", discover.tokens("BVRIT Hyderabad College of Engineering for Women") == ["bvrit"])
ok("directories are skipped", discover.pick_official("GRIET College", [H("https://www.shiksha.com/college/griet"), H("https://www.collegedunia.com/griet")]) is None)
ok("the official site is picked over directories", discover.pick_official("GRIET College", [H("https://www.shiksha.com/griet"), H("https://www.griet.ac.in/about")]) == "https://griet.ac.in/")
ok("a department subdomain becomes the main site", discover.pick_official("GRIET college", [H("http://www.csbs.griet.ac.in/")]) == "http://griet.ac.in/")
ok("social pages are never the official site", discover.pick_official("Narayana", [H("https://www.facebook.com/narayana"), H("https://narayanagroup.com/")]) == "https://narayanagroup.com/")
ok("an unrelated site is not picked", discover.pick_official("GRIET College", [H("https://www.randomtravel.com/hotels")]) is None)
ok("a name with no distinctive word finds nothing", discover.pick_official("Junior College", [H("https://junior.example.com/")]) is None)
ok("PDFs are not websites", discover.pick_official("GRIET", [H("https://griet.ac.in/brochure.pdf")]) is None)
ok("two-part endings are handled", discover.root_domain("x.y.example.co.uk") == "example.co.uk" and discover.root_domain("a.griet.ac.in") == "griet.ac.in")

# ---- the same place listed twice -----------------------------------------------------------------------
def row(name, lat, lon, site="", phone=""):
    return {"Name": name, "Latitude": lat, "Longitude": lon, "Website": site, "Phone": phone, "Email": "", "Address": "", "Postcode": "",
            "Opening Hours": "", "Other Details": "amenity=college"}
ok("an abbreviation is recognised (VJIET = Vignana Jyothi Institute of Engineering and Technology)",
   discover.same_place_by_name("vnr vjiet college", "VNR Vignana Jyothi Institute of Engineering and Technology") == "similar")
ok("a short form is recognised", discover.same_place_by_name("BVRIT College", "BVRIT Hyderabad College of Engineering for Women") == "similar")
ok("identical names are recognised", discover.same_place_by_name("griet college", "GRIET COLLEGE") == "same")
ok("different institutions are not confused", discover.same_place_by_name("GRIET College", "VNR VJIET College") is None)
ok("GRIET is the abbreviation of its full name", discover.same_place_by_name("griet college", "Gokaraju Rangaraju Institute of Engineering and Technology") == "similar")

data = [row("vnr vjiet college", 17.5399, 78.3860, "https://vnrvjiet.ac.in/"), row("VNR Vignana Jyothi Institute of Engineering and Technology", 17.5390, 78.3855),
        row("griet college", 17.5202, 78.3660), row("GRIET COLLEGE", 17.5209, 78.3666),
        row("BVRIT College", 17.5263, 78.3703), row("BVRIT Hyderabad College of Engineering for Women", 17.5264, 78.3699)]
merged_rows, n = discover.merge_duplicates(data)
ok("six listings of three institutions become three", len(merged_rows) == 3 and n == 3, str([r["Name"] for r in merged_rows]))
vnr = next(r for r in merged_rows if "VNR" in r["Name"].upper())
ok("the merged row keeps the longest name and the website only the duplicate had", vnr["Name"].startswith("VNR Vignana") and vnr["Website"] == "https://vnrvjiet.ac.in/")
ok("the other name is noted", "Also mapped as: vnr vjiet college" in vnr["Other Details"])
ok("no trailing separator when there's nothing else to add", not vnr["Other Details"].rstrip().endswith(";"))
branches = [row("Sri Chaitanya College", 17.52, 78.36, "https://sri.example.com/"), row("Sri Chaitanya College", 17.60, 78.45, "https://sri.example.com/")]
ok("branches of a chain far apart stay separate", len(discover.merge_duplicates(branches)[0]) == 2)
near_other = [row("GRIET College", 17.5202, 78.3660), row("VNR VJIET College", 17.5203, 78.3661)]
ok("different institutions close together stay separate", len(discover.merge_duplicates(near_other)[0]) == 2)
site_twins = [row("Alpha Institute", 17.5, 78.4, "https://www.alpha.edu/"), row("Alpha Campus Block B", 17.5001, 78.4001, "https://alpha.edu/")]
ok("listings sharing a website and close together merge", len(discover.merge_duplicates(site_twins)[0]) == 1)

# ---- one lookup, several tries ---------------------------------------------------------------------------
import importlib
real_discover = importlib.reload(discover)  # undo the stand-ins used for the other tests so the real function is tested
real_discover.time.sleep = lambda s: None
queries = []
engines_used = []
def fake_search(q, region, n, say, engines=None):
    engines_used.append(engines)
    queries.append(q)
    table = {"VNR Vignana Jyothi Institute of Engineering and Technology Bachupally official website": [H("https://www.shiksha.com/x")],
             "vnr vjiet college official website": [H("https://www.vnrvjiet.ac.in/contact", "Contact VNRVJIET")]}
    return table.get(q, [H("https://www.randomtravel.com/hotels")])  # unrelated results: the search worked, nothing official in it
real_discover.web_search = fake_search
url, n = real_discover.find_website("VNR Vignana Jyothi Institute of Engineering and Technology", "Bachupally, Hyderabad", aliases=["vnr vjiet college"])
ok("if the first search has no official site it tries the other names", url == "https://vnrvjiet.ac.in/" and n == 1 and len(queries) == 2, str((url, n, queries)))
queries.clear()
url, n = real_discover.find_website("Totally Unknown Place", "Bachupally, Hyderabad")
ok("a place with no official site stops after its tries", url is None and len(queries) == 2, str(queries))
queries.clear(); real_discover.web_search = lambda q, r, n, s, e=None: (queries.append(q) or [])
ok("zero results on the first search is reported for the caller to handle", real_discover.find_website("X Academy", "Y, Z") == (None, 0) and len(queries) == 1)
ok("the last try uses a different set of search engines", engines_used[0] is None and engines_used[-1] == real_discover.OTHER_ENGINES, str(engines_used))
discover = jobs.discover = real_discover

# ---- looking websites up, politely ----------------------------------------------------------------------
class Job:
    def __init__(self):
        self.cancelled = type("E", (), {"is_set": staticmethod(lambda: False)})
        self.log, self.phase, self.total, self.done, self.activity = [], "", 0, 0, ""
    def say(self, m): self.log.append(m)

def places_rows(*names):
    return [{"Name": n, "Website": "", "Search Location": "Bachupally, Hyderabad"} for n in names]

lookups = []
def fake_find(name, where, say=None, aliases=()):
    lookups.append(name)
    return ("https://" + name.split()[0].lower() + ".ac.in/", 5)
discover.find_website = fake_find
j, rws = Job(), places_rows("griet college", "BVRIT College", "griet college")
jobs._discover_websites(j, rws)
ok("websites found by search are filled in and labelled", rws[0]["Website"] == "https://griet.ac.in/" and rws[0]["Website Source"] == "Found by web search")
ok("the same name is only searched once", lookups == ["griet college", "BVRIT College"] and rws[2]["Website"] == rws[0]["Website"])

outcomes = iter([(None, 0), (None, 0)])
discover.find_website = lambda n, w, say=None, aliases=(): next(outcomes)
j, rws = Job(), places_rows("A College", "B College")
jobs._discover_websites(j, rws)
ok("when searches keep coming back empty it waits once, then stops and says why",
   any("waiting" in m for m in j.log) and any("still limiting" in m for m in j.log) and not rws[1].get("Website"))

outcomes = iter([(None, 0), ("https://a.ac.in/", 4), (None, 3)])
discover.find_website = lambda n, w, say=None, aliases=(): next(outcomes)
j, rws = Job(), places_rows("A College", "B College")
jobs._discover_websites(j, rws)
ok("a throttled lookup is retried and then succeeds", rws[0].get("Website") == "https://a.ac.in/" and "no official website found" in " ".join(j.log))

def blocked(n, w, say=None, aliases=()):
    raise discover.SearchBlocked("blocked")
discover.find_website = blocked
j = Job(); jobs._discover_websites(j, places_rows("A College"))
ok("blocked search stops cleanly with advice", any("refused" in m and "Brave" in m for m in j.log))

discover.find_website = lambda n, w, say=None, aliases=(): (None, 3)
jobs.MAX_DISCOVER = 2
j = Job(); jobs._discover_websites(j, places_rows("A College", "B College", "C College"))
ok("only a limited number of places are looked up per run", any("first 2 of 3" in m for m in j.log))

# ---- unreadable pages (the Brotli bug) ----------------------------------------------------------------------
ok("the app no longer asks for Brotli it can't unpack", "br" not in fetch._SESSION_HEADERS["Accept-Encoding"].replace("deflate", "").split(","))

class Raw:
    def __init__(self, b): self.b = b
    def read(self, n, decode_content=True): return self.b
class Resp:
    def __init__(self, b, ctype="text/html; charset=utf-8"): self.status_code, self.headers, self.raw, self.encoding = 200, {"Content-Type": ctype}, Raw(b), "utf-8"
f = fetch.Fetcher({})
f.allowed = lambda u: True
f._wait_turn = lambda u: None
f.session.get = lambda *a, **k: Resp(b"[kU5\x0bI;\xaf\x87GQQ\xfba" * 200)
ok("compressed garbage is reported, not treated as an empty page", f.get_html("https://x.example/") == (None, "unreadable page"))
f.session.get = lambda *a, **k: Resp(b"<html><title>Hi</title><body>info@x.com</body></html>")
html, note = f.get_html("https://x.example/")
ok("a normal page still reads fine", note == "ok" and "info@x.com" in html)

# ---- phone number formatting (for a customer-facing, scannable column) ---------------------------------------
from scraper.extract import normalize_phone  # noqa: E402

PHONE_CASES = [
    ("9848012345", "+91 98480 12345"),            # bare mobile
    ("+91 98480 12345", "+91 98480 12345"),        # already formatted - unchanged
    ("+919848012345", "+91 98480 12345"),          # no spaces
    ("09848012345", "+91 98480 12345"),            # leading trunk 0
    ("98480-12345", "+91 98480 12345"),            # dashed
    ("040-23456789", "040-23456789"),              # landline with STD code: left as scraped, not guessed at
    ("+91-40-23456789", "+91-40-23456789"),        # landline with country code: left as scraped
    ("1800 222 333", "1800-222-333"),              # toll-free, 7 digits after 1800
    ("1800222333", "1800-222-333"),
    ("18002093456", "1800-209-3456"),              # toll-free, 8 digits after 1800 (wider block)
    ("+1 415 555 0132", "+1 415 555 0132"),        # foreign number: left as scraped
]
for raw, expect in PHONE_CASES:
    ok(f"phone format: {raw!r} -> {expect!r}", normalize_phone(raw) == expect, normalize_phone(raw))
ok("two spellings of the same mobile normalize identically",
   normalize_phone("9848012345") == normalize_phone("+91 98480 12345") == normalize_phone("98480-12345"))
ok("normalizing never loses digits for an unrecognised shape",
   re.sub(r"\D", "", normalize_phone("+1 415 555 0132")) == "14155550132")

# a page with both spellings of one mobile ends up with just one phone number, in the clean format
import scraper.extract as extract_mod
page_phones = [extract_mod._clean_phone("9848012345"), extract_mod._clean_phone("+91 98480 12345")]
ok("the extractor's own clean+dedupe path collapses both spellings to one clean number",
   extract_mod.dedupe_phones(page_phones) == ["+91 98480 12345"])

# ---- "Other Details": human-readable facts only, no raw database tags -----------------------------------------
junk_tags = {"name": "GRIET College", "amenity": "college", "building": "yes", "type": "multipolygon",
            "wikidata": "Q7907349", "created_by": "Merkaartor 0.12", "historic": "yes", "education": "college"}
ok("purely technical map tags produce an empty Other Details", places._other_details(junk_tags) == "")
useful_tags = {**junk_tags, "operator": "Gokaraju Rangaraju Educational Society", "short_name": "GRIET",
              "wikipedia": "en:GRIET"}
detail = places._other_details(useful_tags)
ok("genuinely useful tags are kept, in plain English, technical ones are not",
   "Run by: Gokaraju Rangaraju Educational Society" in detail and "Short name: GRIET" in detail
   and "Wikipedia: GRIET" in detail and "building" not in detail and "multipolygon" not in detail
   and "Q7907349" not in detail and "Merkaartor" not in detail, detail)

# ---- the free search engine list is real (the "bing" bug) -----------------------------------------------------
import scraper.search as search_mod  # noqa: E402
ok("'bing' is not in the free engine list (ddgs has no Bing backend; passing that name silently runs every "
   "engine at once instead of just Bing, and the log then claims the wrong engine answered)",
   "bing" not in search_mod.FREE_ENGINES and "bing" not in discover.OTHER_ENGINES)
try:
    from ddgs.engines import ENGINES
    real_backends = {e.name if hasattr(e, "name") else type(e).__name__.lower() for e in __import__("ddgs").DDGS()._get_engines("text", "auto")}
    unknown = [e for e in search_mod.FREE_ENGINES if not any(e in b for b in real_backends)]
    ok(f"every engine in the free list is one ddgs actually has ({sorted(real_backends)})", not unknown, str(unknown))
except Exception as e:
    print(f"SKIP  engine-list cross-check against the installed ddgs version ({type(e).__name__})")

# ---- Google Programmable Search (free 100/day tier) as a second official option before the free-for-all --------
import requests as requests_mod  # noqa: E402

class FakeResp:
    def __init__(self, status, data): self.status_code, self._data = status, data
    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests_mod.HTTPError(f"{self.status_code}")
    def json(self): return self._data

calls = []
def fake_get(url, params=None, timeout=None):
    calls.append((url, params))
    return FakeResp(200, {"items": [{"title": "GRIET", "link": "https://griet.ac.in/", "snippet": "..."}]})
requests_mod.get = fake_get
hits = search_mod._google_cse("GRIET college official website", "in-en", 8, "FAKEKEY", "FAKECX")
ok("Google CSE is called with the key, engine id and query", calls[0][1]["key"] == "FAKEKEY" and calls[0][1]["cx"] == "FAKECX"
   and calls[0][1]["q"] == "GRIET college official website")
ok("Google CSE results are normalised to the same shape as every other engine",
   hits == [{"title": "GRIET", "href": "https://griet.ac.in/", "body": "..."}])

def quota_used(url, params=None, timeout=None):
    return FakeResp(429, {})
requests_mod.get = quota_used
try:
    search_mod._google_cse("x", "in-en", 8, "K", "C")
    ok("a used-up daily quota raises instead of silently returning nothing", False)
except requests_mod.HTTPError:
    ok("a used-up daily quota raises instead of silently returning nothing", True)

os.environ["GOOGLE_CSE_KEY"], os.environ["GOOGLE_CSE_CX"] = "FAKEKEY", "FAKECX"
os.environ.pop("BRAVE_API_KEY", None)
requests_mod.get = fake_get
msgs = []
hits = search_mod.web_search("GRIET college official website", "in-en", 8, msgs.append)
ok("web_search reaches for Google CSE before scraping any free engine when both keys are set",
   hits and hits[0]["href"] == "https://griet.ac.in/" and not any("duckduckgo" in m for m in msgs))
del os.environ["GOOGLE_CSE_KEY"], os.environ["GOOGLE_CSE_CX"]

# ---- the discovery pacing can be tuned per deployment without a code change ------------------------------------
os.environ["DISCOVER_MAX_PER_RUN"], os.environ["DISCOVER_GAP_SECONDS"], os.environ["DISCOVER_RETRY_WAIT"] = "7", "1.5", "9"
import importlib
jobs2 = importlib.reload(jobs)
ok("MAX_DISCOVER reads from DISCOVER_MAX_PER_RUN", jobs2.MAX_DISCOVER == 7)
ok("SEARCH_GAP reads from DISCOVER_GAP_SECONDS", jobs2.SEARCH_GAP == 1.5)
ok("THROTTLE_WAIT reads from DISCOVER_RETRY_WAIT", jobs2.THROTTLE_WAIT == 9)
for k in ("DISCOVER_MAX_PER_RUN", "DISCOVER_GAP_SECONDS", "DISCOVER_RETRY_WAIT"):
    del os.environ[k]
importlib.reload(jobs)  # back to defaults for anything that runs after this file

print(f"\n{len(failures)} failure(s)" if failures else "\nAll tests passed")
sys.exit(1 if failures else 0)
