"""Home screen: jobs list + add/load-config form.

Adding a job saves (or reuses) a configuration in SQLite, enqueues a job and
hands it to the queue. Past configs can be reloaded into the form.
"""

import json
import os
import platform
import time

from PyQt5.QtCore import QDate, Qt, pyqtSignal
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (QApplication, QCheckBox, QComboBox, QDateEdit,
                             QDialog, QFileDialog, QFormLayout, QHBoxLayout,
                             QHeaderView, QLabel, QLineEdit, QMessageBox,
                             QPushButton, QScrollArea, QSpinBox, QStackedWidget,
                             QTableWidget, QTableWidgetItem, QVBoxLayout,
                             QWidget)

from .. import config_bridge, db, paths, providers, theme
from ..widgets import fit_combo_popup, hrule


class ConfigDialog(QDialog):
    """Form for creating a new job config (or editing a loaded one).

    Laid out as a three-step wizard (Inputs -> Analysis -> Samples) so each
    screen stays short: the per-barcode sample sheet gets its own step instead
    of being buried at the bottom of one long scroll.
    """

    # Sample metadata that must be filled for every barcode before a job can
    # be added: labels (region tagged down to district level per user policy).
    _REQUIRED_SAMPLE_FIELDS = (("alias", "alias"), ("region", "region"),
                              ("district", "district"),
                              ("collection_date", "collection date"))

    _STEPS = ("Inputs", "Analysis", "Samples")
    _STEP_HINTS = (
        "Name the job, point at a run's FASTQ folder and pick its "
        "reference set.",
        "Choose where it runs, the Clair3 model and the variant-calling "
        "thresholds.",
        "Give every barcode an alias, region, district and collection date "
        "(required) \u2014 pin the site and add notes if you like.",
    )

    def __init__(self, parent=None, session=None):
        super().__init__(parent)
        self._session = session
        self.setWindowTitle("Add analysis job")
        self.setMinimumWidth(580)

        # Sample sheet state: the discovered barcodes and their captured
        # metadata (keyed by barcode). Each row is edited in the same rich
        # per-sample dialog the Results screen uses, so entry here matches the
        # polish of that page instead of cramped inline table widgets.
        self._barcodes = []
        self._sample_meta = {}

        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("e.g. Run 2026-06 batch A")

        self.fastq_edit = QLineEdit()
        self.fastq_edit.setPlaceholderText("Folder of barcode* sub-folders")
        self._fastq_row = self._with_browse(self.fastq_edit, self._browse_fastq)

        self.output_edit = QLineEdit(paths.default_output_root())
        self._output_row = self._with_browse(self.output_edit,
                                             self._browse_output)

        # Reference set for this run. It pre-selects the current Data-sources
        # default but is freely selectable per run (local or cloud); whatever is
        # chosen is recorded on the job and stamped into its reference
        # provenance. Cloud also offers any sets the service advertises.
        self.ref_combo = QComboBox()
        self.ref_combo.setCursor(Qt.PointingHandCursor)
        fit_combo_popup(self.ref_combo)

        # Execution target: run on this machine (local queue) or hand off to the
        # cloud pipeline. The choice repopulates the Clair3 model combo and
        # toggles the local disclaimer below.
        self.target_combo = QComboBox()
        self.target_combo.setCursor(Qt.PointingHandCursor)
        self.target_combo.addItem("Local (this machine)", "local")
        self.target_combo.addItem("Cloud", "cloud")
        fit_combo_popup(self.target_combo)

        # Clair3 basecalling model. Local reads the on-disk registry; cloud
        # would fetch a list from the service (stub -> empty placeholder).
        self.model_combo = QComboBox()
        self.model_combo.setCursor(Qt.PointingHandCursor)
        fit_combo_popup(self.model_combo)

        self.target_combo.currentIndexChanged.connect(self._on_target_changed)
        self.model_combo.currentIndexChanged.connect(self._update_disclaimer)

        self.threads_spin = self._spin(1, 256, 8)
        self.qual_spin = self._spin(0, 100, 15)
        self.dp_spin = self._spin(0, 10000, 10)
        self.mq_spin = self._spin(0, 100, 20)

        # Quality-control & reporting choices. NanoPlot (full read-QC diagrams)
        # plus pre-trim QC are the defaults so a new run gives the richest
        # picture out of the box; NanoStat is the faster stats-only option.
        self.qc_combo = QComboBox()
        self.qc_combo.setCursor(Qt.PointingHandCursor)
        self.qc_combo.addItem("NanoStat \u2014 fast stats", "nanostat")
        self.qc_combo.addItem("NanoPlot \u2014 full plots", "nanoplot")
        self.qc_combo.setCurrentIndex(self.qc_combo.findData("nanoplot"))
        fit_combo_popup(self.qc_combo)
        self.pretrim_check = QCheckBox("Also run QC on raw reads (pre-trim)")
        self.pretrim_check.setChecked(True)
        self.report_combo = QComboBox()
        self.report_combo.setCursor(Qt.PointingHandCursor)
        self.report_combo.addItem("Combined \u2014 one PDF", "combined")
        self.report_combo.addItem("Per-sample \u2014 one PDF each", "per-sample")
        self.report_combo.addItem("Both", "both")
        fit_combo_popup(self.report_combo)

        # Obvious, bordered disclaimer shown only for Local runs: it headlines
        # the selected Clair3 model (the primary ask) plus this machine's
        # profile, so the operator can confirm the model matches their
        # sequencing chemistry/basecaller before committing CPU time. Built here
        # so the Analysis page can just place it; refreshed by _update_disclaimer.
        self.disclaimer = QLabel()
        self.disclaimer.setObjectName("LocalDisclaimer")
        self.disclaimer.setWordWrap(True)
        self.disclaimer.setTextFormat(Qt.RichText)
        self.disclaimer.setStyleSheet(
            "QLabel#LocalDisclaimer {"
            " background:%s; border:1px solid %s; border-radius:8px;"
            " padding:12px 14px; color:%s; font-size:12px; }"
            % (theme.ACCENT_WASH, theme.BORDER, theme.TEXT))

        # Run-level collection date: a convenience default that fills every
        # barcode's date at once (each row can still be overridden). Left at
        # "Not set" it changes nothing, so a run with mixed/unknown dates is
        # unaffected. Trends bucket by this date, falling back to the run date.
        self.run_date = self._date_edit()
        self.run_date.dateChanged.connect(self._apply_run_date_to_rows)

        # A clean, read-only summary of each barcode's labels; double-click a
        # row (or its Edit button) to open the full sample editor. Mirrors the
        # Results "Samples" table so the two feel like one product.
        self._sample_cols = ["Barcode", "Name / alias", "Region", "District",
                             "Collection date", ""]
        self.samples_table = QTableWidget(0, len(self._sample_cols))
        self.samples_table.setHorizontalHeaderLabels(self._sample_cols)
        self.samples_table.verticalHeader().setVisible(False)
        self.samples_table.setAlternatingRowColors(True)
        self.samples_table.setShowGrid(False)
        self.samples_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.samples_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.samples_table.setSelectionMode(QTableWidget.SingleSelection)
        self.samples_table.verticalHeader().setDefaultSectionSize(40)
        self.samples_table.setMinimumHeight(180)
        sh = self.samples_table.horizontalHeader()
        sh.setHighlightSections(False)
        sh.setStretchLastSection(False)
        sh.setSectionResizeMode(0, QHeaderView.ResizeToContents)  # Barcode
        sh.setSectionResizeMode(1, QHeaderView.Stretch)           # Alias
        sh.setSectionResizeMode(2, QHeaderView.Stretch)           # Region
        sh.setSectionResizeMode(3, QHeaderView.Stretch)           # District
        sh.setSectionResizeMode(4, QHeaderView.ResizeToContents)  # Date
        sh.setSectionResizeMode(5, QHeaderView.Fixed)             # Edit action
        self.samples_table.setColumnWidth(5, 84)
        self.samples_table.cellDoubleClicked.connect(
            lambda r, _c: self._edit_sample_row(r))

        self.samples_empty = QLabel(
            "No barcodes found. On the Inputs step pick the folder that "
            "directly contains the barcode* sub-folders \u2014 usually the "
            "run's \u201cfastq_pass\u201d folder.")
        self.samples_empty.setAlignment(Qt.AlignCenter)
        self.samples_empty.setStyleSheet(
            "color:%s; font-size:12px; padding:10px;" % theme.MUTED)

        # Three-step wizard: each page is its own scroll area so a small screen
        # only ever scrolls one short section, and the sample sheet finally gets
        # a screen of its own instead of hanging off the bottom of a long form.
        self.stack = QStackedWidget()
        self.stack.addWidget(self._wrap_scroll(self._build_inputs_page()))
        self.stack.addWidget(self._wrap_scroll(self._build_analysis_page()))
        self.stack.addWidget(self._wrap_scroll(self._build_samples_page()))

        # Fixed header: title + a step indicator (which of the 3 steps is
        # showing) + a per-step hint that swaps as you move between steps.
        title = QLabel("Add analysis job")
        title.setObjectName("DialogTitle")
        self.step_hint = QLabel()
        self.step_hint.setObjectName("DialogHint")
        self.step_hint.setWordWrap(True)
        header = QVBoxLayout()
        header.setContentsMargins(28, 22, 28, 8)
        header.setSpacing(8)
        header.addWidget(title)
        header.addLayout(self._build_step_indicator())
        header.addWidget(self.step_hint)

        # Footer: Load previous on the left; Cancel / Back / Next-or-Add on the
        # right. "Next" becomes "Add job" on the final (Samples) step.
        footer = QHBoxLayout()
        footer.setContentsMargins(28, 12, 28, 16)
        load_btn = QPushButton("Load previous\u2026")
        load_btn.setObjectName("Ghost")
        load_btn.setCursor(Qt.PointingHandCursor)
        load_btn.clicked.connect(self._load_previous)
        footer.addWidget(load_btn)
        footer.addStretch(1)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.setObjectName("Ghost")
        cancel_btn.setCursor(Qt.PointingHandCursor)
        cancel_btn.clicked.connect(self.reject)
        footer.addWidget(cancel_btn)
        self.back_btn = QPushButton("\u2039 Back")
        self.back_btn.setObjectName("Ghost")
        self.back_btn.setCursor(Qt.PointingHandCursor)
        self.back_btn.clicked.connect(self._go_back)
        footer.addWidget(self.back_btn)
        self.next_btn = QPushButton()
        self.next_btn.setObjectName("Primary")
        self.next_btn.setCursor(Qt.PointingHandCursor)
        self.next_btn.setDefault(True)
        self.next_btn.clicked.connect(self._go_next)
        footer.addWidget(self.next_btn)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addLayout(header)
        outer.addWidget(hrule())
        outer.addWidget(self.stack, 1)
        outer.addWidget(hrule())
        outer.addLayout(footer)

        # Keep the modal within the screen; each page's scroll area absorbs
        # any overflow.
        screen = QApplication.primaryScreen()
        avail_h = screen.availableGeometry().height() if screen else 900
        self.setMaximumHeight(avail_h - 60)
        self.resize(620, min(760, avail_h - 60))

        # Auto-list barcodes whenever the FASTQ path is typed/edited, and set
        # the initial empty state.
        self.fastq_edit.editingFinished.connect(self._populate_samples)
        self._populate_samples()

        # Prime the model combo + disclaimer for the default (Local) target,
        # then paint the wizard chrome for the first step.
        self._on_target_changed()
        self._update_nav()

    # -- wizard pages / navigation --------------------------------------
    def _wrap_scroll(self, content):
        """Put a page widget in a frameless, vertically-scrolling area."""
        scroll = QScrollArea()
        scroll.setWidget(content)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        return scroll

    def _page(self):
        """A blank wizard page with consistent margins/spacing."""
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(28, 18, 28, 22)
        lay.setSpacing(16)
        return page, lay

    def _build_inputs_page(self):
        page, root = self._page()
        root.addWidget(self._section("Inputs"))
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(10)
        form.addRow("Name", self.name_edit)
        form.addRow("FASTQ dir", self._fastq_row)
        form.addRow("Output dir", self._output_row)
        form.addRow("Reference set", self.ref_combo)
        root.addLayout(form)
        root.addStretch(1)
        return page

    def _build_analysis_page(self):
        page, root = self._page()
        root.addWidget(self._section("Execution"))
        execu = QFormLayout()
        execu.setLabelAlignment(Qt.AlignRight)
        execu.setHorizontalSpacing(16)
        execu.setVerticalSpacing(10)
        execu.addRow("Run on", self.target_combo)
        execu.addRow("Clair3 model", self.model_combo)
        root.addLayout(execu)
        root.addWidget(self.disclaimer)

        root.addWidget(self._section("Parameters"))
        params = QFormLayout()
        params.setLabelAlignment(Qt.AlignRight)
        params.setHorizontalSpacing(16)
        params.setVerticalSpacing(10)
        params.addRow("Threads", self.threads_spin)
        params.addRow("Min QUAL", self.qual_spin)
        params.addRow("Min DP", self.dp_spin)
        params.addRow("Min MAPQ", self.mq_spin)
        root.addLayout(params)

        root.addWidget(self._section("Quality control & reporting"))
        qc = QFormLayout()
        qc.setLabelAlignment(Qt.AlignRight)
        qc.setHorizontalSpacing(16)
        qc.setVerticalSpacing(10)
        qc.addRow("QC tool", self.qc_combo)
        qc.addRow("", self.pretrim_check)
        qc.addRow("PDF report", self.report_combo)
        root.addLayout(qc)
        root.addStretch(1)
        return page

    def _build_samples_page(self):
        page, root = self._page()
        root.setSpacing(12)
        root.addWidget(self._section("Samples"))
        samp_hint = QLabel("Double-click a barcode (or its \u201cEdit\u201d "
                           "button) to open the full editor \u2014 the same one "
                           "as the Results page. Every sample needs an "
                           "<b>alias, region, district and collection date</b> "
                           "before you can add the job; the map pin and notes "
                           "are optional.")
        samp_hint.setTextFormat(Qt.RichText)
        samp_hint.setObjectName("DialogHint")
        samp_hint.setWordWrap(True)
        root.addWidget(samp_hint)

        run_date_row = QHBoxLayout()
        rd_cap = QLabel("Collection date (applies to all)")
        rd_cap.setObjectName("DialogHint")
        run_date_row.addWidget(rd_cap)
        run_date_row.addWidget(self.run_date)
        run_date_row.addStretch(1)
        root.addLayout(run_date_row)

        root.addWidget(self.samples_table, 1)
        root.addWidget(self.samples_empty)
        # Live completeness caption: turns green once every barcode has its
        # required alias/region/district/date, red while any are missing.
        self.samples_status = QLabel()
        self.samples_status.setWordWrap(True)
        root.addWidget(self.samples_status)
        return page

    def _build_step_indicator(self):
        """A '1. Inputs > 2. Analysis > 3. Samples' breadcrumb; the active
        step is highlighted by _update_nav."""
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        self._step_labels = []
        for i, name in enumerate(self._STEPS):
            lbl = QLabel("%d. %s" % (i + 1, name))
            lbl.setObjectName("WizardStep")
            self._step_labels.append(lbl)
            row.addWidget(lbl)
            if i < len(self._STEPS) - 1:
                sep = QLabel("\u203a")
                sep.setStyleSheet("color:%s;" % theme.FAINT)
                row.addWidget(sep)
        row.addStretch(1)
        return row

    def _update_nav(self):
        """Repaint the wizard chrome (indicator, hint, buttons) for the
        current step."""
        step = self.stack.currentIndex()
        last = self.stack.count() - 1
        self.step_hint.setText(self._STEP_HINTS[step])
        self.back_btn.setEnabled(step > 0)
        self.next_btn.setText("Add job" if step == last
                              else "Next \u203a")
        for i, lbl in enumerate(self._step_labels):
            active = (i == step)
            lbl.setStyleSheet(
                "QLabel#WizardStep { color:%s; font-weight:%s; }"
                % (theme.ACCENT if active else theme.MUTED,
                   "600" if active else "400"))

    def _go_back(self):
        step = self.stack.currentIndex()
        if step > 0:
            self.stack.setCurrentIndex(step - 1)
            self._update_nav()

    def _go_next(self):
        """Advance a step (validating Inputs on the way out of step 1), or
        finish the job on the last step."""
        step = self.stack.currentIndex()
        last = self.stack.count() - 1
        if step == 0 and not self._validate_inputs():
            return
        if step == last:
            self._finish()
            return
        self.stack.setCurrentIndex(step + 1)
        self._update_nav()

    # -- helpers ---------------------------------------------------------
    def _section(self, text):
        lbl = QLabel(text.upper())
        lbl.setObjectName("FormSection")
        return lbl

    def _spin(self, lo, hi, val):
        s = QSpinBox()
        s.setRange(lo, hi)
        s.setValue(val)
        return s

    def _date_edit(self):
        """A calendar date field that reads "Not set" until a real date is
        picked (its minimum sentinel), bounded to no future dates."""
        d = QDateEdit()
        d.setCalendarPopup(True)
        d.setDisplayFormat("yyyy-MM-dd")
        d.setMinimumDate(QDate(2000, 1, 1))
        d.setMaximumDate(QDate.currentDate())
        d.setSpecialValueText("Not set")
        d.setDate(d.minimumDate())
        return d

    def _apply_run_date_to_rows(self, *_):
        """Push the run-level collection date onto every barcode.

        A no-op while the run-level picker is "Not set" (its minimum), so it
        never clobbers per-row dates unless the operator actually chose one.
        """
        if self.run_date.date() == self.run_date.minimumDate():
            return
        ds = self.run_date.date().toString("yyyy-MM-dd")
        for bc in self._barcodes:
            self._sample_meta.setdefault(bc, {})["collection_date"] = ds
        self._render_samples()

    # -- execution target / Clair3 model --------------------------------
    def _on_target_changed(self, *_):
        """Repopulate the model combo for the chosen target and refresh UI.

        Local lists the on-disk registry; Cloud shows a disabled placeholder
        (the real list is fetched at submit time by the future client). The
        disclaimer is only meaningful for local runs.
        """
        target = self.target_combo.currentData()
        self.model_combo.blockSignals(True)
        self.model_combo.clear()
        if target == "cloud":
            names = providers.cloud_model_names(self._session)
            if names:
                self.model_combo.addItems(names)
                self.model_combo.setEnabled(True)
            elif self._session is not None and self._session.is_authenticated():
                # Signed in but the registry came back empty/unreachable.
                self.model_combo.addItem("No models available")
                self.model_combo.setEnabled(False)
            else:
                # Signed out or no server: the list can't be fetched yet.
                self.model_combo.addItem("Sign in to load models")
                self.model_combo.setEnabled(False)
        else:
            self.model_combo.addItems(config_bridge.clair3_model_names())
            self.model_combo.setEnabled(True)
        fit_combo_popup(self.model_combo)
        self.model_combo.blockSignals(False)
        self._populate_ref_combo()
        self._update_disclaimer()

    def _selected_model(self):
        """The chosen model name, or None when the combo is a placeholder."""
        if not self.model_combo.isEnabled():
            return None
        return self.model_combo.currentText().strip() or None

    def _machine_profile(self):
        """One-line description of this machine for the local disclaimer."""
        cores = os.cpu_count() or "?"
        return "%s / %s \u00b7 %s cores \u00b7 runs locally on CPU \u2014 " \
               "no GPU required" % (
                   platform.system() or "?", platform.machine() or "?", cores)

    def _update_disclaimer(self, *_):
        """Show/refresh the local disclaimer; hide it for cloud runs."""
        if self.target_combo.currentData() != "local":
            self.disclaimer.setVisible(False)
            return
        model = self._selected_model() or config_bridge.DEFAULT_CLAIR3_MODEL
        self.disclaimer.setText(
            "<b>Clair3 model: %s</b><br/>"
            "Make sure this matches your sequencing chemistry and "
            "basecaller \u2014 the wrong model degrades variant calls.<br/>"
            "<span style='color:%s'>%s</span>"
            % (model, theme.MUTED, self._machine_profile()))
        self.disclaimer.setVisible(True)

    def _with_browse(self, line_edit, handler):
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        btn = QPushButton("Browse\u2026")
        btn.clicked.connect(handler)
        lay.addWidget(line_edit)
        lay.addWidget(btn)
        return w

    def _browse_fastq(self):
        d = QFileDialog.getExistingDirectory(self, "Select FASTQ directory")
        if d:
            self.fastq_edit.setText(d)
            self._populate_samples()

    def _browse_output(self):
        d = QFileDialog.getExistingDirectory(self, "Select output directory")
        if d:
            self.output_edit.setText(d)

    def _load_previous(self):
        configs = db.list_configs()
        if not configs:
            QMessageBox.information(self, "No saved configs",
                                   "There are no previously saved configurations.")
            return
        names = ["%s  (%s)" % (c["name"], str(c["id"])[:8]) for c in configs]
        from PyQt5.QtWidgets import QInputDialog
        choice, ok = QInputDialog.getItem(
            self, "Load Previous Configuration",
            "Choose a configuration:", names, 0, False)
        if not ok:
            return
        cfg = configs[names.index(choice)]
        self._populate(cfg)

    def _populate_samples(self):
        """Re-list the FASTQ dir's barcodes, keeping any captured metadata.

        If the operator picked the run folder (or a wrapper) rather than the
        ``fastq_pass`` that directly holds ``barcode*``, quietly descend to the
        right level and rewrite the field so both the sheet and the pipeline
        run against the same directory.
        """
        fastq = self.fastq_edit.text().strip()
        if fastq and os.path.isdir(fastq):
            resolved = paths.resolve_barcode_root(fastq)
            if resolved != fastq:
                self.fastq_edit.setText(resolved)
                fastq = resolved
        self._barcodes = (paths.discover_barcodes(fastq)
                          if fastq and os.path.isdir(fastq) else [])
        self._render_samples()

    def _render_samples(self):
        """Paint the read-only summary table from the captured metadata.

        Metadata lives in ``self._sample_meta`` (survives FASTQ re-scans), so
        rebuilding the rows never loses what the operator already entered.
        """
        t = self.samples_table
        t.setRowCount(len(self._barcodes))
        for r, bc in enumerate(self._barcodes):
            m = self._sample_meta.get(bc, {})
            gaps = self._sample_gaps(m)
            # The barcode tints red while its row is missing required fields,
            # so incomplete samples are obvious without opening each one.
            self._set_sample_cell(r, 0, bc, bold=True,
                                  color=(theme.DANGER_TEXT if gaps
                                         else theme.HEADING))
            self._set_sample_cell(r, 1, m.get("alias") or "")
            self._set_sample_cell(r, 2, m.get("region") or "")
            self._set_sample_cell(r, 3, m.get("district") or "")
            self._set_sample_cell(r, 4, m.get("collection_date") or "")
            t.setCellWidget(r, 5, self._edit_button(r))
        has = bool(self._barcodes)
        t.setVisible(has)
        self.samples_empty.setVisible(not has)
        self._update_samples_status()

    def _set_sample_cell(self, row, col, value, bold=False, color=None):
        item = QTableWidgetItem(value)
        item.setFlags(item.flags() & ~Qt.ItemIsEditable)
        if bold:
            f = item.font(); f.setBold(True); item.setFont(f)
        if color is None:
            color = theme.MUTED if not value else theme.TEXT
        item.setForeground(QColor(color))
        self.samples_table.setItem(row, col, item)

    def _sample_gaps(self, meta):
        """Required-field labels still missing from a sample's metadata."""
        return [label for key, label in self._REQUIRED_SAMPLE_FIELDS
                if not str(meta.get(key) or "").strip()]

    def _incomplete_samples(self):
        """``[(barcode, [missing labels])]`` for rows lacking required fields."""
        out = []
        for bc in self._barcodes:
            gaps = self._sample_gaps(self._sample_meta.get(bc, {}))
            if gaps:
                out.append((bc, gaps))
        return out

    def _update_samples_status(self):
        """Refresh the green/red completeness caption under the table."""
        total = len(self._barcodes)
        if total == 0:
            self.samples_status.setText("")
            return
        incomplete = self._incomplete_samples()
        done = total - len(incomplete)
        if incomplete:
            self.samples_status.setText(
                "%d of %d samples complete \u2014 each needs an alias, region, "
                "district and collection date." % (done, total))
            color = theme.DANGER_TEXT
        else:
            self.samples_status.setText("All %d samples complete." % total)
            color = "#15803d"
        self.samples_status.setStyleSheet("color:%s; font-size:12px;" % color)

    def _edit_button(self, row):
        host = QWidget()
        lay = QHBoxLayout(host)
        lay.setContentsMargins(4, 0, 4, 0)
        btn = QPushButton("Edit")
        btn.setObjectName("Ghost")
        btn.setCursor(Qt.PointingHandCursor)
        btn.clicked.connect(lambda _=False, r=row: self._edit_sample_row(r))
        lay.addWidget(btn)
        return host

    def _edit_sample_row(self, row):
        """Open the rich per-sample editor (alias, geo-tag, notes) for a row.

        Reuses the Results screen's dialog so job setup and later editing share
        one polished editor. The returned values are held in memory and written
        as this run's sample metadata once the job is created.
        """
        if not (0 <= row < len(self._barcodes)):
            return
        from .dashboard import _SampleEditDialog
        bc = self._barcodes[row]
        current = dict(self._sample_meta.get(bc, {}))
        # Seed the run-level default date when this row has none of its own.
        if not current.get("collection_date") and \
                self.run_date.date() != self.run_date.minimumDate():
            current["collection_date"] = \
                self.run_date.date().toString("yyyy-MM-dd")
        dlg = _SampleEditDialog(bc, current, self)
        if dlg.exec() != QDialog.Accepted:
            return
        self._sample_meta[bc] = dlg.values()
        self._render_samples()

    def sample_sheet(self):
        """Captured labels as ``{barcode: {fields}}`` (empty dict if none).

        Only barcodes present in the current FASTQ dir with at least one
        non-empty field are emitted; ``upsert_sample_meta`` keeps just the
        recognised metadata columns.
        """
        out = {}
        for bc in self._barcodes:
            m = self._sample_meta.get(bc)
            if not m:
                continue
            fields = {k: v for k, v in m.items() if v not in (None, "")}
            if fields:
                out[bc] = fields
        return out

    def _populate(self, cfg):
        self.name_edit.setText(cfg["name"])
        self.fastq_edit.setText(cfg["fastq_dir"])
        self.output_edit.setText(cfg["output_dir"])
        # Pre-select the reference set this config recorded (falling back to the
        # current default). _on_target_changed below repopulates the combo and
        # preserves this selection; the user is free to change it for this run.
        self._select_reference_set(cfg.get("reference_set")
                                   or config_bridge.default_reference_set_name())
        self.threads_spin.setValue(cfg["threads"])
        self.qual_spin.setValue(cfg["min_qual"])
        self.dp_spin.setValue(cfg["min_dp"])
        self.mq_spin.setValue(cfg["min_mq"])
        try:
            extra = json.loads(cfg.get("extra_json") or "{}")
        except (ValueError, TypeError):
            extra = {}
        self._select_data(self.qc_combo, extra.get("QC_TOOL"))
        self.pretrim_check.setChecked(bool(extra.get("RUN_PRETRIM_QC")))
        self._select_data(self.report_combo, extra.get("REPORT_MODE"))
        # Restore execution target first (it repopulates the model combo),
        # then re-select the saved model within that list.
        self._select_data(self.target_combo, cfg.get("execution_target"))
        self._on_target_changed()
        model = cfg.get("clair3_model")
        if model:
            idx = self.model_combo.findText(model)
            if idx >= 0:
                self.model_combo.setCurrentIndex(idx)
        self._populate_samples()

    def _populate_ref_combo(self):
        """Fill the reference-set combo for the current target.

        Lists the local catalog (builtin + user/synced sets); Cloud also merges
        in any sets the service advertises. Keeps the current selection when it
        survives, else falls back to the Data-sources default. Free to change
        per run -- the choice is what gets recorded and synced as provenance.
        """
        keep = self.ref_combo.currentText().strip()
        names = list(config_bridge.reference_set_names())
        if self.target_combo.currentData() == "cloud":
            for n in providers.cloud_reference_set_names(self._session):
                if n not in names:
                    names.append(n)
        want = keep or config_bridge.default_reference_set_name()
        if want and want not in names:
            names.insert(0, want)
        self.ref_combo.blockSignals(True)
        self.ref_combo.clear()
        self.ref_combo.addItems(names)
        idx = self.ref_combo.findText(want)
        if idx >= 0:
            self.ref_combo.setCurrentIndex(idx)
        fit_combo_popup(self.ref_combo)
        self.ref_combo.blockSignals(False)

    def _select_reference_set(self, name):
        """Select ``name`` in the reference combo, adding it if not present."""
        if not name:
            return
        idx = self.ref_combo.findText(name)
        if idx < 0:
            self.ref_combo.addItem(name)
            idx = self.ref_combo.findText(name)
        self.ref_combo.setCurrentIndex(idx)

    @staticmethod
    def _select_data(combo, data):
        """Select the combo entry whose userData matches ``data`` (if any)."""
        if data is None:
            return
        idx = combo.findData(data)
        if idx >= 0:
            combo.setCurrentIndex(idx)

    def _validate_inputs(self):
        """Gate for leaving the Inputs step: name + a usable FASTQ/output dir.

        Returns True when the inputs are valid; otherwise warns and returns
        False so the wizard stays on (or jumps back to) the Inputs step.
        """
        if not self.name_edit.text().strip():
            QMessageBox.warning(self, "Missing name", "Please name this job.")
            return False
        fastq = self.fastq_edit.text().strip()
        if not fastq or not os.path.isdir(fastq):
            QMessageBox.warning(self, "Invalid FASTQ dir",
                                "Please choose an existing FASTQ directory.")
            return False
        output = self.output_edit.text().strip()
        if not output:
            QMessageBox.warning(self, "Missing output dir",
                                "Please choose an output directory.")
            return False
        # Clair3 and parts of the bash pipeline word-split unquoted paths, so
        # a space anywhere in the FASTQ or output path breaks variant calling.
        if " " in fastq or " " in output:
            QMessageBox.warning(
                self, "Path contains a space",
                "The FASTQ and output paths must not contain spaces "
                "(the variant caller cannot handle them).\n\n"
                "Please choose locations without spaces in the path.")
            return False
        return True

    def _validate_samples(self):
        """Gate the final accept: every barcode needs its required fields.

        Returns True when all barcodes carry an alias, region, district and
        collection date; otherwise names the incomplete ones and returns False
        so the wizard stays on the Samples step.
        """
        incomplete = self._incomplete_samples()
        if not incomplete:
            return True
        lines = ["\u2022 %s \u2014 missing %s" % (bc, ", ".join(gaps))
                 for bc, gaps in incomplete]
        QMessageBox.warning(
            self, "Sample details required",
            "Every barcode needs an alias, region, district and collection "
            "date before you can add the job. Double-click a row to fill it "
            "in.\n\n" + "\n".join(lines))
        return False

    def _finish(self):
        """Final-step accept: re-check inputs, require complete sample data,
        then the duplicate/barcode gates, then close the wizard."""
        if not self._validate_inputs():
            # A late input edit slipped through; send the user back to fix it.
            self.stack.setCurrentIndex(0)
            self._update_nav()
            return
        if not self._validate_samples():
            return
        fastq = self.fastq_edit.text().strip()
        # Last gates: warn on an exact duplicate run, then on reused barcodes.
        # "Go back" leaves the dialog open with everything the user typed.
        fields = {k: self.values().get(k) for k in db.FINGERPRINT_KEYS}
        had_dup, proceed = self._check_duplicate_run(fastq, fields)
        if not proceed:
            return
        # an exact duplicate already implies barcode reuse; skip the redundant nag
        if not had_dup and not self._confirm_barcode_reuse(fastq):
            return
        self.accept()

    def _check_duplicate_run(self, fastq_dir, fields):
        """Warn if this exact run (inputs + params) was already enqueued.

        Returns ``(had_duplicates, proceed)``. Non-fatal: any lookup error
        yields ``(False, True)`` so it never blocks job creation. On a match,
        names each earlier run and lets the user proceed or go back.
        """
        try:
            fp = db.compute_input_fingerprint(fastq_dir, fields)
            dups = db.find_duplicate_jobs(fp)
        except Exception:
            return (False, True)
        if not dups:
            return (False, True)
        lines = []
        for d in dups:
            when = time.strftime("%Y-%m-%d %H:%M",
                                 time.localtime(d["queued_at"]))
            lines.append("\u2022 %s \u2014 queued %s" % (d["name"], when))
        msg = QMessageBox(self)
        msg.setIcon(QMessageBox.Warning)
        msg.setWindowTitle("Duplicate run")
        msg.setText("This looks identical to %d earlier run(s) "
                    "(same input files and analysis settings):" % len(dups))
        msg.setInformativeText("\n".join(lines))
        proceed = msg.addButton("Add job anyway", QMessageBox.AcceptRole)
        msg.addButton("Go back", QMessageBox.RejectRole)
        msg.setDefaultButton(proceed)
        msg.exec()
        return (True, msg.clickedButton() is proceed)

    def _confirm_barcode_reuse(self, fastq_dir):
        """Warn if this run's barcodes were already used in earlier runs.

        Returns True to proceed (Add anyway / nothing to warn / lookup failed),
        False if the user chooses to go back. Non-fatal: a lookup error never
        blocks job creation.
        """
        try:
            barcodes = paths.discover_barcodes(fastq_dir)
            reuse = db.find_barcode_reuse(barcodes)
        except Exception:
            return True
        if not reuse:
            return True
        lines = []
        for bc in sorted(reuse):
            runs = ", ".join(sorted({r["name"] for r in reuse[bc]}))
            lines.append("\u2022 %s \u2014 also in: %s" % (bc, runs))
        msg = QMessageBox(self)
        msg.setIcon(QMessageBox.Warning)
        msg.setWindowTitle("Barcodes reused across runs")
        msg.setText("%d barcode(s) in this run were already used before. "
                    "Each sample still gets its own unique ID, but check "
                    "these aren't mix-ups:" % len(reuse))
        msg.setInformativeText("\n".join(lines))
        # QMessageBox has no setButtonText in PyQt5; add explicit buttons.
        proceed = msg.addButton("Add job anyway", QMessageBox.AcceptRole)
        msg.addButton("Go back", QMessageBox.RejectRole)
        msg.setDefaultButton(proceed)
        msg.exec()
        return msg.clickedButton() is proceed

    def values(self):
        return dict(
            name=self.name_edit.text().strip(),
            fastq_dir=self.fastq_edit.text().strip(),
            output_dir=self.output_edit.text().strip(),
            reference_set=self.ref_combo.currentText().strip(),
            threads=self.threads_spin.value(),
            min_qual=self.qual_spin.value(),
            min_dp=self.dp_spin.value(),
            min_mq=self.mq_spin.value(),
            execution_target=self.target_combo.currentData(),
            clair3_model=self._selected_model())

    def extra(self):
        """QC/reporting choices, keyed by pipeline conf name for the .conf."""
        return {
            "QC_TOOL": self.qc_combo.currentData(),
            "RUN_PRETRIM_QC": self.pretrim_check.isChecked(),
            "REPORT_MODE": self.report_combo.currentData(),
        }


