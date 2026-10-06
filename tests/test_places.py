"""Place search tests. Run:  python tests/test_places.py   (no network needed; exit code 1 if anything fails)."""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scraper import discover, fetch, jobs, listings, places  # noqa: E402

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
places._geocode_photon = lambda q: None  # the backup geocoder has nothing better either, for this test
g = places.geocode("Some Shop, Hyderabad")
ok("only a business found: search around it and say so", g["kind"] == "around" and g["radius"] == 3000 and g["approx"] is True)
ok("the approximate case is described honestly", "Couldn't find an area" in places.describe_location(g, "Some Shop, Hyderabad"))

places._nominatim = lambda q, limit=8: []
places._geocode_photon = lambda q: {"kind": "box", "bbox": (1, 2, 3, 4), "label": "Backup Town"}
ok("the backup geocoder is used when the main one knows nothing", places.geocode("Nowhere")["label"] == "Backup Town")

# ---- a typo'd state/district name doesn't quietly turn into "a few km around some unrelated business" ---------
# Real failure: "Telengana, India" (a common misspelling of "Telangana") matched no area in Nominatim, but did
# fuzzy-match a random bank branch whose name happened to contain the word - old code used that immediately
# without ever trying the backup geocoder, turning a request for the whole state into a 3 km search around a bank.
ok("a common state-name misspelling is corrected before geocoding", places._fix_spelling("Telengana, India") == "telangana, India")
bank = {**poi, "display_name": "Telengana Cooperative bank, Vanasthalipuram, Hyderabad"}
calls.clear()
def fake_nominatim_typo(q, limit=8):
    calls.append(q)
    return [bank]  # Nominatim's best (and only) fuzzy match: an unrelated small business, not an area
places._nominatim = fake_nominatim_typo
places._geocode_photon = lambda q: {"kind": "box", "bbox": (1, 2, 3, 4), "label": "Telangana (backup match)"}
g = places.geocode("Telengana, India")
ok("the backup geocoder is tried (and preferred) before settling for an unrelated point match",
   g["label"] == "Telangana (backup match)", str(g))
ok("it was asked for the corrected spelling", calls and "telangana" in calls[0].lower(), calls)

places._geocode_photon = lambda q: None
g = places.geocode("Telengana, India")
ok("only once the backup geocoder also comes up empty does it fall back to the point match",
   g["kind"] == "around" and g["approx"] is True, str(g))

places._nominatim = lambda q, limit=8: []
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

# ---- meta-refresh redirects (the cvr.ac.in case: a real site, no JS involved) ----------------------------------
pages = {
    "https://x.example/": b'<html><head><meta http-equiv="Refresh" content="0; url=/home4/"></head></html>',
    "https://x.example/home4/": b"<html><title>Real site</title><body>contact@x.example</body></html>",
}
f.session.get = lambda u, **k: Resp(pages[u])
html, note = f.get_html("https://x.example/")
ok("a meta-refresh redirect is followed to the real page", note == "ok" and "contact@x.example" in (html or ""), (html, note))

loop_pages = {"https://x.example/": b'<html><head><meta http-equiv="Refresh" content="0; url=/b/"></head></html>',
              "https://x.example/b/": b'<html><head><meta http-equiv="Refresh" content="0; url=/a/"></head></html>',
              "https://x.example/a/": b'<html><head><meta http-equiv="Refresh" content="0; url=/b/"></head></html>'}
f.session.get = lambda u, **k: Resp(loop_pages.get(u, b"<html></html>"))
import time as _time
t0 = _time.time()
html, note = f.get_html("https://x.example/")
ok("a redirect loop stops instead of hanging", _time.time() - t0 < 3, f"{_time.time() - t0:.1f}s")

f.session.get = lambda *a, **k: Resp(b"<html><body>no redirect here</body></html>")
html, note = f.get_html("https://x.example/")
ok("a normal page with no meta-refresh is unaffected", note == "ok" and "no redirect" in html)

# ---- phone extraction precision: a student roll number must never be read as a phone number ---------------------
import scraper.extract as _extract_mod  # noqa: E402
roll_number_text = ("2026 Batch Students - Maddi Srihitha - Mech - 160122736077, "
                    "Kotte Haindhavi Rao-EEE- 160122734005 is placed in ITC with CTC:9LPA")
