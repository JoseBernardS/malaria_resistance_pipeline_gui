"""Results dashboard: summary cards, tabbed tables, charts and exports.

Loads the three CSVs from ``<output_dir>/final_reports`` via the report
module's own loaders so the data and color language match the PDF exactly.
"""

import json
import os
import re
import subprocess
import sys

from PyQt5.QtCore import (QDate, QObject, QPoint, QRect, QRectF, QSize,
                          QStringListModel, Qt, QThread, QTimer, pyqtSignal)
from PyQt5.QtGui import (QColor, QFont, QFontMetrics, QPainter, QPen, QPixmap,
                         QTextCharFormat, QTextDocument)
from PyQt5.QtWidgets import (QAbstractItemView, QComboBox, QCompleter,
                             QDateEdit, QDialog, QDialogButtonBox, QFileDialog,
                             QFormLayout, QFrame, QGridLayout, QHBoxLayout,
                             QHeaderView, QLabel, QLineEdit, QMenu, QMessageBox,
                             QPushButton, QScrollArea, QSizePolicy, QSlider,
                             QSpinBox, QSplitter,
                             QStackedWidget, QStyle, QStyledItemDelegate,
                             QTableWidget, QTableWidgetItem, QTextEdit,
                             QTreeWidget, QTreeWidgetItem, QVBoxLayout,
                             QWidget)

from .. import db, geo, paths, proteins, theme
from ..charts import BarChart, CoverageChart, GhanaPicker, LollipopChart
from ..webmap import WEBENGINE_AVAILABLE, WebMapPicker
from ..widgets import (COVERAGE_COLORS, PALETTE, FlowLayout, SearchableComboBox,
                       card, fit_combo_popup, hrule)

def _reference_label(output_dir):
    """The reference-set name a run used, read from its output manifest.

    Provenance is read straight from the artifact (``manifest.json`` /
    ``provenance.json`` at the output root), not the DB, so it is correct even
    for a folder opened without a job row and never drifts from a later config
    edit. Returns the label or ``None`` when no manifest/token is present.
    """
    if not output_dir:
        return None
    for name in ("manifest.json", "provenance.json"):
        path = os.path.join(output_dir, name)
        if not os.path.isfile(path):
            continue
        try:
            with open(path) as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        # Prefer the top-level token; fall back to the nested reference block
        # and the legacy provenance schema's reference_release.
        ref = data.get("reference")
        return (data.get("reference_version")
                or (ref.get("version") if isinstance(ref, dict) else None)
                or (data.get("sources", {}) or {}).get("reference_release"))
    return None


# Custom item role carrying the semantic colour for a status cell, so the
# delegate can paint a soft clinical "pill" instead of plain coloured text.
BADGE_ROLE = Qt.UserRole + 1
# Cells opting in via this role are drawn as an unfilled, dashed-outline tile
# (used for "not assessed" — absence of data reads better as a hollow box than
# a solid fill that looks like a positive category).
OUTLINE_ROLE = Qt.UserRole + 2


class BadgeDelegate(QStyledItemDelegate):
    """Paints status cells as bold colour-coded text; plain text otherwise.

    A cell opts in by carrying a colour string under ``BADGE_ROLE`` and the
    label is drawn in that solid semantic colour — the same red/amber/green
    language as the printed report, with no background tint so the table reads
    as clean data.
    """

    def paint(self, painter, option, index):
        color = index.data(BADGE_ROLE)
        text = index.data(Qt.DisplayRole) or ""
        if not color or not text:
            super().paint(painter, option, index)
            return
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        if option.state & QStyle.State_Selected:
            painter.fillRect(option.rect, QColor(theme.ACCENT_WASH))

        c = QColor(color)
        fm = option.fontMetrics
        rect = option.rect
        f = QFont(option.font)
        f.setBold(True)
        painter.setFont(f)
        painter.setPen(c)
        text_rect = rect.adjusted(10, 0, -8, 0)
        elided = fm.elidedText(text, Qt.ElideRight, text_rect.width())
        painter.drawText(text_rect, Qt.AlignVCenter | Qt.AlignLeft, elided)
        painter.restore()


class HeatmapDelegate(QStyledItemDelegate):
    """Fills a heatmap cell with the solid tier colour carried on
    ``BADGE_ROLE`` as an inset rounded tile.

    Painting directly is necessary because the app stylesheet
    (``QTableWidget::item``) overrides ``QTableWidgetItem.setBackground`` — so
    a plain background brush never shows. Cells with no colour (the sample
    label column) fall through to the default text painter.
    """

    def paint(self, painter, option, index):
        selected = bool(option.state & QStyle.State_Selected)
        color = index.data(BADGE_ROLE)
        if not color:
            # Sample-label column: strip the selected state so no wash/box is
            # painted behind the name — the accent outline below is the only
            # selection cue, so the selected row is never greyed out.
            if selected:
                option.state &= ~QStyle.State_Selected
            super().paint(painter, option, index)
        else:
            painter.save()
            painter.setRenderHint(QPainter.Antialiasing, True)
            rect = QRectF(option.rect).adjusted(2.5, 2.5, -2.5, -2.5)
            if index.data(OUTLINE_ROLE):
                # "Not assessed": a hollow tile with a dashed outline — signals
                # absence of data rather than a measured category.
                pen = QPen(QColor(color))
                pen.setWidthF(1.3)
                pen.setStyle(Qt.DashLine)
                painter.setPen(pen)
                painter.setBrush(Qt.NoBrush)
            else:
                painter.setPen(Qt.NoPen)
                painter.setBrush(QColor(color))
            painter.drawRoundedRect(rect, 4, 4)
            painter.restore()
        if selected:
            self._paint_row_border(painter, option, index)

    def _paint_row_border(self, painter, option, index):
        """Outline the selected row with a prominent accent border.

        Each selected cell draws the shared top/bottom edges, and the first and
        last columns cap the ends, so the per-cell painting joins into a single
        box around the whole row — a clearer "this is selected" cue than a wash.
        """
        painter.save()
        pen = QPen(QColor(theme.ACCENT))
        pen.setWidth(2)
        painter.setPen(pen)
        r = option.rect
        top, bottom = r.top() + 1, r.bottom() - 1
        painter.drawLine(r.left(), top, r.right(), top)
        painter.drawLine(r.left(), bottom, r.right(), bottom)
        last = index.model().columnCount() - 1
        if index.column() == 0:
            painter.drawLine(r.left() + 1, top, r.left() + 1, bottom)
        if index.column() == last:
            painter.drawLine(r.right() - 1, top, r.right() - 1, bottom)
        painter.restore()


class RotatedHeader(QHeaderView):
    """Horizontal header that paints selected sections' labels on a 45\u00b0
    diagonal, so long drug names read at a glance above narrow heatmap tiles
    without the head-tilt of fully vertical text.

    Sections before ``rotate_from`` (the wide Sample column) and any empty
    label paint with the normal horizontal style, so only the tile columns are
    angled.
    """

    _ANGLE = 45
    _COS = 0.70710678  # cos/sin of 45\u00b0, for the diagonal's screen extent

    def __init__(self, rotate_from=1, parent=None):
        super().__init__(Qt.Horizontal, parent)
        self._rotate_from = rotate_from
        self.setHighlightSections(False)
        self.setSectionsClickable(False)

    def _label(self, index):
        return str(self.model().headerData(index, Qt.Horizontal) or "")

    def _font(self):
        f = QFont(self.font())
        f.setBold(True)
        f.setPointSize(max(1, f.pointSize() - 1) if f.pointSize() > 0 else 10)
        return f

    def paintEvent(self, event):
        # Fill the whole header once up front so a label ascending into the
        # next column's space is not erased by that section's own fill (which
        # would clip every short label down to a few characters).
        p = QPainter(self.viewport())
        p.fillRect(self.viewport().rect(), QColor(theme.SURFACE))
        p.end()
        super().paintEvent(event)

    def paintSection(self, painter, rect, index):
        text = self._label(index)
        if index < self._rotate_from or not text:
            super().paintSection(painter, rect, index)
            return
        painter.save()
        # Background is pre-filled in paintEvent; here just underline the labels
        # with a hairline and draw the angled text over the shared surface.
        painter.setPen(QColor(theme.BORDER))
        painter.drawLine(rect.bottomLeft(), rect.bottomRight())
        painter.setFont(self._font())
        painter.setPen(QColor(theme.TEXT))
        painter.setRenderHint(QPainter.Antialiasing, True)
        # Anchor at the column's bottom-centre and let the label ascend to the
        # upper-right at 45\u00b0; painting is not clipped to the section, so each
        # label rises over the trailing header space like a staircase.
        fm = QFontMetrics(self._font())
        painter.translate(rect.center().x(), rect.bottom() - 5)
        painter.rotate(-self._ANGLE)
        box = QRectF(6, -fm.height() / 2.0,
                     fm.horizontalAdvance(text) + 8, fm.height())
        painter.drawText(box, Qt.AlignLeft | Qt.AlignVCenter, text)
        painter.restore()

    def header_height(self, labels):
        """Diagonal rise of the longest label + padding, for sizing the table."""
        fm = QFontMetrics(self._font())
        longest = max((fm.horizontalAdvance(t) for t in labels), default=0)
        return int((longest + fm.height()) * self._COS) + 16

    def sectionSizeFromContents(self, index):
        text = self._label(index)
        if index < self._rotate_from or not text:
            return super().sectionSizeFromContents(index)
        fm = QFontMetrics(self._font())
        h = int((fm.horizontalAdvance(text) + fm.height()) * self._COS) + 16
        return QSize(fm.height() + 10, h)


# Reuse the report's loaders + semantic helpers.
sys.path.insert(0, paths.src_dir())
try:
    from generate_report import (STATUS_META, all_samples, classify_tier,
                                 coverage_index, drug_status, load_data,
                                 load_qc, panel_drugs)
except Exception:  # pragma: no cover
    STATUS_META = {}
    load_data = all_samples = coverage_index = load_qc = None
    drug_status = panel_drugs = classify_tier = None

# Plain-language meaning of each resistance tier, for the legend "?" popups.
# Keyed by STATUS_META status key; falls back to the report's short label.
TIER_HELP = {
    "validated": (
        "Resistant \u2014 a validated resistance marker was detected. This "
        "mutation has confirmed evidence (clinical and laboratory) linking it "
        "to reduced response to this drug, so the parasite is expected to be "
        "resistant."),
    "candidate": (
        "Candidate \u2014 a candidate resistance marker was detected. This "
        "mutation is associated with resistance and evidence is building, but "
        "it is not yet fully validated. Treat as a likely warning sign."),
    "potential": (
        "Potential \u2014 a potential resistance marker was detected. Only "
        "preliminary or limited evidence links it to resistance; its "
        "significance is uncertain and worth monitoring."),
    "nomarker": (
        "No marker \u2014 none of the known resistance markers for this drug "
        "were found at the positions that were sequenced. No genetic evidence "
        "of resistance in this sample."),
    "notassessed": (
        "Not assessed \u2014 the marker positions for this drug could not be "
        "read because sequencing coverage was missing or too low, so the "
        "resistance status is unknown (not the same as 'no resistance')."),
}


def _open_path(path):
    if sys.platform == "darwin":
        subprocess.Popen(["open", path])
    elif sys.platform.startswith("linux"):
        subprocess.Popen(["xdg-open", path])


def _fmt_num(value):
    """Pretty-print a QC metric: thousands separators, trimmed decimals."""
    if value is None or value == "":
        return "\u2014"
    try:
        f = float(value)
    except (TypeError, ValueError):
        return str(value)
    if f == int(f):
        return "{:,}".format(int(f))
    return "{:,.2f}".format(f)


class _ClickableLabel(QLabel):
    """A QLabel that emits ``clicked`` on a left mouse press."""

    clicked = pyqtSignal()

    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(ev)


class _HelpBadge(QLabel):
    """A small circular "?" that reveals its explanation on hover or click.

    Uses a real floating ``Qt.ToolTip`` label (shown on ``enterEvent``, hidden
    on ``leaveEvent``) rather than ``QToolTip.showText``, which proved flaky
    here — the tip was dismissed by mouse jitter before it appeared. A click
    toggles the same popup, so it works whether or not hover fires. The popup
    doesn't grab input, so the rest of the UI stays interactive.
    """

    def __init__(self, tip, parent=None):
        super().__init__("?", parent)
        self._tip = tip
        self.setAlignment(Qt.AlignCenter)
        self.setFixedSize(15, 15)
        self.setCursor(Qt.PointingHandCursor)
        self._popup = None

    def enterEvent(self, ev):
        self._show_popup()
        super().enterEvent(ev)

    def leaveEvent(self, ev):
        self._hide_popup()
        super().leaveEvent(ev)

    def mousePressEvent(self, ev):
        if self._popup is not None and self._popup.isVisible():
            self._hide_popup()
        else:
            self._show_popup()
        super().mousePressEvent(ev)

    def _show_popup(self):
        if self._popup is None:
            # Bold the leading tier name (the part before the em dash) so the
            # popup echoes the legend label it explains.
            tip = self._tip
            if "\u2014" in tip:
                name, rest = tip.split("\u2014", 1)
                tip = "<b>%s</b> \u2014%s" % (name.strip(), rest)
            pop = QLabel(tip, None, Qt.ToolTip)
            pop.setWordWrap(True)
            pop.setMaximumWidth(300)
            pop.setStyleSheet(
                "QLabel { background:%s; border-radius:6px; "
                "padding:8px 10px; color:%s; font-size:12px; }"
                % (theme.SURFACE, theme.HEADING))
            pop.adjustSize()
            self._popup = pop
        self._popup.move(self.mapToGlobal(QPoint(0, self.height() + 4)))
        self._popup.show()

    def _hide_popup(self):
        if self._popup is not None:
            self._popup.hide()


