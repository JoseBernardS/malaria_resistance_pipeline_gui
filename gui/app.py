"""Application entry point and main window.

``python -m gui.app`` (also the bundle entrypoint) builds the QApplication
and a MainWindow: a flat light top bar with text tabs over full-width Jobs,
Progress and Results pages, wired to a persisted ``JobQueue``.
"""

import math
import os
import sys

from PyQt5.QtCore import (QEasingCurve, QParallelAnimationGroup, QPointF,
                          QPropertyAnimation, QRectF, QSize, Qt, QTimer)
from PyQt5.QtGui import (QColor, QIcon, QPainter, QPainterPath, QPen,
                         QPixmap, QPolygonF)
from PyQt5.QtWidgets import (QApplication, QFrame, QHBoxLayout, QLabel,
                             QMessageBox, QPushButton, QStackedWidget,
                             QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from . import db, paths, theme
from .auth import Session


def _nav_icon(kind, color, px=18):
    """Render a crisp monochrome line icon for a nav row.

    Drawn with QPainter so the four top-level glyphs are pixel-consistent and
    recolourable (muted by default, accent when the row is active) without
    bundling any image assets or depending on a system emoji font.
    """
    # Render at the display's device-pixel ratio so the vector glyph stays
    # crisp on Retina/HiDPI screens: a plain ``px``-sized pixmap is generated at
    # 1x and then upscaled by Qt, which is what made the icons look pixelated.
    app = QApplication.instance()
    dpr = app.devicePixelRatio() if app is not None else 1.0
    pm = QPixmap(int(round(px * dpr)), int(round(px * dpr)))
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    pen = QPen(QColor(color))
    pen.setWidthF(1.6)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    m = 2.5
    w = px - 2 * m
    c = px / 2.0
    if kind == "menu":                       # hamburger: three full-width bars
        for y in (m + 1.5, c, px - m - 1.5):
            p.drawLine(QPointF(m, y), QPointF(px - m, y))
    elif kind in ("chevron_left", "chevron_right"):   # sidebar open/close arrow
        hx, vy = w * 0.18, w * 0.30
        if kind == "chevron_left":           # points to the edge: collapse
            pts = [QPointF(c + hx, c - vy), QPointF(c - hx, c),
                   QPointF(c + hx, c + vy)]
        else:                                # points to the body: expand
            pts = [QPointF(c - hx, c - vy), QPointF(c + hx, c),
                   QPointF(c - hx, c + vy)]
        p.drawPolyline(QPolygonF(pts))
    elif kind == "jobs":                     # stacked list rows
        for y in (m + 1.0, c, px - m - 1.0):
            p.drawLine(QPointF(m, y), QPointF(px - m, y))
    elif kind == "progress":                 # clock face + hands
        p.drawEllipse(QRectF(m, m, w, w))
        p.drawLine(QPointF(c, c), QPointF(c, m + 2.6))
        p.drawLine(QPointF(c, c), QPointF(c + 3.0, c + 0.6))
    elif kind == "results":                  # bar chart on a baseline
        base = px - m
        p.drawLine(QPointF(m, base), QPointF(px - m, base))
        for x, h in ((m + 1.6, 5.0), (c, 8.5), (px - m - 1.6, 11.5)):
            p.drawLine(QPointF(x, base), QPointF(x, base - h))
    elif kind == "trends":                   # rising trend line + arrowhead
        base = px - m
        p.drawPolyline(QPolygonF([
            QPointF(m, base - 2.0), QPointF(c - 2.0, base - 6.5),
            QPointF(c + 1.0, base - 4.5), QPointF(px - m, m + 2.0)]))
        p.drawLine(QPointF(px - m - 3.0, m + 2.0), QPointF(px - m, m + 2.0))
        p.drawLine(QPointF(px - m, m + 2.0), QPointF(px - m, m + 5.0))
    elif kind == "samples":                  # location pin: ring + tip + dot
        r = w * 0.30
        cy = m + r + 0.6
        p.drawEllipse(QRectF(c - r, cy - r, 2 * r, 2 * r))
        p.drawLine(QPointF(c - r * 0.70, cy + r * 0.72), QPointF(c, px - m))
        p.drawLine(QPointF(c + r * 0.70, cy + r * 0.72), QPointF(c, px - m))
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(color))
        p.drawEllipse(QRectF(c - 1.3, cy - 1.3, 2.6, 2.6))
    elif kind == "surveillance":             # concentric target + dot
        p.drawEllipse(QRectF(m, m, w, w))
        p.drawEllipse(QRectF(c - w * 0.22, c - w * 0.22, w * 0.44, w * 0.44))
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(color))
        p.drawEllipse(QRectF(c - 1.3, c - 1.3, 2.6, 2.6))
    elif kind == "explorer":                 # magnifier: lens + handle
        r = w * 0.34
        lx, ly = c - w * 0.08, c - w * 0.08
        p.drawEllipse(QRectF(lx - r, ly - r, 2 * r, 2 * r))
        a = math.radians(45)
        p.drawLine(QPointF(lx + math.cos(a) * r, ly + math.sin(a) * r),
                   QPointF(px - m, px - m))
    elif kind == "sun":                      # filled disc + rays
        r = w * 0.20
        p.setBrush(QColor(color))
        p.drawEllipse(QRectF(c - r, c - r, 2 * r, 2 * r))
        for k in range(8):
            a = math.radians(k * 45)
            ca, sa = math.cos(a), math.sin(a)
            p.drawLine(QPointF(c + ca * w * 0.30, c + sa * w * 0.30),
                       QPointF(c + ca * w * 0.46, c + sa * w * 0.46))
    elif kind == "moon":                     # crescent (disc minus disc)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(color))
        disc = QPainterPath()
        disc.addEllipse(QRectF(m, m, w, w))
        cut = QPainterPath()
        cut.addEllipse(QRectF(m + w * 0.34, m - w * 0.10, w, w))
        p.drawPath(disc.subtracted(cut))
    elif kind == "user":                     # head disc + shoulders arc
        r = w * 0.20
        hy = m + r + 0.4
        p.drawEllipse(QRectF(c - r, hy - r, 2 * r, 2 * r))
        p.drawArc(QRectF(c - w * 0.34, px - m - w * 0.30,
                         w * 0.68, w * 0.60), 20 * 16, 140 * 16)
    elif kind == "settings":                 # gear: ring + hub + spokes
        r = w * 0.30
        p.drawEllipse(QRectF(c - r, c - r, 2 * r, 2 * r))
        hub = w * 0.11
        p.drawEllipse(QRectF(c - hub, c - hub, 2 * hub, 2 * hub))
        for k in range(8):
            a = math.radians(k * 45)
            ca, sa = math.cos(a), math.sin(a)
            p.drawLine(QPointF(c + ca * r, c + sa * r),
                       QPointF(c + ca * (r + w * 0.12),
                               c + sa * (r + w * 0.12)))
    p.end()
    return QIcon(pm)


