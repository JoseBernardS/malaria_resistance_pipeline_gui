"""Trends screen: a visual, sliceable, cross-run view of resistance.

Where the Results dashboard shows one run in depth, Trends aggregates *every*
completed run (read from each run's ``final_reports/`` CSVs) and draws the one
thing a single-run dashboard cannot: how resistance patterns move across runs.
It is charts only — no tables (those would duplicate Results) and no exports.

Three light controls slice the whole screen:
  * a dynamic **From / To** date range (calendar pickers bounded to the real
    run span, so every day the calendar shows is selectable),
  * a **Drug** focus, and
  * a **Mutation** focus.

Picking a drug or a mutation re-scopes the headline trend, the status donut and
the mutation panel to *that* item and retitles them, so you can ask "how has
Pyrimethamine resistance moved?" or "is DHFR C59R spreading?" directly.
Everything is drawn with the app's own theme-aware chart widgets so it repaints
correctly on a light/dark toggle; all aggregation lives in :mod:`trends_data`.
"""

from PyQt5.QtCore import QDate, Qt, pyqtSignal
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (QAbstractItemView, QComboBox, QDateEdit, QFrame,
                             QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                             QListWidget, QListWidgetItem, QPushButton,
                             QScrollArea, QStackedWidget, QTableWidget,
                             QTableWidgetItem, QVBoxLayout, QWidget)

from .. import db, theme
from ..charts import StackedBarChart, TrendChart
from ..widgets import PALETTE, STATUS_META, card, fit_combo_popup
from . import trends_data as td


def _tier_label(tier):
    meta = STATUS_META.get(tier)
    return meta[0] if meta else str(tier).title()


def _tier_color(tier):
    return PALETTE.get(tier, theme.MUTED)


