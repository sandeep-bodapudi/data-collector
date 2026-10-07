"""Place listings (temples, hospitals, schools, ...) from OpenStreetMap - free, open data, no login."""
import re
import time

import requests

from .extract import normalize_phone


# Common misspellings of Indian state/city names worth correcting before asking the map to geocode them - a
# structured geocoder like Nominatim is much less typo-tolerant than a search engine, so a simple misspelling
# ("Telengana" for "Telangana") can make it return nothing useful at all for what is otherwise a perfectly
# well-known place.
LOCATION_SPELLING = {"telengana": "telangana", "andra pradesh": "andhra pradesh", "bangalore": "bengaluru"}


def _fix_spelling(location: str) -> str:
    parts = [LOCATION_SPELLING.get(p.strip().lower(), p.strip()) for p in location.split(",")]
    return ", ".join(parts)


NOMINATIM = "https://nominatim.openstreetmap.org/search"
PHOTON = "https://photon.komoot.io/api/"
# Public Overpass servers; tried in order when one is busy.
OVERPASS_SERVERS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]
SERVER_TIMEOUT = 75  # seconds to wait for one server before trying the next
# OpenStreetMap services require an honest, identifying User-Agent (fake browser agents get HTTP 406).
HEADERS = {"User-Agent": "OneBridgeDataCollector/2.0 (internal business research tool)"}

# Category label -> list of OSM tag filters (any match counts).
CATEGORIES = {
    "Hindu temples": ['[amenity=place_of_worship][religion=hindu]'],
    "Churches": ['[amenity=place_of_worship][religion=christian]'],
    "Mosques": ['[amenity=place_of_worship][religion=muslim]'],
    "Gurudwaras": ['[amenity=place_of_worship][religion=sikh]'],
    "Buddhist / Jain temples": ['[amenity=place_of_worship][religion=buddhist]', '[amenity=place_of_worship][religion=jain]'],
    "All places of worship": ['[amenity=place_of_worship]'],
    "Hospitals": ['[amenity=hospital]'],
    "Clinics & doctors": ['[amenity=clinic]', '[amenity=doctors]'],
    "Pharmacies": ['[amenity=pharmacy]'],
    "Schools": ['[amenity=school]'],
    "Colleges & universities": ['[amenity=college]', '[amenity=university]'],
    # Colleges whose name says engineering or technology (plus the well-known Hyderabad short names, which the map
    # often uses on its own, e.g. "griet college"). Junior colleges and schools are left out.
    # Many engineering colleges have no "engineering" in the map name (e.g. "CMR College", "Malla Reddy Institute"), so the
    # map query is wide (anything called a college/institute/university) and ENGINEERING_NOT drops the clearly
    # non-engineering ones afterwards.
    "Engineering colleges": ['[amenity~"^(college|university)$"][name~"engineer|tech|polytechnic|jntu|iiit|vjiet|griet|bvrit|cbit|mgit|college|institute|university|vidya|iit|nit",i]',
                             '[office=educational_institution][name~"engineer|tech|college|institute|university",i]'],
    "Restaurants": ['[amenity=restaurant]'],
    "Cafes": ['[amenity=cafe]'],
    "Hotels": ['[tourism=hotel]', '[tourism=guest_house]'],
    "Banks": ['[amenity=bank]'],
    "ATMs": ['[amenity=atm]'],
    "Supermarkets & shops": ['[shop=supermarket]', '[shop=convenience]'],
    "Offices / companies": ['[office]'],
    "IT companies": ['[office=it]', '[office=company][industry~"IT|software",i]'],
    "Factories / industrial": ['[man_made=works]', '[landuse=industrial][name]'],
    "Petrol pumps": ['[amenity=fuel]'],
    "Tourist attractions": ['[tourism=attraction]', '[tourism=museum]'],
    "Police stations": ['[amenity=police]'],
    "Government offices": ['[office=government]', '[amenity=townhall]'],
    # Added after live-checking each tag against a real area (Hyderabad) and confirming real, non-zero results -
    # not guessed. A few more candidates (dentists, opticians, libraries, lawyers, travel agents...) were tried
    # the same way but the map servers were overloaded (504s) partway through, so those are deferred rather than
    # added on an unconfirmed guess - ask to have them checked again when the servers aren't busy.
    "Veterinary clinics": ['[amenity=veterinary]'],
    "Furniture stores": ['[shop=furniture]'],
    "Railway stations": ['[railway=station]'],
    "Accountants": ['[office=accountant]'],
    "Car wash": ['[amenity=car_wash]'],
    "Courthouses": ['[amenity=courthouse]'],
    "Parks": ['[leisure=park]'],
    "Co-working spaces": ['[office=coworking]'],
    "Dry cleaners & laundry": ['[shop=laundry]'],
}

