"""Surveillance screen: a Ghana map aggregating the user's local history.

Reads the durable, persisted outcomes (``sample_outcome`` left-joined with
``sample_meta``) across *all* of the user's runs and aggregates them by
region. The map is local-only for now; a future backend will serve scoped
nationwide trends. Output dirs may be long gone — this never touches them.

A metric toggle switches the choropleth between resistance *prevalence*
(share of assessed samples with any resistance call) and raw *sample count*.
GPS pins are drawn for samples carrying lat/lon, coloured by their worst tier.
"""

import csv
import datetime

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (QAbstractItemView, QButtonGroup, QComboBox,
                             QFileDialog, QHBoxLayout, QHeaderView, QLabel,
                             QMessageBox, QPushButton, QSplitter,
                             QTableWidget, QTableWidgetItem, QVBoxLayout,
                             QWidget)

from .. import db, geo, theme
from ..charts import GhanaMap, TrendChart
from ..widgets import KeyFigures, card, fit_combo_popup

# A sample counts as "resistant" for prevalence if its worst tier is one of
# the resistance tiers (any call was made).
_RESISTANT_TIERS = {"validated", "candidate", "potential"}

# Period filter label -> day window (None = all time).
_PERIODS = [
    ("All time", None),
    ("Last 30 days", 30),
    ("Last 90 days", 90),
    ("Last 12 months", 365),
]