found = [p for p in (_extract_mod._clean_phone(m.group(0)) for m in _extract_mod.PHONE_RE.finditer(roll_number_text)) if p]
ok("a student roll number sitting in ordinary text is not read as a phone number (seen for real on cbit.ac.in)",
   found == [], found)
ok("but a genuine unseparated 10-digit mobile is still accepted", _extract_mod._clean_phone("9848012345") == "+91 98480 12345")
ok("a genuine trunk-prefixed landline run is still accepted", _extract_mod._clean_phone("04023146077") is not None)
ok("Unicode dashes/spaces in a phone number are normalised to plain ASCII",
   _extract_mod.normalize_phone("040 – 67135100") == "040 - 67135100" and "–" not in _extract_mod.normalize_phone("040—67135100"))

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
search_mod.time.sleep = lambda s: None
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

# ---- no key: a free engine returning fewer than max_results tops up from the next engine instead of stopping ----
# (this was the actual cause of "max results 200" runs coming back with under 10 rows: the old code returned
# the first engine's hits no matter how few, even when far more were asked for and other engines had more to give.)
for k in ("BRAVE_API_KEY", "GOOGLE_CSE_KEY", "GOOGLE_CSE_CX"):
    os.environ.pop(k, None)

class FakeDDGS:
    calls = []
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False
    def text(self, query, region, max_results, backend):
        FakeDDGS.calls.append((backend, max_results))
        return {
            "duckduckgo": [{"href": "https://griet.ac.in/", "title": "GRIET", "body": ""}],
            "yahoo": [{"href": "https://griet.ac.in/", "title": "GRIET", "body": ""},     # same site again: deduped
                      {"href": "https://cbit.ac.in/", "title": "CBIT", "body": ""}],
            "brave": [],  # a real engine with nothing for this query
            "google": [{"href": f"https://college{i}.ac.in/", "title": f"College {i}", "body": ""} for i in range(5)],
        }.get(backend, [])
search_mod.DDGS = FakeDDGS
FakeDDGS.calls = []
hits = search_mod.web_search("engineering colleges in Bachupally", "in-en", 20, print)
ok("results from every free engine are merged, not just the first one that answered anything",
   {h["href"] for h in hits} == {"https://griet.ac.in/", "https://cbit.ac.in/"} | {f"https://college{i}.ac.in/" for i in range(5)},
   str(hits))
ok("the same URL from a second engine is deduplicated", len(hits) == 7, str(hits))
ok("every free engine is tried since none alone reached max_results",
   {b for b, _ in FakeDDGS.calls} == set(search_mod.FREE_ENGINES), str(FakeDDGS.calls))

FakeDDGS.calls = []
hits = search_mod.web_search("engineering colleges in Bachupally", "in-en", 1, print)
ok("once max_results is reached, later engines are skipped rather than queried for nothing",
   len(hits) == 1 and len(FakeDDGS.calls) == 1, str(FakeDDGS.calls))
import ddgs as ddgs_mod
search_mod.DDGS = ddgs_mod.DDGS

# ---- the discovery pacing can be tuned per deployment without a code change ------------------------------------
os.environ["DISCOVER_MAX_PER_RUN"], os.environ["DISCOVER_GAP_SECONDS"], os.environ["DISCOVER_RETRY_WAIT"] = "7", "1.5", "9"
import importlib
jobs2 = importlib.reload(jobs)
ok("MAX_DISCOVER reads from DISCOVER_MAX_PER_RUN", jobs2.MAX_DISCOVER == 7)
ok("SEARCH_GAP reads from DISCOVER_GAP_SECONDS", jobs2.SEARCH_GAP == 1.5)
ok("THROTTLE_WAIT reads from DISCOVER_RETRY_WAIT", jobs2.THROTTLE_WAIT == 9)
for k in ("DISCOVER_MAX_PER_RUN", "DISCOVER_GAP_SECONDS", "DISCOVER_RETRY_WAIT"):
    del os.environ[k]
jobs3 = importlib.reload(jobs)

# ---- an official search key shortens the pacing automatically, on both "Search the web" and Places -------------
for k in ("BRAVE_API_KEY", "GOOGLE_CSE_KEY", "GOOGLE_CSE_CX"):
    os.environ.pop(k, None)