class HelpHeader(QHeaderView):
    """Table header that carries a circular "?" badge *after* each title,
    reusing the very :class:`_HelpBadge` widget from the legend so the help
    affordance looks identical everywhere.

    A ``QHeaderView::section`` stylesheet makes custom ``paintSection`` drawing
    invisible, so the badge can't be painted — instead a real ``_HelpBadge`` is
    overlaid on the header, positioned just past each title and kept in place as
    the sections resize or move. ``help_map`` maps a logical column index to its
    explanation; ``titles`` maps that column to the plain title text (without
    the reserved trailing padding) so the badge can sit snug after the words.
    """

    _GAP = 5          # px between the title text and its badge

    def __init__(self, help_map, titles, parent=None):
        super().__init__(Qt.Horizontal, parent)
        self._help = dict(help_map)
        self._titles = dict(titles)
        self.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.setHighlightSections(False)
        self.setSectionsClickable(False)
        self._badges = {}
        for col, tip in self._help.items():
            badge = _HelpBadge(tip, self)
            badge.setStyleSheet(
                "QLabel { border:1px solid %s; border-radius:7px; color:%s; "
                "font-size:10px; font-weight:700; }"
                % (theme.BORDER, theme.MUTED))
            self._badges[col] = badge
        self.sectionResized.connect(self._place_badges)
        self.sectionMoved.connect(self._place_badges)

    def _left_pad(self):
        # Matches QHeaderView::section { padding: 8px 10px } in the theme QSS.
        return 10

    def _place_badges(self, *args):
        fm = QFontMetrics(self.font())
        for col, badge in self._badges.items():
            if self.isSectionHidden(col):
                badge.hide()
                continue
            title = self._titles.get(col, "")
            x = (self.sectionViewportPosition(col) + self._left_pad()
                 + fm.horizontalAdvance(title) + self._GAP)
            y = max(0, (self.height() - badge.height()) // 2)
            badge.move(x, y)
            badge.show()

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._place_badges()

    def showEvent(self, ev):
        super().showEvent(ev)
        self._place_badges()

    def paintEvent(self, ev):
        # Horizontal scrolling shifts the sections via the header's offset but
        # emits no resize/move signal, so re-anchor the badges on every repaint
        # (which a scroll triggers) to keep each one glued to its title.
        super().paintEvent(ev)
        self._place_badges()


class _ImageZoomDialog(QDialog):
    """Full-resolution QC plot in a scroll area driven by a zoom slider."""

    _MIN, _MAX = 25, 400          # zoom percent

    def __init__(self, path, title="", parent=None):
        super().__init__(parent)
        self.setWindowTitle(title or "Plot")
        self._src = QPixmap(path)
        self.resize(940, 700)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(8)

        bar = QHBoxLayout()
        bar.setSpacing(8)
        zlabel = QLabel("Zoom")
        zlabel.setStyleSheet("color:%s;" % theme.MUTED)
        self._slider = QSlider(Qt.Horizontal)
        self._slider.setRange(self._MIN, self._MAX)
        self._slider.setValue(100)
        self._pct = QLabel("100%")
        self._pct.setMinimumWidth(44)
        self._pct.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        bar.addWidget(zlabel)
        bar.addWidget(self._slider, 1)
        bar.addWidget(self._pct)
        lay.addLayout(bar)

        self._view = QScrollArea()
        self._view.setWidgetResizable(False)
        self._view.setAlignment(Qt.AlignCenter)
        self._img = QLabel()
        self._img.setAlignment(Qt.AlignCenter)
        self._view.setWidget(self._img)
        lay.addWidget(self._view, 1)

        self._slider.valueChanged.connect(self._apply)
        self._apply(100)

    def _apply(self, pct):
        self._pct.setText("%d%%" % pct)
        if self._src.isNull():
            return
        w = max(1, int(round(self._src.width() * pct / 100.0)))
        scaled = self._src.scaledToWidth(w, Qt.SmoothTransformation)
        self._img.setPixmap(scaled)
        self._img.resize(scaled.size())


class DashboardScreen(QWidget):
    """Results view for a completed run's ``output_dir``."""

    # Emitted (with the job id) whenever a sample's metadata is edited, so the
    # app can push the change to the run's cloud twin if it has one.
    metadata_edited = pyqtSignal(str)

    # Emitted when the empty-state "Open a run" button is clicked, so the app
    # can switch to the Jobs page (there are no completed runs to show yet).
    open_jobs_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._reports_dir = None
        self._output_dir = None
        self._job_id = None
        self._alias_map = {}     # sample -> alias (for "alias (barcode)" display)
        self._data = None  # (calls, variants, coverage, samples, cov_idx, qc)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(16)

        header = QHBoxLayout()
        col = QVBoxLayout()
        col.setSpacing(2)
        page_title = QLabel("Results")
        page_title.setObjectName("PageTitle")
        self.title = QLabel("No run loaded")
        self.title.setObjectName("PageHint")
        # Compact provenance line: which reference set produced these results,
        # read from the run's output manifest so "what produced this" is obvious.
        self.subtitle = QLabel("")
        self.subtitle.setObjectName("PageHint")
        self.subtitle.setStyleSheet("color:%s;" % theme.FAINT)
        self.subtitle.hide()
        col.addWidget(page_title)
        col.addWidget(self.title)
        col.addWidget(self.subtitle)
        header.addLayout(col)
        header.addStretch(1)

        # Run picker: switch between completed runs without going back to Jobs.
        # Populated from the DB (newest first) by ``refresh_runs``; selecting an
        # entry loads that run's output dir. Hidden while there are no runs.
        self.run_combo = QComboBox()
        self.run_combo.setObjectName("RunPicker")
        self.run_combo.setCursor(Qt.PointingHandCursor)
        self.run_combo.setMinimumWidth(240)
        self.run_combo.setToolTip("Switch to another completed run")
        fit_combo_popup(self.run_combo)
        self.run_combo.activated.connect(self._on_pick_run)
        self.run_combo.hide()
        self._runs = []            # [(job_id, output_dir)], newest first
        header.addWidget(self.run_combo, 0, Qt.AlignTop)

        view_btn = QPushButton("View report")
        view_btn.setObjectName("Primary")
        view_btn.setCursor(Qt.PointingHandCursor)
        view_btn.clicked.connect(self._view_pdf)
        header.addWidget(view_btn, 0, Qt.AlignTop)

        # A single Export menu replaces the row of export buttons; each action
        # calls the existing slot unchanged.
        export_btn = QPushButton("Export \u25be")
        export_btn.setCursor(Qt.PointingHandCursor)
        menu = QMenu(export_btn)
        for text, slot in (("Open folder", self._open_folder),
                           ("Open QC diagrams", self._open_qc),
                           ("Export CSV", self._export_csv),
                           ("Export Excel", self._export_excel),
                           ("Export PDF", self._export_pdf)):
            menu.addAction(text, slot)
        export_btn.setMenu(menu)
        header.addWidget(export_btn, 0, Qt.AlignTop)
        root.addLayout(header)

        # A single section stack replaces the old QTabWidget; the hierarchical
        # sidebar navigates between named sections, each rebuilt on render.
        self.sections = QStackedWidget()
        root.addWidget(self.sections, 1)
        self._section_index = {}        # section name -> stack index
        self._pending_section = "overview"

        # Full-page empty state, shown only when no run is loaded (which, given
        # auto-load of the latest run at startup, means there are no completed
        # runs yet). A single call-to-action jumps to the Jobs list.
        self.empty = QWidget()
        empty_lay = QVBoxLayout(self.empty)
        empty_lay.setAlignment(Qt.AlignCenter)
        empty_lay.setSpacing(10)
        self._empty_title = QLabel("No completed runs yet")
        self._empty_title.setAlignment(Qt.AlignCenter)
        self._empty_body = QLabel(
            "Results appear here once a job finishes.\n"
            "Add a run from the Jobs page to get started.")
        self._empty_body.setAlignment(Qt.AlignCenter)
        self._empty_body.setWordWrap(True)
        empty_btn = QPushButton("Open a run")
        empty_btn.setObjectName("Primary")
        empty_btn.setCursor(Qt.PointingHandCursor)
        empty_btn.clicked.connect(self.open_jobs_requested)
        empty_lay.addWidget(self._empty_title)
        empty_lay.addWidget(self._empty_body)
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        btn_row.addWidget(empty_btn)
        btn_row.addStretch(1)
        empty_lay.addLayout(btn_row)
        self._style_empty()
        root.addWidget(self.empty)

    # -- loading ---------------------------------------------------------
    def load_output_dir(self, output_dir, job_id=None):
        """Point the dashboard at an output dir (containing final_reports).

        ``job_id`` ties the run to its persisted sample metadata so the
        Samples tab is editable and aliases display; pass None (e.g. for an
        ad-hoc folder open) for a read-only view.
        """
        if load_data is None:
            QMessageBox.critical(self, "Report module unavailable",
                                 "Could not import src/generate_report.py.")
            return False
        reports_dir = os.path.join(output_dir, "final_reports")
        if not os.path.isdir(reports_dir):
            # allow passing the reports dir directly
            if os.path.isfile(os.path.join(output_dir, "resistance_calls.csv")):
                reports_dir = output_dir
            else:
                self._show_empty("No final_reports found in %s" % output_dir)
                return False
        try:
            calls, variants, coverage = load_data(reports_dir)
        except SystemExit as e:
            self._show_empty(str(e))
            return False
        samples = all_samples(calls, variants, coverage)
        cov_idx = coverage_index(coverage)
        qc_dir = os.path.join(output_dir, "qc_trimmed")
        qc = load_qc(qc_dir, samples)

        self._reports_dir = reports_dir
        self._output_dir = output_dir
        self._job_id = job_id
        self._data = (calls, variants, coverage, samples, cov_idx, qc)
        self.empty.hide()
        self._render()
        self._sync_picker()
        return True

    # -- run picker ------------------------------------------------------
    def refresh_runs(self):
        """Repopulate the run picker from the DB and re-point it at the loaded
        run. Safe to call whenever the job list changes."""
        self._reload_run_list()
        self._sync_picker()

    def _reload_run_list(self):
        """Fill the picker with completed runs that still have their output on
        disk, newest first."""
        import time
        self.run_combo.blockSignals(True)
        self.run_combo.clear()
        self._runs = []
        for job in db.list_jobs():
            if job.get("status") != "completed":
                continue
            out = job.get("output_dir")
            if not out or not os.path.isdir(out):
                continue
            label = job.get("config_name") or ("Job %s" % str(job["id"])[:8])
            when = job.get("finished_at")
            if when:
                label += "  \u00b7 %s" % time.strftime(
                    "%Y-%m-%d", time.localtime(when))
            self.run_combo.addItem(label, job["id"])
            self._runs.append((job["id"], out))
        fit_combo_popup(self.run_combo)
        self.run_combo.setVisible(self.run_combo.count() > 0)
        self.run_combo.blockSignals(False)

    def _sync_picker(self):
        """Select the loaded run in the picker without triggering a reload.

        If the loaded run isn't in the list yet (e.g. a run opened from an
        ad-hoc folder, or one whose status hasn't flipped to completed), add a
        transient entry so the picker always reflects what's on screen.
        """
        self.run_combo.blockSignals(True)
        idx = self.run_combo.findData(self._job_id) if self._job_id else -1
        if idx < 0 and self._job_id and self._output_dir:
            label = os.path.basename(self._output_dir.rstrip("/")) or "This run"
            self.run_combo.addItem(label, self._job_id)
            self._runs.append((self._job_id, self._output_dir))
            idx = self.run_combo.findData(self._job_id)
            self.run_combo.setVisible(True)
        self.run_combo.setCurrentIndex(idx)
        self.run_combo.blockSignals(False)

    def _on_pick_run(self, index):
        """Load the run chosen in the picker (no-op if it's already shown)."""
        jid = self.run_combo.itemData(index)
        if not jid or jid == self._job_id:
            return
        out = dict(self._runs).get(jid)
        if out and os.path.isdir(out):
            self.load_output_dir(out, jid)

    def autoload_latest(self):
        """Open the most recent completed run if nothing is loaded yet, so the
        Results pages are populated on launch instead of blank."""
        if self._data is not None:
            return
        self._reload_run_list()
        if self._runs:
            jid, out = self._runs[0]   # list_jobs is newest-first
            self.load_output_dir(out, jid)

    def _style_empty(self):
        self._empty_title.setStyleSheet(
            "color:%s; font-size:16px; font-weight:600;" % theme.HEADING)
        self._empty_body.setStyleSheet(
            "color:%s; font-size:13px;" % theme.MUTED)

    def _empty_panel(self, heading, body):
        """A soft, centered 'nothing here — and that's fine' card used inside a
        section (e.g. a run with no resistance calls). Distinct from the
        full-page ``self.empty`` state (which means no run is loaded)."""
        box = QFrame()
        box.setObjectName("EmptyPanel")
        box.setStyleSheet(
            "QFrame#EmptyPanel { background:%s; border:1px solid %s;"
            " border-radius:10px; }" % (theme.ACCENT_WASH, theme.BORDER))
        box.setMaximumWidth(560)
        inner = QVBoxLayout(box)
        inner.setContentsMargins(28, 24, 28, 24)
        inner.setSpacing(6)
        h = QLabel(heading)
        h.setAlignment(Qt.AlignCenter)
        h.setStyleSheet(
            "color:%s; font-size:14.5px; font-weight:600;" % theme.HEADING)
        b = QLabel(body)
        b.setAlignment(Qt.AlignCenter)
        b.setWordWrap(True)
        b.setStyleSheet("color:%s; font-size:12.5px;" % theme.MUTED)
        inner.addWidget(h)
        inner.addWidget(b)

        wrap = QWidget()
        wl = QVBoxLayout(wrap)
        wl.setContentsMargins(0, 0, 0, 0)
        wl.addStretch(1)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(box)
        row.addStretch(1)
        wl.addLayout(row)
        wl.addStretch(1)
        return wrap

    def _show_empty(self, msg=None):
        """Reveal the full-page empty state. ``msg`` (a load failure) is shown
        as the body under a 'Results unavailable' heading; without one it reads
        as the first-run 'no completed runs yet' prompt."""
        if msg:
            self._empty_title.setText("Results unavailable")
            self._empty_body.setText(msg)
        else:
            self._empty_title.setText("No completed runs yet")
            self._empty_body.setText(
                "Results appear here once a job finishes.\n"
                "Add a run from the Jobs page to get started.")
        self._style_empty()
        self.empty.show()

    # -- rendering -------------------------------------------------------
    def _disp(self, sample):
        """Display label for a sample: ``"alias (barcode)"`` when aliased."""
        alias = self._alias_map.get(sample)
        if alias and alias != sample:
            return "%s (%s)" % (alias, sample)
        return sample

    def _render(self):
        calls, variants, coverage, samples, cov_idx, qc = self._data
        self.title.setText(os.path.basename(self._output_dir.rstrip("/")))

        # Provenance line: the reference set that produced these results, from
        # the output manifest. Hidden when the run predates manifest emission.
        ref_label = _reference_label(self._output_dir)
        if ref_label:
            self.subtitle.setText("Reference set: %s" % ref_label)
            self.subtitle.show()
        else:
            self.subtitle.hide()

        # Alias map drives the "alias (barcode)" display in every tab. Lookups
        # elsewhere keep using the raw sample id; only the shown text changes.
        self._alias_map = {}
        if self._job_id:
            for s, m in db.list_sample_meta(self._job_id).items():
                if m.get("alias"):
                    self._alias_map[s] = m["alias"]

        # Rebuild the section stack, then show whichever section the sidebar
        # last requested (default: Overview). Charts are filled inside the
        # build once their widgets exist.
        self._build_sections(calls, variants, coverage, samples, cov_idx, qc)
        self.show_section(self._pending_section or "overview")

    def restyle(self):
        """Re-render with the active theme so colours baked into table/label
        items pick up the new palette."""
        if self._data is not None:
            self._render()
        else:
            self._style_empty()

    # -- tab builders ----------------------------------------------------
    def _table(self, headers, rows, color_fn=None, bold_first=True,
               tip_fn=None):
        t = QTableWidget(len(rows), len(headers))
        t.setHorizontalHeaderLabels(headers)
        t.setEditTriggers(QTableWidget.NoEditTriggers)
        t.setAlternatingRowColors(True)
        t.setShowGrid(False)
        t.setSelectionBehavior(QAbstractItemView.SelectRows)
        t.verticalHeader().setVisible(False)
        t.verticalHeader().setDefaultSectionSize(36)
        t.setItemDelegate(BadgeDelegate(t))

        badge_cols = set()
        for r, row in enumerate(rows):
            for c, val in enumerate(row):
                item = QTableWidgetItem("" if val is None else str(val))
                if c == 0 and bold_first:
                    f = item.font(); f.setBold(True); item.setFont(f)
                    item.setForeground(QColor(theme.HEADING))
                elif color_fn is None:
                    item.setForeground(QColor(theme.TEXT))
                if color_fn:
                    color = color_fn(r, c, row)
                    if color:
                        item.setData(BADGE_ROLE, color)
                        badge_cols.add(c)
                    elif c != 0:
                        item.setForeground(QColor(theme.MUTED))
                if tip_fn:
                    tip = tip_fn(r, c, row)
                    if tip:
                        item.setToolTip(str(tip))
                t.setItem(r, c, item)

        t.resizeColumnsToContents()
        for c in badge_cols:
            t.setColumnWidth(c, t.columnWidth(c) + 30)
        hh = t.horizontalHeader()
        hh.setStretchLastSection(True)
        hh.setHighlightSections(False)
        if headers:
            hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        return t

    @staticmethod
    def _fit_table(table):
        """Size a table to show every row, with no internal vertical scroll.

        Used when a table sits beneath a chart inside a page-level scroll
        area: the table reports its full height so the *outer* scroll moves
        through chart + all rows, avoiding a nested-scrollbar trap where the
        table would otherwise be squished and unreachable.
        """
        table.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        row_h = table.verticalHeader().defaultSectionSize()
        head_h = table.horizontalHeader().sizeHint().height()
        total = head_h + row_h * table.rowCount() + 4
        table.setFixedHeight(total)
        return table

    # Tier order for severity sorting on the resistance page.
    _TIER_RANK = {"validated": 0, "candidate": 1, "potential": 2}

    def _drug_summary_tab(self, calls):
        """Resistance calls, grouped under a collapsible header per sample.

        Matches the Genes/Mutations tabs: the flat call list becomes a tidy
        index of barcodes. Each parent row carries the sample's *worst* call in
        the Tier cell, so a collapsed run still reads as a per-sample verdict,
        and expands to one row per drug.
        """
        wrap = QWidget()
        lay = QVBoxLayout(wrap)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)
        lay.addWidget(self._drug_summary_tree(calls))
        lay.addStretch(1)
        return wrap

    def _drug_summary_tree(self, calls):
        """Per-drug resistance calls grouped under a collapsible sample header.

        Groups sit worst-first (the most resistant barcodes at the top) and open
        by default, since the calls are the headline of this tab. The parent
        Tier cell shows the barcode's worst call so the verdict survives a
        collapse. The mutation detail is the whole point of this tab, so rather
        than eliding it, Alteration and Evidence get fixed widths and wrap onto
        extra lines — no finding is ever cut off.
        """
        titles = ["Sample \u00b7 Drug", "Tier", "Genes", "Alteration",
                  "Evidence"]
        # Reserve trailing room on each title (non-breaking spaces so the width
        # survives ResizeToContents) for the circular "?" badge the HelpHeader
        # overlays just after the words.
        pad = "\u00a0" * 6
        headers = [t + pad for t in titles]
        tree = QTreeWidget()
        tree.setObjectName("DataTree")
        tree.setColumnCount(len(headers))
        tree.setHeaderLabels(headers)
        tree.setUniformRowHeights(False)
        tree.setWordWrap(True)
        tree.setSelectionMode(QAbstractItemView.NoSelection)
        tree.setEditTriggers(QAbstractItemView.NoEditTriggers)
        tree.setFocusPolicy(Qt.NoFocus)
        tree.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        tree.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        tree.setIndentation(16)
        # Fixed widths for the two detail columns; long findings wrap within
        # them and the child row grows to fit (see the height maths below).
        ALT_W, EVID_W = 180, 480
        # A hover "?" on each title spells out what the column means, so the
        # bioinformatics shorthand isn't a mystery to a clinical reader.
        col_help = {
            0: "Resistance calls grouped by barcode. Each parent shows the "
               "sample's worst call; expand it for one row per drug assessed.",
            1: "How strong the evidence is that this parasite resists the "
               "drug \u2014 Resistant (validated marker) \u2192 Candidate "
               "(evidence building) \u2192 Potential (limited/preliminary). "
               "Hover a tier badge for its exact basis.",
            2: "The resistance-marker gene(s) carrying the mutation(s) behind "
               "this call, e.g. pfdhfr, pfdhps.",
            3: "The amino-acid change(s) detected in those genes "
               "(e.g. C59R+N51I+S108N) \u2014 the mutations driving the call.",
            4: "Each mutation mapped to its gene locus "
               "(accession:change, e.g. PF3D7_0417200:C59R) \u2014 the exact "
               "coordinates supporting the call.",
        }
        # A real circular "?" badge (the legend's _HelpBadge) is overlaid just
        # after each title by the HelpHeader, so the affordance reads the same
        # everywhere. ``titles`` (unpadded) lets it sit snug past the words.
        tree.setHeader(HelpHeader(col_help, dict(enumerate(titles)), tree))
        hh = tree.header()
        hh.setHighlightSections(False)
        hh.setStretchLastSection(False)
        hh.setSectionResizeMode(0, QHeaderView.Interactive)
        hh.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.Fixed)
        hh.setSectionResizeMode(4, QHeaderView.Fixed)
        tree.setColumnWidth(3, ALT_W)
        tree.setColumnWidth(4, EVID_W)

        def tier_of(c):
            return classify_tier(c.get("Classification", "")) \
                if classify_tier else None

        groups = {}             # sample -> [call, ...], first-seen order
        for c in calls:
            groups.setdefault(self._disp(c.get("Sample", "")), []).append(c)

        if not groups:
            body = ("No variants in this run matched a catalog resistance "
                    "marker, so no drug-resistance calls were made.")
            return self._empty_panel("No resistance markers detected", body)

        # Worst (lowest-rank) tier per sample sets both the parent badge and the
        # group order, so the most resistant barcodes surface at the top.
        def worst(cs):
            return min((self._TIER_RANK.get(tier_of(c), 3) for c in cs),
                       default=3)
        ordered = sorted(groups.items(), key=lambda kv: (worst(kv[1]), kv[0]))

        bold_fm = QFontMetrics(QFont(tree.font().family(),
                                     tree.font().pointSize(), QFont.Bold))
        fm = QFontMetrics(tree.font())

        def _wrap_h(text, w):
            """Height a wrapped string needs at column width ``w`` (0 if empty)."""
            if not text:
                return 0
            return fm.boundingRect(0, 0, w - 14, 100000,
                                   Qt.TextWordWrap, text).height()

        col0_w = 160                # floor so short labels still look balanced
        for sample, cs in ordered:
            cs = sorted(cs, key=lambda c: (self._TIER_RANK.get(tier_of(c), 3),
                                           c.get("Drug", "")))
            parent = QTreeWidgetItem(tree)
            tag = "  \u00b7  %d call%s" % (len(cs), "" if len(cs) == 1 else "s")
            label = sample + tag
            parent.setText(0, label)
            col0_w = max(col0_w, bold_fm.horizontalAdvance(label) + 48)
            pf = parent.font(0); pf.setBold(True); parent.setFont(0, pf)
            parent.setForeground(0, QColor(theme.HEADING))
            parent.setSizeHint(0, QSize(0, self._TREE_ROW_H))
            w_tier = tier_of(cs[0])          # cs sorted worst-first above
            w_meta = STATUS_META.get(w_tier) if w_tier else None
            if w_meta:
                parent.setText(1, w_meta[0])
                pf1 = parent.font(1); pf1.setBold(True); parent.setFont(1, pf1)
                parent.setForeground(1, QColor(PALETTE.get(w_meta[2])))
            for c in cs:
                tier = tier_of(c)
                meta = STATUS_META.get(tier) if tier else None
                child = QTreeWidgetItem(parent)
                child.setText(0, c.get("Drug", ""))
                child.setText(1, meta[0] if meta
                              else (c.get("Classification", "") or "\u2014"))
                alteration = c.get("Alteration", "")
                evidence = c.get("Evidence", "")
                child.setText(2, c.get("Genes", ""))
                child.setText(3, alteration)
                child.setText(4, evidence)
                # Grow the row to whichever detail column needs the most lines.
                child.setSizeHint(0, QSize(0, max(
                    self._TREE_ROW_H,
                    _wrap_h(alteration, ALT_W) + 12,
                    _wrap_h(evidence, EVID_W) + 12)))
                if meta:
                    cf = child.font(1); cf.setBold(True); child.setFont(1, cf)
                    child.setForeground(1, QColor(PALETTE.get(meta[2])))
                    # Keep the confidence basis (Epi./Lab.) a hover away.
                    child.setToolTip(1, c.get("Classification", ""))
                for col in (0, 2, 3, 4):
                    child.setForeground(col, QColor(
                        theme.HEADING if col == 0 else theme.MUTED))
            parent.setExpanded(True)

        tree.setColumnWidth(0, col0_w)
        self._fit_tree(tree)
        tree.itemExpanded.connect(lambda *_: self._fit_tree(tree))
        tree.itemCollapsed.connect(lambda *_: self._fit_tree(tree))
        return tree

    # Mutation rows grouped under a collapsible header per sample, so a long
    # run reads as a tidy index of barcodes you open on demand rather than one
    # endless flat list. Collapsed by default.
    _TREE_ROW_H = 34

    def _gene_summary_tree(self, coverage):
        """Per-gene coverage grouped under a collapsible header per sample."""
        headers = ["Sample \u00b7 Gene", "Mean depth", "Amplicon depth",
                   "Status"]
        tree = QTreeWidget()
        tree.setObjectName("DataTree")
        tree.setColumnCount(len(headers))
        tree.setHeaderLabels(headers)
        tree.setUniformRowHeights(True)
        tree.setSelectionMode(QAbstractItemView.NoSelection)
        tree.setEditTriggers(QAbstractItemView.NoEditTriggers)
        tree.setFocusPolicy(Qt.NoFocus)
        tree.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        tree.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        tree.setIndentation(16)
        hh = tree.header()
        hh.setHighlightSections(False)
        hh.setStretchLastSection(True)
        hh.setSectionResizeMode(0, QHeaderView.Interactive)

        groups = {}             # sample -> [row, ...], first-seen order
        for r in coverage:
            groups.setdefault(r.get("Sample", ""), []).append(r)

        bold_fm = QFontMetrics(QFont(tree.font().family(),
                                     tree.font().pointSize(), QFont.Bold))
        col0_w = 160                # floor so short labels still look balanced
        for sample, rs in groups.items():
            n_ok = sum(1 for r in rs
                       if (r.get("Status", "") or "").upper() == "OK")
            parent = QTreeWidgetItem(tree)
            tag = "  \u00b7  %d gene%s" % (len(rs), "" if len(rs) == 1 else "s")
            tag += ", %d OK" % n_ok
            label = self._disp(sample) + tag
            parent.setText(0, label)
            col0_w = max(col0_w, bold_fm.horizontalAdvance(label) + 48)
            pf = parent.font(0); pf.setBold(True); parent.setFont(0, pf)
            parent.setForeground(0, QColor(theme.HEADING))
            parent.setSizeHint(0, QSize(0, self._TREE_ROW_H))
            for r in rs:
                child = QTreeWidgetItem(parent)
                child.setText(0, r.get("Gene", "") or r.get("Gene_ID", ""))
                if r.get("Gene_ID"):
                    child.setToolTip(0, r["Gene_ID"])
                child.setText(1, str(r.get("Mean_Depth", "")))
                child.setText(2, str(r.get("Amplicon_Depth", "")))
                status = str(r.get("Status", ""))
                child.setText(3, status)
                child.setSizeHint(0, QSize(0, self._TREE_ROW_H))
                key = COVERAGE_COLORS.get(status.upper())
                if key:
                    cf = child.font(3); cf.setBold(True); child.setFont(3, cf)
                    child.setForeground(3, QColor(PALETTE.get(key)))
                for col in (0, 1, 2):
                    child.setForeground(col, QColor(
                        theme.HEADING if col == 0 else theme.MUTED))
            parent.setExpanded(False)

        tree.setColumnWidth(0, col0_w)
        self._fit_tree(tree)
        tree.itemExpanded.connect(lambda *_: self._fit_tree(tree))
        tree.itemCollapsed.connect(lambda *_: self._fit_tree(tree))
        return tree

    def _mutations_tree(self, variants):
        headers = ["Sample \u00b7 Gene", "Consequence", "AA change",
                   "Catalog status", "QUAL", "DP", "AF"]
        tree = QTreeWidget()
        tree.setObjectName("DataTree")
        tree.setColumnCount(len(headers))
        tree.setHeaderLabels(headers)
        tree.setUniformRowHeights(True)
        tree.setSelectionMode(QAbstractItemView.NoSelection)
        tree.setEditTriggers(QAbstractItemView.NoEditTriggers)
        tree.setFocusPolicy(Qt.NoFocus)
        tree.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        tree.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        tree.setIndentation(16)
        hh = tree.header()
        hh.setHighlightSections(False)
        hh.setStretchLastSection(True)
        # Column 0 carries the long "sample  ·  N variants" group label. We size
        # it to the widest label below (ResizeToContents would instead shrink to
        # the short child gene names and elide the sample names), keeping it
        # user-resizable.
        hh.setSectionResizeMode(0, QHeaderView.Interactive)

        known_c = QColor(PALETTE["known"])
        novel_c = QColor(PALETTE["novel"])

        groups = {}             # sample -> [variant, ...], first-seen order
        for v in variants:
            groups.setdefault(v.get("Sample", ""), []).append(v)

        bold_fm = QFontMetrics(QFont(tree.font().family(),
                                     tree.font().pointSize(), QFont.Bold))
        col0_w = 160                # floor so short labels still look balanced
        for sample, vs in groups.items():
            n_known = sum(1 for v in vs if (v.get("Catalog_status") or "")
                          .lower() == "known_marker_component")
            parent = QTreeWidgetItem(tree)
            tag = "  \u00b7  %d variant%s" % (len(vs), "" if len(vs) == 1 else "s")
            if n_known:
                tag += ", %d known" % n_known
            label = self._disp(sample) + tag
            parent.setText(0, label)
            # Widest bold label (+ indentation, branch arrow, padding) sets the
            # column width so no sample name is clipped.
            col0_w = max(col0_w, bold_fm.horizontalAdvance(label) + 48)
            pf = parent.font(0); pf.setBold(True); parent.setFont(0, pf)
            parent.setForeground(0, QColor(theme.HEADING))
            parent.setSizeHint(0, QSize(0, self._TREE_ROW_H))
            for v in vs:
                child = QTreeWidgetItem(parent)
                child.setText(0, v.get("Gene", "") or v.get("Gene_ID", ""))
                if v.get("Gene_ID"):
                    child.setToolTip(0, v["Gene_ID"])
                child.setText(1, str(v.get("Consequence", "")))
                child.setText(2, str(v.get("AA_Change", "")))
                status = str(v.get("Catalog_status", ""))
                child.setText(3, status)
                child.setText(4, str(v.get("QUAL", "")))
                child.setText(5, str(v.get("DP", "")))
                child.setText(6, str(v.get("AF", "")))
                child.setSizeHint(0, QSize(0, self._TREE_ROW_H))
                s = status.lower()
                if s in ("known_marker_component", "uncharacterized"):
                    cf = child.font(3); cf.setBold(True); child.setFont(3, cf)
                    child.setForeground(
                        3, known_c if s == "known_marker_component" else novel_c)
                for col in (0, 1, 2, 4, 5, 6):
                    if col != 3:
                        child.setForeground(col, QColor(
                            theme.HEADING if col == 0 else theme.MUTED))
            parent.setExpanded(False)

        tree.setColumnWidth(0, col0_w)
        self._fit_tree(tree)
        tree.itemExpanded.connect(lambda *_: self._fit_tree(tree))
        tree.itemCollapsed.connect(lambda *_: self._fit_tree(tree))
        return tree

    @classmethod
    def _fit_tree(cls, tree):
        """Size the tree to its currently-visible rows (no inner scrollbar).

        Recomputed on every expand/collapse so the *page* scroll moves through
        the whole thing — same nested-scroll fix as :meth:`_fit_table`, but the
        height now tracks which sample groups are open. Summing each visible
        item's own size-hint height (rather than a flat per-row constant) also
        makes room for rows that wrap onto extra lines, e.g. long resistance
        evidence.
        """
        def row_h(item):
            h = item.sizeHint(0).height()
            return h if h > 0 else cls._TREE_ROW_H
        total = 0
        for i in range(tree.topLevelItemCount()):
            it = tree.topLevelItem(i)
            total += row_h(it)
            if it.isExpanded():
                for j in range(it.childCount()):
                    total += row_h(it.child(j))
        head_h = tree.header().sizeHint().height()
        tree.setFixedHeight(head_h + total + 6)

    # -- Genes / Mutations: bioinformatics diagram + raw table ----------
    def _genes_section(self, coverage):
        """Coverage-depth track over the per-gene coverage table."""
        wrap = QWidget()
        lay = QVBoxLayout(wrap)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(16)
        chart_card, cl = card("Sequencing depth by gene")
        chart = CoverageChart()
        chart.setMinimumHeight(240)
        chart.set_data(self._coverage_track(coverage))
        cl.addWidget(chart)
        lay.addWidget(chart_card)
        # Collapsible per-sample tree; the section scrolls as one.
        lay.addWidget(self._gene_summary_tree(coverage))
        lay.addStretch(1)
        return wrap

    def _mutations_section(self, variants):
        """Mutation lollipop plot over the per-variant detail table.

        A sample filter above the plot switches it between the whole-cohort
        view (counts = how many barcodes share each change) and a single
        barcode (where each mutation lands on the protein relative to its
        functional domains). Hovering a head shows the change, count, samples
        and the domain it sits in.
        """
        wrap = QWidget()
        lay = QVBoxLayout(wrap)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(16)

        chart_card, cl = card("Mutations by protein position")

        # Filter row: "Sample:" + dropdown of all barcodes (raw id stashed as
        # item data; "All samples" carries None).
        filter_row = QHBoxLayout()
        filter_row.setSpacing(8)
        flabel = QLabel("Sample")
        flabel.setStyleSheet("color:%s;" % theme.MUTED)
        sample_combo = QComboBox()
        sample_combo.addItem("All samples", None)
        seen = set()
        for v in variants:
            s = v.get("Sample", "")
            if s and s not in seen:
                seen.add(s)
                sample_combo.addItem(self._disp(s), s)
        sample_combo.setMaximumWidth(320)
        sample_combo.setCursor(Qt.PointingHandCursor)
        fit_combo_popup(sample_combo)
        filter_row.addWidget(flabel)
        filter_row.addWidget(sample_combo)
        filter_row.addStretch(1)
        cl.addLayout(filter_row)

        chart = LollipopChart()
        cl.addWidget(chart)

        def _redraw(sample_filter):
            chart.set_data(self._mutation_tracks(variants, sample_filter))

        sample_combo.currentIndexChanged.connect(
            lambda i: _redraw(sample_combo.itemData(i)))
        _redraw(None)

        lay.addWidget(chart_card)
        # Collapsible per-sample tree; the section scrolls as one.
        lay.addWidget(self._mutations_tree(variants))
        lay.addStretch(1)
        return wrap

    @staticmethod
    def _aa_pos(aa_change):
        """First integer in an AA-change string (``C59R`` -> 59), or None."""
        m = re.search(r"(\d+)", aa_change or "")
        return int(m.group(1)) if m else None

    def _coverage_track(self, coverage):
        """Aggregate coverage rows to per-gene mean depth + worst status.

        Returns ``[(gene, mean_depth, palette_key)]`` sorted by depth. A gene
        is coloured by the *worst* status seen across samples (no < low < ok)
        so any coverage gap is visible at a glance.
        """
        agg = {}
        for r in coverage:
            gene = r.get("Gene") or r.get("Gene_ID") or "?"
            try:
                depth = float(r.get("Mean_Depth"))
            except (TypeError, ValueError):
                depth = 0.0
            status = (r.get("Status") or "").upper()
            a = agg.setdefault(gene, {"depths": [], "status": set()})
            a["depths"].append(depth)
            a["status"].add(status)
        items = []
        for gene, a in agg.items():
            mean = sum(a["depths"]) / len(a["depths"]) if a["depths"] else 0.0
            worst = next((s for s in ("NO_COVERAGE", "LOW_COVERAGE", "OK")
                          if s in a["status"]), "OK")
            items.append((gene, mean, COVERAGE_COLORS.get(worst, "ok")))
        items.sort(key=lambda it: it[1], reverse=True)
        return items

    def _mutation_tracks(self, variants, sample_filter=None):
        """Group variants into per-gene lollipop tracks.

        Returns ``[(gene, max_pos, [(pos, label, count, key, samples)],
        domains)]`` where ``count`` is how many samples carry that amino-acid
        change, ``samples`` is the sorted list of their display labels (for the
        hover tooltip) and ``key`` is ``"known"`` (catalogued marker) or
        ``"novel"``. Pass ``sample_filter`` (a raw sample id) to restrict the
        plot to a single barcode. Variants without a parseable AA position are
        skipped (they stay in the table below).

        ``max_pos`` is the gene's *true* protein length (aa), derived from the
        pipeline's GFF via :mod:`gui.proteins`, so the backbone is drawn to
        scale. When the length is unknown (GFF absent, or a position runs past
        it) we fall back to the observed mutation range. ``domains`` is the
        gene's Pfam/UniProt domain bands ``[(start, end, name)]`` (clipped to
        ``max_pos``), empty when none are catalogued.
        """
        genes = {}
        gene_ids = {}               # gene label -> Gene_ID, for true length
        for v in variants:
            if sample_filter and v.get("Sample", "") != sample_filter:
                continue
            pos = self._aa_pos(v.get("AA_Change"))
            if pos is None:
                continue
            gene = v.get("Gene") or v.get("Gene_ID") or "?"
            gid = v.get("Gene_ID")
            if gid:
                gene_ids.setdefault(gene, gid)
            label = (v.get("AA_Change") or "").strip()
            status = (v.get("Catalog_status") or "").lower()
            key = "known" if status == "known_marker_component" else "novel"
            muts = genes.setdefault(gene, {})
            m = muts.setdefault((pos, label), {"key": key, "samples": set()})
            m["samples"].add(self._disp(v.get("Sample", "")))
            if key == "known":          # a catalogued hit dominates the colour
                m["key"] = "known"
        tracks = []
        for gene, muts in genes.items():
            pts = sorted(
                ((pos, label, len(m["samples"]), m["key"],
                  tuple(sorted(m["samples"])))
                 for (pos, label), m in muts.items()),
                key=lambda t: t[0])
            gid = gene_ids.get(gene)
            observed = max((t[0] for t in pts), default=1)
            length = proteins.protein_length(gid)
            max_pos = length if (length and length >= observed) else observed
            domains = [(s, e, name) for (s, e, name)
                       in proteins.protein_domains(gid) if s <= max_pos]
            tracks.append((gene, max_pos, pts, domains))
        tracks.sort(key=lambda t: t[0])
        return tracks

    def _qc_tab(self, qc, samples):
        metric_keys = ["number_of_reads", "number_of_bases",
                       "median_read_length", "n50", "mean_qual"]
        headers = ["Sample", "Reads", "Bases", "Median length",
                   "N50", "Mean qual"]
        rows = []
        for s in samples:
            m = qc.get(s, {})
            rows.append([self._disp(s)] +
                        [_fmt_num(m.get(k)) for k in metric_keys])
        return self._table(headers, rows)

    # NanoPlot diagram filenames, in the order we want them on the page;
    # the trailing "" labels keep the gallery captions short.
    _QC_PLOTS = [
        ("LengthvsQualityScatterPlot_dot.png", "Read length vs quality"),
        ("Non_weightedHistogramReadlength.png", "Read-length histogram"),
        ("WeightedHistogramReadlength.png", "Weighted read-length histogram"),
        ("Yield_By_Length.png", "Cumulative yield by length"),
    ]

    def _qc_plot_dir(self, sample):
        """Directory holding ``sample``'s NanoPlot PNGs/HTML, or None.

        Prefers the collected ``final_reports/qc/trimmed/<sample>`` copy, then
        the working ``qc_trimmed``/``qc_raw`` dirs. Only returns a folder that
        actually contains at least one PNG.
        """
        if not self._output_dir:
            return None
        cands = [
            os.path.join(self._output_dir, "final_reports", "qc", "trimmed",
                         sample),
            os.path.join(self._output_dir, "qc_trimmed", sample),
            os.path.join(self._output_dir, "final_reports", "qc", "raw",
                         sample),
            os.path.join(self._output_dir, "qc_raw", sample),
        ]
        for d in cands:
            if not os.path.isdir(d):
                continue
            if any(f.endswith(".png") for f in os.listdir(d)):
                return d
        return None

    def _quality_section(self, qc, samples):
        """Read-QC stats table over an inline NanoPlot diagram gallery.

        The stats table is unchanged; below it a barcode dropdown drives a
        gallery that scales this run's NanoPlot PNGs to fit, plus a button to
        open the interactive ``NanoPlot-report.html`` in a browser. The gallery
        only lists samples that actually shipped diagrams (NanoStat runs leave
        none), and the whole card hides when no sample has plots.
        """
        wrap = QWidget()
        lay = QVBoxLayout(wrap)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(16)

        # Full-height metrics table (no nested scrollbar); clicking a row jumps
        # the plot selector below to that barcode.
        table_card, tl = card("Read quality metrics")
        table = self._qc_tab(qc, samples)
        self._fit_table(table)
        tl.addWidget(table)
        lay.addWidget(table_card)

        plotted = [s for s in samples if self._qc_plot_dir(s)]
        if not plotted:
            lay.addStretch(1)
            return wrap

        plot_card, pl = card("Read QC plots")
        ctrl = QHBoxLayout()
        ctrl.setSpacing(8)
        clabel = QLabel("Sample")
        clabel.setStyleSheet("color:%s;" % theme.MUTED)
        combo = QComboBox()
        for s in plotted:
            combo.addItem(self._disp(s), s)
        combo.setMaximumWidth(320)
        combo.setCursor(Qt.PointingHandCursor)
        fit_combo_popup(combo)
        open_btn = QPushButton("Open interactive report")
        open_btn.setObjectName("Ghost")
        ctrl.addWidget(clabel)
        ctrl.addWidget(combo)
        ctrl.addStretch(1)
        ctrl.addWidget(open_btn)
        pl.addLayout(ctrl)

        # Plots in a 2-column grid so all four read at a glance with minimal
        # scrolling (rather than a tall single-column stack).
        gallery = QWidget()
        grid = QGridLayout(gallery)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(16)
        pl.addWidget(gallery)
        lay.addWidget(plot_card)
        lay.addStretch(1)

        def _show(sample):
            while grid.count():
                item = grid.takeAt(0)
                w = item.widget()
                if w:
                    w.deleteLater()
            d = self._qc_plot_dir(sample)
            if not d:
                return
            shown = 0
            for fname, caption in self._QC_PLOTS:
                path = os.path.join(d, fname)
                if not os.path.isfile(path):
                    continue
                pix = QPixmap(path)
                if pix.isNull():
                    continue
                cell = QWidget()
                cv = QVBoxLayout(cell)
                cv.setContentsMargins(0, 0, 0, 0)
                cv.setSpacing(4)
                cap = QLabel(caption)
                cap.setStyleSheet("color:%s; font-weight:600;" % theme.MUTED)
                img = _ClickableLabel()
                img.setAlignment(Qt.AlignLeft | Qt.AlignTop)
                img.setPixmap(pix.scaledToWidth(360, Qt.SmoothTransformation))
                img.setCursor(Qt.PointingHandCursor)
                img.setToolTip("Click to zoom")
                img.clicked.connect(
                    lambda p=path, c=caption: self._open_plot_zoom(p, c))
                cv.addWidget(cap)
                cv.addWidget(img)
                grid.addWidget(cell, shown // 2, shown % 2)
                shown += 1

        def _open_report():
            d = self._qc_plot_dir(combo.currentData())
            if not d:
                return
            html = os.path.join(d, "NanoPlot-report.html")
            _open_path(html if os.path.isfile(html) else d)

        def _row_to_combo(row, _col):
            if 0 <= row < len(samples):
                idx = combo.findData(samples[row])
                if idx >= 0 and idx != combo.currentIndex():
                    combo.setCurrentIndex(idx)

        combo.currentIndexChanged.connect(
            lambda i: _show(combo.itemData(i)))
        table.cellClicked.connect(_row_to_combo)
        open_btn.clicked.connect(_open_report)
        _show(combo.currentData())
        return wrap

    def _open_plot_zoom(self, path, title=""):
        """Show a QC plot full-size in a zoomable, scrollable popup."""
        _ImageZoomDialog(path, title, self).exec_()

    # -- section stack ---------------------------------------------------
    def _build_sections(self, calls, variants, coverage, samples, cov_idx, qc):
        """Rebuild the navigable section stack from scratch each render.

        Sections are recreated (not reused) so re-loading a run never leaves
        stale widgets parented to a freed stack. The names here are the same
        keys the sidebar navigates with via :meth:`show_section`.
        """
        while self.sections.count():
            w = self.sections.widget(0)
            self.sections.removeWidget(w)
            w.deleteLater()
        self._section_index = {}

        def add(name, widget):
            self._section_index[name] = self.sections.addWidget(widget)

        add("overview", self._scroll(self._overview_section(calls, samples)))
        add("samples", self._samples_tab(samples))
        add("data:resistance", self._drug_summary_tab(calls))
        add("data:genes", self._scroll(self._genes_section(coverage)))
        add("data:mutations", self._scroll(self._mutations_section(variants)))
        add("data:quality", self._scroll(self._quality_section(qc, samples)))

        # The bar chart exists now (created in _overview_section); fill it.
        self._render_charts(calls)

    def _scroll(self, widget):
        """Wrap a section so dense content scrolls instead of clipping."""
        sa = QScrollArea()
        sa.setWidgetResizable(True)
        sa.setFrameShape(QFrame.NoFrame)
        sa.setWidget(widget)
        return sa

    def show_section(self, name):
        """Navigate the stack to a named section (called by the sidebar)."""
        self._pending_section = name
        idx = self._section_index.get(name)
        if idx is not None:
            self.sections.setCurrentIndex(idx)

    # -- Overview: heatmap hero + bar (left) beside sample evidence ------
    def _overview_section(self, calls, samples):
        """Colour-filled heatmap + legend + bar chart (left) beside the
        whole-sample evidence panel (right).

        The left column stacks the heatmap card (stretch) above the "Resistant
        calls by drug" bar; the right column is an always-populated evidence
        panel. Clicking any heatmap row opens that sample's evidence; the
        highest-severity sample is shown on load so nothing is hidden behind a
        click.
        """
        wrap = QWidget()
        lay = QHBoxLayout(wrap)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        # Left column: heatmap + legend (stretch) above the bar chart.
        left = QWidget()
        left_lay = QVBoxLayout(left)
        left_lay.setContentsMargins(0, 0, 0, 0)
        left_lay.setSpacing(16)
        grid_card, grid_lay = card("Resistance by sample \u00d7 drug")
        grid_lay.addWidget(self._heatmap_grid(calls, samples))
        grid_lay.addWidget(self._heatmap_legend())
        left_lay.addWidget(grid_card)

        bar_card, bar_lay = card("Resistant calls by drug")
        self.bar = BarChart()
        bar_lay.addWidget(self.bar)
        bar_card.setMinimumHeight(200)
        left_lay.addWidget(bar_card)
        left_lay.addStretch(1)

        # Right column: the always-populated whole-sample evidence panel.
        self._evidence_host = QWidget()
        QVBoxLayout(self._evidence_host).setContentsMargins(0, 0, 0, 0)
        self._evidence_host.setMinimumWidth(320)

        # Responsive: a splitter lets the user balance heatmap vs. evidence
        # and lets both reflow at small widths instead of clipping.
        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)
        left.setMinimumWidth(300)
        splitter.addWidget(left)
        splitter.addWidget(self._evidence_host)
        splitter.setSizes([700, 360])
        lay.addWidget(splitter)

        # Populate the evidence panel now so nothing is hidden behind a click.
        self._auto_select_sample(calls, samples)
        return wrap

    def _heatmap_grid(self, calls, samples):
        """The sample\u00d7drug matrix as a colour-filled heatmap.

        Each drug cell is a solid tier-colour tile with no text (meaning read
        from the legend), painted by ``HeatmapDelegate`` so the colour is not
        eaten by the table stylesheet. The sample label sits full-width in
        column 0 (sized to fit the longest name), and drug columns are fixed
        compact tiles under rotated full-name headers. Clicking anywhere on a
        row selects it — the whole row highlights and its evidence opens in the
        side panel — so no button is needed. The table is sized to its content
        so the card leaves no dead space.
        """
        drugs = panel_drugs(calls) if panel_drugs else []
        _, _, _, _, cov_idx, _ = self._data
        headers = [""] + drugs
        tile = 46
        row_h = 38
        self._sample_rows = {}

        table = QTableWidget(len(samples), len(headers))
        header = RotatedHeader(rotate_from=1, parent=table)
        table.setHorizontalHeader(header)
        table.setHorizontalHeaderLabels(headers)
        table.setItemDelegate(HeatmapDelegate(table))
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        # Whole-row click selects the sample; the row highlights and its
        # evidence opens in the side panel.
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setSelectionMode(QAbstractItemView.SingleSelection)
        table.setFocusPolicy(Qt.NoFocus)
        table.setCursor(Qt.PointingHandCursor)
        table.setShowGrid(False)
        # Tiles are a fixed size, so when the panel is too narrow to show every
        # drug the matrix scrolls horizontally rather than clipping columns.
        table.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        table.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        table.verticalHeader().setVisible(False)
        table.verticalHeader().setDefaultSectionSize(row_h)
        # Homogenise the background: header sections and the empty trailing
        # header area (which otherwise falls through to the near-black window
        # base in dark mode) share the table surface, so no darker patches show.
        table.setStyleSheet(
            "QTableWidget { background:%s; border:1px solid %s; "
            "border-radius:3px; selection-background-color:%s; "
            "selection-color:%s; }"
            "QHeaderView { background:%s; }"
            "QHeaderView::section { background:%s; border:none; }"
            "QTableWidget::item:selected { background:%s; color:%s; }"
            % (theme.SURFACE, theme.BORDER, theme.SURFACE, theme.HEADING,
               theme.SURFACE, theme.SURFACE, theme.SURFACE, theme.HEADING))
        table.viewport().setStyleSheet("background:%s;" % theme.SURFACE)

        for r, s in enumerate(samples):
            label = self._disp(s)
            self._sample_rows[s] = r
            name = QTableWidgetItem(label)
            f = name.font(); f.setBold(True); name.setFont(f)
            name.setForeground(QColor(theme.HEADING))
            name.setToolTip(label)
            table.setItem(r, 0, name)
            for ci, d in enumerate(drugs, start=1):
                item = QTableWidgetItem("")
                if drug_status is not None:
                    key = drug_status(s, d, calls, cov_idx)[0]
                    meta = STATUS_META.get(key)
                    color = PALETTE.get(meta[2]) if meta else None
                    if color:
                        item.setData(BADGE_ROLE, color)
                        if key == "notassessed":
                            item.setData(OUTLINE_ROLE, True)
                        item.setToolTip("%s \u00b7 %s \u2014 %s"
                                        % (label, d, meta[0]))
                table.setItem(r, ci, item)

        table.cellClicked.connect(
            lambda r, _c: self._show_sample_evidence(samples[r])
            if 0 <= r < len(samples) else None)

        hh = table.horizontalHeader()
        # The name column sizes to its longest label so barcodes/aliases show
        # in full; drug tiles stay fixed and compact.
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        for ci in range(1, len(drugs) + 1):
            hh.setSectionResizeMode(ci, QHeaderView.Fixed)
            table.setColumnWidth(ci, tile)

        # Size to content: rotated header height + one row per sample, plus
        # room for the horizontal scrollbar so a narrow panel never hides the
        # last sample row behind it.
        header_h = header.header_height(drugs) if drugs else 40
        sb = table.style().pixelMetric(QStyle.PM_ScrollBarExtent)
        table.setFixedHeight(header_h + row_h * len(samples) + 4 + sb)
        self._heatmap_table = table
        return table

    def _heatmap_legend(self):
        """A colour key mapping each tier to its meaning.

        Each entry is a rounded chip (matching the heatmap tiles) beside the
        tier name and a small "?" badge whose hover tooltip spells out what
        the tier means, so the key stays compact but self-explanatory. The row
        wraps on narrow panels via ``FlowLayout``.
        """
        host = QWidget()
        # Match the card surface explicitly: a bare child widget can otherwise
        # fall through to the near-black window base, leaving the legend strip
        # darker than the card body above it.
        host.setStyleSheet("background:%s;" % theme.SURFACE)
        flow = FlowLayout(host, hspacing=20, vspacing=8)
        for _key, meta in STATUS_META.items():
            color = PALETTE.get(meta[2], theme.MUTED)
            item = QWidget()
            il = QHBoxLayout(item)
            il.setContentsMargins(0, 0, 0, 0)
            il.setSpacing(7)
            chip = QLabel()
            chip.setFixedSize(16, 16)
            if _key == "notassessed":
                # Match the heatmap: a hollow, dashed tile for absent data.
                chip.setStyleSheet(
                    "background:transparent; border:1px dashed %s; "
                    "border-radius:4px;" % color)
            else:
                chip.setStyleSheet(
                    "background:%s; border-radius:4px;" % color)
            name = QLabel(meta[0])
            name.setStyleSheet(
                "font-size:12px; font-weight:600; color:%s;" % theme.TEXT)
            il.addWidget(chip)
            il.addWidget(name)
            il.addWidget(self._help_badge(TIER_HELP.get(_key, meta[1])))
            flow.addWidget(item)
        return host

    def _help_badge(self, tip):
        """A small circular "?" that shows ``tip`` on hover or click."""
        badge = _HelpBadge(tip)
        badge.setStyleSheet(
            "QLabel { border:1px solid %s; border-radius:7px; color:%s; "
            "font-size:10px; font-weight:700; }" % (theme.BORDER, theme.MUTED))
        return badge

    def _auto_select_sample(self, calls, samples):
        """Populate the evidence panel with the highest-severity sample so it
        is never an empty hint. Falls back to the first sample, or a short
        note when there are no samples."""
        if not samples:
            note_card, note_lay = card("Evidence")
            note_lay.addWidget(self._evidence_note("No samples to show."))
            note_lay.addStretch(1)
            self._evidence_host.layout().addWidget(note_card)
            return

        target = samples[0]
        drugs = panel_drugs(calls) if panel_drugs else []
        if drugs and drug_status is not None:
            _, _, _, _, cov_idx, _ = self._data
            rank = {"validated": 0, "candidate": 1, "potential": 2,
                    "notassessed": 3, "nomarker": 4}
            best = None
            for s in samples:
                sr = min((rank.get(drug_status(s, d, calls, cov_idx)[0], 5)
                          for d in drugs), default=5)
                if best is None or sr < best:
                    best = sr
                    target = s
        self._show_sample_evidence(target)

    def _show_sample_evidence(self, sample):
        """Repopulate the side panel with the whole-sample evidence card."""
        lay = self._evidence_host.layout()
        while lay.count():
            item = lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
        lay.addWidget(self._sample_evidence_panel(sample))
        self._current_sample = sample

        # Keep the heatmap selection in sync so the highlighted row and the
        # shown evidence always agree (whether opened by click or auto-select).
        row = getattr(self, "_sample_rows", {}).get(sample)
        table = getattr(self, "_heatmap_table", None)
        if row is not None and table is not None:
            table.blockSignals(True)
            table.selectRow(row)
            table.blockSignals(False)

    def _sample_evidence_panel(self, sample):
        """Whole-sample evidence card: a per-drug status list followed by the
        sample's resistance calls, variants, coverage and QC.

        Built on demand from the loaded
        ``(calls, variants, coverage, samples, cov_idx, qc)`` tuple.
        """
        calls, variants, coverage, _samples, cov_idx, qc = self._data
        card_w, lay = card("Evidence")

        head = QLabel(self._disp(sample))
        head.setObjectName("SectionTitle")
        lay.addWidget(head)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        inner = QWidget()
        ilay = QVBoxLayout(inner)
        ilay.setContentsMargins(0, 0, 0, 0)
        ilay.setSpacing(12)

        # Surveillance metadata for the sample (alias, location, date, notes),
        # shown first so the reader knows *which* specimen the evidence is for.
        meta_widget = self._sample_meta_section(sample)
        if meta_widget is not None:
            ilay.addWidget(meta_widget)

        # Per-drug status: drug name beside its tier label in the tier colour.
        drugs = panel_drugs(calls) if panel_drugs else []
        if drugs and drug_status is not None:
            box = QWidget()
            bl = QVBoxLayout(box)
            bl.setContentsMargins(0, 0, 0, 0)
            bl.setSpacing(6)
            title = QLabel("Drug status")
            title.setObjectName("SectionTitle")
            bl.addWidget(title)
            t = QTableWidget(len(drugs), 2)
            t.setHorizontalHeaderLabels(["Drug", "Status"])
            t.setEditTriggers(QTableWidget.NoEditTriggers)
            t.setShowGrid(False)
            t.verticalHeader().setVisible(False)
            t.verticalHeader().setDefaultSectionSize(28)
            for r, d in enumerate(drugs):
                key = drug_status(sample, d, calls, cov_idx)[0]
                meta = STATUS_META.get(key)
                di = QTableWidgetItem(d)
                di.setForeground(QColor(theme.HEADING))
                t.setItem(r, 0, di)
                si = QTableWidgetItem(meta[0] if meta else key)
                if meta:
                    f = si.font(); f.setBold(True); si.setFont(f)
                    si.setForeground(QColor(PALETTE.get(meta[2], theme.TEXT)))
                t.setItem(r, 1, si)
            hh = t.horizontalHeader()
            hh.setHighlightSections(False)
            hh.setStretchLastSection(True)
            self._fit_table_to_rows(t)
            bl.addWidget(t)
            ilay.addWidget(box)

        # All resistance calls for this sample (across every drug).
        sc = [c for c in calls if c.get("Sample") == sample]
        if sc:
            crows = [[c.get("Drug", ""), c.get("Classification", ""),
                      c.get("Genes", ""), c.get("Alteration", ""),
                      c.get("Evidence", "")] for c in sc]
            ilay.addWidget(self._evidence_section(
                "Resistance call(s)",
                ["Drug", "Classification", "Genes", "Alteration", "Evidence"],
                crows))
        else:
            ilay.addWidget(self._evidence_note(
                "No resistance calls for this sample."))

        # All variants for the sample.
        svars = [v for v in variants if v.get("Sample") == sample]
        if svars:
            vrows = [[v.get("Gene", "") or v.get("Gene_ID", ""),
                      v.get("AA_Change", ""), v.get("Catalog_status", ""),
                      v.get("AF", "")] for v in svars]
            ilay.addWidget(self._evidence_section(
                "Variants", ["Gene", "AA change", "Catalog", "AF"], vrows))

        # Coverage across the sample's genes.
        scov = [r for r in coverage if r.get("Sample") == sample]
        if scov:
            covrows = [[r.get("Gene", "") or r.get("Gene_ID", ""),
                        r.get("Mean_Depth", ""), r.get("Status", "")]
                       for r in scov]
            ilay.addWidget(self._evidence_section(
                "Coverage", ["Gene", "Mean depth", "Status"], covrows))

        # Sample QC summary.
        m = qc.get(sample, {})
        if m:
            qcrows = [["Reads", _fmt_num(m.get("number_of_reads"))],
                      ["Median length", _fmt_num(m.get("median_read_length"))],
                      ["N50", _fmt_num(m.get("n50"))],
                      ["Mean qual", _fmt_num(m.get("mean_qual"))]]
            ilay.addWidget(self._evidence_section(
                "Quality", ["Metric", "Value"], qcrows))

        ilay.addStretch(1)
        scroll.setWidget(inner)
        lay.addWidget(scroll, 1)
        return card_w

    def _sample_meta_section(self, sample):
        """A compact key/value block of the sample's surveillance metadata.

        Returns ``None`` when the sample has no user-entered metadata, so the
        evidence panel only shows this section when there is something to show.
        The deterministic sample UID is included for reference when any real
        field is present.
        """
        if db is None or not getattr(self, "_job_id", None):
            return None
        meta = db.get_sample_meta(self._job_id, sample) or {}

        # Only the fields a user actually fills in count as "has metadata"; the
        # UID always exists, so it alone shouldn't trigger the section.
        rows = []
        if meta.get("alias"):
            rows.append(("Name / alias", meta["alias"]))
        if meta.get("internal_id"):
            rows.append(("Internal ID", meta["internal_id"]))
        loc = ", ".join(x for x in (meta.get("district"), meta.get("region"))
                        if x)
        if loc:
            rows.append(("Location", loc))
        lat, lon = meta.get("latitude"), meta.get("longitude")
        if lat is not None and lon is not None:
            rows.append(("Coordinates", "%.4f, %.4f" % (lat, lon)))
        if meta.get("collection_date"):
            rows.append(("Collected", meta["collection_date"]))

        notes = meta.get("notes")
        notes_text = ""
        if notes:
            doc = QTextDocument()
            doc.setHtml(notes)
            notes_text = doc.toPlainText().strip()

        if not rows and not notes_text:
            return None

        box = QWidget()
        bl = QVBoxLayout(box)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(6)
        title = QLabel("Sample metadata")
        title.setObjectName("SectionTitle")
        bl.addWidget(title)

        # UID leads the grid as a reference row once real metadata exists.
        grid_rows = [("Sample ID", db.sample_uid(self._job_id, sample))] + rows
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(4)
        for r, (k, v) in enumerate(grid_rows):
            klbl = QLabel(k)
            klbl.setStyleSheet("font-size:12px; color:%s;" % theme.MUTED)
            vlbl = QLabel(str(v))
            vlbl.setWordWrap(True)
            vlbl.setStyleSheet(
                "font-size:12px; font-weight:600; color:%s;" % theme.HEADING)
            grid.addWidget(klbl, r, 0, Qt.AlignTop | Qt.AlignLeft)
            grid.addWidget(vlbl, r, 1)
        grid.setColumnStretch(1, 1)
        bl.addLayout(grid)

        if notes_text:
            nkey = QLabel("Notes")
            nkey.setStyleSheet("font-size:12px; color:%s;" % theme.MUTED)
            nbody = QLabel(notes_text)
            nbody.setWordWrap(True)
            nbody.setStyleSheet("font-size:12px; color:%s;" % theme.TEXT)
            bl.addWidget(nkey)
            bl.addWidget(nbody)
        return box

    def _evidence_section(self, title, headers, rows):
        """A titled compact table for one evidence block."""
        box = QWidget()
        bl = QVBoxLayout(box)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(6)
        lbl = QLabel(title)
        lbl.setObjectName("SectionTitle")
        bl.addWidget(lbl)
        t = QTableWidget(len(rows), len(headers))
        t.setHorizontalHeaderLabels(headers)
        t.setEditTriggers(QTableWidget.NoEditTriggers)
        t.setShowGrid(False)
        t.setWordWrap(True)
        t.verticalHeader().setVisible(False)
        t.verticalHeader().setDefaultSectionSize(28)
        for r, row in enumerate(rows):
            for c, val in enumerate(row):
                item = QTableWidgetItem("" if val is None else str(val))
                item.setForeground(QColor(
                    theme.HEADING if c == 0 else theme.TEXT))
                t.setItem(r, c, item)
        t.resizeColumnsToContents()
        hh = t.horizontalHeader()
        hh.setHighlightSections(False)
        hh.setStretchLastSection(True)
        # Size to full content so every row shows and only the Evidence panel's
        # own scroll area scrolls. The old ``30 + 28*n`` estimate undershot the
        # stylesheet's padded row height, clipping rows behind a cramped inner
        # scrollbar. Wide tables keep a horizontal scrollbar, so reserve room.
        self._fit_table_to_rows(t, reserve_hscroll=len(headers) > 2)
        bl.addWidget(t)
        return box

    # Forced row/header heights for the compact evidence tables, so a table can
    # be sized to show all its rows exactly (measured content heights are
    # unreliable with stylesheet ``::item`` padding, which is why these are
    # pinned rather than estimated).
    _EV_ROW_H = 34
    _EV_HEAD_H = 34

    def _fit_table_to_rows(self, t, reserve_hscroll=False):
        """Fix a table's height to display every row (no inner vertical scroll).

        The evidence tables live inside the panel's own scroll area, so each
        inner table should show all its rows rather than keep a tiny vertical
        scrollbar. Row and header heights are pinned so the total is exact.
        """
        t.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        t.verticalHeader().setDefaultSectionSize(self._EV_ROW_H)
        t.horizontalHeader().setFixedHeight(self._EV_HEAD_H)
        n = max(1, t.rowCount())
        h = self._EV_HEAD_H + self._EV_ROW_H * n + 2 * t.frameWidth() + 2
        if reserve_hscroll:
            h += t.horizontalScrollBar().sizeHint().height()
        t.setFixedHeight(h)

    def _evidence_note(self, text):
        lbl = QLabel(text)
        lbl.setWordWrap(True)
        lbl.setStyleSheet("color:%s; font-size:12px;" % theme.MUTED)
        return lbl

    # -- Samples tab (metadata: alias, geo-tag, audit) -------------------
    def _samples_tab(self, samples):
        wrap = QWidget()
        lay = QVBoxLayout(wrap)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        if self._job_id is None:
            note = QLabel("Open this run from the Jobs list to edit sample "
                          "labels (read-only here).")
            note.setStyleSheet("color:%s; font-size:12px;" % theme.MUTED)
            lay.addWidget(note)

        editable = self._job_id is not None
        meta = db.list_sample_meta(self._job_id) if self._job_id else {}
        headers = ["Barcode", "ID", "Name / alias", "Internal ID", "Region",
                   "District", "Lat", "Lon", "Date"]
        if editable:
            headers.append("")          # trailing per-row actions column
        table = QTableWidget(len(samples), len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        table.setAlternatingRowColors(True)
        table.setShowGrid(False)
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setSelectionMode(QTableWidget.SingleSelection)
        table.verticalHeader().setVisible(False)
        table.verticalHeader().setDefaultSectionSize(36)
        hh = table.horizontalHeader()
        hh.setHighlightSections(False)
        hh.setStretchLastSection(False)
        # Stretch the textual columns; keep barcode/numeric snug.
        stretch = {2, 4, 5}             # Alias, Region, District
        actions_col = len(headers) - 1 if editable else -1
        for c in range(len(headers)):
            if c == actions_col:
                # Empty-header column would size to the header and clip the
                # buttons; pin it wide enough for "Report" + "Edit" + "History".
                hh.setSectionResizeMode(c, QHeaderView.Fixed)
                table.setColumnWidth(c, 230)
            elif c in stretch:
                hh.setSectionResizeMode(c, QHeaderView.Stretch)
            else:
                hh.setSectionResizeMode(c, QHeaderView.ResizeToContents)

        def _cell(value):
            return QTableWidgetItem("" if value is None else str(value))

        uid_of = (lambda s: db.sample_uid(self._job_id, s)) if self._job_id \
            else (lambda s: "")
        for r, s in enumerate(samples):
            m = meta.get(s, {})
            cells = [s, uid_of(s), m.get("alias"), m.get("internal_id"),
                     m.get("region"), m.get("district"), m.get("latitude"),
                     m.get("longitude"), m.get("collection_date")]
            for c, val in enumerate(cells):
                item = _cell(val)
                if c == 0:
                    f = item.font(); f.setBold(True); item.setFont(f)
                    item.setForeground(QColor(theme.HEADING))
                elif c == 1:
                    f = item.font(); f.setFamily("monospace"); item.setFont(f)
                    item.setForeground(QColor(theme.MUTED))
                else:
                    item.setForeground(QColor(theme.TEXT))
                table.setItem(r, c, item)
            if editable:
                table.setCellWidget(r, len(headers) - 1,
                                    self._row_actions(s))

        if editable:
            # Double-clicking anywhere on a row opens that sample's editor,
            # so the action is attached to the barcode, not a detached button.
            table.cellDoubleClicked.connect(
                lambda r, _c, t=table: self._edit_sample(samples[r])
                if 0 <= r < len(samples) else None)
        lay.addWidget(table, 1)
        return wrap

    def _row_actions(self, sample):
        """Per-row Report/Edit/History buttons, attached to a barcode."""
        host = QWidget()
        row = QHBoxLayout(host)
        row.setContentsMargins(4, 0, 4, 0)
        row.setSpacing(2)
        for label, slot in (("Report", self._sample_report),
                            ("Edit", self._edit_sample),
                            ("History", self._show_history_for)):
            btn = QPushButton(label)
            btn.setObjectName("Ghost")
            btn.setCursor(Qt.PointingHandCursor)
            btn.clicked.connect(lambda _=False, s=sample, fn=slot: fn(s))
            row.addWidget(btn)
        row.addStretch(1)
        return host

    def _edit_sample(self, sample):
        if not sample or self._job_id is None:
            return
        current = db.get_sample_meta(self._job_id, sample) or {}
        dlg = _SampleEditDialog(sample, current, self,
                                uid=db.sample_uid(self._job_id, sample))
        if dlg.exec() != QDialog.Accepted:
            return
        db.upsert_sample_meta(self._job_id, sample, dlg.values(),
                              source="edit")
        self.metadata_edited.emit(self._job_id)
        self._render()

    def _show_history_for(self, sample):
        if not sample or self._job_id is None:
            return
        rows = db.list_sample_audit(self._job_id, sample)
        _AuditDialog(sample, rows, self).exec()

    # -- charts ----------------------------------------------------------
    def _render_charts(self, calls):
        # Bar: resistant calls per drug.
        per_drug = {}
        for c in calls:
            d = c.get("Drug", "?")
            per_drug[d] = per_drug.get(d, 0) + 1
        pairs = sorted(per_drug.items(), key=lambda kv: kv[1], reverse=True)
        self.bar.set_data(pairs, palette_key="validated")

    # -- exports ---------------------------------------------------------
    def _open_folder(self):
        if self._reports_dir and os.path.isdir(self._reports_dir):
            _open_path(self._reports_dir)
        else:
            QMessageBox.information(self, "No folder", "No results loaded.")

    def _qc_diagram_dir(self):
        """Return the folder holding this run's NanoPlot diagrams, or None.

        Prefers the collected ``final_reports/qc`` copy (travels with the
        deliverable); falls back to the working ``qc_trimmed`` dir. NanoStat
        runs leave only text stats, so a folder is only reported when it holds
        at least one NanoPlot HTML/PNG.
        """
        if not self._output_dir:
            return None
        for cand in (os.path.join(self._output_dir, "final_reports", "qc"),
                     os.path.join(self._output_dir, "qc_trimmed")):
            if not os.path.isdir(cand):
                continue
            for root, _dirs, files in os.walk(cand):
                for f in files:
                    if f.endswith((".html", ".png")):
                        return cand
        return None

    def _open_qc(self):
        qc = self._qc_diagram_dir()
        if qc:
            _open_path(qc)
        else:
            QMessageBox.information(
                self, "No QC diagrams",
                "This run has no NanoPlot diagrams. Re-run with the NanoPlot "
                "QC tool to generate plots.")

    def _export_csv(self):
        if not self._reports_dir:
            return
        d = QFileDialog.getExistingDirectory(self, "Export CSVs to folder")
        if not d:
            return
        import shutil
        for name in ("resistance_calls.csv", "variant_detail.csv",
                     "coverage_report.csv"):
            src = os.path.join(self._reports_dir, name)
            if os.path.isfile(src):
                shutil.copy(src, os.path.join(d, name))
        QMessageBox.information(self, "Exported", "CSV files copied to %s" % d)

    def _export_excel(self):
        if not self._data:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Excel", "results.xlsx", "Excel (*.xlsx)")
        if not path:
            return
        try:
            from openpyxl import Workbook
        except Exception:
            QMessageBox.critical(self, "openpyxl missing",
                                 "openpyxl is required for Excel export.")
            return
        calls, variants, coverage, _, _, _ = self._data
        wb = Workbook()
        sheets = [("Resistance", calls), ("Variants", variants),
                  ("Coverage", coverage)]
        first = True
        for name, rows in sheets:
            ws = wb.active if first else wb.create_sheet()
            ws.title = name
            first = False
            if rows:
                headers = list(rows[0].keys())
                ws.append(headers)
                for r in rows:
                    ws.append([r.get(h, "") for h in headers])
        wb.save(path)
        QMessageBox.information(self, "Exported", "Workbook saved to %s" % path)

    def _ensure_pdf(self):
        """Return the run's combined (overview) PDF path, regenerating it.

        Always regenerates rather than returning a stale cache so edits made in
        the Overview-report settings tab take effect. Uses an interpreter that
        actually has ReportLab (``report_python``) and passes the "overview"
        scope's saved settings. Returns None and shows a message if it cannot be
        produced.
        """
        if not self._reports_dir:
            return None
        pdf = os.path.join(self._reports_dir, "resistance_report.pdf")
        python = paths.report_python()
        if not python:
            QMessageBox.critical(
                self, "PDF unavailable",
                "No Python environment with ReportLab was found, so the PDF "
                "report cannot be generated.\n\nInstall it with "
                "'pip install reportlab' (or 'conda install reportlab').")
            return None
        from .. import reportcfg
        cmd = [python, paths.generate_report_script(),
               "--reports_dir", self._reports_dir,
               "--output_dir", self._reports_dir,
               "--qc_dir", os.path.join(self._output_dir, "qc_trimmed"),
               "--mode", "combined",
               "--settings", reportcfg.write_sidecar("overview")]
        meta_csv = self._write_sample_meta_sidecar()
        if meta_csv:
            cmd += ["--sample_meta", meta_csv]
        try:
            subprocess.run(cmd, check=True, cwd=paths.app_root())
        except Exception as e:
            QMessageBox.critical(self, "PDF generation failed", str(e))
            return None
        return pdf if os.path.isfile(pdf) else None

    def _write_sample_meta_sidecar(self):
        """Write ``final_reports/sample_metadata.csv`` for this run, or None.

        Returns the path when there is metadata to pass to the PDF (so
        aliases and a Collection sites table appear); None otherwise, leaving
        the report's behaviour unchanged.
        """
        if not self._job_id or not self._reports_dir:
            return None
        meta = db.list_sample_meta(self._job_id)
        if not meta:
            return None
        import csv
        path = os.path.join(self._reports_dir, "sample_metadata.csv")
        cols = ["Sample", "Sample_UID", "Alias", "Internal_ID", "Region",
                "District", "Latitude", "Longitude", "Collection_date",
                "Case_classification", "Age_years", "Notes"]
        try:
            with open(path, "w", newline="") as fh:
                w = csv.writer(fh)
                w.writerow(cols)
                for sample, m in meta.items():
                    w.writerow([
                        sample, db.sample_uid(self._job_id, sample),
                        m.get("alias") or "", m.get("internal_id") or "",
                        m.get("region") or "",
                        m.get("district") or "",
                        "" if m.get("latitude") is None else m.get("latitude"),
                        "" if m.get("longitude") is None else m.get("longitude"),
                        m.get("collection_date") or "",
                        m.get("case_class") or "",
                        "" if m.get("age_years") is None else m.get("age_years"),
                        m.get("notes") or ""])
        except Exception:
            return None
        return path

    def _sample_report(self, sample):
        """Generate and open a single-sample clinical PDF, on demand.

        Always regenerates (settings may have changed since a prior render) and
        opens it embedded via ``ReportViewer``. Uses an interpreter that has
        ReportLab and passes the saved report-designer settings so branding,
        page size and section toggles apply.
        """
        if not sample or not self._reports_dir:
            return
        python = paths.report_python()
        if not python:
            QMessageBox.critical(
                self, "PDF unavailable",
                "No Python environment with ReportLab was found, so the PDF "
                "report cannot be generated.\n\nInstall it with "
                "'pip install reportlab' (or 'conda install reportlab').")
            return
        from .. import reportcfg
        pdf = os.path.join(self._reports_dir, "report_%s.pdf" % sample)
        cmd = [python, paths.generate_report_script(),
               "--reports_dir", self._reports_dir,
               "--output_dir", self._reports_dir,
               "--qc_dir", os.path.join(self._output_dir, "qc_trimmed"),
               "--mode", "per-sample", "--sample", sample,
               "--settings", reportcfg.write_sidecar("sample")]
        meta_csv = self._write_sample_meta_sidecar()
        if meta_csv:
            cmd += ["--sample_meta", meta_csv]
        try:
            subprocess.run(cmd, check=True, cwd=paths.app_root())
        except Exception as e:
            QMessageBox.critical(self, "Report generation failed", str(e))
            return
        if not os.path.isfile(pdf):
            return
        try:
            from .report_view import ReportViewer
        except Exception:
            _open_path(pdf)
            return
        ReportViewer(pdf, self).exec()

    def first_sample(self):
        """The first sample barcode of the loaded run, or None."""
        if self._data and self._data[3]:
            return self._data[3][0]
        return None

    def preview_report(self):
        """Render a sample report for the loaded run (used by Report settings).

        Falls back to a prompt when no run is loaded, since a preview needs
        real sample data to be meaningful.
        """
        sample = self.first_sample()
        if not sample:
            QMessageBox.information(
                self, "No run loaded",
                "Open a completed run under Results first, then preview a "
                "sample report to see your settings applied.")
            return
        self._sample_report(sample)

    def preview_overview_report(self):
        """Render the combined overview report (used by Report settings).

        Regenerates via ``_ensure_pdf`` (which applies the overview scope's
        settings) and opens it embedded. Prompts when no run is loaded.
        """
        if not self.first_sample():
            QMessageBox.information(
                self, "No run loaded",
                "Open a completed run under Results first, then preview the "
                "overview report to see your settings applied.")
            return
        pdf = self._ensure_pdf()
        if not pdf:
            return
        try:
            from .report_view import ReportViewer
        except Exception:
            _open_path(pdf)
            return
        ReportViewer(pdf, self).exec()

    def _view_pdf(self):
        """Open the run's PDF inside the app (embedded), or externally."""
        pdf = self._ensure_pdf()
        if not pdf:
            return
        try:
            from .report_view import ReportViewer
        except Exception:
            _open_path(pdf)
            return
        viewer = ReportViewer(pdf, self)
        viewer.exec()

    def _export_pdf(self):
        pdf = self._ensure_pdf()
        if not pdf:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export PDF", "resistance_report.pdf", "PDF (*.pdf)")
        if not path:
            return
        import shutil
        shutil.copy(pdf, path)
        QMessageBox.information(self, "Exported", "PDF saved to %s" % path)


class _NomWorker(QObject):
    """Runs one network callable off the UI thread; emits whatever it returns.

    Generalises the old single-purpose geocode worker so the sample editor can
    run forward search, reverse geocode and connectivity checks on the same
    QThread lifecycle. ``fn`` is any zero-arg callable (typically a lambda
    closing over :mod:`gui.geocode`); its result is emitted verbatim via
    ``done`` (``[]``/``None`` on failure — the callables never raise).
    """

    done = pyqtSignal(object)

    def __init__(self, fn):
        super().__init__()
        self._fn = fn

    def run(self):
        try:
            result = self._fn()
        except Exception:
            result = None
        self.done.emit(result)


# WHO clinical case classification — the one severity concept WHO defines, so
# it stays comparable across sites/time. A controlled vocabulary (not free
# entry): blank = not recorded.
WHO_CASE_CLASSES = ("Asymptomatic", "Uncomplicated", "Severe")
WHO_CASE_TOOLTIP = (
    "WHO clinical case classification (optional):\n"
    "\u2022 Asymptomatic \u2014 parasitaemia without malaria symptoms.\n"
    "\u2022 Uncomplicated \u2014 symptomatic, no signs of severity.\n"
    "\u2022 Severe \u2014 P. falciparum with \u22651 WHO severe-malaria feature "
    "(impaired consciousness / cerebral, severe anaemia, acute kidney injury, "
    "respiratory distress, hypoglycaemia, shock, abnormal bleeding, jaundice, "
    "hyperparasitaemia)."
)

# Patient age in whole years. Exact age (not a band) mirrors WHO Therapeutic
# Efficacy Study practice for antimalarial drug-resistance work, where dosing
# and analysis are age/weight-based; it can be bucketed into any strata at
# analysis time. Optional: the spinbox's minimum doubles as a "not recorded"
# sentinel, so 0 stays valid for infants under one year.
AGE_YEARS_MIN = 0
AGE_YEARS_MAX = 120
AGE_YEARS_TOOLTIP = (
    "Patient age in whole years (optional).\n"
    "Use 0 for infants under one year; leave as \u201cNot set\u201d if unknown."
)


class _SampleEditDialog(QDialog):
    """Single-row editor for one sample's metadata (alias + geo-tag)."""

    def __init__(self, sample, current, parent=None, uid=None):
        super().__init__(parent)
        self.setWindowTitle("Edit sample")
        self.setMinimumWidth(720)
        current = current or {}
        self._uid = uid

        self.alias = QLineEdit(current.get("alias") or "")
        # Optional lab-specific identifier — not every site keeps one, so it
        # sits alongside the alias rather than replacing the barcode / UID.
        self.internal_id = QLineEdit(current.get("internal_id") or "")
        self.internal_id.setPlaceholderText("Lab / accession no. (optional)")
        self.region = QComboBox()
        self.region.setCursor(Qt.PointingHandCursor)
        self.region.addItem("")
        self.region.addItems(list(geo.GHANA_REGIONS))
        fit_combo_popup(self.region)
        cur_region = geo.normalize_region(current.get("region")) or ""
        if cur_region:
            i = self.region.findText(cur_region)
            if i >= 0:
                self.region.setCurrentIndex(i)
        # A searchable dropdown: clicking anywhere opens the district list and
        # typing filters it. (A plain editable combo only drops down on its
        # tiny arrow zone and otherwise just shows a text cursor.)
        self.district = SearchableComboBox()
        self.district.setCursor(Qt.PointingHandCursor)
        # An editable combo sizes to its line-edit contents, so an empty
        # district collapses to a tiny box. Force it to fill the form field
        # column (matching the non-editable Region combo).
        self.district.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.district.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.district.setMinimumContentsLength(24)
        self._fill_districts(cur_region, current.get("district"))
        self.region.currentTextChanged.connect(
            lambda _t: self._fill_districts(self.region.currentText(),
                                            self.district.currentText()))
        # Collection date as a calendar-popup picker. The minimum date doubles
        # as a "not set" sentinel (shown via specialValueText) so a sample can
        # legitimately have no recorded date.
        self.date = QDateEdit()
        self.date.setCalendarPopup(True)
        self.date.setDisplayFormat("yyyy-MM-dd")
        self.date.setMinimumDate(QDate(2000, 1, 1))
        self.date.setMaximumDate(QDate.currentDate())
        self.date.setSpecialValueText("Not set")
        cd = QDate.fromString(current.get("collection_date") or "", "yyyy-MM-dd")
        self.date.setDate(cd if cd.isValid() else self.date.minimumDate())
        # WHO clinical case classification: a controlled dropdown (blank = not
        # recorded) so the value stays comparable across sites, unlike a
        # subjective free-entry severity score.
        self.case_class = QComboBox()
        self.case_class.setCursor(Qt.PointingHandCursor)
        self.case_class.setToolTip(WHO_CASE_TOOLTIP)
        self.case_class.addItem("")
        self.case_class.addItems(list(WHO_CASE_CLASSES))
        fit_combo_popup(self.case_class)
        cur_case = (current.get("case_class") or "").strip()
        if cur_case:
            i = self.case_class.findText(cur_case)
            if i >= 0:
                self.case_class.setCurrentIndex(i)
        # Patient age in whole years. A spin box constrains input to the valid
        # range; the sub-minimum sentinel (shown as "Not set" via
        # specialValueText) means "not recorded", keeping 0 free as a real value
        # for infants under one year.
        self.age_years = QSpinBox()
        self.age_years.setToolTip(AGE_YEARS_TOOLTIP)
        self.age_years.setRange(AGE_YEARS_MIN - 1, AGE_YEARS_MAX)
        self.age_years.setSpecialValueText("Not set")
        self.age_years.setSuffix(" yr")
        cur_age = current.get("age_years")
        self.age_years.setValue(int(cur_age) if cur_age is not None
                                else AGE_YEARS_MIN - 1)
        # Rich-text notes: bold/italic/underline, stored as a safe HTML subset.
        self.notes = QTextEdit()
        self.notes.setAcceptRichText(True)
        self.notes.setMinimumHeight(80)
        self.notes.setTabChangesFocus(True)
        self.notes.setPlaceholderText(
            "Free-text notes \u2014 select text, then B / I / U to format.")
        if current.get("notes"):
            self.notes.setHtml(current["notes"])

        # Monotonic guard so a slow reverse-geocode from an earlier pin can't
        # clobber the region/district of a newer one.
        self._rev_seq = 0

        # Interactive collection-site picker. Prefer the satellite web map
        # (drop/drag a pin, search exact places); fall back to the offline
        # vector picker only when QtWebEngine is unavailable. Coords come from
        # the pin alone — there are no separate lat/lon fields.
        if WEBENGINE_AVAILABLE:
            self.map = WebMapPicker()
            self.map.picked.connect(self._on_map_pick)
        else:
            self.map = GhanaPicker()
            self.map.picked.connect(
                lambda lo, la, _r: self._on_map_pick(lo, la))
        if current.get("latitude") is not None \
                and current.get("longitude") is not None:
            # Queued until the map is ready (web) / drawn immediately (vector).
            self.map.set_point(current["latitude"], current["longitude"],
                               cur_region or None)

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 18)
        root.setSpacing(12)
        title_row = QHBoxLayout()
        title = QLabel("Edit %s" % sample)
        title.setObjectName("DialogTitle")
        title_row.addWidget(title)
        if self._uid:
            # Stable label code, monospace so it reads as an ID; selectable
            # so it can be copied onto a tube label.
            uid_lbl = QLabel(self._uid)
            uid_lbl.setStyleSheet(
                "color:%s; font-family:%s; font-size:12px; font-weight:600;"
                % (theme.ACCENT_DARK, theme.MONO_STACK))
            uid_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
            title_row.addStretch(1)
            title_row.addWidget(uid_lbl)
        root.addLayout(title_row)
        root.addWidget(hrule())

        # Two columns: metadata form (left) | interactive map (right).
        cols = QHBoxLayout()
        cols.setSpacing(20)
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)
        form.addRow("Name / alias", self.alias)
        form.addRow("Internal ID", self.internal_id)
        form.addRow("Region", self.region)
        form.addRow("District", self.district)
        form.addRow("Collection date", self.date)
        form.addRow("Case severity", self.case_class)
        form.addRow("Age", self.age_years)
        form_box = QVBoxLayout()
        form_box.addLayout(form)
        form_box.addStretch(1)
        cols.addLayout(form_box, 1)

        map_box = QVBoxLayout()
        map_box.setSpacing(6)
        # Header row: section title on the left, live connectivity badge on the
        # right so the user knows whether online search will work before trying.
        head_row = QHBoxLayout()
        map_hint = QLabel("Collection site")
        map_hint.setObjectName("SectionTitle")
        head_row.addWidget(map_hint)
        head_row.addStretch(1)
        self.net_lbl = QLabel("Checking\u2026")
        self.net_lbl.setObjectName("DialogHint")
        head_row.addWidget(self.net_lbl)
        map_box.addLayout(head_row)

        # Search row (over the map): online place search in Ghana with a live
        # dropdown of real geocoder results (no offline district/region list —
        # that conflated admin names with places and was confusing). The whole
        # row is disabled while offline, so the *only* way to set a site then is
        # to drop a pin.
        self._online = None            # tri-state: None=checking, then bool
        search_row = QHBoxLayout()
        search_row.setSpacing(6)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Checking connection\u2026")
        self.search.setClearButtonEnabled(True)
        self.search.setEnabled(False)
        # Suggestions come from the online geocoder as you type (debounced).
        # Unfiltered mode: the popup shows exactly the results we supply, since
        # they are already Ghana-bounded and ranked by the geocoder.
        self._search_results = []
        self._sugg_model = QStringListModel(self.search)
        self._completer = QCompleter(self._sugg_model, self.search)
        self._completer.setCaseSensitivity(Qt.CaseInsensitive)
        self._completer.setCompletionMode(QCompleter.UnfilteredPopupCompletion)
        self.search.setCompleter(self._completer)
        self._completer.activated[str].connect(self._on_suggestion_chosen)
        self._sugg_timer = QTimer(self)
        self._sugg_timer.setSingleShot(True)
        self._sugg_timer.setInterval(400)      # debounce; respects rate limits
        self._sugg_timer.timeout.connect(self._run_suggest)
        self.search.textEdited.connect(self._on_search_typed)
        # Enter jumps straight to the top match; the dropdown covers the rest.
        self.search.returnPressed.connect(self._locate_online)
        search_row.addWidget(self.search, 1)
        map_box.addLayout(search_row)

        self.map.setMinimumWidth(300)
        map_box.addWidget(self.map, 1)
        # Coordinate readout sits strictly *below* the map so it never overlaps
        # Leaflet's on-map attribution/citation control.
        self.coord_lbl = QLabel("")
        self.coord_lbl.setObjectName("DialogHint")
        self.coord_lbl.setAlignment(Qt.AlignCenter)
        map_box.addWidget(self.coord_lbl)
        tip = QLabel("Search a place in Ghana (when online), or click/drag the "
                     "pin to mark the exact collection site.")
        tip.setObjectName("DialogHint")
        tip.setWordWrap(True)
        map_box.addWidget(tip)
        cols.addLayout(map_box, 1)
        root.addLayout(cols)
        self._update_coord_label()
        self._check_connectivity()

        # Full-width rich-text notes with a small B / I / U toolbar.
        notes_head = QHBoxLayout()
        notes_head.setSpacing(8)
        notes_lbl = QLabel("Notes")
        notes_lbl.setObjectName("SectionTitle")
        notes_head.addWidget(notes_lbl)
        for text, slot, bold, ital in (
                ("B", self._toggle_bold, True, False),
                ("I", self._toggle_italic, False, True),
                ("U", self._toggle_underline, False, False)):
            b = QPushButton(text)
            b.setObjectName("Ghost")
            b.setCheckable(True)
            b.setFixedWidth(28)
            b.setCursor(Qt.PointingHandCursor)
            f = b.font(); f.setBold(bold); f.setItalic(ital)
            f.setUnderline(text == "U"); b.setFont(f)
            b.clicked.connect(slot)
            notes_head.addWidget(b)
        notes_head.addStretch(1)
        root.addLayout(notes_head)
        root.addWidget(self.notes)

        root.addWidget(hrule())
        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        ok = buttons.button(QDialogButtonBox.Ok)
        ok.setText("Save"); ok.setObjectName("Primary")
        ok.setCursor(Qt.PointingHandCursor)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    # -- worker plumbing -------------------------------------------------
    def _run_worker(self, fn, on_done, hold):
        """Run ``fn`` off the UI thread; call ``on_done(result)`` when it ends.

        ``hold`` is an attribute-name prefix under which the thread/worker refs
        are stashed so they outlive this call (Qt would otherwise GC them mid
        run). Different purposes (reverse, forward, net) use different prefixes
        so they don't stomp on each other.
        """
        thread = QThread(self)
        worker = _NomWorker(fn)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.done.connect(on_done)
        worker.done.connect(thread.quit)
        worker.done.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        setattr(self, hold + "_thread", thread)
        setattr(self, hold + "_worker", worker)
        thread.start()

    # -- connectivity badge ---------------------------------------------
    def _check_connectivity(self):
        """Poll the geocoder's reachability (off-thread) to set the badge."""
        from .. import geocode
        self._run_worker(geocode.is_online, self._on_connectivity, "_net")

    def _on_connectivity(self, online):
        self._online = bool(online)
        if online:
            self.net_lbl.setText("\u25cf Online")
            self.net_lbl.setStyleSheet("color:%s;" % PALETTE.get("ok",
                                                                 "#1a9850"))
        else:
            self.net_lbl.setText("\u25cf Offline")
            self.net_lbl.setStyleSheet(
                "color:%s; font-weight:600;" % theme.DANGER_TEXT)
        self._set_search_enabled(self._online)

    def _set_search_enabled(self, on):
        """Enable/disable place search; offline, dropping a pin is the only
        way to set a site, so the search box goes dead (no typing) and the
        badge reads red 'Offline'."""
        self.search.setEnabled(on)
        self.search.setPlaceholderText(
            "Search a place in Ghana\u2026" if on
            else "Offline \u2014 drop a pin on the map instead")
        if not on:
            self._sugg_timer.stop()
            self._sugg_model.setStringList([])

    # -- pin / back-fill -------------------------------------------------
    def _on_map_pick(self, lon, lat):
        """User dropped/dragged the pin: back-fill region, kick a reverse geo.

        The precise collection site is the pin; the region is just the polygon
        it lands in (offline, exact). District can't come from geometry (we
        have no district polygons), so we *best-effort* reverse-geocode online
        and fill District only when the returned name matches our list.
        """
        self._update_coord_label(lon, lat)
        region = geo.region_at(lon, lat)
        if region:
            i = self.region.findText(region)
            if i >= 0:
                self.region.setCurrentIndex(i)   # refreshes district list
        # Best-effort online reverse geocode with a stale-guard.
        from .. import geocode
        self._rev_seq += 1
        seq = self._rev_seq
        self._run_worker(
            lambda: geocode.reverse(lon, lat),
            lambda res: self._on_reverse_done(res, seq), "_rev")

    def _on_reverse_done(self, result, seq):
        """Apply a reverse-geocode result if still the newest and usable."""
        if seq != self._rev_seq or not result:
            return
        self._backfill_from_address(result.get("address") or {})

    def _backfill_from_address(self, address):
        """Set Region/District from a Nominatim structured address (on match).

        Region comes from ``state`` (normalised); District from ``county`` or
        ``state_district`` matched case-insensitively against the region's
        district list — set only on a hit, otherwise left for a manual pick.
        """
        region = geo.normalize_region(address.get("state"))
        if region:
            i = self.region.findText(region)
            if i >= 0:
                self.region.setCurrentIndex(i)
        cur_region = self.region.currentText()
        cand = (address.get("county") or address.get("state_district") or "")
        cand = cand.strip()
        if cand and cur_region:
            for d in geo.districts_for(cur_region):
                if d.lower() == cand.lower():
                    self._fill_districts(cur_region, d)
                    break

    # -- search (live online suggestions) --------------------------------
    def _on_search_typed(self, text):
        """Debounce keystrokes into a background suggestion query (online)."""
        if not self._online or len(text.strip()) < 3:
            self._sugg_timer.stop()
            return
        self._sugg_timer.start()

    def _run_suggest(self):
        query = self.search.text().strip()
        if not query or not self._online:
            return
        from .. import geocode
        self._run_worker(lambda: geocode.search(query, 8),
                         self._on_suggest_done, "_sugg")

    def _on_suggest_done(self, results):
        """Populate the completer dropdown with the geocoder's ranked hits."""
        self._search_results = results or []
        names = [r.get("display_name", "?") for r in self._search_results]
        self._sugg_model.setStringList(names)
        if names and self.search.hasFocus():
            self._completer.complete()

    def _on_suggestion_chosen(self, name):
        """Apply the picked dropdown row by matching its display name."""
        for r in self._search_results:
            if r.get("display_name") == name:
                self._apply_result(r["lon"], r["lat"], r.get("address"))
                break

    def _apply_result(self, lon, lat, address=None):
        """Recentre + drop the pin at a search hit, back-filling from address.

        Uses the result's structured ``address`` so no extra network call is
        needed; region also falls back to the offline polygon under the point.
        """
        self.map.set_center(lon, lat, 12)
        self.map.set_point(lon, lat)
        self._update_coord_label(lon, lat)
        if address:
            self._backfill_from_address(address)
        else:
            region = geo.region_at(lon, lat)
            if region:
                i = self.region.findText(region)
                if i >= 0:
                    self.region.setCurrentIndex(i)

    def _locate_online(self):
        """Ghana-bounded place search in a worker thread; never blocks the UI."""
        query = self.search.text().strip()
        if not query:
            return
        from .. import geocode
        self.coord_lbl.setText("Searching\u2026")
        self._run_worker(lambda: geocode.search(query, 5),
                         self._on_search_done, "_fwd")

    def _on_search_done(self, results):
        if not results:
            self.coord_lbl.setText("No match")
            return
        # Explicit lookup applies the top-ranked hit directly (the live dropdown
        # already lets the user pick a specific one as they type).
        r = results[0]
        self._apply_result(r["lon"], r["lat"], r.get("address"))

    def _update_coord_label(self, lon=None, lat=None):
        if lon is None or lat is None:
            pt = self.map.point()
        else:
            pt = (lon, lat)
        if pt:
            self.coord_lbl.setText("%.4f, %.4f  (lon, lat)" % (pt[0], pt[1]))
        else:
            self.coord_lbl.setText("No site marked")

    def _toggle_bold(self):
        fmt = QTextCharFormat()
        cur = self.notes.currentCharFormat().fontWeight()
        fmt.setFontWeight(QFont.Normal if cur > QFont.Normal else QFont.Bold)
        self._merge_format(fmt)

    def _toggle_italic(self):
        fmt = QTextCharFormat()
        fmt.setFontItalic(not self.notes.currentCharFormat().fontItalic())
        self._merge_format(fmt)

    def _toggle_underline(self):
        fmt = QTextCharFormat()
        fmt.setFontUnderline(not self.notes.currentCharFormat().fontUnderline())
        self._merge_format(fmt)

    def _merge_format(self, fmt):
        """Apply a char format to the selection (or the cursor going forward)."""
        cursor = self.notes.textCursor()
        cursor.mergeCharFormat(fmt)
        self.notes.mergeCurrentCharFormat(fmt)
        self.notes.setFocus()

    def _notes_html(self):
        """QTextDocument -> compact safe HTML subset (<b>/<i>/<u>, <br/>).

        QTextEdit.toHtml() emits a verbose full document; we re-serialise just
        the inline styling we support so what is stored (and later rendered in
        the PDF via reportlab Paragraph) stays small and predictable.
        """
        import html as _html
        doc = self.notes.document()
        if doc.isEmpty() or not self.notes.toPlainText().strip():
            return None
        lines = []
        block = doc.begin()
        while block.isValid():
            parts = []
            it = block.begin()
            while not it.atEnd():
                frag = it.fragment()
                if frag.isValid():
                    text = _html.escape(frag.text())
                    if text:
                        cf = frag.charFormat()
                        if cf.fontUnderline():
                            text = "<u>%s</u>" % text
                        if cf.fontItalic():
                            text = "<i>%s</i>" % text
                        if cf.fontWeight() > QFont.Normal:
                            text = "<b>%s</b>" % text
                        parts.append(text)
                it += 1
            lines.append("".join(parts))
            block = block.next()
        return "<br/>".join(lines) or None

    def _fill_districts(self, region, current=None):
        self.district.blockSignals(True)
        self.district.clear()
        self.district.addItem("")
        if region:
            self.district.addItems(geo.districts_for(region))
        fit_combo_popup(self.district)
        if current:
            i = self.district.findText(current)
            if i >= 0:
                self.district.setCurrentIndex(i)
            else:
                self.district.setEditText(current)
        self.district.blockSignals(False)

    def values(self):
        # Coordinates come only from the dropped pin now (no lat/lon fields).
        pt = self.map.point()
        return {
            "alias": self.alias.text().strip() or None,
            "internal_id": self.internal_id.text().strip() or None,
            "region": self.region.currentText().strip() or None,
            "district": self.district.currentText().strip() or None,
            "case_class": self.case_class.currentText().strip() or None,
            "age_years": (self.age_years.value()
                          if self.age_years.value() >= AGE_YEARS_MIN else None),
            "latitude": pt[1] if pt else None,
            "longitude": pt[0] if pt else None,
            "collection_date": (
                None if self.date.date() == self.date.minimumDate()
                else self.date.date().toString("yyyy-MM-dd")),
            "notes": self._notes_html(),
        }


