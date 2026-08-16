"""Offline Ghana geo-reference data: regions, districts and helpers.

Pure stdlib + JSON, no Qt — so it is import-safe for tests and the map
widget alike. Boundary polygons come from the bundled
``ghana_regions.geojson`` (geoBoundaries, ODbL); the district lookup from
``ghana_districts.json``. Loads are cached for the process.

The canonical region names are the post-2019 sixteen (``GHANA_REGIONS``).
A small alias map normalises common spellings/legacy names so user input
and external data line up with the bundled polygons.
"""

import json

from . import paths

# Canonical post-2019 sixteen regions (sorted), the names used everywhere.
GHANA_REGIONS = (
    "Ahafo", "Ashanti", "Bono", "Bono East", "Central", "Eastern",
    "Greater Accra", "North East", "Northern", "Oti", "Savannah",
    "Upper East", "Upper West", "Volta", "Western", "Western North",
)

# Lowercased alias -> canonical region. Covers the " Region" suffix, legacy
# names and a few common variants so normalisation is forgiving.
_ALIASES = {
    "greater accra region": "Greater Accra",
    "accra": "Greater Accra",
    "brong ahafo": "Bono",          # split in 2019 into Bono/Bono East/Ahafo
    "brong-ahafo": "Bono",
    "western north region": "Western North",
}


def _canon_key(name):
    return (name or "").strip().lower().replace("_", " ")


def normalize_region(name):
    """Map a free-text region name to a canonical ``GHANA_REGIONS`` entry.

    Returns the canonical name, or None when nothing plausible matches.
    """
    if not name:
        return None
    key = _canon_key(name)
    # direct canonical match
    for r in GHANA_REGIONS:
        if key == r.lower():
            return r
    # strip a trailing " region"
    if key.endswith(" region"):
        stripped = key[:-len(" region")].strip()
        for r in GHANA_REGIONS:
            if stripped == r.lower():
                return r
    return _ALIASES.get(key)


_REGIONS_CACHE = None
_DISTRICTS_CACHE = None


def load_regions_geojson(path=None):
    """Parsed region FeatureCollection (cached), or None if unavailable.

    Each feature's ``properties.region`` is normalised to a canonical name.
    """
    global _REGIONS_CACHE
    if _REGIONS_CACHE is not None:
        return _REGIONS_CACHE or None
    p = path or paths.ghana_geojson_path()
    try:
        with open(p) as fh:
            data = json.load(fh)
    except Exception:
        _REGIONS_CACHE = {}
        return None
    for f in data.get("features", []):
        props = f.setdefault("properties", {})
        canon = normalize_region(props.get("region"))
        if canon:
            props["region"] = canon
    _REGIONS_CACHE = data
    return data


def load_districts(path=None):
    """Parsed ``{region: [districts...]}`` lookup (cached); {} if unavailable."""
    global _DISTRICTS_CACHE
    if _DISTRICTS_CACHE is not None:
        return _DISTRICTS_CACHE
    p = path or paths.ghana_districts_path()
    try:
        with open(p) as fh:
            raw = json.load(fh)
    except Exception:
        _DISTRICTS_CACHE = {}
        return _DISTRICTS_CACHE
    out = {}
    for region, districts in raw.items():
        canon = normalize_region(region) or region
        out[canon] = list(districts)
    _DISTRICTS_CACHE = out
    return out


def districts_for(region):
    """Sorted district list for a region (canonical or alias), or []."""
    canon = normalize_region(region) or region
    return list(load_districts().get(canon, []))


def _iter_coords(features):
    for f in features:
        geom = f.get("geometry") or {}
        gtype = geom.get("type")
        coords = geom.get("coordinates") or []
        polys = coords if gtype == "MultiPolygon" else [coords]
        for poly in polys:
            for ring in poly:
                for pt in ring:
                    yield pt[0], pt[1]


def bbox_of(features):
    """(min_lon, min_lat, max_lon, max_lat) over all features, or None."""
    min_lon = min_lat = float("inf")
    max_lon = max_lat = float("-inf")
    found = False
    for lon, lat in _iter_coords(features):
        found = True
        min_lon = min(min_lon, lon)
        max_lon = max(max_lon, lon)
        min_lat = min(min_lat, lat)
        max_lat = max(max_lat, lat)
    if not found:
        return None
    return (min_lon, min_lat, max_lon, max_lat)