# Names that say it is not an engineering college (unless the name also says engineering/technology).
ENGINEERING_NOT = re.compile(r"junior|\bjr\b|intermediate|degree college|pharm|medical|dental|nursing|\blaw\b|b\.? ?ed\b|ayurved|homeo|"
                             r"agricultur|veterinar|physio|\bschool\b|commerce|arts and|hotel management|\bmba\b|\bpg college|coaching|tutorial", re.I)
ENGINEERING_YES = re.compile(r"engineer|technolog|polytechnic|\btech\b|jntu|iiit|vjiet|griet|bvrit|cbit|mgit", re.I)

# What the sheet shows: only real, useful data. Map coordinates, opening hours, internal notes and how each
# value was found go to the run log, not the sheet.
OUTPUT_COLUMNS = ["Name", "Phone", "Email", "Website", "Address", "Google Maps Link"]
PLACE_COLUMNS = ["Name", "Category", "Address", "City", "State", "Postcode", "Phone", "Email",
                 "Website", "Opening Hours", "Latitude", "Longitude", "Google Maps Link", "Other Details"]


class PlaceError(Exception):
    pass


# How far to search around a place that is only a point on the map (metres), by what kind of place it is.
AREA_PLACE_TYPES = {"city": 12000, "town": 6000, "municipality": 8000, "suburb": 2500, "neighbourhood": 2000,
                    "neighborhood": 2000, "quarter": 2000, "city_district": 4000, "borough": 4000, "village": 3000,
                    "hamlet": 2000, "locality": 2500, "isolated_dwelling": 1500, "district": 15000, "county": 20000,
                    "state_district": 20000, "region": 30000}
POINT_RADIUS = 3000  # when only one business or building matched, search this far around it
BIG_BOX_DEGREES = 0.008  # about a kilometre


