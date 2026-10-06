"""Find the official website of a place that the map doesn't list one for, using web search.

The map data for most Indian colleges, hospitals and schools has a name and a position but no website, phone or email.
The websites of those places do have contact details, so: search for "<name> <area> official website", pick the first
result that looks like the place's own site (not a directory or social page), and let the normal page reader take over.
"""
import math
import re
import socket
import time
from urllib.parse import urlparse

from .fetch import domain_of
from .search import SearchBlocked, web_search  # noqa: F401  (SearchBlocked is re-exported for callers)

# Directories, aggregators, social networks and maps: they list many places and are never "the" website of one.
NOT_OFFICIAL = (
    "shiksha.com", "collegedunia.com", "careers360.com", "collegedekho.com", "getmyuni.com", "indcareer.com", "edufever.com",
    "justdial.com", "sulekha.com", "indiamart.com", "yellowpages.in", "magicbricks.com", "99acres.com", "nobroker.in",
    "wikipedia.org", "wikimapia.org", "wikidata.org", "facebook.com", "instagram.com", "linkedin.com", "twitter.com", "x.com",
    "youtube.com", "pinterest.com", "quora.com", "reddit.com", "google.com", "bing.com", "yahoo.com", "mapquest.com",
    "tripadvisor.com", "yelp.com", "zaubacorp.com", "mapsofindia.com", "indiatoday.in", "timesofindia.indiatimes.com",
    "thehindu.com", "naukri.com", "glassdoor.co.in", "indeed.com", "ambitionbox.com", "scribd.com", "slideshare.net",
    "edurank.org", "topuniversities.com", "timeshighereducation.com", "universityguru.com", "nirfindia.org", "ugc.gov.in",
    "aicte-india.org", "facilities.aicte-india.org",
    # indiastudychannel.com, asklaila.com, webindia123.com, colleges9.in and collegeandfees.com were seen slipping
    # through as if they were someone's own site on a live "engineering colleges in <neighbourhood>" run - each
    # lists many unrelated colleges per area/category, same as the directories above.
    "indiastudychannel.com", "asklaila.com", "webindia123.com", "colleges9.in", "collegeandfees.com", "fullhyderabad.com",
    "vedantu.com", "telugucolleges.com", "educationdunia.com", "careercartz.com",
)
# Words that say what kind of place it is, not which one. They don't identify the right website.
GENERIC_WORDS = {
    "college", "colleges", "junior", "jr", "senior", "sr", "seniors", "institute", "institutes", "institution", "engineering",
    "engineer", "technology", "technological", "technical", "tech", "university", "school", "schools", "academy", "polytechnic",
    "campus", "the", "of", "for", "and", "women", "womens", "girls", "boys", "men", "sri", "shri", "new", "old", "aieee",
    "hospital", "clinic", "centre", "center", "main", "branch", "hyderabad", "telangana", "india", "private", "limited", "ltd",
    "public", "govt", "government", "model", "english", "medium", "education", "educational", "society", "trust",
}


def tokens(name: str) -> list[str]:
    """The distinctive words of a name, e.g. "BVRIT Hyderabad College of Engineering for Women" -> ["bvrit"]."""
    return [t for t in re.findall(r"[a-z0-9]+", name.lower()) if len(t) >= 3 and t not in GENERIC_WORDS]


SECOND_LEVEL = {"ac", "edu", "co", "org", "gov", "net", "com", "nic"}


def root_domain(host: str) -> str:
    """csbs.griet.ac.in -> griet.ac.in (the main site, not a department's subdomain)."""
    labels = host.lower().split(".")
    if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in SECOND_LEVEL:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:]) if len(labels) >= 2 else host


def _home(url: str) -> str:
    u = urlparse(url)
    return f"{u.scheme}://{root_domain(domain_of(url))}/"


# A URL path shaped like "...-colleges-in-<place>" or "list-of-.../top-10-..." names a CATEGORY, not a place - no
# real institution's own site has a page like that about itself. This catches the long tail of directory sites a
# fixed domain blocklist can never keep up with (chunocollege.com, findmycollege.com, studyclap.com and others seen
# slipping through on live runs, each a direct hit for this exact pattern) without needing to list every one by name.
_LISTING_PATH_RE = re.compile(
    r"(colleges?|institutes?|universit(y|ies)|schools?|hospitals?|hotels?|restaurants?)s?[-/](in|near)[-/]"
    r"|/(top|best)[-/]\d*[-/]?.*(colleges?|institutes?|universit(y|ies)|schools?)"
    r"|/list[-/]of[-/]", re.I)


def _is_official_candidate(url: str) -> bool:
    d = domain_of(url)
    path = urlparse(url).path.lower()
    if not d or url.lower().split("?")[0].endswith((".pdf", ".doc", ".docx", ".xls", ".xlsx")):
        return False
    if _LISTING_PATH_RE.search(path):
        return False
    return not any(d == bad or d.endswith("." + bad) for bad in NOT_OFFICIAL)


