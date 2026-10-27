"""Free-plan connectors (Serper, Tavily, Firecrawl) and the free/paid AI choice - mocked, no network.
Run: python tests/test_free_connectors.py (exit 1 on failure)."""
import os
import tempfile

TMP = tempfile.mkdtemp()
os.environ["JOB_STORE_DIR"] = os.path.join(TMP, "jobs")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(TMP, "free.db").replace("\\", "/")
os.environ["APP_USER"] = "bossfree"
os.environ["APP_PASSWORD"] = "adminpass123"
os.environ["SECRET_KEY"] = "test-secret-key-free-connectors-only"
os.environ.setdefault("SKIP_STARTUP_PROBES", "1")
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests  # noqa: E402

import app as appmod  # noqa: E402
from scraper import ai_extract, fetch, jobs as J, search as S  # noqa: E402

app = appmod.app
failures = []


def ok(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        failures.append(name)


class Resp:
    def __init__(self, status, data):
        self.status_code, self._data = status, data

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code), response=self)


# Real-shaped answers for each service (field names per each API's docs; read defensively in search.py).
ANSWERS = {
    S.SERPER_URL: Resp(200, {"organic": [{"title": "Digital Medha", "link": "https://digitalmedha.com/", "snippet": "Ameerpet"}]}),
    S.TAVILY_URL: Resp(200, {"results": [{"title": "Lemonoids", "url": "https://www.lemonoids.com/", "content": "Mehdipatnam"}]}),
    S.FIRECRAWL_SEARCH_URL: Resp(200, {"success": True, "data": {"web": [{"title": "Web Trainings", "url": "https://webtrainings.in/", "description": "Lakdikapul"}]}}),
    S.FIRECRAWL_SCRAPE_URL: Resp(200, {"success": True, "data": {"html": "<html><body>Contact: info@blocked.example</body></html>"}}),
}
posted = []


def fake_post(url, headers=None, json=None, timeout=None):
    posted.append(url)
    return ANSWERS[url]


_orig_post = S.requests.post
S.requests.post = fake_post
S.time.sleep = lambda s: None

# ---- 1. each service's answer becomes the same {title, href, body} shape as every other engine ------------------
ok("Serper results are read", S._serper("q", "in-en", 10, "k") == [{"title": "Digital Medha", "href": "https://digitalmedha.com/", "body": "Ameerpet"}])
ok("Tavily results are read", S._tavily("q", "in-en", 10, "k")[0]["href"] == "https://www.lemonoids.com/")
ok("Firecrawl v2 results ({data: {web: [...]}}) are read", S._firecrawl_search("q", "in-en", 10, "k")[0]["href"] == "https://webtrainings.in/")
ANSWERS[S.FIRECRAWL_SEARCH_URL] = Resp(200, {"success": True, "data": [{"title": "Old shape", "url": "https://old.example/"}]})
ok("Firecrawl's older list shape ({data: [...]}) is read too", S._firecrawl_search("q", "in-en", 10, "k")[0]["href"] == "https://old.example/")
ANSWERS[S.FIRECRAWL_SEARCH_URL] = Resp(200, {"success": True, "data": {"web": [{"title": "Web Trainings", "url": "https://webtrainings.in/"}]}})
_tav = ANSWERS[S.TAVILY_URL]
ANSWERS[S.TAVILY_URL] = Resp(200, {"something_else": [1, 2], "results": ["not-a-dict"]})
ok("an unexpected answer shape gives no results instead of crashing", S._tavily("q", "in-en", 5, "k") == [])
ANSWERS[S.TAVILY_URL] = _tav

# ---- 2. web_search: a user's keys are used after Brave, before the free engines; a spent key is skipped ---------
logs = []
with S.search_keys("", {"tavily": "tv-key"}):
    ok("keyed_search_set sees a Tavily-only user", S.keyed_search_set())
    hits = S.web_search("digital marketing institutes Hyderabad", "in-en", 10, logs.append)
ok("with only a Tavily key, Tavily answers the search", hits and hits[0]["href"] == "https://www.lemonoids.com/", str(hits))
ok("no free engine was needed", S.TAVILY_URL in posted)

ANSWERS[S.SERPER_URL] = Resp(402, {"error": "Insufficient credits"})
logs, posted[:] = [], []
with S.search_keys("", {"serper": "spent-key", "tavily": "tv-key"}):
    first = S.web_search("q1", "in-en", 10, logs.append)
    second = S.web_search("q2", "in-en", 10, logs.append)
