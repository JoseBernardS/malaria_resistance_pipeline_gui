"""Optional online geocoding via OpenStreetMap Nominatim.

The app is offline-first: this is the *only* module that reaches the network,
and every call is best-effort. Any failure (no connection, timeout, rate
limit, empty/garbled response) returns ``None`` so the offline flow is never
blocked. Queries are restricted to Ghana. Run it off the UI thread.
"""

import json
import urllib.parse
import urllib.request

_ENDPOINT = "https://nominatim.openstreetmap.org/search"
_REVERSE_ENDPOINT = "https://nominatim.openstreetmap.org/reverse"
# Nominatim's usage policy asks for an identifying User-Agent.
_USER_AGENT = "PfDrugResistance/1.0 (malaria surveillance app)"

# Ghana bounding box (min_lon, min_lat, max_lon, max_lat). Kept as a plain
# constant here so this module never imports gui.geo (avoids an import cycle
# between the geocoder and the offline geo helpers).
GHANA_VIEWBOX = (-3.2608, 4.7393, 1.1996, 11.1749)


def is_online(timeout=2.5):
    """Best-effort reachability check for the geocoding service.

    A quick HEAD-ish request to the Nominatim host; ``True`` when it answers,
    ``False`` on any failure (no network, DNS, timeout). Kept cheap so the UI
    can poll it to show an online/offline badge before a user tries an online
    lookup. When the app later routes geocoding through its own backend, point
    this (and the endpoints above) at that server.
    """
    req = urllib.request.Request(
        "https://nominatim.openstreetmap.org/status.php?format=json",
        headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 400
    except Exception:
        return False


def _get_json(endpoint, params, timeout):
    """GET ``endpoint?params`` and parse JSON, or None on any failure."""
    url = endpoint + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def geocode(query, timeout=6.0):
    """Best ``(lon, lat, display_name)`` for a place in Ghana, or None.

    Never raises: returns None on any network/parse problem so callers can
    fall back to the offline name search.
    """
    q = (query or "").strip()
    if not q:
        return None
    data = _get_json(_ENDPOINT, {
        "q": q, "format": "json", "limit": 1, "countrycodes": "gh",
    }, timeout)
    if not data:
        return None
    top = data[0]
    try:
        return (float(top["lon"]), float(top["lat"]),
                top.get("display_name", q))
    except (KeyError, ValueError, TypeError):
        return None


def search(query, limit=5, timeout=6.0):
    """Ranked Ghana matches for ``query``: ``[{lon,lat,display_name,address}]``.

    Ghana-bounded (``countrycodes=gh`` + a bounded ``viewbox``) and enriched
    with ``addressdetails`` so callers can back-fill region/district from the
    result's structured address without a second request. Best-effort: returns
    ``[]`` on any network/parse problem so the offline flow is never blocked.
    """
    q = (query or "").strip()
    if not q:
        return []
    min_lon, min_lat, max_lon, max_lat = GHANA_VIEWBOX
    data = _get_json(_ENDPOINT, {
        "q": q, "format": "json", "limit": max(1, int(limit)),
        "countrycodes": "gh",
        # Nominatim viewbox order is left,top,right,bottom.
        "viewbox": "%s,%s,%s,%s" % (min_lon, max_lat, max_lon, min_lat),
        "bounded": 1, "addressdetails": 1,
    }, timeout)
    if not data:
        return []
    out = []
    for hit in data:
        try:
            out.append({
                "lon": float(hit["lon"]),
                "lat": float(hit["lat"]),
                "display_name": hit.get("display_name", q),
                "address": hit.get("address") or {},
            })
        except (KeyError, ValueError, TypeError):
            continue
    return out


def reverse(lon, lat, timeout=6.0):
    """Structured address for a point: ``{display_name, address}`` or None.

    Best-effort reverse geocode (``/reverse`` + ``addressdetails``) used to
    suggest a region/district for a dropped pin. Returns ``None`` on any
    network/parse problem, or when coords are unusable.
    """
    try:
        lon = float(lon)
        lat = float(lat)
    except (TypeError, ValueError):
        return None
    data = _get_json(_REVERSE_ENDPOINT, {
        "lon": lon, "lat": lat, "format": "json", "addressdetails": 1,
    }, timeout)
    if not data or not isinstance(data, dict) or "error" in data:
        return None
    return {
        "display_name": data.get("display_name", ""),
        "address": data.get("address") or {},
    }