class SurveillanceScreen(QWidget):
    """National map + per-region rollup of the local run history."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._mode = "prevalence"
        self._region_filter = ""     # "" = all regions
        self._period = None          # day window; None = all time
        # Last computed per-region aggregation, for CSV export.
        self._last_agg = []          # [(region, n, resistant)]

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(16)

        header = QHBoxLayout()
        col = QVBoxLayout()
        col.setSpacing(2)
        title = QLabel("Surveillance")
        title.setObjectName("PageTitle")
        hint = QLabel("Resistance across your runs, aggregated by region. "
                      "Local history only.")
        hint.setObjectName("PageHint")
        col.addWidget(title)
        col.addWidget(hint)
        header.addLayout(col)
        header.addStretch(1)

        self._prev_btn = QPushButton("Prevalence")
        self._count_btn = QPushButton("Sample count")
        for b in (self._prev_btn, self._count_btn):
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
        self._prev_btn.setChecked(True)
        group = QButtonGroup(self)
        group.setExclusive(True)
        group.addButton(self._prev_btn)
        group.addButton(self._count_btn)
        self._prev_btn.clicked.connect(lambda: self._set_mode("prevalence"))
        self._count_btn.clicked.connect(lambda: self._set_mode("count"))
        header.addWidget(self._prev_btn, 0, Qt.AlignTop)
        header.addWidget(self._count_btn, 0, Qt.AlignTop)

        # Region + period filters narrow every figure/table/trend below.
        self._region_combo = QComboBox()
        self._region_combo.setCursor(Qt.PointingHandCursor)
        self._region_combo.addItem("All regions")
        self._region_combo.addItems(list(geo.GHANA_REGIONS))
        fit_combo_popup(self._region_combo)
        self._region_combo.currentIndexChanged.connect(self._on_region_changed)
        header.addWidget(self._region_combo, 0, Qt.AlignTop)

        self._period_combo = QComboBox()
        self._period_combo.setCursor(Qt.PointingHandCursor)
        for label, _ in _PERIODS:
            self._period_combo.addItem(label)
        fit_combo_popup(self._period_combo)
        self._period_combo.currentIndexChanged.connect(self._on_period_changed)
        header.addWidget(self._period_combo, 0, Qt.AlignTop)

        export_btn = QPushButton("Export CSV")
        export_btn.setCursor(Qt.PointingHandCursor)
        export_btn.clicked.connect(self._export_csv)
        header.addWidget(export_btn, 0, Qt.AlignTop)
        root.addLayout(header)

        # Map | summary split via a horizontal splitter so both reflow on
        # resize instead of the summary keeping a fixed width.
        body = QSplitter(Qt.Horizontal)
        body.setChildrenCollapsible(False)

        map_card, map_lay = card("National map")
        self.map = GhanaMap()
        map_lay.addWidget(self.map)
        self._unmapped = QLabel("")
        self._unmapped.setStyleSheet(
            "color:%s; font-size:11px;" % theme.FAINT)
        map_lay.addWidget(self._unmapped)
        map_card.setMinimumWidth(360)
        body.addWidget(map_card)

        right = QVBoxLayout()
        right.setSpacing(16)
        self.figures = KeyFigures([
            ("samples", "Samples", theme.HEADING),
            ("regions", "Regions", theme.HEADING),
            ("resistant", "% resistant", theme.DANGER_TEXT),
        ])
        fig_card, fig_lay = card("Summary")
        fig_lay.addWidget(self.figures)
        right.addWidget(fig_card)

        table_card, table_lay = card("By region")
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Region", "Samples",
                                              "%Resistant"])
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(30)
        hh = self.table.horizontalHeader()
        hh.setHighlightSections(False)
        hh.setSectionResizeMode(0, QHeaderView.Stretch)
        hh.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        # Clicking a region row focuses the Region filter on it (drill-down).
        self.table.cellClicked.connect(self._on_table_click)
        table_lay.addWidget(self.table)
        right.addWidget(table_card, 1)

        right_wrap = QWidget()
        right_wrap.setLayout(right)
        right_wrap.setMinimumWidth(320)
        body.addWidget(right_wrap)
        body.setSizes([720, 380])
        root.addWidget(body, 1)

        # Full-width resistance-over-time trend beneath the map/summary.
        trend_card, trend_lay = card("Resistance over time")
        self.trend = TrendChart()
        trend_lay.addWidget(self.trend)
        self._trend_caption = QLabel("")
        self._trend_caption.setStyleSheet(
            "color:%s; font-size:11px;" % theme.FAINT)
        trend_lay.addWidget(self._trend_caption)
        root.addWidget(trend_card)

    def _on_region_changed(self, _idx):
        text = self._region_combo.currentText()
        self._region_filter = "" if text == "All regions" else text
        self.refresh()

    def _on_period_changed(self, idx):
        self._period = _PERIODS[idx][1] if 0 <= idx < len(_PERIODS) else None
        self.refresh()

    def _on_table_click(self, row, _col):
        item = self.table.item(row, 0)
        if not item:
            return
        i = self._region_combo.findText(item.text())
        if i >= 0 and i != self._region_combo.currentIndex():
            self._region_combo.setCurrentIndex(i)   # triggers refresh()

    def _set_mode(self, mode):
        self._mode = mode
        self.refresh()

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh()

    @staticmethod
    def _parse_date(value):
        """Parse a ``collection_date`` to a ``date``, or None if unparseable."""
        if not value:
            return None
        text = str(value).strip()
        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%m/%d/%Y", "%Y-%m"):
            try:
                return datetime.datetime.strptime(text, fmt).date()
            except ValueError:
                continue
        return None

    def _filtered_rows(self, rows):
        """Apply the region + period filters before any aggregation.

        Region filtering keeps rows whose normalised region matches. The
        period keeps rows whose ``collection_date`` parses and is on/after the
        cutoff; rows with no/unparseable date are kept under "All time" only.
        """
        cutoff = None
        if self._period is not None:
            cutoff = (datetime.date.today()
                      - datetime.timedelta(days=self._period))
        out = []
        for r in rows:
            if self._region_filter:
                if geo.normalize_region(r.get("region")) != self._region_filter:
                    continue
            if cutoff is not None:
                d = self._parse_date(r.get("collection_date"))
                if d is None or d < cutoff:
                    continue
            out.append(r)
        return out

    def restyle(self):
        """Re-render with the active theme: rebake the figure colours, then
        rebuild the map/table so baked cell colours follow the new palette."""
        self.figures.restyle([theme.HEADING, theme.HEADING, theme.DANGER_TEXT])
        self.refresh()

    def refresh(self):
        rows = self._filtered_rows(db.list_all_outcomes_with_meta())

        # Aggregate by region: total samples, resistant samples.
        by_region = {}          # region -> [total, resistant]
        pins = []
        total = 0
        resistant_total = 0
        unmapped = 0
        for r in rows:
            total += 1
            tier = r.get("worst_tier")
            is_res = tier in _RESISTANT_TIERS
            if is_res:
                resistant_total += 1
            region = geo.normalize_region(r.get("region"))
            if region:
                agg = by_region.setdefault(region, [0, 0])
                agg[0] += 1
                if is_res:
                    agg[1] += 1
            else:
                unmapped += 1
            lat, lon = r.get("latitude"), r.get("longitude")
            if lat is not None and lon is not None:
                key = tier if tier in _RESISTANT_TIERS else "nomarker"
                pins.append((lon, lat, key))

        # Choropleth metric per region.
        metric = {}
        for region, (n, res) in by_region.items():
            if self._mode == "count":
                metric[region] = n
            else:
                metric[region] = (res / n) if n else 0.0
        self.map.set_data(metric, pins=pins, mode=self._mode)

        # Key figures.
        self.figures.set_value("samples", total)
        self.figures.set_value("regions", len(by_region))
        pct = (100.0 * resistant_total / total) if total else 0
        self.figures.set_value("resistant", "%.0f%%" % pct)

        # Per-region table.
        ordered = sorted(by_region.items(),
                         key=lambda kv: kv[1][0], reverse=True)
        self._last_agg = [(region, n, res) for region, (n, res) in ordered]
        self.table.setRowCount(len(ordered))
        for ri, (region, (n, res)) in enumerate(ordered):
            rpct = (100.0 * res / n) if n else 0
            cells = [region, str(n), "%.0f%%" % rpct]
            for ci, val in enumerate(cells):
                item = QTableWidgetItem(val)
                if ci == 0:
                    f = item.font(); f.setBold(True); item.setFont(f)
                    item.setForeground(QColor(theme.HEADING))
                else:
                    item.setForeground(QColor(theme.TEXT))
                self.table.setItem(ri, ci, item)

        self._unmapped.setText(
            "unmapped: %d (no region set)" % unmapped if unmapped else "")

        self._update_trend(rows)

    def _update_trend(self, rows):
        """Monthly resistance prevalence over the filtered rows.

        Buckets by ``YYYY-MM`` from ``collection_date``; per bucket prevalence
        is resistant/total. Samples with no/unparseable date are excluded from
        the trend only (counted in a caption), still counted everywhere else.
        """
        buckets = {}            # "YYYY-MM" -> [total, resistant]
        no_date = 0
        for r in rows:
            d = self._parse_date(r.get("collection_date"))
            if d is None:
                no_date += 1
                continue
            key = "%04d-%02d" % (d.year, d.month)
            agg = buckets.setdefault(key, [0, 0])
            agg[0] += 1
            if r.get("worst_tier") in _RESISTANT_TIERS:
                agg[1] += 1

        points = []
        for key in sorted(buckets):
            n, res = buckets[key]
            value = (res / n) if n else 0.0
            points.append((key, value, n))
        self.trend.set_data(points, mode="prevalence")
        self._trend_caption.setText(
            "n excluded: %d (no date)" % no_date if no_date else "")

    def _export_csv(self):
        """Write the current per-region aggregation to a CSV the user picks."""
        if not self._last_agg:
            QMessageBox.information(self, "Nothing to export",
                                    "No regions in the current view.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export surveillance CSV", "surveillance.csv",
            "CSV (*.csv)")
        if not path:
            return
        try:
            with open(path, "w", newline="") as fh:
                w = csv.writer(fh)
                w.writerow(["Region", "Samples", "Resistant", "%Resistant"])
                for region, n, res in self._last_agg:
                    rpct = (100.0 * res / n) if n else 0
                    w.writerow([region, n, res, "%.0f" % rpct])
        except Exception as e:
            QMessageBox.critical(self, "Export failed", str(e))
            return
        QMessageBox.information(self, "Exported", "Saved to %s" % path)