class RunSamplesPopup(QFrame):
    """A navigable, drill-in panel for one run's flagged samples.

    Opened by clicking a dot on the prevalence trend. The left pane is a
    keyboard/mouse-navigable list of the run's samples (coloured by worst
    tier); selecting one fills the right pane with that sample's metadata —
    tier, the drugs it was flagged for, the mutations it carries (known vs
    novel, with allele fractions) and a per-gene coverage summary. Uses the
    ``Qt.Popup`` flag so it closes on any outside click and never steals the
    window's focus permanently.
    """

    def __init__(self, parent=None):
        super().__init__(parent, Qt.Popup)
        self.setObjectName("Card")
        self._samples = []           # [(name, card), ...]
        self.setMinimumWidth(460)
        self.setMaximumWidth(560)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 12, 14, 14)
        outer.setSpacing(8)

        self._title = QLabel()
        self._title.setObjectName("CardTitle")
        self._subtitle = QLabel()
        self._subtitle.setObjectName("PageHint")
        outer.addWidget(self._title)
        outer.addWidget(self._subtitle)

        body = QHBoxLayout()
        body.setSpacing(12)
        self._list = QListWidget()
        self._list.setFixedWidth(168)
        self._list.setObjectName("SamplePicker")
        self._list.currentRowChanged.connect(self._on_row)
        body.addWidget(self._list)

        self._detail = QLabel()
        self._detail.setObjectName("SampleMeta")
        self._detail.setWordWrap(True)
        self._detail.setTextFormat(Qt.RichText)
        self._detail.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        detail_scroll = QScrollArea()
        detail_scroll.setWidgetResizable(True)
        detail_scroll.setFrameShape(QFrame.NoFrame)
        detail_scroll.setWidget(self._detail)
        detail_scroll.setMinimumHeight(220)
        body.addWidget(detail_scroll, 1)
        outer.addLayout(body)

    def show_run(self, title, subtitle, samples, global_pos):
        """Populate for a run and pop up anchored near ``global_pos``."""
        self._samples = list(samples)
        self._title.setText(title)
        self._subtitle.setText(subtitle)
        self._list.blockSignals(True)
        self._list.clear()
        for name, card_data in self._samples:
            item = QListWidgetItem(name)
            item.setForeground(QColor(_tier_color(card_data.get("tier"))))
            self._list.addItem(item)
        self._list.blockSignals(False)
        if self._samples:
            self._list.setCurrentRow(0)
        else:
            self._detail.setText("<i>No flagged samples in this run.</i>")
        self.adjustSize()
        self.move(global_pos)
        self.show()
        self._list.setFocus()

    def _on_row(self, row):
        if 0 <= row < len(self._samples):
            name, card_data = self._samples[row]
            self._detail.setText(self._card_html(name, card_data))

    def _card_html(self, name, card_data):
        muted = theme.MUTED
        head = theme.HEADING
        tier = card_data.get("tier", "nomarker")
        parts = [
            "<div style='font-size:13px;font-weight:600;color:%s'>%s</div>"
            % (head, name),
            "<div style='margin-top:2px;color:%s'>%s</div>"
            % (_tier_color(tier), _tier_label(tier)),
        ]

        drugs = card_data.get("drugs") or []
        if drugs:
            rows = "".join(
                "<div>&bull; <b>%s</b> &mdash; <span style='color:%s'>%s</span>"
                "</div>%s" % (d, _tier_color(t), cls, self._evidence_html(ev))
                for d, cls, t, ev in drugs)
            parts.append(self._section("Drugs flagged", rows, muted))

        muts = card_data.get("mutations") or []
        if muts:
            rows = "".join(
                "<div>&bull; <b>%s %s</b> &mdash; "
                "<span style='color:%s'>%s</span>%s</div>%s"
                % (g, aa, _tier_color("validated" if k == "known"
                                      else "candidate"),
                   ("known marker" if k == "known" else "novel"),
                   (" &middot; AF %s" % af) if af else "",
                   self._confidence_html(dp, qual, cons, muted))
                for g, aa, k, af, dp, qual, cons in muts)
            parts.append(self._section("Mutations carried", rows, muted))

        cov = card_data.get("coverage") or []
        if cov:
            ok = sum(1 for _g, s, _d in cov if s == "OK")
            low = [(g, d) for g, s, d in cov if s != "OK"]
            summary = "<div>%d of %d genes OK</div>" % (ok, len(cov))
            if low:
                summary += "".join(
                    "<div>&bull; <b>%s</b> low (%sx)</div>" % (g, d)
                    for g, d in low)
            parts.append(self._section("Coverage", summary, muted))

        return "".join(parts)

    def _evidence_html(self, evidence):
        """The mutations that justify a drug call, indented under it."""
        if not evidence:
            return ""
        return ("<div style='margin-left:12px;color:%s'>via %s</div>"
                % (theme.MUTED, " + ".join(evidence)))

    def _confidence_html(self, dp, qual, cons, muted):
        """A dim confidence line (depth / quality / consequence) for a variant."""
        bits = []
        if cons:
            bits.append(cons)
        if dp:
            bits.append("DP %s" % dp)
        if qual:
            try:
                bits.append("Q %.0f" % float(qual))
            except (TypeError, ValueError):
                bits.append("Q %s" % qual)
        if not bits:
            return ""
        return ("<div style='margin-left:12px;color:%s;font-size:11px'>%s</div>"
                % (muted, " \u00b7 ".join(bits)))

    def _section(self, title, inner, muted):
        return ("<div style='margin-top:10px;font-size:11px;"
                "letter-spacing:.5px;text-transform:uppercase;color:%s'>%s"
                "</div><div style='margin-top:3px'>%s</div>"
                % (muted, title, inner))