def _point_in_ring(lon, lat, ring):
    """Ray-casting point-in-polygon for a single ``[ [lon,lat], ... ]`` ring."""
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if ((yi > lat) != (yj > lat)) and \
                (lon < (xj - xi) * (lat - yi) / ((yj - yi) or 1e-12) + xi):
            inside = not inside
        j = i
    return inside


def _point_in_geometry(lon, lat, geom):
    """True if (lon, lat) falls inside a Polygon/MultiPolygon (holes honoured)."""
    gtype = geom.get("type")
    coords = geom.get("coordinates") or []
    polys = coords if gtype == "MultiPolygon" else [coords]
    for poly in polys:
        if not poly:
            continue
        if _point_in_ring(lon, lat, poly[0]):
            if not any(_point_in_ring(lon, lat, hole) for hole in poly[1:]):
                return True
    return False


def region_at(lon, lat, path=None):
    """Canonical region whose polygon contains (lon, lat), or None.

    Lets an interactive map turn a clicked GPS point into its administrative
    region without any external service — the collection site stays the
    precise point; the region is just the polygon it lands in.
    """
    data = load_regions_geojson(path)
    if not data:
        return None
    for f in data.get("features", []):
        if _point_in_geometry(lon, lat, f.get("geometry") or {}):
            return f.get("properties", {}).get("region")
    return None


def _ring_centroid(ring):
    """Shoelace area-weighted centroid of a ring; vertex mean if degenerate."""
    area = cx = cy = 0.0
    n = len(ring)
    for i in range(n):
        x0, y0 = ring[i][0], ring[i][1]
        x1, y1 = ring[(i + 1) % n][0], ring[(i + 1) % n][1]
        cross = x0 * y1 - x1 * y0
        area += cross
        cx += (x0 + x1) * cross
        cy += (y0 + y1) * cross
    if abs(area) < 1e-12:
        if not ring:
            return None
        return (sum(p[0] for p in ring) / n, sum(p[1] for p in ring) / n)
    area *= 0.5
    return (cx / (6 * area), cy / (6 * area))


def region_centroid(region, path=None):
    """(lon, lat) centre of a region's largest polygon, or None.

    Used to "snap" an offline name search to roughly the middle of a region
    — coarser than a precise GPS point, then draggable to fine-tune.
    """
    canon = normalize_region(region)
    data = load_regions_geojson(path)
    if not canon or not data:
        return None
    for f in data.get("features", []):
        if f.get("properties", {}).get("region") != canon:
            continue
        geom = f.get("geometry") or {}
        gtype = geom.get("type")
        coords = geom.get("coordinates") or []
        polys = coords if gtype == "MultiPolygon" else [coords]
        best, best_len = None, -1
        for poly in polys:
            if poly and len(poly[0]) > best_len:
                best, best_len = poly[0], len(poly[0])
        if best:
            return _ring_centroid(best)
    return None


def search_names(path=None):
    """Sorted offline place names for a search completer.

    Regions plus every district as ``"District (Region)"`` so the same box
    matches both granularities.
    """
    names = list(GHANA_REGIONS)
    for region, districts in load_districts(path).items():
        for d in districts:
            names.append("%s (%s)" % (d, region))
    return sorted(names)


def locate_offline(name, path=None):
    """Best offline ``(lon, lat, region, district)`` for a name, or None.

    Resolves the name to a region (districts map to their parent region) and
    snaps to that region's centroid — deliberately coarse; the user drags to
    refine, or uses online lookup for a precise point. When the query matches
    a district (e.g. ``"Adansi North (Ashanti)"`` or just ``"Adansi North"``)
    the matched district name is returned too; it is ``""`` for a region query.
    """
    if not name:
        return None
    key = name.strip()
    region = normalize_region(key)
    district = ""
    if not region:
        low = key.lower()
        for reg, districts in load_districts(path).items():
            for d in districts:
                if low in (d.lower(), ("%s (%s)" % (d, reg)).lower()):
                    region = reg
                    district = d
                    break
            if region:
                break
    if not region:
        return None
    c = region_centroid(region, path)
    if not c:
        return None
    return (c[0], c[1], region, district)