ok("no key: full, defensive pacing", jobs3._current_gap() == jobs3.SEARCH_GAP)
os.environ["BRAVE_API_KEY"] = "x"
ok("a Brave key: short, quota-based pacing", jobs3._current_gap() == jobs3.KEYED_GAP and jobs3._current_gap() < jobs3.SEARCH_GAP)
del os.environ["BRAVE_API_KEY"]
ok("half of the Google CSE pair alone changes nothing (both key and engine id are required)",
   (os.environ.__setitem__("GOOGLE_CSE_KEY", "x"), jobs3._current_gap() == jobs3.SEARCH_GAP, os.environ.pop("GOOGLE_CSE_KEY"))[1])
os.environ["GOOGLE_CSE_KEY"], os.environ["GOOGLE_CSE_CX"] = "x", "y"
ok("a full Google CSE pair: short pacing too", jobs3._current_gap() == jobs3.KEYED_GAP)
del os.environ["GOOGLE_CSE_KEY"], os.environ["GOOGLE_CSE_CX"]

class FakeJob:
    def __init__(self):
        self.cancelled = type("E", (), {"is_set": staticmethod(lambda: False)})
        self.log, self.phase, self.total, self.done, self.activity = [], "", 0, 0, ""
    def say(self, m): self.log.append(m)

jobs3.discover = jobs3.discover  # keep the already-patched discover module from earlier in this file
jobs3.discover.find_website = lambda n, w, say=None, aliases=(): (None, 0)
jobs3.time.sleep = lambda s: None
j = FakeJob()
jobs3._discover_websites(j, [{"Name": "A College", "Website": "", "Search Location": "X"}])
ok("with no key, zero hits still triggers the throttle wait-and-retry", any("waiting" in m for m in j.log))
os.environ["BRAVE_API_KEY"] = "x"
j = FakeJob()
jobs3._discover_websites(j, [{"Name": "A College", "Website": "", "Search Location": "X"}])
ok("with a key, zero hits is taken at face value - no pointless wait-and-retry", not any("waiting" in m for m in j.log))
del os.environ["BRAVE_API_KEY"]
importlib.reload(jobs)  # back to defaults for anything that runs after this file

# ---- search-free website guessing, contact cleanup, engineering filter ----------------------------------
ok("initials of the full name are a candidate", "griet" in discover.guess_stems("Gokaraju Rangaraju Institute of Engineering and Technology"))
ok("a distinctive word is a candidate", "bvrit" in discover.guess_stems("BVRIT Hyderabad College of Engineering for Women"))
ok("an alias adds candidates", "vjiet" in discover.guess_stems("VNR college", ["VNR Vignana Jyothi Institute of Engineering and Technology"]) or "vnrvjiet" in discover.guess_stems("vnr vjiet college"))
ok("a college page naming the college is verified", discover.verify_page("GRIET Gokaraju Rangaraju Institute of Engineering and Technology, Bachupally", "GRIET College", ["Gokaraju Rangaraju Institute of Engineering and Technology"], "Bachupally, Hyderabad"))
ok("an unrelated page is rejected", not discover.verify_page("Best travel deals and hotels in Goa", "GRIET College", [], "Bachupally"))
ok("matching initials alone need the city too", not discover.verify_page("CBIT college of nursing, Kerala", "Chaitanya Bharathi Institute of Technology", [], "Hyderabad")
   and discover.verify_page("CBIT engineering college, Gandipet Hyderabad", "Chaitanya Bharathi Institute of Technology", [], "Hyderabad"))

# ---- a "...-colleges-in-<place>" URL path is a directory, whichever domain it's on ---------------------------
# Real failure: chunocollege.com, findmycollege.com and studyclap.com (none on the domain blocklist - an endless
# long tail) all slipped through as if they were someone's own site, purely because their domain names sound
# college-ish. None of them would ever pass this check for their OWN homepage ("/") - only their category pages.
ok("a 'colleges-in-<place>' category page is a directory on any domain",
   not discover._is_official_candidate("https://findmycollege.com/engineering-colleges-in-secunderabad"))
ok("a 'top-N-...-colleges' listing page is a directory on any domain",
   not discover._is_official_candidate("https://example.com/top-10-engineering-colleges-in-hyderabad"))