class TrendsScreen(QWidget):
    """Cross-run resistance visualisation plus a searchable specimen registry.

    Two sections share this screen via an internal ``QStackedWidget``: the
    charts view (page 0) and a read-only "Patient search" registry (page 1)
    that finds a specimen across *every* completed run and opens its report.
    :meth:`show_section` switches between them for the sidebar's Trends
    children.
    """

    # Emitted (job_id, sample) when the user asks to open a specimen's report.
    open_report_requested = pyqtSignal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._bundle = td.RunBundle([], [], [], [])
        self._loading = False        # guard combo/date signals during refresh
        self._search_rows = []       # [(row_dict, uid, haystack), ...]

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._stack = QStackedWidget()

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidget(self._build_body())
        self._stack.addWidget(scroll)              # page 0: charts
        self._stack.addWidget(self._build_search())  # page 1: patient search
        root.addWidget(self._stack)

    # -- sections --------------------------------------------------------
    def show_section(self, section):
        """Switch between the charts (page 0) and patient search (page 1)."""
        if section == "search":
            self._stack.setCurrentIndex(1)
            self._load_search()
        else:
            self._stack.setCurrentIndex(0)
            self.refresh()

    # -- construction ----------------------------------------------------
    def _build_body(self):
        body = QWidget()
        lay = QVBoxLayout(body)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.setSpacing(14)

        lay.addLayout(self._build_header())
        lay.addWidget(self._build_controls())

        # The stack of chart cards, hidden as a group in the empty state.
        self._charts = QWidget()
        cl = QVBoxLayout(self._charts)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(14)

        self.trend = TrendChart()
        self.trend.pointClicked.connect(self._open_run_popup)
        self._popup = RunSamplesPopup(self)
        self._run_points = []        # per-trend-point run context, set in _render
        trend_card, tl, self._trend_title = self._titled_card(
            "Resistance prevalence across runs")
        tl.addWidget(self.trend)
        cl.addWidget(trend_card)

        self.bars = StackedBarChart(
            x_label="Drug", y_label="% of samples", legend_title="Severity")
        bar_card, bl, self._bar_title = self._titled_card(
            "Samples flagged by drug")
        bl.addWidget(self.bars)
        cl.addWidget(bar_card)

        self.mutbars = StackedBarChart(
            x_label="Mutation", y_label="% of samples",
            legend_title="Marker")
        mut_card, ml, self._mutbar_title = self._titled_card(
            "Mutation distribution across all runs")
        ml.addWidget(self.mutbars)
        cl.addWidget(mut_card)

        lay.addWidget(self._charts)

        # Centred empty-state notice, shown when there is nothing to plot.
        self.empty = QLabel(
            "No completed runs yet.\nFinish a run to see cross-run trends.")
        self.empty.setObjectName("PageHint")
        self.empty.setAlignment(Qt.AlignCenter)
        self.empty.setWordWrap(True)
        self.empty.hide()
        lay.addWidget(self.empty)

        lay.addStretch(1)
        return body

    def _titled_card(self, title):
        """A card whose title label is returned so it can be retitled live."""
        frame, lay = card()
        head = QLabel(title.upper())
        head.setObjectName("CardTitle")
        lay.addWidget(head)
        return frame, lay, head

    def _build_header(self):
        col = QVBoxLayout()
        col.setSpacing(2)
        title = QLabel("Trends")
        title.setObjectName("PageTitle")
        hint = QLabel("Resistance patterns across all your previous runs.")
        hint.setObjectName("PageHint")
        col.addWidget(title)
        col.addWidget(hint)
        return col

    def _build_controls(self):
        wrap = QFrame()
        wrap.setObjectName("Card")
        lay = QVBoxLayout(wrap)
        lay.setContentsMargins(16, 12, 16, 14)
        lay.setSpacing(12)

        row = QHBoxLayout()
        row.setSpacing(14)
        self._from_date = self._date_picker()
        self._to_date = self._date_picker()
        row.addWidget(self._field("From", self._from_date))
        row.addWidget(self._field("To", self._to_date))

        self._drug_combo = self._combo()
        row.addWidget(self._field("Drug", self._drug_combo))
        self._mut_combo = self._combo()
        row.addWidget(self._field("Mutation", self._mut_combo))
        row.addStretch(1)
        lay.addLayout(row)
        return wrap

    def _field(self, caption, widget):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(3)
        cap = QLabel(caption)
        cap.setObjectName("FieldCaption")
        v.addWidget(cap)
        v.addWidget(widget)
        return w

    def _date_picker(self):
        """A calendar-popup date field, bounded to the run span in
        ``_rebuild_controls`` so every day it shows is selectable."""
        d = QDateEdit()
        d.setCalendarPopup(True)
        d.setDisplayFormat("yyyy-MM-dd")
        d.setMinimumWidth(130)
        d.setEnabled(False)         # enabled once a run span is known
        d.dateChanged.connect(self._on_control_changed)
        return d

    def _combo(self):
        c = QComboBox()
        c.setCursor(Qt.PointingHandCursor)
        c.setMinimumWidth(150)
        c.currentIndexChanged.connect(self._on_focus_changed)
        return c

    # -- lifecycle -------------------------------------------------------
    def showEvent(self, event):
        super().showEvent(event)
        self.refresh()

    def refresh(self):
        """Reload every run's CSVs, repopulate controls, re-render."""
        self._bundle = td.load_all_runs()
        self._rebuild_controls()
        self._render()

    def restyle(self):
        """Re-bake figure colours for the active theme, re-render.

        The charts read ``theme.*`` at paint time; ``_render`` re-feeds their
        ``set_data`` and the window toggle nudges a repaint.
        """
        self._render()
        # When the patient-search page is active, rerun the filter so the status
        # chips (built from ``theme.*``/``PALETTE`` at paint time) recolour.
        if self._stack.currentIndex() == 1:
            self._run_search(self._search_box.text())

    # -- controls --------------------------------------------------------
    def _rebuild_controls(self):
        """Repopulate the date span and focus combos from the loaded bundle,
        preserving the current selections where they still exist."""
        self._loading = True

        lo, hi = td.date_span(self._bundle.dates)
        self._set_date(self._from_date, default=lo)
        self._set_date(self._to_date, default=hi)

        self._fill_combo(self._drug_combo, "All drugs",
                         [(d, d) for d in td.distinct_drugs(self._bundle.calls)])
        self._fill_combo(
            self._mut_combo, "All mutations",
            [(lbl, (gene, aa))
             for lbl, gene, aa in td.distinct_mutations(self._bundle.variants)])

        self._loading = False

    def _fill_combo(self, combo, all_label, items):
        prev = combo.currentText()
        combo.blockSignals(True)
        combo.clear()
        combo.addItem(all_label, None)
        for label, data in items:
            combo.addItem(label, data)
        idx = combo.findText(prev)
        combo.setCurrentIndex(idx if idx >= 0 else 0)
        combo.blockSignals(False)
        fit_combo_popup(combo)

    def _set_date(self, picker, default):
        """Anchor a picker on a run month without caging it.

        The picker's value defaults to a real run date, so the calendar popup
        opens on a month that actually has runs (never a greyed-out future
        month) — but selection is otherwise free. The only limit is "no future
        dates", since a run can't post-date today. A prior user pick is kept
        across reloads (e.g. when a finished job triggers a refresh).
        """
        d = self._to_qdate(default)
        if d is None:
            picker.setEnabled(False)
            return
        prev = picker.date() if picker.isEnabled() else None
        picker.blockSignals(True)
        picker.setEnabled(True)
        picker.setMaximumDate(QDate.currentDate())
        picker.setDate(prev if prev is not None else d)
        picker.blockSignals(False)

    @staticmethod
    def _to_qdate(iso):
        """``YYYY-MM-DD`` -> ``QDate`` (invalid/None -> ``None``)."""
        if not iso:
            return None
        d = QDate.fromString(iso, "yyyy-MM-dd")
        return d if d.isValid() else None

    def _on_control_changed(self, *_):
        if not self._loading:
            self._render()

    def _on_focus_changed(self, *_):
        """Drug and Mutation focuses are mutually exclusive: picking one
        specific value resets the other to "All" so the scope stays clear."""
        if self._loading:
            return
        sender = self.sender()
        other = (self._mut_combo if sender is self._drug_combo
                 else self._drug_combo)
        if sender.currentIndex() > 0 and other.currentIndex() != 0:
            other.blockSignals(True)
            other.setCurrentIndex(0)
            other.blockSignals(False)
        self._render()

    def _date_bounds(self):
        if not (self._from_date.isEnabled() and self._to_date.isEnabled()):
            return None, None
        df = self._from_date.date().toString("yyyy-MM-dd")
        dt = self._to_date.date().toString("yyyy-MM-dd")
        if df > dt:                  # tolerate an inverted pick — treat as range
            df, dt = dt, df
        return df, dt

    def _focus_drug(self):
        return self._drug_combo.currentData()

    def _focus_mutation(self):
        data = self._mut_combo.currentData()
        return data if data else (None, None)

    # -- rendering -------------------------------------------------------
    def _render(self):
        """Scope by date + focus, recompute helpers, feed every chart."""
        b = self._bundle
        df, dt = self._date_bounds()
        calls = td.filter_rows(b.calls, date_from=df, date_to=dt)
        variants = td.filter_rows(b.variants, date_from=df, date_to=dt)
        coverage = td.filter_rows(b.coverage, date_from=df, date_to=dt)
        universe = td.sample_universe(coverage, calls, variants)
        sample_dates = td.sample_dates(coverage, calls, variants)

        if not universe:
            self._charts.hide()
            self.empty.show()
            return
        self.empty.hide()
        self._charts.show()

        drug = self._focus_drug()
        gene, aa = self._focus_mutation()

        # Headline trend: overall / per-drug / per-mutation prevalence. Each dot
        # is clickable — it opens a navigable panel of that run's samples with
        # their metadata — and hovering shows a one-line summary.
        if gene:
            positive = td.carrier_keys(variants, gene, aa)
            series = td.mutation_prevalence_series(
                variants, gene, aa, universe, sample_dates)
            self._trend_title.setText(("%s %s prevalence across runs"
                                       % (gene, aa)).upper())
            noun = "carrying"
        elif drug:
            positive = td.flagged_keys(calls, drug=drug)
            series = td.drug_prevalence_series(
                calls, drug, universe, sample_dates)
            self._trend_title.setText(
                ("%s resistance across runs" % drug).upper())
            noun = "flagged"
        else:
            positive = td.flagged_keys(calls)
            series = td.prevalence_series(calls, universe, sample_dates)
            self._trend_title.setText("RESISTANCE PREVALENCE ACROSS RUNS")
            noun = "flagged"

        cards = td.sample_cards(calls, variants, coverage, positive)
        self._run_points = self._build_run_points(
            series, positive, universe, sample_dates, cards, noun)
        hints = [rp["hint"] for rp in self._run_points]
        self.trend.set_data(series, mode="prevalence", hints=hints)

        # Drug bars: per-drug flagged prevalence split by worst resistance tier
        # (validated/candidate/potential), so severity reads at a glance. Whole
        # landscape, or the drugs co-occurring in a focused mutation's carriers.
        if gene:
            bar_keys = td.carrier_keys(variants, gene, aa)
            self._bar_title.setText(
                ("Drugs flagged with %s %s" % (gene, aa)).upper())
        else:
            bar_keys = None
            self._bar_title.setText("SAMPLES FLAGGED BY DRUG")
        dcats, dgroups, ddist, dglabels = td.drug_tier_prevalence(
            calls, universe, keys=bar_keys)
        self.bars.set_data(dcats, dgroups, ddist, group_labels=dglabels)

        # Mutation distribution: for each mutated locus, the share of sequenced
        # samples carrying each alternate residue, stacked. Whole landscape, or
        # restricted to the samples flagged for a focused drug.
        if drug:
            dk = td.flagged_keys(calls, drug=drug)
            mut_variants = [v for v in variants
                            if (v.get("job_id"), v.get("Sample")) in dk]
            self._mutbar_title.setText(
                ("Mutations behind %s resistance" % drug).upper())
        else:
            mut_variants = variants
            self._mutbar_title.setText("MUTATION DISTRIBUTION ACROSS ALL RUNS")
        cats, groups, dist, glabels = td.mutation_distribution(
            mut_variants, universe, top=12)
        self.mutbars.set_data(cats, groups, dist, group_labels=glabels)

        for w in (self.trend, self.bars, self.mutbars):
            w.update()

    def _build_run_points(self, series, positive, universe, sample_dates,
                          cards, noun):
        """Per-trend-point context (parallel to ``series``).

        Each entry carries the hover ``hint`` and the ``samples`` payload the
        click popup drills into: ``[(sample_name, card), ...]`` for the date's
        positive samples, looked up from ``cards``. Samples pool by their
        effective (collection) date, matching the series buckets.
        """
        keys_by_date = {}
        for job_id, samples in universe.items():
            for s in samples:
                date = sample_dates.get((job_id, s), "")
                if not date:
                    continue
                k = (job_id, s)
                if k in positive:
                    keys_by_date.setdefault(date, []).append(k)

        points = []
        for date, frac, n in series:
            keys = sorted(keys_by_date.get(date, []), key=lambda k: k[1])
            samples = [(k[1], cards.get(k, {})) for k in keys]
            hint = ("%s &middot; %d of %d %s &middot; %.0f%% "
                    "&mdash; click for samples"
                    % (date, len(keys), n, noun, 100.0 * frac))
            points.append({"date": date, "n": n, "frac": frac,
                           "noun": noun, "samples": samples})
            points[-1]["hint"] = hint
        return points

    def _open_run_popup(self, index):
        """Open the navigable sample panel for the clicked trend point."""
        if not (0 <= index < len(self._run_points)):
            return
        rp = self._run_points[index]
        title = rp["date"]
        subtitle = ("%d of %d samples %s \u00b7 %.0f%%"
                    % (len(rp["samples"]), rp["n"], rp["noun"],
                       100.0 * rp["frac"]))
        pos = self.trend.mapToGlobal(self.trend.rect().topLeft())
        pos.setX(pos.x() + 40)
        pos.setY(pos.y() + 30)
        self._popup.show_run(title, subtitle, rp["samples"], pos)

    # -- patient search --------------------------------------------------
    _SEARCH_COLS = ("Name / alias", "Internal ID", "Specimen UID", "Region",
                    "District", "Collected", "Status", "Run", "")

    def _build_search(self):
        """The read-only specimen registry: a free-text box over a table that
        spans every completed run and opens a row's report on demand."""
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.setSpacing(14)

        head = QVBoxLayout()
        head.setSpacing(2)
        title = QLabel("Patient search")
        title.setObjectName("PageTitle")
        hint = QLabel("Find a specimen across every completed run, then open "
                      "its report.")
        hint.setObjectName("PageHint")
        head.addWidget(title)
        head.addWidget(hint)
        lay.addLayout(head)

        self._search_box = QLineEdit()
        self._search_box.setPlaceholderText(
            "Search name, ID, specimen UID, region, district, date\u2026")
        self._search_box.setClearButtonEnabled(True)
        self._search_box.textChanged.connect(self._run_search)
        lay.addWidget(self._search_box)

        frame, cl = card()
        self._search_table = QTableWidget(0, len(self._SEARCH_COLS))
        self._search_table.setHorizontalHeaderLabels(self._SEARCH_COLS)
        self._search_table.verticalHeader().setVisible(False)
        self._search_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._search_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._search_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._search_table.setAlternatingRowColors(True)
        self._search_table.setShowGrid(False)
        self._search_table.doubleClicked.connect(self._on_search_activated)
        hdr = self._search_table.horizontalHeader()
        hdr.setStretchLastSection(False)
        hdr.setSectionResizeMode(QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(0, QHeaderView.Stretch)
        cl.addWidget(self._search_table)

        self._search_hint = QLabel("Type to search across all runs.")
        self._search_hint.setObjectName("PageHint")
        self._search_hint.setAlignment(Qt.AlignCenter)
        cl.addWidget(self._search_hint)
        lay.addWidget(frame, 1)
        return page

    def _load_search(self):
        """(Re)read every persisted specimen and precompute its UID + haystack,
        then apply the current filter. Cheap (run-scale) and filesystem-free."""
        self._search_rows = []
        for row in db.list_all_samples_for_search():
            job_id = row.get("job_id") or ""
            sample = row.get("sample") or ""
            uid = db.sample_uid(job_id, sample) if job_id and sample else ""
            fields = [uid, sample, row.get("alias"), row.get("internal_id"),
                      row.get("case_class"), row.get("region"),
                      row.get("district"), row.get("collection_date"),
                      _tier_label(row.get("worst_tier"))]
            haystack = " ".join(str(f) for f in fields if f).lower()
            self._search_rows.append((row, uid, haystack))
        self._run_search(self._search_box.text())

    def _run_search(self, text):
        """Filter the registry by a case-insensitive substring over all fields
        (including the derived UID); update the table and the empty notice."""
        query = (text or "").strip().lower()
        if query:
            matches = [(r, uid) for (r, uid, hay) in self._search_rows
                       if query in hay]
        else:
            matches = [(r, uid) for (r, uid, _hay) in self._search_rows]

        table = self._search_table
        table.setRowCount(0)
        for row, uid in matches:
            self._append_search_row(row, uid)

        if not self._search_rows:
            table.hide()
            self._search_hint.setText("No specimens recorded yet.")
            self._search_hint.show()
        elif not query:
            table.hide()
            self._search_hint.setText("Type to search across all runs.")
            self._search_hint.show()
        elif not matches:
            table.hide()
            self._search_hint.setText("No specimens match \u201c%s\u201d." % text)
            self._search_hint.show()
        else:
            self._search_hint.hide()
            table.show()

    def _append_search_row(self, row, uid):
        table = self._search_table
        r = table.rowCount()
        table.insertRow(r)
        job_id = row.get("job_id") or ""
        sample = row.get("sample") or ""

        def cell(col, value):
            item = QTableWidgetItem(str(value) if value else "\u2014")
            table.setItem(r, col, item)

        cell(0, row.get("alias") or sample)
        cell(1, row.get("internal_id"))
        cell(2, uid)
        cell(3, row.get("region"))
        cell(4, row.get("district"))
        cell(5, row.get("collection_date"))
        table.setCellWidget(r, 6, self._status_chip(row.get("worst_tier")))
        cell(7, str(job_id)[:8] if job_id else "")

        btn = QPushButton("Open report")
        btn.setCursor(Qt.PointingHandCursor)
        btn.clicked.connect(
            lambda _=False, j=job_id, s=sample: self._emit_open_report(j, s))
        table.setCellWidget(r, 8, btn)

    def _status_chip(self, tier):
        """A small themed pill matching the popup's tier colours."""
        colour = _tier_color(tier)
        chip = QLabel(_tier_label(tier))
        chip.setAlignment(Qt.AlignCenter)
        chip.setStyleSheet(
            "QLabel{color:%s;border:1px solid %s;border-radius:9px;"
            "padding:1px 8px;font-size:11px;}" % (colour, colour))
        wrap = QWidget()
        wl = QHBoxLayout(wrap)
        wl.setContentsMargins(6, 2, 6, 2)
        wl.addWidget(chip)
        wl.addStretch(1)
        return wrap

    def _on_search_activated(self, index):
        """Double-clicking a row opens that specimen's report."""
        r = index.row()
        matches = [(row, uid) for (row, uid, _h) in self._search_rows]
        # The visible table is the filtered set, so re-derive from the row cells
        # rather than the unfiltered list.
        uid_item = self._search_table.item(r, 2)
        if uid_item is None:
            return
        uid = uid_item.text()
        for row, ruid, _h in self._search_rows:
            if ruid == uid:
                self._emit_open_report(row.get("job_id") or "",
                                       row.get("sample") or "")
                return

    def _emit_open_report(self, job_id, sample):
        if job_id and sample:
            self.open_report_requested.emit(str(job_id), str(sample))