ok("a key out of credit falls through to the next service", first and first[0]["href"] == "https://www.lemonoids.com/")
ok("the spent key is reported once, not on every search", sum("Serper refused" in m for m in logs) == 1, str(logs))
ok("and isn't called again in the same run", posted.count(S.SERPER_URL) == 1, str(posted))
ANSWERS[S.SERPER_URL] = Resp(200, {"organic": [{"title": "Digital Medha", "link": "https://digitalmedha.com/"}]})
with S.search_keys("", {}):
    ok("no keys: keyed_search_set is false", not S.keyed_search_set())
with S.search_keys("", {"nonsense": "x"}):
    ok("an unknown service name is ignored", not S.keyed_search_set())

# ---- 3. Firecrawl reads a page that refused the normal visit - only for refusals, only with a key ---------------
f = fetch.Fetcher({})
f.allowed = lambda url: True
f._get_once = lambda url: (None, "HTTP 403")
with S.search_keys("", {"firecrawl": "fc-key"}):
    html, note = f.get_html("https://blocked.example/contact")
ok("a 403 page is read through Firecrawl when the person has a key", html and "info@blocked.example" in html and "Firecrawl" in note, note)
with S.search_keys("", {}):
    html, note = f.get_html("https://blocked.example/contact")
ok("without a Firecrawl key, a refused page stays refused", html is None and note == "HTTP 403")
f._get_once = lambda url: (None, "HTTP 404")
posted[:] = []
with S.search_keys("", {"firecrawl": "fc-key"}):
    html, note = f.get_html("https://gone.example/")
ok("a 404 is not sent to Firecrawl (no credit wasted on a missing page)", html is None and S.FIRECRAWL_SCRAPE_URL not in posted)

# ---- 4. Settings: save, mask, test; keys never leave the server or reach exports --------------------------------
c = app.test_client()
c.post("/login", data={"username": "bossfree", "password": "adminpass123"})
c.post("/api/settings", json={"serper_api_key": "serper-secret-123456"})
s = c.get("/api/settings").get_json()
ok("a saved Serper key comes back masked, never in full", s["search_key_masks"]["serper"] and "serper-secret-123456" not in str(s), s["search_key_masks"])
_orig_test = S.test_keyed_search
S.test_keyed_search = lambda name, key: 7 if key == "serper-secret-123456" else 0
r = c.post("/api/settings/search-test", json={"name": "serper"}).get_json()
ok("Test key runs a real search with the saved key", r.get("ok") and r.get("results") == 7, r)
r = c.post("/api/settings/search-test", json={"name": "tavily"}).get_json()
ok("Test key without a saved key says so", not r.get("ok") and "Save your Tavily key" in r.get("error", ""), r)
ok("an unknown service is refused", c.post("/api/settings/search-test", json={"name": "evil"}).status_code == 400)
S.test_keyed_search = _orig_test
ok("search keys are treated as secrets in run details and exports",
   "search_keys" not in J.public_spec({"mode": "web", "search_keys": {"serper": "x"}}))
ok("search keys can be shared company-wide like the others", "serper_api_key" in appmod.ORG_SHAREABLE)

# ---- 5. preflight counts a Serper/Tavily/Firecrawl key as a working search option ---------------------------------
pf = J.preflight({"mode": "web", "search_keys": {"serper": "k"}})
ok("preflight lists Serper when the person has its key", "serper" in pf.get("search", []), pf.get("search"))
pf = J.preflight({"mode": "web"})
ok("and skips it without one", "serper" not in pf.get("search", []))

# ---- 6. AI: every provider is marked free or paid, and the free ones are real free-key providers ----------------
tiers = {k: v.get("tier") for k, v in ai_extract.PROVIDERS.items()}
ok("every AI provider says whether it's free or paid", all(t in ("free", "paid") for t in tiers.values()), tiers)
ok("free choices: Gemini, Groq, OpenRouter free models, Mistral",
   {k for k, t in tiers.items() if t == "free"} == {"gemini", "groq", "openrouter_free", "mistral"}, tiers)
ok("OpenRouter's free choice uses its free-models router", ai_extract.PROVIDERS["openrouter_free"]["model"] == "openrouter/free")
c.post("/api/settings", json={"ai_provider": "mistral"})
ok("a free provider can be saved", c.get("/api/settings").get_json()["ai_provider"] == "mistral")
page = c.get("/").get_data(as_text=True)
ok("the page config tells the browser each provider's tier", '"tier"' in page and "openrouter_free" in page)

S.requests.post = _orig_post
print(f"\n{len(failures)} failure(s)" if failures else "\nAll tests passed")
sys.exit(1 if failures else 0)