ok("a real college's own homepage is unaffected", discover._is_official_candidate("https://griet.ac.in/"))
ok("a real college's own page about its own schools/departments is unaffected (not every '.../schools...' path is a directory)",
   discover._is_official_candidate("https://somecollege.edu.in/schools-of-management"))

class FakeFetcher:
    pages = {"https://griet.ac.in/": "<html><title>GRIET</title><body>Gokaraju Rangaraju Institute of Engineering and Technology, Hyderabad</body></html>",
             "https://griet.in/": "<html><body>Shoe shop</body></html>"}
    def get_html(self, url):
        return (self.pages[url], "ok") if url in self.pages else (None, "HTTP 404")
exists = {"griet.in", "griet.ac.in"}
def fake_resolve(h):
    if h not in exists: raise OSError
found = discover.guess_website(FakeFetcher(), "Gokaraju Rangaraju Institute of Engineering and Technology", "Bachupally, Hyderabad", [], fake_resolve)
ok("the right address is found without any search engine", found == "https://griet.ac.in/", str(found))
ok("nothing is guessed when no address resolves", discover.guess_website(FakeFetcher(), "Zzyzx Institute of Technology", "Hyderabad", [], lambda h: (_ for _ in ()).throw(OSError())) is None)

ok("own-domain emails come first, third parties are dropped",
   jobs._own_emails(["design@webagency.com", "info@griet.ac.in", "x@gmail.com"], "https://griet.ac.in/") == ["info@griet.ac.in", "x@gmail.com"])

captured = {}
places.geocode = lambda loc: {"kind": "area", "osm_type": "relation", "osm_id": 1, "label": "x"}
places._overpass = lambda q, notify=None: [
    {"type": "way", "center": {"lat": 17.5, "lon": 78.3}, "tags": {"name": "CMR College"}},
    {"type": "way", "center": {"lat": 17.6, "lon": 78.3}, "tags": {"name": "Akshara Junior College"}},
    {"type": "way", "center": {"lat": 17.7, "lon": 78.3}, "tags": {"name": "Sri Medical College"}},
    {"type": "way", "center": {"lat": 17.8, "lon": 78.3}, "tags": {"name": "Junior College of Engineering", "short_name": "JCE"}}]
got = places.search_places("Engineering colleges", "X", "", 100)
ok("engineering list keeps colleges without 'engineering' in the name and drops junior/medical ones",
   [r["Name"] for r in got] == ["CMR College", "Junior College of Engineering"], str([r["Name"] for r in got]))
ok("map short names become aliases for the website lookup", got[1]["_aliases"] == ["JCE"])
ok("the sheet has only real data columns", places.OUTPUT_COLUMNS == ["Name", "Phone", "Email", "Website", "Address", "Google Maps Link"])

# ---- "Search the web" drops off-topic results a struggling free engine pads its answer with -------------------
# Real failure: asking for "engineering colleges in Bachupally, Hyderabad" got back Britannica's definition of
# "engineering", Oregon State University, National University (US) and similar - none of them about a real
# college in the area that was actually asked for, let alone India.
sig = jobs._signal_words("engineering colleges in Bachupally, Hyderabad")
ok("generic words from the query (engineering, colleges, in) are not signal words", sig == ["bachupally", "hyderabad"], sig)
ok("a 'site:' operator is not mistaken for a signal word", jobs._signal_words("IT companies in Hyderabad site:linkedin.com") == ["companies", "hyderabad"])

off_topic_hits = [
    {"title": "Engineering", "body": "", "href": "https://www.britannica.com/technology/engineering"},
    {"title": "Engineering.com", "body": "", "href": "https://www.engineering.com/"},
    {"title": "What Do Engineers Do?", "body": "", "href": "https://www.snhu.edu/about-us/newsroom/stem/what-do-engineers-do"},
    {"title": "Oregon State University College of Engineering", "body": "askengineering@oregonstate.edu",
     "href": "https://engineering.oregonstate.edu/"},
    {"title": "The Editorial Advisory Board", "body": "", "href": "https://www.nu.edu/"},
]
ok("none of the real unrelated results the engine actually returned pass the relevance check",
   not any(jobs._on_topic(h, "https://example.edu/", sig) for h in off_topic_hits))
