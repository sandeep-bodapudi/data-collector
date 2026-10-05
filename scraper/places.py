"""Place listings (temples, hospitals, schools, ...) from OpenStreetMap - free, open data, no login."""
import time

import requests

from .fetch import USER_AGENT

NOMINATIM = "https://nominatim.openstreetmap.org/search"
PHOTON = "https://photon.komoot.io/api/"
# Public Overpass servers; tried in order when one is busy.
OVERPASS_SERVERS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
HEADERS = {"User-Agent": USER_AGENT}

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

PLACE_COLUMNS = ["Name", "Category", "Address", "City", "State", "Postcode", "Phone", "Email",
                 "Website", "Opening Hours", "Latitude", "Longitude", "Google Maps Link", "Other Details"]


class PlaceError(Exception):
    pass


def _geocode_nominatim(location: str) -> dict | None:
    r = requests.get(NOMINATIM, params={"q": location, "format": "json", "limit": 1}, headers=HEADERS, timeout=30)
    r.raise_for_status()
    data = r.json()
    return data[0] if data else None


def _geocode_photon(location: str) -> dict | None:
    """Backup geocoder (also OpenStreetMap data); used when Nominatim limits cloud servers."""
    r = requests.get(PHOTON, params={"q": location, "limit": 1}, headers=HEADERS, timeout=30)
    r.raise_for_status()
    feats = r.json().get("features", [])
    if not feats:
        return None
    p = feats[0]["properties"]
    lon, lat = feats[0]["geometry"]["coordinates"]
    # Photon extent is [minLon, maxLat, maxLon, minLat]; fall back to ~10 km around the point.
    w, n, e, s = p.get("extent") or [lon - 0.1, lat + 0.1, lon + 0.1, lat - 0.1]
    return {"osm_id": p["osm_id"], "osm_type": {"R": "relation", "W": "way", "N": "node"}.get(p.get("osm_type"), "node"),
            "boundingbox": [s, n, w, e]}


def geocode(location: str) -> dict:
    for finder in (_geocode_nominatim, _geocode_photon):
        try:
            found = finder(location)
        except requests.RequestException:
            continue
        if found:
            return found
    raise PlaceError(f"Location not found: {location}. Try adding the state or country, e.g. 'Tirupati, India'.")


def _area_clause(geo: dict) -> tuple[str, str]:
    """Return (prefix statement, filter suffix) restricting the query to the location."""
    osm_id = int(geo["osm_id"])
    if geo["osm_type"] == "relation":
        return f"area({3600000000 + osm_id})->.a;", "(area.a)"
    if geo["osm_type"] == "way":
        return f"area({2400000000 + osm_id})->.a;", "(area.a)"
    s, n, w, e = geo["boundingbox"]
    return "", f"({s},{w},{n},{e})"


def _overpass(query: str) -> list[dict]:
    last = ""
    for attempt in range(2):
        for server in OVERPASS_SERVERS:
            try:
                r = requests.post(server, data={"data": query}, headers=HEADERS, timeout=180)
            except requests.RequestException as e:
                last = type(e).__name__
                continue
            if r.status_code == 200:
                return r.json().get("elements", [])
            last = f"HTTP {r.status_code}"
            if r.status_code not in (429, 502, 503, 504):
                break
        time.sleep(10)
    raise PlaceError(f"OpenStreetMap servers are busy ({last}). Please try again in a few minutes.")


def _addr(tags: dict) -> str:
    if tags.get("addr:full"):
        return tags["addr:full"]
    parts = [tags.get("addr:housenumber"), tags.get("addr:street"), tags.get("addr:suburb") or tags.get("addr:place")]
    return ", ".join(p for p in parts if p)


def search_places(category: str, location: str, name_filter: str = "", limit: int = 500) -> list[dict]:
    geo = geocode(location)
    prefix, area = _area_clause(geo)
    name_part = f'[name~"{name_filter.replace(chr(34), "")}",i]' if name_filter else "[name]"
    stmts = "".join(f"nwr{f}{name_part}{area};" for f in CATEGORIES[category])
    query = f"[out:json][timeout:120];{prefix}({stmts});out center tags {int(limit)};"
    elements = _overpass(query)

    rows, seen = [], set()
    for el in elements:
        tags = el.get("tags", {})
        lat = el.get("lat") or el.get("center", {}).get("lat")
        lon = el.get("lon") or el.get("center", {}).get("lon")
        key = (tags.get("name", "").lower(), round(lat or 0, 3), round(lon or 0, 3))
        if key in seen:
            continue
        seen.add(key)
        used = {"name", "phone", "contact:phone", "email", "contact:email", "website", "contact:website",
                "opening_hours", "addr:city", "addr:state", "addr:postcode", "addr:full", "addr:housenumber",
                "addr:street", "addr:suburb", "addr:place"}
        other = "; ".join(f"{k}={v}" for k, v in tags.items() if k not in used and not k.startswith("name:"))
        rows.append({
            "Name": tags.get("name", ""),
            "Category": category,
            "Address": _addr(tags),
            "City": tags.get("addr:city", ""),
            "State": tags.get("addr:state", ""),
            "Postcode": tags.get("addr:postcode", ""),
            "Phone": tags.get("phone") or tags.get("contact:phone", ""),
            "Email": tags.get("email") or tags.get("contact:email", ""),
            "Website": tags.get("website") or tags.get("contact:website", ""),
            "Opening Hours": tags.get("opening_hours", ""),
            "Latitude": lat,
            "Longitude": lon,
            "Google Maps Link": f"https://www.google.com/maps?q={lat},{lon}" if lat else "",
            "Other Details": other[:500],
        })
    return rows