class _AuditDialog(QDialog):
    """Read-only change history for one sample (newest first)."""

    def __init__(self, sample, rows, parent=None):
        super().__init__(parent)
        self.setWindowTitle("History \u2014 %s" % sample)
        self.setMinimumSize(560, 360)
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 18)
        root.setSpacing(10)
        title = QLabel("Change history \u2014 %s" % sample)
        title.setObjectName("DialogTitle")
        root.addWidget(title)
        root.addWidget(hrule())

        headers = ["When", "Field", "Old", "New", "Source"]
        table = QTableWidget(len(rows), len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        table.setAlternatingRowColors(True)
        table.setShowGrid(False)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setHighlightSections(False)
        table.horizontalHeader().setStretchLastSection(True)
        import datetime
        for r, row in enumerate(rows):
            ts = row.get("changed_at")
            when = (datetime.datetime.fromtimestamp(ts).strftime(
                "%Y-%m-%d %H:%M") if ts else "\u2014")
            cells = [when, row.get("field", ""),
                     row.get("old_value") or "\u2014",
                     row.get("new_value") or "\u2014",
                     row.get("source", "")]
            for c, val in enumerate(cells):
                item = QTableWidgetItem(str(val))
                item.setForeground(QColor(
                    theme.HEADING if c == 1 else theme.TEXT))
                table.setItem(r, c, item)
        table.resizeColumnsToContents()
        root.addWidget(table, 1)
        if not rows:
            empty = QLabel("No changes recorded yet.")
            empty.setStyleSheet("color:%s;" % theme.FAINT)
            root.addWidget(empty)