ok("a real match (place name in the snippet) passes",
   jobs._on_topic({"title": "GRIET - Gokaraju Rangaraju Institute of Engineering and Technology",
                   "body": "Located in Bachupally, Hyderabad"}, "https://griet.ac.in/", sig))
ok("a query with nothing distinctive in it (no signal words) filters nothing",
   jobs._signal_words("list of engineering colleges") == []
   and jobs._on_topic({"title": "anything at all", "body": ""}, "https://example.com/", jobs._signal_words("list of engineering colleges")))

class FakeJob2:
    def __init__(self):
        self.cancelled = type("E", (), {"is_set": staticmethod(lambda: False)})
        self.log, self.phase, self.activity, self.problems = [], "", "", []
    def say(self, m): self.log.append(m)
real_jobs_web_search = jobs.web_search
jobs.web_search = lambda q, region, n, say: (off_topic_hits + [
    {"href": "https://griet.ac.in/", "title": "GRIET, Bachupally, Hyderabad", "body": "Gokaraju Rangaraju Institute"}])
j2 = FakeJob2()
j2.spec = {"queries": ["engineering colleges in Bachupally, Hyderabad"], "platforms": ["web"], "max_results": 50,
           "one_per_site": False, "region": "in-en"}
rows, _ = jobs._search(j2)
ok("end to end: only the real college survives, the unrelated pages the engine padded its answer with are dropped",
   [r["url"] for r in rows] == ["https://griet.ac.in/"], str(rows))
ok("the run log says how many unrelated results were skipped", any("unrelated" in m and "skipped" in m for m in j2.log), j2.log)
jobs.web_search = real_jobs_web_search

# ---- a genuine official site for a hyperlocal query is kept even when it never repeats the micro-area name --------
# Real failure, found on a live run: "engineering colleges in Tarnaka" returned Osmania University's own engineering
# college page, whose own text says "Hyderabad - 500 007" (city + PIN code) and never "Tarnaka" - a real institution
# describes its own location by city, not by a search engine's choice of neighbourhood. Directory pages for the same
# query DO say "Tarnaka" (that's literally their business), so their presence proves the engine understood the area;
# that should be enough to stop demanding the official site repeat it too.
hyperlocal_hits = [
    {"title": "20+ Engineering Colleges in Tarnaka - Justdial", "body": "", "href": "https://www.justdial.com/Hyderabad/Engineering-Colleges-in-Tarnaka/nct-1"},
    {"title": "University College of Engineering - Osmania University", "body": "Osmania University, Hyderabad - 500 007, Telangana",
     "href": "https://www.uceou.edu/contactus.php"},
]
jobs.web_search = lambda q, region, n, say: hyperlocal_hits
j3 = FakeJob2()
j3.spec = {"queries": ["engineering colleges in Tarnaka"], "platforms": ["web"], "max_results": 50, "one_per_site": False, "region": "in-en"}
rows3, _ = jobs._search(j3)
ok("the real official site survives once a directory result confirms the engine understood the area",
   [r["url"] for r in rows3] == ["https://www.uceou.edu/contactus.php"], str(rows3))
jobs.web_search = real_jobs_web_search

# ---- a .ac.in/.edu.in result survives even with NO directory confirmation and a snippet mentioning nothing ------
# Real failure: "engineering colleges in Secunderabad, India" got back real college results whose SERP snippets just
# said the college name - no city, no "India", nothing _on_topic could match - and this particular query's result
# set happened to have no directory hit either, so area_confirmed stayed False too. .ac.in/.edu.in registration is
# gated to accredited Indian institutions, so that domain shape alone is real evidence, independent of snippet text.
no_snippet_hits = [{"title": "Vasavi College of Engineering", "body": "", "href": "https://www.vce.ac.in/"}]
jobs.web_search = lambda q, region, n, say: no_snippet_hits
j5 = FakeJob2()
j5.spec = {"queries": ["engineering colleges in Secunderabad, India"], "platforms": ["web"], "max_results": 50, "one_per_site": False, "region": "in-en"}
rows5, _ = jobs._search(j5)
ok("a .ac.in result with no matching text and no directory confirmation is still kept",
   [r["url"] for r in rows5] == ["https://www.vce.ac.in/"], str(rows5))