def _spinner_pixmap(angle, color, px=16):
    """A single frame of a rotating gapped-ring spinner as a ``QPixmap``.

    Drawn with the same HiDPI/device-pixel-ratio handling as :func:`_nav_icon`
    so it stays crisp; ``angle`` (degrees) rotates the 270° arc, and a QTimer in
    the window steps it to animate a "syncing" indicator without any GIF asset.
    """
    app = QApplication.instance()
    dpr = app.devicePixelRatio() if app is not None else 1.0
    pm = QPixmap(int(round(px * dpr)), int(round(px * dpr)))
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    pen = QPen(QColor(color))
    pen.setWidthF(1.8)
    pen.setCapStyle(Qt.RoundCap)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    m = 2.5
    rect = QRectF(m, m, px - 2 * m, px - 2 * m)
    # Qt arc angles are 1/16-degree, positive = counter-clockwise. Start at the
    # rotating offset and sweep 270° so a quarter-gap orbits as ``angle`` grows.
    p.drawArc(rect, int(-angle * 16), int(270 * 16))
    p.end()
    return pm
from .cloud_queue import CloudJobController
from .live_controller import LiveRunController
from .sync_controller import SyncController
from .metadata_sync_controller import MetadataSyncController
from .providers import CloudProvider, LocalProvider
from .queue import JobQueue
from .screens.auth_dialog import LoginDialog
from .screens.dashboard import DashboardScreen
from .screens.jobs import JobsScreen
from .screens.report_settings import ReportSettingsScreen
from .screens.data_sources import DataSourcesScreen
from .screens.trends import TrendsScreen
from .screens.progress import ProgressScreen