def _nominatim(location: str, limit: int = 8) -> list[dict]:
    r = requests.get(NOMINATIM, params={"q": location, "format": "json", "limit": limit}, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()


def _bbox_from_point(lat: float, lon: float, radius_m: float) -> tuple[float, float, float, float]:
    """An approximate (south, west, north, east) box around a point - good enough to bound a fallback search,
    not for anything that needs real precision. 1 degree of latitude is ~111,320m everywhere; a degree of
    longitude shrinks toward the poles, so it's scaled by the latitude's cosine."""
    import math
    d_lat = radius_m / 111320
    d_lon = radius_m / (111320 * max(0.1, math.cos(math.radians(lat))))
    return lat - d_lat, lon - d_lon, lat + d_lat, lon + d_lon


def _as_area(c: dict) -> dict | None:
    """Turn a geocoder result into somewhere we can search inside. Returns None for a single business or building:
    searching inside a shop's 10-metre outline finds nothing."""
    cls, typ = c.get("class"), c.get("type")
    s_, n_, w_, e_ = [float(x) for x in c["boundingbox"]]
    if cls == "boundary" and c.get("osm_type") in ("relation", "way"):
        # Kept too, even though Overpass itself only needs the osm_id - a fallback search (Nominatim/Wikidata,
        # used only when Overpass itself doesn't answer) needs some box to stay inside, and this one is exact.
        return {"kind": "area", "osm_type": c["osm_type"], "osm_id": int(c["osm_id"]), "bbox": (s_, w_, n_, e_)}
    if cls in ("place", "boundary"):
        if (n_ - s_) > BIG_BOX_DEGREES or (e_ - w_) > BIG_BOX_DEGREES:
            return {"kind": "box", "bbox": (s_, w_, n_, e_)}
        radius = AREA_PLACE_TYPES.get(typ, POINT_RADIUS)
        return {"kind": "around", "lat": float(c["lat"]), "lon": float(c["lon"]), "radius": radius,
                "bbox": _bbox_from_point(float(c["lat"]), float(c["lon"]), radius)}
    return None


def _geocode_photon(location: str) -> dict | None:
    """Backup geocoder (also OpenStreetMap data); used when Nominatim limits cloud servers."""
    r = requests.get(PHOTON, params={"q": location, "limit": 1}, headers=HEADERS, timeout=30)
    r.raise_for_status()
    feats = r.json().get("features", [])
    if not feats:
        return None
    p = feats[0]["properties"]
    lon, lat = feats[0]["geometry"]["coordinates"]
    label = ", ".join(x for x in (p.get("name"), p.get("city"), p.get("state"), p.get("country")) if x)
    extent = p.get("extent")  # [minLon, maxLat, maxLon, minLat]
    if extent and (abs(extent[2] - extent[0]) > BIG_BOX_DEGREES or abs(extent[1] - extent[3]) > BIG_BOX_DEGREES):
        w_, n_, e_, s_ = extent
        return {"kind": "box", "bbox": (s_, w_, n_, e_), "label": label}
    return {"kind": "around", "lat": lat, "lon": lon, "radius": POINT_RADIUS, "label": label, "approx": True,
            "bbox": _bbox_from_point(lat, lon, POINT_RADIUS)}


def geocode(location: str) -> dict:
    """Find the place to search in, preferring a whole area (suburb, town, district) over one business.

    "Bachupally, Hyderabad" used to match a single junior college, so the search covered 10 metres and found nothing.
    Now: look for an area in the full text; if the matches are all businesses, retry with just the first part
    ("Bachupally"); if that fails too, try the backup geocoder (it has its own fuzzy matching, and sometimes
    resolves a typo or area Nominatim's stricter parser missed); only then search a few kilometres around the
    best match and say so, rather than silently turning "a whole state" into a few km around an unrelated shop."""
    query_text = _fix_spelling(location)
    parts = [x.strip() for x in query_text.split(",") if x.strip()]
    attempts = [query_text] + ([parts[0]] if len(parts) > 1 else [])
    first_match = None
    for q in attempts:
        try:
            cands = _nominatim(q)
        except requests.RequestException:
            cands = []
        time.sleep(1.0)  # the free map search allows one request per second
        for c in cands:
            area = _as_area(c)
            if area:
                area["label"] = c.get("display_name", q)
                area["fallback"] = q != query_text
                return area
        if cands and first_match is None:
            first_match = cands[0]
    try:
        found = _geocode_photon(query_text)
    except requests.RequestException:
        found = None
    if found:
        found["fallback"] = False
        return found
    if first_match is not None:
        lat, lon = float(first_match["lat"]), float(first_match["lon"])
        return {"kind": "around", "lat": lat, "lon": lon, "radius": POINT_RADIUS, "bbox": _bbox_from_point(lat, lon, POINT_RADIUS),
                "label": first_match.get("display_name", location), "approx": True, "fallback": False}
    raise PlaceError(f"Location not found: {location}. Try the area name with the city or country, e.g. 'Bachupally, Hyderabad, India'.")


def describe_location(geo: dict, asked: str) -> str:
    """One plain sentence saying where the search really ran, so a wrong match is easy to spot."""
    label = geo.get("label", asked)
    if geo.get("approx"):
        return f"Couldn't find an area called “{asked}”, so searching about {geo['radius'] / 1000:g} km around: {label}"
    note = f" (matched from “{asked.split(',')[0].strip()}”)" if geo.get("fallback") else ""
    return f"Searching inside: {label}{note}"


def _area_clause(geo: dict) -> tuple[str, str]:
    """Return (prefix statement, filter suffix) restricting the query to the location."""
    if geo["kind"] == "area":
        base = 3600000000 if geo["osm_type"] == "relation" else 2400000000
        return f"area({base + int(geo['osm_id'])})->.a;", "(area.a)"
    if geo["kind"] == "box":
        s_, w_, n_, e_ = geo["bbox"]
        return "", f"({s_},{w_},{n_},{e_})"
    return "", f"(around:{int(geo['radius'])},{geo['lat']},{geo['lon']})"


def _overpass(query: str, notify=None) -> list[dict]:
    """One pass over the mirrors, not two - a dead or overloaded mirror answering the same way 15 seconds later is
    the usual case, not the exception, so a second identical pass mostly just doubles the wait. search_places()
    has two other free sources to fall back to now (see _fallback_nominatim/_fallback_wikidata below), so giving
    up on Overpass sooner and trying those is faster than retrying Overpass's own mirrors a second time."""
    last = ""
    for i, server in enumerate(OVERPASS_SERVERS):
        if notify and i:
            notify(f"The map service is busy - trying backup server {i + 1} of {len(OVERPASS_SERVERS)}…")
        try:
            r = requests.post(server, data={"data": query}, headers=HEADERS, timeout=SERVER_TIMEOUT)
        except requests.RequestException as e:
            last = type(e).__name__
            continue
        if r.status_code == 200:
            return r.json().get("elements", [])
        last = f"HTTP {r.status_code}"
        if r.status_code not in (429, 502, 503, 504):
            break
    raise PlaceError(f"OpenStreetMap's Overpass servers are busy or unreachable ({last}).")


def _addr(tags: dict) -> str:
    if tags.get("addr:full"):
        return tags["addr:full"]
    parts = [tags.get("addr:housenumber"), tags.get("addr:street"), tags.get("addr:suburb") or tags.get("addr:place")]
    return ", ".join(p for p in parts if p)


# Map data carries dozens of internal/technical tags per place (building=yes, type=multipolygon, wikidata=Q123,
# created_by=Merkaartor...). None of that means anything to someone reading the sheet. Only these are worth keeping,
# and only under a plain-English label - everything else is dropped rather than dumped in raw "key=value" form.
DETAIL_TAGS = {
    "operator": "Run by", "operator:type": "Type of operator", "short_name": "Short name", "old_name": "Formerly known as",
    "alt_name": "Also known as", "description": "Description", "note": "Note", "brand": "Chain/brand",
    "affiliation": "Affiliated with", "denomination": "Denomination", "religion": "Religion", "cuisine": "Cuisine",
    "healthcare": "Healthcare type", "healthcare:speciality": "Specialities", "beds": "Beds", "capacity": "Capacity",
    "isced:level": "Education level", "wikipedia": "Wikipedia",
}


def _other_details(tags: dict) -> str:
    bits = []
    for key, label in DETAIL_TAGS.items():
        v = tags.get(key)
        if v:
            bits.append(f"{label}: {v.split(':', 1)[-1] if key == 'wikipedia' else v}")
    return "; ".join(bits)[:500]


# A free-text hint word for Nominatim's plain search (it has no tag-filter syntax like Overpass), and, where a
# category maps onto a real Wikidata class, that class's QID (verified live against Wikidata's own search API
# before being written here - never guessed; a category with no verified QID just gets no Wikidata fallback,
# rather than a guess that might silently match the wrong thing).
FALLBACK_HINT = {
    "Hindu temples": ("temple", "Q842402"), "Churches": ("church", "Q16970"), "Mosques": ("mosque", "Q32815"),
    "Gurudwaras": ("gurudwara", "Q337986"), "Buddhist / Jain temples": ("temple", ("Q5393308", "Q2613100")),
    "All places of worship": ("place of worship", None), "Hospitals": ("hospital", "Q16917"),
    "Clinics & doctors": ("clinic", None), "Pharmacies": ("pharmacy", None), "Schools": ("school", None),
    "Colleges & universities": ("college", None), "Engineering colleges": ("engineering college", "Q1663017"),
    "Restaurants": ("restaurant", None), "Cafes": ("cafe", None), "Hotels": ("hotel", None), "Banks": ("bank", None),
    "ATMs": ("atm", None), "Supermarkets & shops": ("supermarket", None), "Offices / companies": ("office", None),
    "IT companies": ("IT company", None), "Factories / industrial": ("factory", None), "Petrol pumps": ("petrol station", None),
    "Tourist attractions": ("tourist attraction", None), "Police stations": ("police station", None),
    "Government offices": ("government office", None),
    "Veterinary clinics": ("veterinary clinic", None), "Furniture stores": ("furniture store", None),
    "Railway stations": ("railway station", None), "Accountants": ("accountant", None),
    "Car wash": ("car wash", None), "Courthouses": ("courthouse", None), "Parks": ("park", None),
    "Co-working spaces": ("co-working space", None), "Dry cleaners & laundry": ("dry cleaner", None),
}


def _in_bbox(lat, lon, bbox) -> bool:
    if lat is None or lon is None or not bbox:
        return True  # nothing to check against - keep it rather than silently drop a result
    s_, w_, n_, e_ = bbox
    return s_ <= lat <= n_ and w_ <= lon <= e_


def _photon_osm_tag(category: str) -> str | None:
    """The category's first, primary `[key=value]` Overpass filter translates directly into Photon's own
    server-side category filter (a second filter chained onto it, such as temples' own `[religion=hindu]`, has
    no Photon equivalent and is dropped - Photon then matches the broader tag alone, same loss of precision
    Nominatim's plain text search already has for that part). A regex filter (Engineering colleges' name
    pattern, or a `~` alternative list) has no Photon tag equivalent at all, so that category gets no tag filter
    and relies on the free-text hint word alone, same as Nominatim."""
    m = re.match(r'^\[(\w+)=([\w:]+)]', CATEGORIES[category][0])
    return f"{m.group(1)}:{m.group(2)}" if m else None


def _fallback_photon(category: str, geo: dict, name_filter: str, limit: int) -> list[dict]:
    """Photon (run by Komoot) reads the same OpenStreetMap data Overpass and Nominatim do, but it's a third,
    separately-run service - not down just because Overpass or Nominatim is. Unlike Nominatim's plain free-text
    search, Photon can filter by the category's own primary OSM tag server-side when that category boils down to
    a plain key=value (see _photon_osm_tag) - a real category match, not just a hopeful text search."""
    hint = FALLBACK_HINT.get(category, (category.lower(), None))[0]
    q = f"{name_filter} {hint}".strip() if name_filter else hint
    params = {"q": q, "limit": min(int(limit), 50)}
    bbox = geo.get("bbox")
    if bbox:
        s_, w_, n_, e_ = bbox
        params["bbox"] = f"{w_},{s_},{e_},{n_}"
    tag = _photon_osm_tag(category)
    if tag:
        params["osm_tag"] = tag
    r = requests.get(PHOTON, params=params, headers=HEADERS, timeout=30)
    r.raise_for_status()
    out = []
    for f in r.json().get("features", []):
        p = f.get("properties", {})
        coords = f.get("geometry", {}).get("coordinates", [None, None])
        lon, lat = coords[0], coords[1]
        tags = {"name": p.get("name", "")}
        for k, v in (("addr:city", p.get("city")), ("addr:state", p.get("state")), ("addr:postcode", p.get("postcode")),
                     ("addr:full", ", ".join(x for x in (p.get("street"), p.get("city"), p.get("state")) if x))):
            if v:
                tags[k] = v
        out.append({"tags": tags, "lat": lat, "lon": lon})
    return out


def _fallback_nominatim(category: str, geo: dict, name_filter: str, limit: int) -> list[dict]:
    """Same OpenStreetMap data Overpass reads, but through Nominatim's separate search service instead - run by
    the same project, but independent infrastructure, so it isn't necessarily down at the same time Overpass is.
    Plain free-text search, not a tag filter, so results are approximate: kept inside the searched area's box,
    but not guaranteed to actually be the right category - better than nothing when Overpass itself won't answer,
    not a replacement for it."""
    hint = FALLBACK_HINT.get(category, (category.lower(), None))[0]
    q = f"{name_filter} {hint}".strip() if name_filter else hint
    params = {"q": q, "format": "jsonv2", "extratags": 1, "addressdetails": 1, "limit": min(int(limit), 50)}
    bbox = geo.get("bbox")
    if bbox:
        s_, w_, n_, e_ = bbox
        params.update({"viewbox": f"{w_},{n_},{e_},{s_}", "bounded": 1})
    r = requests.get(NOMINATIM, params=params, headers=HEADERS, timeout=30)
    time.sleep(1.0)  # same free service as geocode() - one request per second
    r.raise_for_status()
    out = []
    for item in r.json():
        lat, lon = float(item["lat"]), float(item["lon"])
        tags = dict(item.get("extratags") or {})
        tags.setdefault("name", item.get("name") or item.get("display_name", "").split(",")[0])
        addr = item.get("address") or {}
        for k, v in (("addr:city", addr.get("city") or addr.get("town")), ("addr:state", addr.get("state")),
                     ("addr:postcode", addr.get("postcode")), ("addr:full", item.get("display_name"))):
            if v:
                tags.setdefault(k, v)
        out.append({"tags": tags, "lat": lat, "lon": lon})
    return out


WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"


def _fallback_wikidata(category: str, geo: dict, name_filter: str, limit: int) -> list[dict]:
    """Wikidata, not OpenStreetMap at all - a genuinely independent data source and service, so it doesn't share
    whatever took Overpass and Nominatim down. Only tried for a category with a verified class above, and only
    when a name was actually given to search for: Wikidata has nowhere near every real place OSM has, so used
    unfiltered it would make a "list everything here" search look emptier than it really is, not more complete.
    Not geographically bounded by the query itself (Wikidata has no simple way to ask "is this inside this area"
    the way Overpass/Nominatim do); instead, every match is fetched with its coordinates, if it has any, and
    filtered against the searched area's box afterwards, in Python."""
    qids = FALLBACK_HINT.get(category, (None, None))[1]
    if not qids or not name_filter:
        return []
    qids = (qids,) if isinstance(qids, str) else qids
    safe_name = name_filter.replace('"', "").replace("\\", "")[:100].lower()
    classes = " ".join(f"wd:{q}" for q in qids)
    query = f"""SELECT ?itemLabel ?coord ?website WHERE {{
      VALUES ?class {{ {classes} }}
      ?item wdt:P31/wdt:P279* ?class .
      ?item rdfs:label ?itemLabel .
      FILTER(CONTAINS(LCASE(?itemLabel), "{safe_name}"))
      FILTER(LANG(?itemLabel) = "en")
      OPTIONAL {{ ?item wdt:P625 ?coord. }}
      OPTIONAL {{ ?item wdt:P856 ?website. }}
    }} LIMIT {min(int(limit), 200)}"""
    r = requests.get(WIKIDATA_SPARQL, params={"query": query, "format": "json"},
                      headers={**HEADERS, "Accept": "application/sparql-results+json"}, timeout=30)
    r.raise_for_status()
    bbox = geo.get("bbox")
    out = []
    for row in r.json()["results"]["bindings"]:
        name = row.get("itemLabel", {}).get("value", "")
        coord = row.get("coord", {}).get("value", "")
        lat = lon = None
        m = re.match(r"Point\(([\d.\-]+) ([\d.\-]+)\)", coord)
        if m:
            lon, lat = float(m.group(1)), float(m.group(2))
        if not _in_bbox(lat, lon, bbox):
            continue
        tags = {"name": name}
        if row.get("website", {}).get("value"):
            tags["website"] = row["website"]["value"]
        out.append({"tags": tags, "lat": lat, "lon": lon})
    return out


def search_places(category: str, location: str, name_filter: str = "", limit: int = 500, notify=None, info=None) -> list[dict]:
    geo = geocode(location)
    if info:
        info(describe_location(geo, location))
    prefix, area = _area_clause(geo)
    name_part = f'[name~"{name_filter.replace(chr(34), "")}",i]' if name_filter else "[name]"
    stmts = "".join(f"nwr{f}{name_part}{area};" for f in CATEGORIES[category])
    query = f"[out:json][timeout:{SERVER_TIMEOUT}];{prefix}({stmts});out center tags {int(limit)};"
    try:
        elements = _overpass(query, notify)
    except PlaceError as e:
        if info:
            info(f"  {e} Trying other free map sources instead of giving up…")
        elements = []
        # Three independent services, tried in the order they're worth trusting: Photon can filter by the real
        # OSM category tag server-side (not just text), Nominatim can't but still searches real OSM data,
        # Wikidata is a different database entirely (last, since it only has famous places at all).
        for fallback, label, caveat in (
            (_fallback_photon, "Photon", "a separate OpenStreetMap search service - a real category match, not just a text search"),
            (_fallback_nominatim, "Nominatim", "a plain text search, not an exact category match - please check these before relying on them"),
            (_fallback_wikidata, "Wikidata", "a smaller, separate database - complete for famous places, not for most ordinary ones"),
        ):
            try:
                elements = fallback(category, geo, name_filter, limit)
            except requests.RequestException:
                elements = []
            if elements:
                if info:
                    info(f"  Found {len(elements)} match{'es' if len(elements) != 1 else ''} through {label} instead ({caveat}).")
                break
        if not elements:
            raise  # every fallback came up empty too - the original Overpass failure is the real story

    rows, seen = [], set()
    for el in elements:
        tags = el.get("tags", {})
        nm = tags.get("name", "")
        if category == "Engineering colleges" and ENGINEERING_NOT.search(nm) and not ENGINEERING_YES.search(nm):
            continue
        lat = el.get("lat") or el.get("center", {}).get("lat")
        lon = el.get("lon") or el.get("center", {}).get("lon")
        key = (tags.get("name", "").lower(), round(lat or 0, 3), round(lon or 0, 3))
        if key in seen:
            continue
        seen.add(key)
        rows.append({
            "Name": tags.get("name", ""),
            "Category": category,
            "Address": _addr(tags),
            "City": tags.get("addr:city", ""),
            "State": tags.get("addr:state", ""),
            "Postcode": tags.get("addr:postcode", ""),
            "Phone": normalize_phone(tags.get("phone") or tags.get("contact:phone", "")) if tags.get("phone") or tags.get("contact:phone") else "",
            "Email": tags.get("email") or tags.get("contact:email", ""),
            "Website": tags.get("website") or tags.get("contact:website", ""),
            "Opening Hours": tags.get("opening_hours", ""),
            "Latitude": lat,
            "Longitude": lon,
            "Google Maps Link": f"https://www.google.com/maps?q={lat},{lon}" if lat else "",
            "Other Details": _other_details(tags),
            "_aliases": [a.strip() for k in ("short_name", "alt_name", "old_name") for a in tags.get(k, "").split(";") if a.strip()],
        })
    if not rows and info:
        info("  Nothing in the map data for this category here. Try the area name on its own, a bigger nearby area, "
             "a broader category, or the Web search option, which also finds places the map doesn't list.")
    return rows