jobs.web_search = lambda q, region, n, say: [{"title": "College of Engineering", "body": "", "href": "https://engineering.oregonstate.edu/"}]
j6 = FakeJob2()
j6.spec = {"queries": ["engineering colleges in Secunderabad, India"], "platforms": ["web"], "max_results": 50, "one_per_site": False, "region": "in-en"}
rows6, _ = jobs._search(j6)
ok("a non-Indian .edu domain with the same empty snippet is still rejected (trust is TLD-specific, not a blanket bypass)",
   rows6 == [], str(rows6))
jobs.web_search = real_jobs_web_search

# ---- listing pages: a directory page with no dedicated parser is still just dropped, but one with a parser -------
# (scraper/listings.py) is expanded into one row per real name, instead of one row for the whole listing page or
# (the old behaviour) being thrown away entirely. Markup below is a trimmed real fragment of colleges9.in's own
# category pages: a <table> of <tr>s, each holding <td><b>NAME</b><br>address</td> next to a /colleges/.../ link.
COLLEGES9_FIXTURE = """
<table class="crs"><tr>
  <td>1</td>
  <td><b>AAR MAHAVEER ENGINEERING COLLEGE</b><br>Vyasapuri, Bandlaguda, Kesavagiri 500005, Hyderabad District.</td>
  <td>AARM</td>
  <td><a href="/colleges/AAR-MAHAVEER-ENGINEERING-COLLEGE/EN728/">College Details</a></td>
</tr><tr>
  <td>2</td>
  <td><b>BVRIT COLLEGE OF ENGINEERING FOR WOMEN</b><br>Nizampet Road, Bachupalli, Hyderabad.</td>
  <td>BVRITW</td>
  <td><a href="/colleges/BVRIT-COLLEGE-OF-ENGINEERING-FOR-WOMEN/EN748/">College Details</a></td>
</tr></table>
"""
items = listings.extract_listing(COLLEGES9_FIXTURE, "https://www.colleges9.in/Telangana/Hyderabad/Engineering-Colleges/")
ok("a registered site's listing page yields one real row per college", len(items) == 2, items)
ok("the name comes from the <b> tag, not the generic 'College Details' link text",
   {i["name"] for i in items} == {"AAR MAHAVEER ENGINEERING COLLEGE", "BVRIT COLLEGE OF ENGINEERING FOR WOMEN"}, items)
ok("the address is whatever follows the name in the same cell",
   next(i["address"] for i in items if "AAR" in i["name"]) == "Vyasapuri, Bandlaguda, Kesavagiri 500005, Hyderabad District.")
ok("the link is resolved to an absolute URL", items[0]["href"].startswith("https://www.colleges9.in/colleges/"))
ok("a site with no registered parser yields nothing (never guessed at generically - see the module docstring)",
   listings.extract_listing(COLLEGES9_FIXTURE, "https://www.some-other-directory.example/list/") == [])
ok("malformed markup for a registered site fails safe (empty, not a crash)",
   listings.extract_listing("<table><tr><td><a href='/colleges/X/'>broken", "https://www.colleges9.in/x/") == [])

# ---- listing pages, end to end through _run_web: real rows, deduplicated against names already found ------------
class FakeFetcher2:
    def get_html(self, url):
        if "colleges9.in" in url:
            return COLLEGES9_FIXTURE, "ok"
        return "<html><title>AAR Mahaveer Engineering College</title><body>Hyderabad</body></html>", "ok"
real_fetcher = jobs.Fetcher
jobs.Fetcher = lambda spec: FakeFetcher2()
jobs.web_search = lambda q, region, n, say: [
    {"href": "https://www.colleges9.in/Telangana/Hyderabad/Engineering-Colleges/", "title": "Engineering Colleges List", "body": ""},
    {"href": "https://aarmahaveer.ac.in/", "title": "AAR Mahaveer Engineering College", "body": "Hyderabad"},
]
# The listing-expanded row (BVRIT) now also goes through _enrich_listed_rows - mocked out here (offline, like
# everywhere else in this file) since enrichment itself is covered thoroughly in its own tests below.
real_guess4, real_find4 = discover.guess_website, discover.find_website
discover.guess_website = lambda fetcher, name, area="", aliases=(): None
discover.find_website = lambda name, where, say=None, aliases=(): (None, 2)
j4 = jobs.Job({"queries": ["engineering colleges in Hyderabad"], "platforms": ["web"], "max_results": 50,
               "one_per_site": False, "region": "in-en", "fields": ["address"], "custom_fields": [], "require": "",
               "follow_contact": False, "ai": {}})
