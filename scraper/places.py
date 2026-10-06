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


def _as_area(c: dict) -> dict | None:
    """Turn a geocoder result into somewhere we can search inside. Returns None for a single business or building:
    searching inside a shop's 10-metre outline finds nothing."""
    cls, typ = c.get("class"), c.get("type")
    s_, n_, w_, e_ = [float(x) for x in c["boundingbox"]]
    if cls == "boundary" and c.get("osm_type") in ("relation", "way"):
        return {"kind": "area", "osm_type": c["osm_type"], "osm_id": int(c["osm_id"])}
    if cls in ("place", "boundary"):
        if (n_ - s_) > BIG_BOX_DEGREES or (e_ - w_) > BIG_BOX_DEGREES:
            return {"kind": "box", "bbox": (s_, w_, n_, e_)}
        return {"kind": "around", "lat": float(c["lat"]), "lon": float(c["lon"]), "radius": AREA_PLACE_TYPES.get(typ, POINT_RADIUS)}
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
    return {"kind": "around", "lat": lat, "lon": lon, "radius": POINT_RADIUS, "label": label, "approx": True}


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
        return {"kind": "around", "lat": float(first_match["lat"]), "lon": float(first_match["lon"]), "radius": POINT_RADIUS,
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
    last = ""
    for attempt in range(2):
        for i, server in enumerate(OVERPASS_SERVERS):
            if notify and (attempt or i):
                notify(f"The map service is busy - trying backup server {attempt * len(OVERPASS_SERVERS) + i + 1} of {2 * len(OVERPASS_SERVERS)}…")
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
        time.sleep(15)
    raise PlaceError(f"OpenStreetMap servers are busy ({last}). Please try again in a few minutes.")


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


def search_places(category: str, location: str, name_filter: str = "", limit: int = 500, notify=None, info=None) -> list[dict]:
    geo = geocode(location)
    if info:
        info(describe_location(geo, location))
    prefix, area = _area_clause(geo)
    name_part = f'[name~"{name_filter.replace(chr(34), "")}",i]' if name_filter else "[name]"
    stmts = "".join(f"nwr{f}{name_part}{area};" for f in CATEGORIES[category])
    query = f"[out:json][timeout:{SERVER_TIMEOUT}];{prefix}({stmts});out center tags {int(limit)};"
    elements = _overpass(query, notify)

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
