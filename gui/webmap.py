"""Satellite drop-a-pin collection-site picker (Leaflet in QWebEngineView).

The sample editor uses this to mark exactly where a sample was collected: a
draggable marker on a satellite/streets basemap of Ghana. The Leaflet shell
(js/css/marker images) is vendored under ``gui/assets/leaflet`` so the widget
loads fully offline; only the map *tiles* and any geocoding reach the network,
and both fail silently (the Ghana region outline is drawn inline from the
bundled GeoJSON so there is always context, even with no tiles).

Public API mirrors ``charts.GhanaPicker`` minus the region argument (region is
derived by the caller via :func:`gui.geo.region_at`):

* ``picked = pyqtSignal(float, float)``  — emitted ``(lon, lat)`` on a user
  drop/drag only (never for programmatic :meth:`set_point`).
* :meth:`set_point`, :meth:`point`, :meth:`clear_point`, :meth:`set_center`.

Module flag ``WEBENGINE_AVAILABLE`` is False when PyQtWebEngine cannot be
imported, so the dialog can fall back to the offline vector ``GhanaPicker``.

Coordinate footgun: Leaflet is ``[lat, lon]``; this widget's API, the JS->Py
bridge and :func:`gui.geo.region_at` are all ``(lon, lat)``. Every boundary is
made explicit below.
"""

import json
import os

# Qt's WebEngine on macOS (conda Qt 5.15) crashes in its in-process GPU thread
# ("Chrome_InProcGpuThread", EXC_BAD_ACCESS) on many machines when it drives the
# GPU directly. A small vector map has no need for GPU acceleration, so force
# Chromium onto software compositing for stability. Must be set *before* the
# WebEngine libraries initialise (i.e. before QApplication/import), so it lives
# at the very top of this module and only *adds* to any flags the user set.
_GPU_SAFE_FLAGS = "--disable-gpu --disable-gpu-compositing --disable-software-rasterizer"
_existing_flags = os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "")
if "--disable-gpu" not in _existing_flags:
    os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = (
        (_existing_flags + " " + _GPU_SAFE_FLAGS).strip())

from PyQt5.QtCore import QObject, QUrl, pyqtSignal, pyqtSlot

from . import geo, paths, theme

try:
    from PyQt5.QtWebChannel import QWebChannel
    from PyQt5.QtWebEngineWidgets import QWebEngineView
    WEBENGINE_AVAILABLE = True
except Exception:  # pragma: no cover - depends on optional Qt module
    WEBENGINE_AVAILABLE = False

from PyQt5.QtWidgets import QVBoxLayout, QWidget

# Ghana centre + a whole-country zoom for the initial view.
_GHANA_CENTER = (-1.0232, 7.9465)      # (lon, lat)
_GHANA_ZOOM = 7


def _asset_url(name):
    """``file://`` URL for a vendored Leaflet asset (posix-style path)."""
    return QUrl.fromLocalFile(paths.leaflet_asset(name)).toString()


class _Bridge(QObject):
    """QWebChannel object exposed to the page as ``bridge``.

    The page calls ``bridge.on_ready()`` once Leaflet is up, and
    ``bridge.on_pick(lng, lat)`` whenever the user drops or drags the marker.
    """

    pick = pyqtSignal(float, float)     # (lon, lat)
    ready = pyqtSignal()

    @pyqtSlot(float, float)
    def on_pick(self, lon, lat):
        self.pick.emit(lon, lat)

    @pyqtSlot()
    def on_ready(self):
        self.ready.emit()


class WebMapPicker(QWidget):
    """Draggable-pin satellite map of Ghana (see module docstring)."""

    picked = pyqtSignal(float, float)   # (lon, lat), user drop/drag only

    def __init__(self, parent=None):
        super().__init__(parent)
        self._point = None              # cached (lon, lat) of the pin
        self._ready = False             # True once the page signalled on_ready
        self._pending = []              # JS snippets queued until ready
        self.setMinimumSize(300, 320)

        self._view = QWebEngineView(self)
        self._bridge = _Bridge(self)
        self._bridge.pick.connect(self._on_bridge_pick)
        self._bridge.ready.connect(self._on_ready)

        self._channel = QWebChannel(self._view.page())
        self._channel.registerObject("bridge", self._bridge)
        self._view.page().setWebChannel(self._channel)
        self._view.loadFinished.connect(self._on_load_finished)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._view)

        # baseUrl is the assets dir so relative "leaflet/..." refs resolve.
        base = QUrl.fromLocalFile(paths.assets_dir() + os.sep)
        self._view.setHtml(self._build_html(), base)

    # -- public API ------------------------------------------------------
    def set_point(self, lon, lat, region=None):
        """Place the pin from existing coords (no ``picked`` signal).

        ``region`` is accepted for call-site symmetry with GhanaPicker and
        ignored (the web map derives nothing from it).
        """
        try:
            self._point = (float(lon), float(lat))
        except (TypeError, ValueError):
            return
        self._run_js("pfSetPoint(%s, %s);"
                     % (self._point[1], self._point[0]))   # JS is (lat, lon)

    def point(self):
        """Current ``(lon, lat)`` of the pin, or None."""
        return self._point

    def clear_point(self):
        self._point = None
        self._run_js("pfClearPoint();")

    def set_center(self, lon, lat, zoom=None):
        """Recentre the view (does not move the pin)."""
        z = "null" if zoom is None else str(int(zoom))
        self._run_js("pfSetCenter(%s, %s, %s);" % (lat, lon, z))  # (lat, lon)

    # -- bridge / readiness ---------------------------------------------
    def _on_bridge_pick(self, lon, lat):
        self._point = (lon, lat)
        self.picked.emit(lon, lat)

    def _on_ready(self):
        self._flush()

    def _on_load_finished(self, ok):
        # loadFinished is a second safety net; on_ready usually fires first.
        if ok:
            self._flush()

    def _flush(self):
        if self._ready:
            return
        self._ready = True
        # If a point was set before the page was live, plant it now.
        if self._point is not None:
            lon, lat = self._point
            self._pending.insert(0, "pfSetPoint(%s, %s);" % (lat, lon))
        for js in self._pending:
            self._view.page().runJavaScript(js)
        self._pending = []

    def _run_js(self, js):
        """Run JS now if the page is ready, else queue it until it is."""
        if self._ready:
            self._view.page().runJavaScript(js)
        else:
            self._pending.append(js)

    # -- HTML ------------------------------------------------------------
    def _build_html(self):
        regions = geo.load_regions_geojson() or {"type": "FeatureCollection",
                                                 "features": []}
        cx_lon, cx_lat = _GHANA_CENTER
        return _HTML_TEMPLATE.format(
            leaflet_css=_asset_url("leaflet.css"),
            leaflet_js=_asset_url("leaflet.js"),
            marker_icon=_asset_url("images/marker-icon.png"),
            marker_icon_2x=_asset_url("images/marker-icon-2x.png"),
            marker_shadow=_asset_url("images/marker-shadow.png"),
            regions_geojson=json.dumps(regions),
            center_lat=cx_lat, center_lon=cx_lon, zoom=_GHANA_ZOOM,
            page=theme.PAGE, accent=theme.ACCENT, outline=theme.BORDER_STRONG)