class MainWindow(QWidget):
    PAGE_JOBS = 0
    PAGE_PROGRESS = 1
    PAGE_DASHBOARD = 2
    PAGE_TRENDS = 3
    PAGE_REPORT_SETTINGS = 4
    PAGE_DATA_SOURCES = 5

    # Sidebar widths and the auto-collapse breakpoint.
    RAIL_WIDTH = 54
    FULL_WIDTH = 208
    RAIL_BREAKPOINT = 1040

    # Painted line-icon drawn for each top-level row (full mode shows it beside
    # the label; rail mode shows it alone).
    NAV_ICONS = {
        PAGE_JOBS: "jobs",
        PAGE_PROGRESS: "progress",
        PAGE_DASHBOARD: "results",
        PAGE_TRENDS: "trends",
        PAGE_REPORT_SETTINGS: "settings",
        PAGE_DATA_SOURCES: "settings",
    }

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Pf Drug Resistance Surveillance")
        self.resize(1240, 800)

        # Icon-rail sidebar state. ``_rail`` is the current collapsed flag;
        # ``_rail_user_override`` records a manual toggle so the responsive
        # breakpoint stops auto-driving the rail once the user has decided.
        self._rail = False
        self._rail_user_override = None

        self.queue = JobQueue(self)

        # Cloud sign-in state, shared by the account row and the cloud provider.
        # In-memory only; a real client will persist the token in the OS
        # keychain (see ``gui.auth``).
        self.session = Session(self)

        # Cloud queue controller: the remote analogue of ``JobQueue`` that drives
        # the pipeline API (upload → submit → poll → download). It shares the
        # session and emits the same signals as the local queue so its jobs flow
        # through the identical Progress/Results path.
        self.cloud = CloudJobController(self.session, self)

        # Only one job may run at a time across both queues: each refuses to
        # start while the other is busy and pokes the other to pick up its
        # deferred jobs when it goes idle. Prevents the local+cloud overlap
        # that corrupted the single shared Progress console.
        self.queue.set_peer(self.cloud)
        self.cloud.set_peer(self.queue)

        # Live-run (folder-watch) controller: watches a MinKNOW output folder
        # while a run is in progress, running cheap align+coverage scans for a
        # live depth grid, then hands off to the normal queue for the full
        # pipeline on "Finalize". It's given both queues so it can refuse to
        # scan while either is running a real job. Sample labels captured at
        # live-run creation are stashed here to apply to the finalize job.
        self.live = LiveRunController(self.queue, self.cloud, self)
        self._live_sheet = {}

        # Auto-sync controller: on sign-in, push completed *local* runs into the
        # cloud surveillance surface (results + provenance) on a worker thread.
        # Independent of the cloud queue above; it only uploads finished-run
        # artifacts, and drives the "Syncing…" indicator in the sidebar.
        self.sync = SyncController(self.session, self)

        # Metadata-sync controller: pushes *mutable* sample metadata (region,
        # GPS, collection date, corrected alias/id, notes) to a run's cloud twin
        # whenever it's edited or a run gains a server id. Separate from the
        # result sync above; keyed on the server run id, idempotent, resumable.
        self.meta_sync = MetadataSyncController(self.session, self)

        # Execution-provider registry: a saved config's ``execution_target``
        # selects one. Local runs in-process; cloud hands off to the controller
        # above. Both providers get the session so cloud can refuse to submit
        # until the user is signed in (and a server URL is configured).
        self._providers = {
            "local": LocalProvider(self.queue),
            "cloud": CloudProvider(self.session, self.cloud),
        }

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_sidebar())

        self.stack = QStackedWidget()
        self.jobs = JobsScreen(self.queue, session=self.session)
        self.progress = ProgressScreen(self.queue)
        self.dashboard = DashboardScreen()
        self.trends = TrendsScreen()
        self.report_settings = ReportSettingsScreen()
        self.data_sources = DataSourcesScreen(session=self.session)
        self.stack.addWidget(self.jobs)
        self.stack.addWidget(self.progress)
        self.stack.addWidget(self.dashboard)
        self.stack.addWidget(self.trends)
        self.stack.addWidget(self.report_settings)
        self.stack.addWidget(self.data_sources)

        body = QWidget()
        body_lay = QVBoxLayout(body)
        body_lay.setContentsMargins(24, 18, 24, 18)
        body_lay.addWidget(self.stack)
        root.addWidget(body, 1)

        self._wire()
        self._show(self.PAGE_JOBS)
        # Progress is only reachable while a run is executing; start greyed out
        # (a resume below may immediately re-enable it via job_started).
        self._set_progress_active(False)

        # Pick up any queued jobs left from a previous session. Cloud jobs only
        # resume once a session is authenticated (the controller guards on that),
        # so also retry cloud resume whenever sign-in state changes.
        self.queue.resume()
        # Restore a prior cloud session from the OS keychain (refresh-token
        # exchange). On success this emits ``session.changed``, which is wired
        # to both cloud.resume and sync.resume — so signing back in silently
        # also picks up queued cloud jobs and kicks the local-run sync sweep.
        self.session.restore()
        self.cloud.resume()
        # Backstop the resume in case the session was already live (or restore
        # was a no-op): sync any completed local runs not yet in the cloud.
        self.sync.resume()
        # And flush any sample-metadata edits made while signed out to their
        # runs' cloud twins.
        self.meta_sync.resume()

        # Preload the most recent completed run so the Results/Samples pages are
        # populated (with a run picker to switch) instead of blank on launch.
        # Stays on the Jobs page — this only fills the dashboard behind it.
        self.dashboard.autoload_latest()

    # -- chrome ----------------------------------------------------------
    def _build_sidebar(self):
        self._sidebar = bar = QFrame()
        bar.setObjectName("Sidebar")
        bar.setFixedWidth(self.FULL_WIDTH)
        lay = QVBoxLayout(bar)
        lay.setContentsMargins(14, 14, 12, 14)
        lay.setSpacing(2)

        # Top row: brand logo with a small hamburger toggle that collapses the
        # sidebar to an icon rail (or expands it back). A manual press wins over
        # the responsive breakpoint thereafter.
        top = QHBoxLayout()
        top.setContentsMargins(2, 0, 0, 0)
        top.setSpacing(6)
        self._logo = logo = QLabel()
        logo.setObjectName("SidebarLogo")
        self._refresh_logo()
        top.addWidget(logo, 1, Qt.AlignVCenter)
        self._rail_toggle = QPushButton()
        self._rail_toggle.setObjectName("Ghost")
        self._rail_toggle.setIconSize(QSize(20, 20))
        self._rail_toggle.setCursor(Qt.PointingHandCursor)
        self._rail_toggle.setFixedSize(30, 30)
        self._rail_toggle.clicked.connect(self._toggle_rail)
        self._refresh_rail_icon()
        top.addWidget(self._rail_toggle, 0, Qt.AlignVCenter)
        lay.addLayout(top)
        lay.addSpacing(16)

        # Hierarchical navigation: Results expands into its sections so the
        # whole app reads as one tree instead of a row of flat buttons. Each
        # item carries its (page, section) target; sections drive the
        # dashboard's own section stack via ``dashboard.show_section``. Native
        # branch arrows (animated) signal expandable rows.
        def _mk_nav_tree():
            t = QTreeWidget()
            t.setObjectName("NavTree")
            t.setHeaderHidden(True)
            t.setIndentation(13)
            t.setIconSize(QSize(18, 18))
            t.setAnimated(True)
            t.setFocusPolicy(Qt.NoFocus)
            t.setVerticalScrollMode(QTreeWidget.ScrollPerPixel)
            t.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            return t

        # Two trees: the main nav fills the sidebar (stretch), a second tree
        # holds low-frequency config and is pinned to the bottom (no stretch),
        # so Settings sits at the foot of the menu like a typical app.
        self.nav = _mk_nav_tree()
        self.nav_bottom = _mk_nav_tree()
        self.nav_bottom.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        # No disclosure column in the footer tree, so the Settings icon lines up
        # flush-left with the account / theme icons beneath it (not indented for
        # an expand arrow like the main nav column).
        self.nav_bottom.setRootIsDecorated(False)
        self.nav_bottom.setIndentation(20)
        self._nav_trees = [self.nav, self.nav_bottom]
        self._nav_items = {}        # key -> QTreeWidgetItem (for selection sync)
        self._top_items = []        # top-level rows, for rail icon-only mode

        def node(parent, label, page, section, key=None, top=False, icon=None):
            item = QTreeWidgetItem(parent, [label])
            item.setData(0, Qt.UserRole, (page, section))
            item.setData(0, Qt.UserRole + 1, label)   # base label (rail clears)
            if top:
                # A top row's glyph is usually its page's icon, but two top rows
                # (Results and Samples) share the dashboard page, so an explicit
                # ``icon`` can override the page default.
                kind = icon or self.NAV_ICONS.get(page)
                item.setData(0, Qt.UserRole + 2, kind)
                item.setIcon(0, _nav_icon(kind, theme.MUTED))
                self._top_items.append(item)
            self._nav_items[key or (page, section)] = item
            return item

        node(self.nav, "Jobs", self.PAGE_JOBS, None, top=True)
        node(self.nav, "Progress", self.PAGE_PROGRESS, None, top=True)
        results = node(self.nav, "Results", self.PAGE_DASHBOARD,
                       "overview", key="results", top=True)
        node(results, "Overview", self.PAGE_DASHBOARD, "overview")
        node(results, "Resistance", self.PAGE_DASHBOARD, "data:resistance")
        node(results, "Genes", self.PAGE_DASHBOARD, "data:genes")
        node(results, "Mutations", self.PAGE_DASHBOARD, "data:mutations")
        node(results, "Quality", self.PAGE_DASHBOARD, "data:quality")
        # Samples is a per-run *metadata* view (aliases, IDs, geo-tagged
        # collection sites), not an analytical finding, so it sits as its own
        # top-level row rather than under Results. It still targets the
        # dashboard's ``samples`` section and stays run-scoped.
        node(self.nav, "Samples", self.PAGE_DASHBOARD, "samples",
             top=True, icon="samples")
        # History mirrors Results: a parent row plus children targeting the same
        # page with sections. The parent (and "Trend chart" child) key off
        # ``(PAGE_TRENDS, "charts")`` so highlighting resolves cleanly.
        trends = node(self.nav, "History", self.PAGE_TRENDS, "charts",
                      key="trends", top=True)
        node(trends, "Trend chart", self.PAGE_TRENDS, "charts")
        node(trends, "Patient search", self.PAGE_TRENDS, "search")
        # Config pages are low-frequency admin tasks, so they're tucked under a
        # collapsed "Settings" group (mirrors the Results tree) rather than
        # sitting as prominent top-level rows. A distinct key avoids colliding
        # with the "Report settings" child that shares its page; clicking the
        # parent opens Report settings and toggles the sub-menu.
        settings = node(self.nav_bottom, "Settings", self.PAGE_REPORT_SETTINGS,
                        None, key="settings", top=True, icon="settings")
        node(settings, "Report settings", self.PAGE_REPORT_SETTINGS, None)
        node(settings, "Data sources", self.PAGE_DATA_SOURCES, None)

        self.nav.setExpandsOnDoubleClick(False)
        self.nav_bottom.setExpandsOnDoubleClick(False)
        results.setExpanded(False)
        trends.setExpanded(False)
        settings.setExpanded(False)
        self.nav.itemClicked.connect(self._on_nav_clicked)
        self.nav_bottom.itemClicked.connect(self._on_nav_clicked)
        self.nav_bottom.itemExpanded.connect(self._resize_bottom_nav)
        self.nav_bottom.itemCollapsed.connect(self._resize_bottom_nav)
        lay.addWidget(self.nav, 1)
        lay.addWidget(self.nav_bottom, 0)
        self._resize_bottom_nav()

        # Account row: reflects cloud sign-in state. A person glyph plus a
        # label that reads "Sign in" when signed out or the account email when
        # signed in; clicking opens the login dialog (or offers sign-out). Kept
        # above the theme footer so account and appearance controls group at the
        # bottom of the rail. Stays icon-only in rail mode.
        acct_row = QHBoxLayout()
        acct_row.setContentsMargins(2, 0, 0, 0)
        acct_row.setSpacing(8)
        # Appearance toggle leads the row, then a thin divider separates it from
        # the account control so the two read as distinct groups on one line.
        self._theme_toggle = QPushButton()
        self._theme_toggle.setObjectName("Ghost")
        self._theme_toggle.setCursor(Qt.PointingHandCursor)
        self._theme_toggle.setFixedSize(30, 30)
        self._theme_toggle.setIconSize(QSize(18, 18))
        self._theme_toggle.clicked.connect(self._toggle_theme)
        self._refresh_theme_icon()
        acct_row.addWidget(self._theme_toggle, 0, Qt.AlignVCenter)
        self._foot_sep = QLabel("|")
        self._foot_sep.setObjectName("SidebarFoot")
        self._foot_sep.setAlignment(Qt.AlignCenter)
        acct_row.addWidget(self._foot_sep, 0, Qt.AlignVCenter)
        self._acct_btn = QPushButton()
        self._acct_btn.setObjectName("Ghost")
        self._acct_btn.setCursor(Qt.PointingHandCursor)
        self._acct_btn.setFixedSize(30, 30)
        self._acct_btn.setIconSize(QSize(18, 18))
        self._acct_btn.setIcon(_nav_icon("user", theme.MUTED, 18))
        self._acct_btn.clicked.connect(self._on_account_clicked)
        acct_row.addWidget(self._acct_btn, 0, Qt.AlignVCenter)
        self._acct_label = QLabel()
        self._acct_label.setObjectName("SidebarFoot")
        self._acct_label.setCursor(Qt.PointingHandCursor)
        self._acct_label.mousePressEvent = \
            lambda _e: self._on_account_clicked()
        acct_row.addWidget(self._acct_label, 1)
        lay.addLayout(acct_row)
        lay.addSpacing(6)
        self.session.changed.connect(self._refresh_account)
        self._refresh_account()

        # Sync indicator: a spinning ring + "Syncing…" caption shown only while
        # the auto-sync sweep is uploading completed local runs. Hidden at rest
        # and in rail mode (the icon stays, the caption hides — like the footer).
        sync_row = QHBoxLayout()
        sync_row.setContentsMargins(2, 0, 0, 0)
        sync_row.setSpacing(8)
        self._sync_icon = QLabel()
        self._sync_icon.setFixedSize(30, 18)
        self._sync_icon.setAlignment(Qt.AlignCenter)
        sync_row.addWidget(self._sync_icon, 0, Qt.AlignVCenter)
        self._sync_label = QLabel("Syncing\u2026")
        self._sync_label.setObjectName("SidebarFoot")
        sync_row.addWidget(self._sync_label, 1)
        self._sync_row = QWidget()
        self._sync_row.setLayout(sync_row)
        self._sync_row.setVisible(False)
        lay.addWidget(self._sync_row)

        # Spinner animation: a QTimer steps the arc angle while a sync is active.
        self._sync_angle = 0
        self._sync_timer = QTimer(self)
        self._sync_timer.setInterval(80)
        self._sync_timer.timeout.connect(self._tick_spinner)

        return bar

    # -- account / cloud sign-in ----------------------------------------
    def _refresh_account(self):
        """Sync the account row with the current session state."""
        if self.session.is_authenticated():
            label = self.session.label() or "Signed in"
            self._acct_label.setText(label)
            self._acct_btn.setToolTip("Signed in as %s \u2014 click to sign out"
                                      % label)
            self._acct_label.setToolTip("Click to sign out")
        else:
            self._acct_label.setText("Sign in")
            self._acct_btn.setToolTip("Sign in to run cloud jobs")
            self._acct_label.setToolTip("Sign in to run cloud jobs")

    # -- auto-sync indicator --------------------------------------------
    def _tick_spinner(self):
        """Advance the spinner one frame while a sync sweep is running."""
        self._sync_angle = (self._sync_angle + 30) % 360
        self._sync_icon.setPixmap(
            _spinner_pixmap(self._sync_angle, theme.ACCENT, 16))

    def _on_sync_started(self, count):
        """Reveal the indicator and start the spinner for a sync sweep."""
        self._sync_label.setText(
            "Syncing %d run%s\u2026" % (count, "" if count == 1 else "s"))
        self._sync_label.setVisible(not self._rail)
        self._sync_row.setVisible(True)
        if not self._sync_timer.isActive():
            self._sync_timer.start()
        self._tick_spinner()

    def _on_sync_finished(self, ok, failed):
        """Stop the spinner and hide the indicator when the sweep ends."""
        self._sync_timer.stop()
        self._sync_row.setVisible(False)
        self._sync_icon.clear()

    def _on_account_clicked(self):
        """Signed out → open the login dialog; signed in → confirm sign-out."""
        if self.session.is_authenticated():
            if QMessageBox.question(
                    self, "Sign out",
                    "Sign out of the cloud service?",
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.No) == QMessageBox.Yes:
                self.session.sign_out()
            return
        LoginDialog(self.session, parent=self).exec()

    # -- theme toggle ----------------------------------------------------
    def _refresh_theme_icon(self):
        """Show the icon for the theme you'd switch *to* (moon in light, sun
        in dark)."""
        to_dark = theme.MODE != "dark"
        self._theme_toggle.setIcon(
            _nav_icon("moon" if to_dark else "sun", theme.MUTED, 18))
        self._theme_toggle.setToolTip(
            "Switch to dark theme" if to_dark else "Switch to light theme")

    def _refresh_logo(self):
        """Load the logo variant that reads on the current sidebar surface —
        the dark build lightens the navy wordmark so it stays legible."""
        pix = QPixmap(paths.logo_path(dark=theme.MODE == "dark"))
        if not pix.isNull():
            self._logo.setPixmap(pix.scaledToWidth(138, Qt.SmoothTransformation))

    def _toggle_theme(self):
        """Flip light/dark live: rebuild the stylesheet and repaint charts."""
        theme.set_mode("light" if theme.MODE == "dark" else "dark")
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(theme.APP_QSS)
        self._refresh_logo()
        self._refresh_theme_icon()
        self._refresh_rail_icon()
        self._paint_nav_icons()
        # Tables/labels bake their colours at build time, so re-render every
        # screen that does so to pick up the new palette.
        self.jobs.refresh()
        self.progress.restyle()
        self.dashboard.restyle()
        self.trends.restyle()
        self.report_settings.restyle()
        self.data_sources.restyle()
        # The custom-painted charts read theme.* at paint time, so nudge every
        # widget to repaint with the new palette.
        if app is not None:
            for w in app.allWidgets():
                w.update()

    def _paint_nav_icons(self, *_):
        """Recolour top-level icons: accent for the selected row's top-level
        ancestor, else muted.

        Highlighting keys off the selected item (not the page) because two top
        rows — Results and Samples — both drive the dashboard page; matching on
        page alone would light them together.
        """
        target = self._current_nav_item()
        while target is not None and target.parent() is not None:
            target = target.parent()
        for item in self._top_items:
            kind = item.data(0, Qt.UserRole + 2)
            if item.isDisabled():
                color = theme.FAINT     # e.g. Progress with no active run
            elif item is target:
                color = theme.ACCENT
            else:
                color = theme.MUTED
            item.setIcon(0, _nav_icon(kind, color))

    def _current_nav_item(self):
        """The selected row across both nav trees (main + bottom Settings)."""
        for tree in self._nav_trees:
            sel = tree.selectedItems()
            if sel:
                return sel[0]
        return None

    def _resize_bottom_nav(self, *_):
        """Pin the bottom tree to exactly its visible rows (33px each) so it
        hugs the foot of the sidebar instead of stealing vertical space."""
        tree = self.nav_bottom
        rows = 0
        for i in range(tree.topLevelItemCount()):
            top = tree.topLevelItem(i)
            rows += 1
            if top.isExpanded() and not self._rail:
                rows += top.childCount()
        tree.setFixedHeight(rows * 33 + 4)

    def _set_progress_active(self, active):
        """Enable/disable the Progress nav row.

        Progress only means something while a run is executing, so the row is
        greyed out and unclickable when nothing is running (and its icon dims
        via ``_paint_nav_icons``).
        """
        item = self._nav_items.get((self.PAGE_PROGRESS, None))
        if item is None:
            return
        item.setDisabled(not active)
        item.setToolTip(0, "" if active else "No active run")
        self._paint_nav_icons()

    # -- icon-rail collapse ----------------------------------------------
    def _refresh_rail_icon(self):
        """Point the toggle arrow the way it acts: ``\u2039`` collapses the open
        sidebar, ``\u203a`` expands the rail — so the glyph always signals the
        next action rather than the current state."""
        kind = "chevron_right" if self._rail else "chevron_left"
        self._rail_toggle.setIcon(_nav_icon(kind, theme.MUTED, 20))
        self._rail_toggle.setToolTip(
            "Expand sidebar" if self._rail else "Collapse sidebar")

    def _toggle_rail(self):
        """Manual sidebar toggle; pins the choice against the breakpoint."""
        self._rail_user_override = not self._rail
        self._apply_rail(self._rail_user_override)

    def _apply_rail(self, rail, animate=True):
        """Collapse to an icon rail, or restore the full labelled tree.

        The width change is animated (parallel min/max width) so the body
        reflows smoothly rather than snapping. In rail mode the labels are
        cleared (icons only), children hidden and branch arrows dropped.
        """
        self._rail = rail
        self._refresh_rail_icon()
        target = self.RAIL_WIDTH if rail else self.FULL_WIDTH
        self._logo.setVisible(not rail)
        self._acct_label.setVisible(not rail)
        # In the icon rail the account label hides; keep the appearance toggle
        # and its divider tucked away too so the single remaining row is just
        # the account glyph.
        self._theme_toggle.setVisible(not rail)
        self._foot_sep.setVisible(not rail)
        # Spinner icon stays; only its caption follows the rail (like the footer).
        self._sync_label.setVisible(not rail and self._sync_row.isVisible())
        self._sidebar.layout().setContentsMargins(
            *((6, 14, 6, 14) if rail else (14, 14, 12, 14)))
        for t in self._nav_trees:
            # Only the main nav shows disclosure arrows; the footer tree stays
            # flush-left so Settings lines up with the account / theme icons.
            t.setRootIsDecorated((not rail) and t is self.nav)
            t.setProperty("rail", "true" if rail else "false")
            t.style().unpolish(t)
            t.style().polish(t)

        for item in self._top_items:
            if rail:
                item.setText(0, "")
                item.setExpanded(False)
                for i in range(item.childCount()):
                    item.child(i).setHidden(True)
            else:
                item.setText(0, item.data(0, Qt.UserRole + 1) or "")
                for i in range(item.childCount()):
                    item.child(i).setHidden(False)
        self._resize_bottom_nav()

        if animate:
            self._animate_sidebar(target)
        else:
            self._sidebar.setFixedWidth(target)

    def _animate_sidebar(self, target):
        """Animate the sidebar's min+max width to ``target`` in parallel."""
        start = self._sidebar.width()
        grp = QParallelAnimationGroup(self)
        for prop in (b"minimumWidth", b"maximumWidth"):
            a = QPropertyAnimation(self._sidebar, prop, grp)
            a.setDuration(180)
            a.setStartValue(start)
            a.setEndValue(target)
            a.setEasingCurve(QEasingCurve.InOutCubic)
            grp.addAnimation(a)
        self._rail_anim = grp        # keep a reference so it isn't GC'd
        grp.start()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # Auto-collapse below the breakpoint / expand above it, but only while
        # the user has not manually toggled the rail. Only the sidebar width
        # changes here, so the central widget reflows via its layout and there
        # is no resizeEvent recursion.
        if self._rail_user_override is not None \
                or not hasattr(self, "_sidebar"):
            return
        want_rail = event.size().width() < self.RAIL_BREAKPOINT
        if want_rail != self._rail:
            self._apply_rail(want_rail)

    def _on_nav_clicked(self, item, _col):
        # A parent row toggles its sub-menu (collapsed by default) as well as
        # navigating to its own target, so one click both expands and opens.
        if item.childCount() and not self._rail:
            item.setExpanded(not item.isExpanded())
        page, section = item.data(0, Qt.UserRole)
        self._show(page, section)

    def _show(self, page, section=None):
        self.stack.setCurrentIndex(page)
        if page == self.PAGE_DASHBOARD:
            self.dashboard.show_section(section or "overview")
        elif page == self.PAGE_TRENDS:
            self.trends.show_section(section or "charts")
        # Select first (it resolves the exact row, with fallbacks), then paint
        # icons off that selection so the right top-level row lights up even
        # when two rows share a page (Results vs Samples).
        self._select_nav(page, section)
        self._paint_nav_icons()

    def _select_nav(self, page, section):
        """Highlight the tree item matching the active page/section."""
        item = self._nav_items.get((page, section))
        if item is None and page == self.PAGE_DASHBOARD:
            item = self._nav_items.get((page, "overview"))
        if item is None:
            item = self._nav_items.get((page, None))
        if item is not None:
            tree = item.treeWidget()
            for t in self._nav_trees:
                t.blockSignals(True)
            # Only one row may look active, so clear the other tree's selection.
            for t in self._nav_trees:
                if t is not tree:
                    t.clearSelection()
            # Open any collapsed ancestors so the selected row is visible, then
            # highlight it. Chevrons re-sync on the resulting expansions.
            parent = item.parent()
            while parent is not None and not self._rail:
                if not parent.isExpanded():
                    parent.setExpanded(True)
                parent = parent.parent()
            tree.setCurrentItem(item)
            for t in self._nav_trees:
                t.blockSignals(False)
            self._resize_bottom_nav()

    # -- wiring ----------------------------------------------------------
    def _wire(self):
        self.jobs.add_job_requested.connect(self._on_add_job)
        self.jobs.open_job_requested.connect(self._on_open_job)
        # Empty Results state's "Open a run" button jumps to the Jobs list.
        self.dashboard.open_jobs_requested.connect(
            lambda: self._show(self.PAGE_JOBS))
        self.queue.job_started.connect(self._on_job_started)
        self.queue.job_finished.connect(self._on_job_finished)
        self.queue.queue_changed.connect(self.jobs.refresh)
        # Keep the Results run-picker current as jobs come and go.
        self.queue.queue_changed.connect(self.dashboard.refresh_runs)
        self.queue.job_finished.connect(
            lambda *_: self.trends.refresh())
        # Patient-search rows open a specimen's report via the dashboard path.
        self.trends.open_report_requested.connect(self._on_open_sample_report)
        # Mirror the local queue: cloud jobs drive the same Progress/Results
        # handlers, and the Progress screen also listens to the controller's
        # phase/log signals (Package/Upload/Submit/Remote run/Download).
        self.cloud.job_started.connect(self._on_job_started)
        self.cloud.job_finished.connect(self._on_job_finished)
        self.cloud.queue_changed.connect(self.jobs.refresh)
        self.cloud.queue_changed.connect(self.dashboard.refresh_runs)
        self.cloud.job_finished.connect(lambda *_: self.trends.refresh())
        self.cloud.step_changed.connect(self.progress.on_cloud_phase)
        self.cloud.progress.connect(self.progress.on_cloud_progress)
        self.cloud.log_line.connect(self.progress.append_log)
        # Signing in may unblock queued cloud jobs left from a prior session.
        self.session.changed.connect(self.cloud.resume)
        # Signing in also kicks an auto-sync sweep of completed local runs into
        # the cloud surface; the controller drives the sidebar "Syncing…" chip.
        self.session.changed.connect(self.sync.resume)
        self.sync.started.connect(self._on_sync_started)
        self.sync.finished.connect(self._on_sync_finished)
        # A just-finished local run is a fresh sync candidate — sweep once it's
        # persisted (no-ops when signed out or already syncing).
        self.queue.job_finished.connect(lambda *_: self.sync.resume())
        # Metadata-sync triggers: sign-in flushes edits made offline; a finished
        # cloud run or completed local sync gives a run its server id (so its
        # metadata becomes pushable); and editing a sample pushes right away.
        self.session.changed.connect(self.meta_sync.resume)
        self.cloud.job_finished.connect(lambda *_: self.meta_sync.resume())
        self.sync.finished.connect(lambda *_: self.meta_sync.resume())
        self.dashboard.metadata_edited.connect(
            lambda *_: self.meta_sync.resume())
        # Live run (folder-watch) wiring. Starting a live run collapses the
        # Progress screen to the coverage grid; the controller drives it each
        # poll cycle. "Finalize now" (or reaching saturation, if the operator
        # clicks) hands the same config to the normal queue for the full run.
        self.jobs.live_run_requested.connect(self._on_live_run)
        self.live.started.connect(self._on_live_started)
        self.live.coverage_updated.connect(self.progress.on_coverage_updated)
        self.live.cycle_log.connect(self.progress.append_log)
        self.live.saturated.connect(self.progress.set_finalize_enabled)
        self.progress.finalize_requested.connect(self.live.finalize)
        self.live.finalize_requested.connect(self._on_live_finalize)
        self.live.stopped.connect(self._on_live_stopped)
        self.progress.stop_requested.connect(self._stop_active_job)
        # Report designer: previewing renders the active tab's report from the
        # loaded run so the user sees their branding/section choices applied
        # immediately. The scope decides which builder runs.
        self.report_settings.preview_requested.connect(self._on_preview_report)
        # Prime Trends once at startup so it aggregates existing runs.
        self.trends.refresh()

    def _on_preview_report(self, scope):
        """Route a Report-settings preview to the matching report builder."""
        if scope == "overview":
            self.dashboard.preview_overview_report()
        else:
            self.dashboard.preview_report()

    def _stop_active_job(self):
        """Stop whichever run is currently active (live watch or a queue)."""
        if self.live.is_busy():
            self.live.stop()
        elif self.cloud.is_busy():
            self.cloud.stop_active()
        else:
            self.queue.stop_active()

    def _on_add_job(self, config_id, sheet):
        # Route through the provider named by the saved config. The cloud
        # provider raises PermissionError (signed out) or NotImplementedError
        # (no server configured), both caught below to show a non-blocking
        # notice — the config row is already persisted, so nothing is lost.
        cfg = db.get_config(config_id) or {}
        target = cfg.get("execution_target") or "local"
        provider = self._providers.get(target, self._providers["local"])
        try:
            job_id = provider.add_job(config_id)
        except PermissionError:
            # Cloud target chosen while signed out: offer the login dialog
            # inline. The config is already saved, so re-submitting after
            # sign-in loses nothing.
            if QMessageBox.question(
                    self, "Sign in required",
                    "Cloud jobs need you to be signed in. Sign in now?",
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.Yes) == QMessageBox.Yes:
                LoginDialog(self.session, parent=self).exec()
            return
        except NotImplementedError:
            QMessageBox.information(
                self, "Cloud server not configured",
                "No cloud server is configured. Set PF_CLOUD_API_URL to your "
                "pipeline API and relaunch. Your job configuration has been "
                "saved and can be run once cloud is reachable.")
            return
        except Exception as e:
            QMessageBox.critical(self, "Could not start job", str(e))
            return
        # Only one job runs at a time across both queues. If the other queue is
        # busy, this job was enqueued but not started — tell the user it's
        # queued (the Jobs list shows it as "queued") so a click that didn't
        # switch to Progress doesn't look like nothing happened.
        controller = self.cloud if target == "cloud" else self.queue
        if controller.active_job_id != job_id:
            QMessageBox.information(
                self, "Job queued",
                "A run is already in progress. This job has been queued and "
                "will start automatically when the current run finishes.")
        # Persist any sample labels captured at creation. Audit provenance is
        # recorded inside the DAO; this is best-effort and never blocks the job.
        for barcode, fields in (sheet or {}).items():
            try:
                db.upsert_sample_meta(
                    job_id, barcode, fields, source="creation")
            except Exception:
                continue

    def _on_live_run(self, config_id, sheet):
        """Start a folder-watch live run for a just-saved config.

        Guards against overlap: a live run and a normal job never run at the
        same time (they share the single Progress console). The sample sheet is
        held until finalize, when the real job that carries it is created.
        """
        if self.queue.is_busy() or self.cloud.is_busy() or self.live.is_busy():
            QMessageBox.information(
                self, "Run in progress",
                "A run is already in progress. Finish or stop it before "
                "starting a live run.")
            return
        self._live_sheet = sheet or {}
        try:
            self.live.start(config_id)
        except Exception as e:
            QMessageBox.critical(self, "Could not start live run", str(e))

    def _on_live_started(self, config_id):
        """Enter the Progress screen's live mode for the started watch loop."""
        cfg = db.get_config(config_id)
        watch = paths.resolve_barcode_root(cfg["fastq_dir"]) if cfg else ""
        barcodes = paths.discover_barcodes(watch) if watch else []
        self.progress.enter_live_mode(barcodes, watch)
        self._set_progress_active(True)
        self._show(self.PAGE_PROGRESS)

    def _on_live_finalize(self, config_id):
        """Hand a finalized live run to the normal queue for the full pipeline.

        Enqueues a standard local job (LIVE_SCAN off) for the same config; the
        usual ``job_started`` path takes over and leaves live mode. Sample
        labels captured at creation are applied to this new job.
        """
        try:
            job_id = self.queue.add_job(config_id)
        except Exception as e:
            QMessageBox.critical(self, "Could not finalize run", str(e))
            return
        for barcode, fields in (self._live_sheet or {}).items():
            try:
                db.upsert_sample_meta(
                    job_id, barcode, fields, source="creation")
            except Exception:
                continue
        self._live_sheet = {}

    def _on_live_stopped(self):
        """A live run was stopped without finalizing: leave the Progress mode."""
        self._set_progress_active(False)
        self._show(self.PAGE_JOBS)

    def _on_job_started(self, job_id):
        job = db.get_job(job_id)
        cfg = db.get_config(job["config_id"]) if job else None
        barcodes = paths.discover_barcodes(cfg["fastq_dir"]) if cfg else []
        self.progress.begin(job, cfg["name"] if cfg else "", barcodes)
        self._set_progress_active(True)
        self._show(self.PAGE_PROGRESS)

    def _on_job_finished(self, job_id, exit_code):
        self.progress.end()
        # The run is over; grey Progress out again (a peer queue's next job will
        # re-enable it via job_started).
        self._set_progress_active(False)
        job = db.get_job(job_id)
        if exit_code == 0 and job:
            if self.dashboard.load_output_dir(job["output_dir"], job["id"]):
                self._show(self.PAGE_DASHBOARD)
        elif exit_code > 0:
            QMessageBox.warning(
                self, "Job failed",
                "Job %s failed (exit %d). See logs." %
                (str(job_id)[:8], exit_code))

    def _on_open_job(self, job_id):
        """Double-clicking a job reopens its results (or progress) in-app."""
        job = db.get_job(job_id)
        if not job:
            return
        if job["status"] == "running":
            self._show(self.PAGE_PROGRESS)
            return
        # Completed (and finished-but-partial) runs reopen their dashboard;
        # the dashboard itself shows an inline notice if data is missing.
        out = job["output_dir"]
        if out and os.path.isdir(out):
            self.dashboard.load_output_dir(out, job["id"])
            self._show(self.PAGE_DASHBOARD)
        else:
            QMessageBox.information(
                self, "Results unavailable",
                "This job's output folder no longer exists:\n%s"
                % (out or "\u2014"))

    def _on_open_sample_report(self, job_id, sample):
        """Open one specimen's report from the patient-search registry.

        Reuses the dashboard path (load the run's output dir, then regenerate
        and view the per-sample PDF). Surveillance rows outlive their output
        dir, so a pruned run still lists but reports its artifacts are gone.
        """
        job = db.get_job(job_id)
        out = job["output_dir"] if job else None
        if out and os.path.isdir(out) and \
                self.dashboard.load_output_dir(out, job["id"]):
            self.dashboard._sample_report(sample)
        else:
            QMessageBox.information(
                self, "Report unavailable",
                "This specimen's metadata is retained, but the run's output "
                "artifacts are no longer on disk, so its report can't be "
                "regenerated.")

    def closeEvent(self, event):
        if self.queue.is_busy() or self.cloud.is_busy() or self.live.is_busy():
            resp = QMessageBox.question(
                self, "Job running",
                "A job is still running. Stop it and quit?")
            if resp != QMessageBox.Yes:
                event.ignore()
                return
            self.queue.stop_active()
            self.cloud.stop_active()
            self.live.stop()
        # Join any in-flight auto-sync so Qt doesn't destroy a running thread.
        self.sync.shutdown()
        self.meta_sync.shutdown()
        event.accept()


def main():
    # The sample editor's collection-site map uses QtWebEngine, which requires
    # shared OpenGL contexts to be enabled *before* the QApplication is built.
    # Harmless when the web map isn't used; set unconditionally so the flag is
    # always in place by the time any dialog constructs a QWebEngineView.
    QApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)

    # HiDPI: scale the UI to the screen's device-pixel ratio and let QIcon/
    # QPixmap supply @2x bitmaps, so the QPainter-drawn nav glyphs render crisp
    # on Retina instead of being upscaled 1x -> pixelated. Must precede the
    # QApplication constructor.
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)

    # Create the QApplication and set its names FIRST: the user-data dir (and
    # therefore the DB path) is resolved via QStandardPaths, which keys off the
    # application name. Initialising the DB before this would write the tables
    # to a different folder than the screens later read from.
    app = QApplication(sys.argv)
    app.setApplicationName(paths.APP_NAME)
    app.setOrganizationName(paths.ORG_NAME)
    icon = QIcon(paths.app_icon_path())
    if not icon.isNull():
        app.setWindowIcon(icon)
    app.setStyleSheet(theme.APP_QSS)

    db.init_db()
    db.reset_running_jobs()

    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