def pick_official(name: str, hits: list[dict]) -> str | None:
    """From search results, return the home page of the first one that looks like this place's own website."""
    toks = tokens(name)
    if not toks:
        return None
    for h in hits:
        url = h.get("href") or ""
        if not _is_official_candidate(url):
            continue
        squashed = re.sub(r"[^a-z0-9]", "", domain_of(url))
        title = (h.get("title") or "").lower()
        # The domain contains a distinctive word of the name (griet.ac.in for "GRIET College") ...
        if any(t in squashed for t in toks):
            return _home(url)
        # ... or the page title mentions at least two of them (for names with several distinctive words).
        if len(toks) >= 2 and sum(t in title for t in toks) >= 2:
            return _home(url)
    return None


# ---------------------------------------------------------------- guessing the website without a search engine
# Indian colleges almost always use their initials: GRIET -> griet.ac.in, CBIT -> cbit.ac.in, VNR VJIET -> vnrvjiet.ac.in.
# Checking a handful of such addresses needs no search engine (so no blocking or pacing), and each guess is verified by
# reading the page, so a wrong guess is rejected rather than shown.
GUESS_TLDS = (".ac.in", ".edu.in", ".in", ".edu", ".org", ".com")
EDU_WORDS = re.compile(r"college|institute|university|engineering|technolog|polytechnic|academy|school", re.I)
PLACE_WORDS = {"hyderabad", "secunderabad", "telangana"}


def guess_stems(name: str, aliases=()) -> list[str]:
    """Likely domain names (no ending) for an institution, most likely first."""
    stems: list[str] = []
    for n in (name, *aliases):
        words = [w for w in _words(n) if w not in LINK_WORDS]
        if not words:
            continue
        for t in tokens(n):                                   # a distinctive one-word name: "griet", "bvrit", "vjiet"
            if t.isalpha() and 3 <= len(t) <= 12:
                stems.append(t)
        if len(words) >= 2:
            stems.append("".join(w[0] for w in words))                         # initials: "gokaraju rangaraju institute..." -> griet
            stems.append("".join(w[0] for w in words if w not in GENERIC_WORDS))  # initials of the distinctive words
            stems.append("".join(words[:2]))                                   # "malla reddy" -> mallareddy
            stems.append("".join(words[:3]))
    seen, out = set(), []
    for st in stems:
        if 3 <= len(st) <= 30 and st.isalnum() and st not in seen:
            seen.add(st)
            out.append(st)
    return out[:6]


def verify_page(text: str, name: str, aliases=(), area: str = "") -> bool:
    """Is this page really that institution's? It must read like an education site AND mention the place's name (a
    distinctive word or its initials); a bare initials match must also mention the city or area, since short
    initials collide with unrelated organisations."""
    low = text.lower()
    if not EDU_WORDS.search(low):
        return False
    words = set(re.findall(r"[a-z0-9]+", low))
    strong = [t for n in (name, *aliases) for t in tokens(n) if len(t) >= 4]
    if sum(t in words for t in set(strong)) >= (2 if len(set(strong)) >= 2 else 1):
        return True
    initials = {"".join(w[0] for w in _words(n) if w not in LINK_WORDS) for n in (name, *aliases)}
    where = {w for w in re.findall(r"[a-z]{4,}", area.lower())} | PLACE_WORDS
    return any(len(i) >= 3 and i in words for i in initials) and bool(where & words)


def guess_website(fetcher, name: str, area: str = "", aliases=(), resolve=socket.gethostbyname) -> str | None:
    """Try the likely addresses; return the home page of the first one that exists and is verified, else None."""
    for stem in guess_stems(name, aliases):
        for tld in GUESS_TLDS:
            host = stem + tld
            try:
                resolve(host)
            except OSError:
                continue
            for scheme in ("https", "http"):
                html, _ = fetcher.get_html(f"{scheme}://{host}/")
                if html:
                    head = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html[:60000], flags=re.S | re.I)
                    if verify_page(re.sub(r"<[^>]+>", " ", head)[:6000], name, aliases, area):
                        return f"{scheme}://{host}/"
                    break  # the address exists but is someone else's: try the next one
    return None


QUERY_GAP = 5.0  # seconds between the tries for one place (search engines limit quick repeats)
# For the last try: engines that answered when the usual first choices returned lists without the official site.
OTHER_ENGINES = ("startpage", "mojeek", "google", "duckduckgo")