# The page is fully self-contained: vendored Leaflet, the QWebChannel client
# (served by Qt at qrc:///qtwebchannel/qwebchannel.js), an explicit marker icon
# (Leaflet's auto image-path detection breaks under setHtml), a satellite +
# streets layer toggle, and the inline Ghana outline. Python<->JS boundary is
# always (lat, lon) inside Leaflet and (lon, lat) across the bridge.
_HTML_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<link rel="stylesheet" href="{leaflet_css}"/>
<script src="{leaflet_js}"></script>
<script src="qrc:///qtwebchannel/qwebchannel.js"></script>
<style>
  html, body {{ margin: 0; height: 100%; background: {page}; }}
  #map {{ position: absolute; top: 0; bottom: 0; left: 0; right: 0; }}
  .leaflet-container {{ background: {page}; }}
</style>
</head>
<body>
<div id="map"></div>
<script>
var map = L.map('map', {{ zoomControl: true }})
    .setView([{center_lat}, {center_lon}], {zoom});

// Basemaps: Esri World Imagery satellite (no key) + OSM streets. Attribution
// is required by both providers, so the control stays visible.
var satellite = L.tileLayer(
    'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/'
    + 'MapServer/tile/{{z}}/{{y}}/{{x}}',
    {{ maxZoom: 19,
       attribution: 'Tiles &copy; Esri, Maxar, Earthstar Geographics' }});
var streets = L.tileLayer(
    'https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png',
    {{ maxZoom: 19,
       attribution: '&copy; OpenStreetMap contributors' }});
satellite.addTo(map);
L.control.layers({{ 'Satellite': satellite, 'Streets': streets }},
                 null, {{ collapsed: false }}).addTo(map);

// Ghana region outline from the bundled GeoJSON — always drawn so there is
// context even when tiles fail to load offline.
try {{
    L.geoJSON({regions_geojson}, {{
        style: {{ color: '{outline}', weight: 1, fill: false,
                  opacity: 0.8 }},
        interactive: false
    }}).addTo(map);
}} catch (e) {{}}

// Explicit icon: auto image-path detection fails under setHtml/baseUrl.
var pinIcon = L.icon({{
    iconUrl: '{marker_icon}',
    iconRetinaUrl: '{marker_icon_2x}',
    shadowUrl: '{marker_shadow}',
    iconSize: [25, 41], iconAnchor: [12, 41],
    popupAnchor: [1, -34], shadowSize: [41, 41]
}});

var marker = null;

function pfPlace(lat, lon) {{
    if (marker === null) {{
        marker = L.marker([lat, lon], {{ draggable: true, icon: pinIcon }})
            .addTo(map);
        marker.on('dragend', function () {{
            var p = marker.getLatLng();
            if (window.bridge) bridge.on_pick(p.lng, p.lat);  // (lon, lat)
        }});
    }} else {{
        marker.setLatLng([lat, lon]);
    }}
}}

// Programmatic set (from Python): move/create the pin, no bridge callback.
function pfSetPoint(lat, lon) {{ pfPlace(lat, lon); }}
function pfClearPoint() {{
    if (marker !== null) {{ map.removeLayer(marker); marker = null; }}
}}
function pfSetCenter(lat, lon, zoom) {{
    if (zoom === null || zoom === undefined) map.panTo([lat, lon]);
    else map.setView([lat, lon], zoom);
}}

// Clicking the map drops/moves the pin and reports it (lon, lat).
map.on('click', function (e) {{
    pfPlace(e.latlng.lat, e.latlng.lng);
    if (window.bridge) bridge.on_pick(e.latlng.lng, e.latlng.lat);
}});

// Wire the QWebChannel bridge, then tell Python the map is live.
new QWebChannel(qt.webChannelTransport, function (channel) {{
    window.bridge = channel.objects.bridge;
    if (window.bridge) bridge.on_ready();
}});
</script>
</body>
</html>
"""