class JobsScreen(QWidget):
    """Jobs data table + Add Job button. Emits requests to the queue."""

    add_job_requested = pyqtSignal(str, dict)  # config_id, sample sheet
    open_job_requested = pyqtSignal(str)       # job id (double-click)
    live_run_requested = pyqtSignal(str, dict)  # config_id, sample sheet

    COLUMNS = ["Job", "ID", "Run on", "Reference set", "Threads", "Status",
               "Duration", "Queued"]
    STATUS_FG = {
        "queued": theme.MUTED, "running": theme.ACCENT,
        "completed": "#15803d", "failed": "#b91c1c", "stopped": theme.FAINT,
    }

    def __init__(self, queue, parent=None, session=None):
        super().__init__(parent)
        self._queue = queue
        self._session = session
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(16)

        header = QHBoxLayout()
        col = QVBoxLayout()
        col.setSpacing(2)
        title = QLabel("Analysis jobs")
        title.setObjectName("PageTitle")
        self._hint = QLabel("Each run is processed sequentially. "
                            "Double-click a completed job to view its results.")
        self._hint.setObjectName("PageHint")
        col.addWidget(title)
        col.addWidget(self._hint)
        header.addLayout(col)
        header.addStretch(1)
        # "Live run" watches a MinKNOW folder while a sequencing run is still in
        # progress, showing a live per-amplicon depth grid so the operator can
        # stop the flow cell early; it's local-only (no folder to watch on the
        # cloud). "Add job" runs the full pipeline as usual.
        live_btn = QPushButton("Live run")
        live_btn.setObjectName("Ghost")
        live_btn.setCursor(Qt.PointingHandCursor)
        live_btn.clicked.connect(self._on_live)
        header.addWidget(live_btn, 0, Qt.AlignTop)
        add_btn = QPushButton("Add job")
        add_btn.setObjectName("Primary")
        add_btn.setCursor(Qt.PointingHandCursor)
        add_btn.clicked.connect(self._on_add)
        header.addWidget(add_btn, 0, Qt.AlignTop)
        root.addLayout(header)

        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setDefaultSectionSize(40)
        hh = self.table.horizontalHeader()
        hh.setHighlightSections(False)
        hh.setSectionResizeMode(0, QHeaderView.Stretch)
        for c in range(1, len(self.COLUMNS)):
            hh.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        self.table.cellDoubleClicked.connect(self._on_double_click)
        root.addWidget(self.table, 1)

        self.empty = QLabel("No jobs yet. Click \u201cAdd job\u201d to start.")
        self.empty.setAlignment(Qt.AlignCenter)
        self.empty.setStyleSheet(
            "color:%s; font-size:14px;" % theme.MUTED)
        # Share the stretch slot with the table: exactly one is visible at a
        # time, so the empty message centres in the content area instead of
        # floating below a zero-height table.
        root.addWidget(self.empty, 1)

        self.refresh()

    def refresh(self):
        jobs = db.list_jobs()
        self.table.setVisible(bool(jobs))
        self.empty.setVisible(not jobs)
        # The top hint only makes sense once there are jobs to act on; hiding it
        # when empty keeps the empty state to a single, uncluttered message.
        self._hint.setVisible(bool(jobs))
        self.table.setRowCount(len(jobs))
        run_on_col = self.COLUMNS.index("Run on")
        status_col = self.COLUMNS.index("Status")
        for r, job in enumerate(jobs):
            cfg = db.get_config(job["config_id"]) or {}
            short_id = str(job["id"])[:8]
            name = job.get("config_name") or "Job %s" % short_id
            status = job.get("status", "queued")
            # Where the run executes. A cloud job is the config's target, made
            # authoritative once it holds a server run id.
            is_cloud = (cfg.get("execution_target") == "cloud"
                        or bool(job.get("remote_run_id")))
            cells = [
                name,
                short_id,
                "Cloud" if is_cloud else "Local",
                job.get("reference_set", ""),
                str(cfg.get("threads", "")),
                status.capitalize(),
                self._fmt_duration(job.get("started_at"),
                                   job.get("finished_at")),
                self._fmt_time(job.get("queued_at")),
            ]
            for c, val in enumerate(cells):
                item = QTableWidgetItem(val)
                item.setData(Qt.UserRole, job["id"])
                if c == 0:
                    f = item.font(); f.setBold(True); item.setFont(f)
                    item.setForeground(QColor(theme.HEADING))
                elif c == run_on_col:
                    # Cloud stands out in accent; local stays quiet.
                    item.setForeground(QColor(theme.ACCENT if is_cloud
                                              else theme.MUTED))
                elif c == status_col:
                    item.setForeground(
                        QColor(self.STATUS_FG.get(status, theme.MUTED)))
                else:
                    item.setForeground(QColor(theme.MUTED))
                self.table.setItem(r, c, item)

    @staticmethod
    def _fmt_time(ts):
        if not ts:
            return "\u2014"
        import datetime
        return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")

    @staticmethod
    def _fmt_duration(started, finished):
        """Wall-clock run time of a finished job, or an em dash if it never ran
        or is still running (no finish timestamp yet)."""
        if not started or not finished:
            return "\u2014"
        secs = int(finished - started)
        if secs < 60:
            return "%ds" % secs
        if secs < 3600:
            return "%dm %02ds" % (secs // 60, secs % 60)
        return "%dh %02dm" % (secs // 3600, (secs % 3600) // 60)

    def _on_add(self):
        dlg = ConfigDialog(self, session=self._session)
        if dlg.exec() != QDialog.Accepted:
            return
        v = dlg.values()
        os.makedirs(v["output_dir"], exist_ok=True)
        config_id = db.save_config(
            name=v["name"], fastq_dir=v["fastq_dir"],
            output_dir=v["output_dir"], reference_set=v["reference_set"],
            threads=v["threads"], min_qual=v["min_qual"],
            min_dp=v["min_dp"], min_mq=v["min_mq"],
            execution_target=v["execution_target"],
            clair3_model=v["clair3_model"], extra=dlg.extra())
        self.add_job_requested.emit(config_id, dlg.sample_sheet() or {})
        self.refresh()

    def _on_live(self):
        """Open the job wizard, persist the config, then start a live run.

        Reuses the saved config verbatim (no new schema): the app hands the
        config id to the LiveRunController, which watches the FASTQ folder and
        runs incremental coverage scans until the operator finalizes. Live runs
        are local-only, so a cloud target is coerced back to local first.
        """
        dlg = ConfigDialog(self, session=self._session)
        if dlg.exec() != QDialog.Accepted:
            return
        v = dlg.values()
        os.makedirs(v["output_dir"], exist_ok=True)
        config_id = db.save_config(
            name=v["name"], fastq_dir=v["fastq_dir"],
            output_dir=v["output_dir"], reference_set=v["reference_set"],
            threads=v["threads"], min_qual=v["min_qual"],
            min_dp=v["min_dp"], min_mq=v["min_mq"],
            execution_target="local",
            clair3_model=v["clair3_model"], extra=dlg.extra())
        # The sample sheet is applied to the finalize job (which mints the real
        # job id); carry it through so the Results view keeps its labels.
        self.live_run_requested.emit(config_id, dlg.sample_sheet() or {})
        self.refresh()

    def _on_double_click(self, row, _col):
        item = self.table.item(row, 0)
        if item is not None:
            self.open_job_requested.emit(item.data(Qt.UserRole))