jobs._run_web(j4)
names = sorted(r["Name"] for r in j4.rows)
ok("the listing page is expanded instead of kept as one row for the page itself",
   "BVRIT COLLEGE OF ENGINEERING FOR WOMEN" in names, names)
ok("a name already found as a real official site is not duplicated from the listing",
   sum(1 for n in names if n.lower() == "aar mahaveer engineering college") == 1, names)
ok("two real rows total: the one official site plus the one new name from the listing", len(j4.rows) == 2, names)
discover.guess_website, discover.find_website = real_guess4, real_find4
jobs.Fetcher = real_fetcher
jobs.web_search = real_jobs_web_search

# ---- known seed directories: real rows with zero search engine involved -----------------------------------------
ok("the Telangana engineering-colleges seed covers all 10 real districts",
   len(listings.KNOWN_SEEDS["telangana_engineering_colleges"]["urls"]) == 10)
ok("every seed URL is colleges9.in's own Engineering-Colleges page for a district",
   all(u.startswith("https://www.colleges9.in/Telangana/") and u.endswith("/Engineering-Colleges/")
       for u in listings.KNOWN_SEEDS["telangana_engineering_colleges"]["urls"]))

COLLEGE_HOME_PAGE = ('<html><title>AAR Mahaveer Engineering College</title>'
                      '<body>Contact us: info@aarmahaveer.ac.in, 040-23146077. Hyderabad.</body></html>')
class FakeFetcher3:
    def get_html(self, url):
        if "colleges9.in" in url:
            return COLLEGES9_FIXTURE, "ok"
        if "aarmahaveer.ac.in" in url:
            return COLLEGE_HOME_PAGE, "ok"
        return None, "not used"
jobs.Fetcher = lambda spec: FakeFetcher3()
real_guess, real_find = discover.guess_website, discover.find_website
# The free, search-engine-free guess (name.ac.in-style addresses + real DNS) is mocked out entirely here so this
# test stays offline, as promised at the top of this file - "nothing is guessed" is exercised directly elsewhere
# (see "nothing is guessed when no address resolves" above). Only the paced web-search fallback is exercised here.
discover.guess_website = lambda fetcher, name, area="", aliases=(): None
discover.find_website = lambda name, where, say=None, aliases=(): (("https://aarmahaveer.ac.in/", 5) if "AAR" in name.upper() else (None, 3))
j7 = jobs.Job({"queries": [], "seed": "telangana_engineering_colleges", "platforms": ["web"], "max_results": 50,
               "one_per_site": False, "region": "in-en", "fields": ["address", "emails", "phones"], "custom_fields": [],
               "require": "", "follow_contact": False, "ai": {}})
jobs._run_web(j7)
ok("a seed-only run (no typed searches at all) still produces real rows", len(j7.rows) == 2, [r["Name"] for r in j7.rows])
aar = next(r for r in j7.rows if "AAR" in r["Name"])
bvrit = next(r for r in j7.rows if "BVRIT" in r["Name"])
ok("a name the search fallback resolves gets its real website and contacts filled in",
   aar["Website"] == "https://aarmahaveer.ac.in/" and aar["Emails"] == "info@aarmahaveer.ac.in" and "23146077" in aar["Phone Numbers"], aar)
ok("a name nothing resolves for keeps the directory's own profile link as the only lead, no contacts invented",
   bvrit["Website"].startswith("https://www.colleges9.in/colleges/") and not bvrit.get("Emails") and not bvrit.get("Phone Numbers"), bvrit)
ok("the internal 'Search Location' helper column never leaks into the sheet", "Search Location" not in aar and "Search Location" not in bvrit)
discover.guess_website, discover.find_website = real_guess, real_find
jobs.Fetcher = real_fetcher

print(f"\n{len(failures)} failure(s)" if failures else "\nAll tests passed")
sys.exit(1 if failures else 0)