def find_website(name: str, where: str, say=lambda msg: None, aliases=(), gap: float = QUERY_GAP) -> tuple[str | None, int]:
    """Search the web for the place's official site. Returns (website or None, results of the first search).

    Tries up to three searches, pausing between them: "<name> <area>", then the other names the map gave the same place,
    then "<name>" alone. Zero results from the first search for a real place name usually means the search engines are
    limiting us, not that it has no site, so the caller can wait and retry.
    Raises SearchBlocked when every search engine refuses outright.

    `gap` defaults to the fixed, free-engine-safe QUERY_GAP, but callers that already know an official Brave/Google
    key is configured (jobs.py's _current_gap()) pass a much shorter one - the 5s pause between THIS function's own
    up to 3 queries was being applied even when every query underneath was already a fast, unthrottled official API
    call, for no reason."""
    area = where.split(",")[0].strip()
    queries = [f"{name} {area} official website"] + [f"{a} official website" for a in list(aliases)[:2]] + [f"{name} official website"]
    first_hits = 0
    tries = list(dict.fromkeys(queries))[:3]
    for i, q in enumerate(tries):
        if i:
            time.sleep(gap)
        last = i == len(tries) - 1 and i > 0
        hits = web_search(q, "in-en", 8, say, OTHER_ENGINES) if last else web_search(q, "in-en", 8, say)
        if i == 0:
            first_hits = len(hits)
            if not hits:
                return None, 0
        for candidate_name in (name, *aliases):
            url = pick_official(candidate_name, hits)
            if url:
                return url, first_hits
    return None, first_hits


# ---------------------------------------------------------------- the same institution mapped more than once
LINK_WORDS = {"of", "and", "for", "the", "&"}
SAME_NAME_M = 150      # the same name this close together is one place
SAME_PLACE_M = 300     # a short name, an abbreviation or the same website this close together is one place


def _distance_m(a: dict, b: dict) -> float:
    la1, lo1, la2, lo2 = (float(a["Latitude"] or 0), float(a["Longitude"] or 0), float(b["Latitude"] or 0), float(b["Longitude"] or 0))
    dx = (lo2 - lo1) * 111320 * math.cos(math.radians((la1 + la2) / 2))
    dy = (la2 - la1) * 110540
    return math.hypot(dx, dy)


def _words(name: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", name.lower())


def _is_abbreviation(short: str, long_name: str) -> bool:
    """True if a word of `short` is the initials of a run of words in `long_name`:
    "vnr vjiet college" vs "VNR Vignana Jyothi Institute of Engineering and Technology" (V J I E T = vjiet)."""
    words = [w for w in _words(long_name) if w not in LINK_WORDS]
    for t in _words(short):
        if len(t) < 3 or not t.isalpha():
            continue
        for start in range(len(words)):
            initials = ""
            for w in words[start:]:
                initials += w[0]
                if initials == t:
                    return True
                if len(initials) >= len(t):
                    break
    return False


def same_place_by_name(a: str, b: str) -> str | None:
    """How two listings' names say they are one place: "same" (identical), or "similar" (one is a short form of the
    other, or an abbreviation of it). None if the names don't suggest it."""
    wa, wb = _words(a), _words(b)
    if wa == wb:
        return "same"
    ta, tb = set(tokens(a)), set(tokens(b))
    if ta and tb and (ta <= tb or tb <= ta):   # "BVRIT College" is a short form of "BVRIT Hyderabad College of Engineering..."
        return "similar"
    if _is_abbreviation(a, b) or _is_abbreviation(b, a):
        return "similar"
    return None


def merge_duplicates(rows: list[dict]) -> tuple[list[dict], int]:
    """The map often lists one institution more than once ("griet college" and "GRIET COLLEGE", or a short and a long
    name). Two listings are one place when they are close together AND either have the same name, one name is a short
    form or abbreviation of the other, or they share a website. Keep the one with the longest name, fill in anything only
    the duplicate had (website, phone, email...), and note the other name. Separate branches of a chain stay separate."""
    kept: list[dict] = []
    merged = 0
    for row in sorted(rows, key=lambda r: -len(r.get("Name", ""))):
        site = domain_of(row["Website"]) if row.get("Website") else ""
        twin = None
        for k in kept:
            d = _distance_m(k, row)
            if d > SAME_PLACE_M:
                continue
            k_site = domain_of(k["Website"]) if k.get("Website") else ""
            how = same_place_by_name(k.get("Name", ""), row.get("Name", ""))
            if (site and k_site == site) or (how == "similar") or (how == "same" and d <= SAME_NAME_M):
                twin = k
                break
        if twin is None:
            kept.append(row)
            continue
        merged += 1
        if row.get("Name", "").lower() != twin.get("Name", "").lower():
            rest = twin.get("Other Details", "")
            twin["Other Details"] = (f"Also mapped as: {row['Name']}" + (f"; {rest}" if rest else ""))[:500]
            twin.setdefault("_aliases", []).append(row["Name"])  # used to look the website up under every name it has
        for col in ("Website", "Website Source", "Phone", "Email", "Address", "Postcode", "Opening Hours"):  # keep what only the duplicate had
            if not twin.get(col) and row.get(col):
                twin[col] = row[col]
    order = {id(r): i for i, r in enumerate(rows)}
    kept.sort(key=lambda r: order.get(id(r), 0))
    return kept, merged
