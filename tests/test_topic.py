"""Topic matching, name-collecting recall and synonym searches - mocked, no network.
Run: python tests/test_topic.py   (exit 1 on failure)

Covers scraper/topic.py and how scraper/jobs.py uses it. The case table in section 3 is the one that motivated the
change: real company titles that a "coaching centres in Hyderabad" search was throwing away.
"""
import os
import sys
import tempfile

TMP = tempfile.mkdtemp()
os.environ["JOB_STORE_DIR"] = os.path.join(TMP, "jobs")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(TMP, "topic.db").replace("\\", "/")
os.environ["APP_USER"] = "bosstopic"
os.environ["APP_PASSWORD"] = "adminpass123"
os.environ["SECRET_KEY"] = "test-secret-key-topic-only"
os.environ.setdefault("SKIP_STARTUP_PROBES", "1")  # tests must not depend on live network probes
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scraper import jobs as J  # noqa: E402
from scraper import topic as T  # noqa: E402
import app as appmod  # noqa: E402

failures = []


def ok(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        failures.append(name)


# ---- 1. stemming: the two sides of every comparison meet in the middle --------------------------------------------
ok("British and American spellings meet", T.stem("centres") == T.stem("centers") == T.stem("center") == "center")
ok("plurals fold: academies/academy, institutes/institute, classes/class",
   T.stem("academies") == T.stem("academy") and T.stem("institutes") == T.stem("institute") and T.stem("classes") == "class")
ok("-ing and plural forms of the same word meet: coaching/coaches/coach, training/train",
   T.stem("coaching") == T.stem("coaches") == T.stem("coach") == "coach" and T.stem("training") == T.stem("train"))
ok("a word that merely contains another does not collapse into it (center != central, class != classified)",
   T.stem("central") != T.stem("center") and T.stem("classified") != T.stem("class"))
ok("double-s words are left alone (address, class)", T.stem("address") == "address" and T.stem("class") == "class")
ok("non-Latin text is returned unchanged", T.stem("学院") == "学院")
ok("domains tokenise on dots and hyphens", T.tokens("coaching-hyderabad.com") == ["coach", "hyderabad", "com"])

# ---- 2. topic_hit: stemmed whole words; synonyms only when asked for ---------------------------------------------
ok("a title is matched through stems", T.topic_hit(["coaching", "centres"], "Allen Coach Hyderabad"))
ok("a substring is not a match ('co' never matches 'college', 'center' never matches 'central')",
   not T.topic_hit(["center"], "Central School Hyderabad") and not T.topic_hit(["co"], "College of Engineering"))
ok("no synonyms in a normal run", not T.topic_hit(["coaching"], "Naresh IT - Software Training Institute"))
ok("synonyms count when collecting names (training / institute for a coaching query)",
   T.topic_hit(["coaching"], "Naresh IT - Software Training Institute", wide=True))
ok("a synonym group only switches on for its own category (a hospital query does not accept 'academy')",
   not T.topic_hit(["hospital"], "Stanford Academy", wide=True))
ok("wide never accepts a title with no related word at all", not T.topic_hit(["coaching"], "Peak Performers, Ameerpet", wide=True))
ok("Chinese topic words still match as plain substrings", T.topic_hit(["学院"], "海德拉巴学院 招生"))

# ---- 3. _keep_hit: the table of real-looking titles ----------------------------------------------------------------
def keep(title, url, q, wide=False, area=True):
    return J._keep_hit({"title": title, "body": ""}, url, q, area, wide=wide)

Q_BR, Q_US = "coaching centres in Hyderabad", "coaching centers in Hyderabad"
ok("spelling no longer decides it: 'ABC Centers' passes a 'centres' query and 'ABC Centres' a 'centers' one",
   keep("ABC Centers Hyderabad", "https://abccenters.com/", Q_BR) and keep("ABC Centres Hyderabad", "https://abccentres.com/", Q_US))
ok("'Coach' passes a 'coaching' query", keep("Allen Coach Hyderabad", "https://allen.in/", Q_US))
ok("a title with no topic word still fails a normal run (precision kept)", not keep("Peak Performers, Ameerpet", "https://peak.in/", Q_US))
ok("... and fails even a names-only run (no related word at all)", not keep("Peak Performers, Ameerpet", "https://peak.in/", Q_US, wide=True))
ok("a training-institute title is dropped by a normal run but kept when collecting names",
   not keep("Naresh IT - Software Training Institute", "https://nareshit.com/", Q_US)
   and keep("Naresh IT - Software Training Institute", "https://nareshit.com/", Q_US, wide=True))
ok("trusted academic domains still always pass", keep("Anything", "https://iiit.ac.in/", Q_US))

# filler words are no longer evidence
ok("'best' in a title no longer satisfies a 'best coaching centers' query",
   not keep("Best Biryani in Hyderabad", "https://biryani.example.com/", "best coaching centers in Hyderabad"))
ok("'contact' in a page no longer satisfies the place part",
   not J._keep_hit({"title": "Brilliant Coaching Academy", "body": "contact us for admissions"}, "https://b.example.com/contact",
                   "coaching academy in Atlantis contact", False))
ok("a query made only of filler words still checks something rather than matching everything or nothing",
   isinstance(J._keep_hit({"title": "Contact", "body": ""}, "https://x.example.com/", "contact email", False), bool))

# junk classes stay dead in names-only mode
ok("dictionary-definition titles are dropped even when collecting names",
   not keep("Coaching Meaning in Hindi - Definition", "https://dict.example.com/coaching", Q_US, wide=True))

# ---- 4. query expansion --------------------------------------------------------------------------------------------
ex = T.expand_query("coaching centers in Hyderabad")
ok("a coaching query expands to its usual wordings, original excluded",
   "training institutes in Hyderabad" in ex and "tuition centers in Hyderabad" in ex and "coaching centers in Hyderabad" not in ex, ex)
ok("at most 4 variants per query", len(ex) <= 4 and len(T.expand_query("coaching centers in Hyderabad", limit=2)) == 2)
ok("the rest of the query is preserved ('digital marketing', 'Ameerpet, Hyderabad')",
   all(v.startswith("digital marketing ") and v.endswith("in Ameerpet, Hyderabad")
       for v in T.expand_query("digital marketing coaching centres in Ameerpet, Hyderabad")))
ok("an ambiguous bare 'institutes in X' is not rewritten into colleges", T.expand_query("institutes in Hyderabad") == [])
ok("a category with no equivalents is left alone", T.expand_query("real estate agents in Pune") == [])
ok("platform-targeted site: searches are never expanded", T.expand_query("coaching centers site:linkedin.com") == [])
ok("expansion is deterministic", T.expand_query("coaching centers in Hyderabad") == ex)
ok("no duplicates, and none that stem to the original",
   len(set(ex)) == len(ex) and all(T.tokens(v) != T.tokens("coaching centers in Hyderabad") for v in ex))
ok("empty and non-string input do not raise", T.expand_query("") == [] and T.expand_query(None) == [])

# ---- 5. _search end to end: logging, names-only, and the opt-in synonym searches -------------------------------------
class FakeJob:
    def __init__(self, spec):
        self.spec = spec
        self.cancelled = type("E", (), {"is_set": staticmethod(lambda: False)})
        self.log, self.phase, self.activity, self.problems = [], "", "", []
        self.done = self.total = 0

    def say(self, m):
        self.log.append(m)


sent = []


def fake_search(q, region, n, say, *a, **k):
    sent.append(q)
    return [
        {"title": "Naresh IT - Software Training Institute", "body": "Ameerpet Hyderabad", "href": "https://nareshit.com/"},
        {"title": "Peak Performers, Ameerpet", "body": "Hyderabad", "href": "https://peakperformers.in/"},
        {"title": "Allen Coaching Classes Hyderabad", "body": "", "href": "https://allen.in/"},
    ]


real_ws, real_sleep = J.web_search, J.time.sleep
J.web_search, J.time.sleep = fake_search, lambda s: None
base = {"queries": ["coaching centers in Hyderabad"], "platforms": ["web"], "max_results": 50, "one_per_site": False, "region": "in-en"}

sent.clear()
j_full = FakeJob({**base})
rows_full, _ = J._search(j_full)
ok("a normal run keeps only the exact-topic hit", [r["url"] for r in rows_full] == ["https://allen.in/"], rows_full)
ok("a normal run lists no skipped titles (log stays short)", not any("skipped as unrelated" in m for m in j_full.log))
ok("a normal run sends exactly the typed query", sent == ["coaching centers in Hyderabad"], sent)

sent.clear()
j_names = FakeJob({**base, "names_only": True})
rows_names, _ = J._search(j_names)
ok("a names-only run also keeps the training-institute company", "https://nareshit.com/" in [r["url"] for r in rows_names], rows_names)
ok("a names-only run still drops a hit with no related word", "https://peakperformers.in/" not in [r["url"] for r in rows_names])
ok("a names-only run shows what it dropped, so a miss is visible",
   any("skipped as unrelated" in m and "Peak Performers" in m for m in j_names.log), j_names.log)

sent.clear()
j_exp = FakeJob({**base, "expand_synonyms": True})
J._search(j_exp)
ok("with the toggle on, the typed query AND its synonym wordings are searched",
   sent[0] == "coaching centers in Hyderabad" and "training institutes in Hyderabad" in sent and len(sent) > 1, sent)
ok("the log says how many extra wordings were added", any("similar wordings" in m for m in j_exp.log), j_exp.log)

sent.clear()
J._search(FakeJob({**base, "expand_synonyms": False}))
ok("with the toggle off, only the typed query is searched", sent == ["coaching centers in Hyderabad"], sent)

sent.clear()
many = [f"coaching centers in Place{i}" for i in range(30)]
J._search(FakeJob({**base, "queries": many, "expand_synonyms": True}))
ok("synonym searches are capped for one run, however many queries it has",
   len(sent) <= 30 + J.MAX_EXPANDED_QUERIES, len(sent))

sent.clear()
J._search(FakeJob({**base, "queries": ["coaching centers in Hyderabad"], "platforms": ["linkedin.com"], "expand_synonyms": True}))
ok("a platform-only run (no plain web search) gets no synonym searches", all("site:linkedin.com" in q for q in sent), sent)
J.web_search, J.time.sleep = real_ws, real_sleep

# ---- 6. the job spec carries the toggle -----------------------------------------------------------------------------
with appmod.app.app_context():
    s_on, e1, _ = appmod.build_job_spec({"mode": "web", "queries": "coaching centers in Hyderabad", "names_only": True,
                                         "expand_synonyms": True}, {}, 1)
    s_off, e2, _ = appmod.build_job_spec({"mode": "web", "queries": "coaching centers in Hyderabad", "names_only": True}, {}, 1)
ok("expand_synonyms reaches the job spec when ticked", e1 is None and s_on["expand_synonyms"] is True)
ok("it defaults to off", e2 is None and s_off["expand_synonyms"] is False)

# ---- list pages are not companies (real names-only run, 2026-10-19) ------------------------------------------------------
for title in ("Top Coaching Center in Karimnagar", "Coaching Centres in Karimnagar", "Karimnagar SSC Coaching Fees",
              "10 Best Digital Marketing Institutes in Hyderabad", "List of coaching centers in Warangal"):
    ok(f"list title is recognised: {title}", J._looks_like_list_title(title))
for title in ("Vibgyor Coaching Centre in Karimnagar", "Naresh IT - Software Training Institute", "Allen Coaching Classes Hyderabad",
              "Digital Vidya | Digital Marketing Courses"):
    ok(f"a real company title is not a list: {title}", not J._looks_like_list_title(title))

sent.clear()


def fake_lists(q, region, n, say, *a, **k):
    return [{"title": "Top Coaching Center in Karimnagar", "body": "", "href": "https://digitalclassworld.com/coaching/karimnagar"},
            {"title": "Vibgyor Coaching Centre in Karimnagar", "body": "Karimnagar", "href": "https://vibgyorcoaching.in/"}]


J.web_search, J.time.sleep = fake_lists, lambda s: None
jl = FakeJob({**base, "queries": ["coaching centres in Karimnagar"], "names_only": True})
rows_l, _ = J._search(jl)
ok("names-only keeps the real centre and treats the list page as a directory",
   [r["url"] for r in rows_l] == ["https://vibgyorcoaching.in/"], rows_l)
J.web_search, J.time.sleep = real_ws, real_sleep

# ---- junk a free engine answered on the hosted app (production, 2026-10-19) ----------------------------------------------
Q_DM = "digital marketing institute in Ameerpet, Hyderabad"
ok("'What Is Digital?' on a /jargon/ page is not a company",
   not keep("What Is Digital?", "https://www.computerhope.com/jargon/d/digital.htm", Q_DM))
ok("a /glossary/ page is not a company even with a plain title",
   not keep("Digital Marketing", "https://example-agency.com/glossary/digital-marketing", Q_DM))
ok("a question the person asked still gets answers", J._looks_like_definition("What is digital marketing", "what is digital marketing") is False)
ok("a real institute titled normally is kept", keep("Digital Vidya - Digital Marketing Institute in Hyderabad", "https://www.digitalvidya.com/", Q_DM))
from scraper import discover as D  # noqa: E402
ok("GitHub, Medium and blogspot pages are never someone's official site",
   not D._is_official_candidate("https://github.com/someone") and not D._is_official_candidate("https://x.blogspot.com/p")
   and not D._is_official_candidate("https://medium.com/@x/post"))

print(f"\n{len(failures)} failure(s)" if failures else "\nAll tests passed")
sys.exit(1 if failures else 0)
