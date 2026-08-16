"""Live progress screen: barcode loop, step detail, log, resource gauges.

The pipeline loops the per-barcode steps once for every ``barcode*`` folder,
then runs a final reports phase. The screen mirrors that: a scrollable sidebar
shows overall progress, a per-barcode checklist (with each barcode's own
duration), the current barcode's step detail and the final reports phase.
"""

import os
import subprocess
import sys
import time

from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtWidgets import (QHBoxLayout, QHeaderView, QLabel, QPlainTextEdit,
                             QProgressBar, QPushButton, QScrollArea,
                             QTableWidget, QVBoxLayout, QWidget)

from .. import theme
from ..runner import FINAL_STEPS, PER_BARCODE_STEPS
from ..widgets import COVERAGE_COLORS, PALETTE, ResourceGauge, StepRow, card, hrule

try:
    import psutil
except Exception:  # pragma: no cover
    psutil = None


class ProgressScreen(QWidget):
    """Shows progress for the active job; wired to the queue's signals."""

    stop_requested = pyqtSignal()
    finalize_requested = pyqtSignal()   # live run: operator clicked "Finalize now"

    def __init__(self, queue, parent=None):
        super().__init__(parent)
        self._queue = queue
        self._start_time = None
        self._output_dir = None
        self._log_path = None

        # barcode-loop state
        self._barcodes = []
        self._bc_rows = {}
        self._bc_started = {}
        self._current_bc = None
        self._in_final = False
        self._cloud_mode = False    # remote run: hide local-only detail cards
        self._cloud_units_seen = False
        self._step_started = {}     # per-barcode step -> start time (current bc)
        self._final_started = {}

        # live-run (folder-watch) state
        self._live_mode = False
        self._live_watch_dir = None
        self._live_cycle = 0
        self._live_cover = {}       # {barcode: {gene: (depth, status)}}
        self._live_genes = []       # column order (gene names), first-seen order

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(16)

        # -- header: title + controls --
        header = QHBoxLayout()
        col = QVBoxLayout()
        col.setSpacing(2)
        page_title = QLabel("Pipeline progress")
        page_title.setObjectName("PageTitle")
        self.title = QLabel("No active run")
        self.title.setObjectName("PageHint")
        col.addWidget(page_title)
        col.addWidget(self.title)
        header.addLayout(col)
        header.addStretch(1)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setObjectName("Danger")
        self.stop_btn.clicked.connect(self.stop_requested)
        self.logs_btn = QPushButton("View logs")
        self.logs_btn.clicked.connect(self._open_logs)
        self.output_btn = QPushButton("Open output")
        self.output_btn.clicked.connect(self._open_output)
        for b in (self.logs_btn, self.output_btn, self.stop_btn):
            header.addWidget(b, 0, Qt.AlignTop)
        outer.addLayout(header)

        body = QHBoxLayout()
        body.setSpacing(16)

        # -- left column (scrollable sidebar) --
        left = QVBoxLayout()
        left.setContentsMargins(0, 0, 0, 0)
        left.setSpacing(16)

        status_card, status_lay = card("Overview")
        meta = QHBoxLayout()
        self.sample_label = QLabel("Barcode \u2014")
        self.sample_label.setStyleSheet("color:%s;" % theme.TEXT)
        self.elapsed_label = QLabel("Elapsed 00:00")
        self.elapsed_label.setStyleSheet("color:%s;" % theme.MUTED)
        meta.addWidget(self.sample_label)
        meta.addStretch(1)
        meta.addWidget(self.elapsed_label)
        status_lay.addLayout(meta)
        self.overall = QProgressBar()
        self.overall.setRange(0, 1)
        self.overall.setTextVisible(False)
        self.overall.setFixedHeight(8)
        status_lay.addWidget(self.overall)
        self.step_count = QLabel("0 / 0 steps")
        self.step_count.setStyleSheet("color:%s; font-size:11px;" % theme.MUTED)
        status_lay.addWidget(self.step_count)
        left.addWidget(status_card)

        # per-barcode checklist (the loop)
        self.barcodes_card, self.barcodes_lay = card("Barcodes")
        self._bc_container = QVBoxLayout()
        self._bc_container.setSpacing(0)
        self.barcodes_lay.addLayout(self._bc_container)
        left.addWidget(self.barcodes_card)

        # current barcode step detail (hidden for cloud runs, which report a
        # sampled snapshot rather than a live per-sub-step event stream)
        self._steps_card, steps_lay = card("Current barcode")
        self._steps = {}
        for i, name in enumerate(PER_BARCODE_STEPS):
            if i:
                steps_lay.addWidget(hrule())
            row = StepRow(name)
            self._steps[name] = row
            steps_lay.addWidget(row)
        left.addWidget(self._steps_card)

        # final reports phase (runs once after the loop)
        self._final_card, final_lay = card("Reports")
        self._final = {}
        for i, name in enumerate(FINAL_STEPS):
            if i:
                final_lay.addWidget(hrule())
            row = StepRow(name)
            self._final[name] = row
            final_lay.addWidget(row)
        left.addWidget(self._final_card)

        self._gauges_card, gauges_lay = card("System resources")
        self.cpu_gauge = ResourceGauge("CPU")
        self.ram_gauge = ResourceGauge("Memory")
        self.disk_gauge = ResourceGauge("Disk")
        for g in (self.cpu_gauge, self.ram_gauge, self.disk_gauge):
            gauges_lay.addWidget(g)
        left.addWidget(self._gauges_card)
        left.addStretch(1)

        left_inner = QWidget()
        left_inner.setLayout(left)
        sidebar = QScrollArea()
        sidebar.setWidgetResizable(True)
        sidebar.setWidget(left_inner)
        sidebar.setFixedWidth(356)
        sidebar.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        body.addWidget(sidebar)

        # -- right column: live coverage grid (folder-watch only) + live log --
        right = QVBoxLayout()
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(16)

        # Live coverage depth grid (folder-watch mode only; hidden otherwise).
        # Placed in the wide main column so every amplicon column is visible.
        # Rows = barcodes, columns = amplicon genes, cells = a coloured chip
        # with the amplicon depth. A "Finalize now" button (disabled until the
        # run is saturated) hands off to the full pipeline.
        self._live_card, live_lay = card("Coverage grid")
        live_head = QHBoxLayout()
        self._live_status = QLabel("Waiting for reads\u2026")
        self._live_status.setWordWrap(True)
        self._live_status.setStyleSheet(
            "color:%s; font-size:11px;" % theme.MUTED)
        live_head.addWidget(self._live_status, 1)
        self.finalize_btn = QPushButton("Finalize now")
        self.finalize_btn.setObjectName("Primary")
        self.finalize_btn.setCursor(Qt.PointingHandCursor)
        self.finalize_btn.setEnabled(False)
        self.finalize_btn.setToolTip(
            "Enabled once every discovered barcode has adequate depth "
            "on all amplicons.")
        self.finalize_btn.clicked.connect(self.finalize_requested)
        live_head.addWidget(self.finalize_btn, 0, Qt.AlignTop)
        live_lay.addLayout(live_head)
        self.cover_table = QTableWidget(0, 0)
        self.cover_table.setShowGrid(False)
        self.cover_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.cover_table.setSelectionMode(QTableWidget.NoSelection)
        self.cover_table.setFocusPolicy(Qt.NoFocus)
        self.cover_table.setMinimumHeight(220)
        self.cover_table.horizontalHeader().setHighlightSections(False)
        self.cover_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.Stretch)
        self.cover_table.verticalHeader().setDefaultSectionSize(40)
        live_lay.addWidget(self.cover_table)
        self._live_card.setVisible(False)
        right.addWidget(self._live_card)

        log_card, log_lay = card("Live log")
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(5000)
        self.log.setFrameShape(QPlainTextEdit.NoFrame)
        self.log.setStyleSheet(
            "QPlainTextEdit { background:%s; color:%s; border:none; "
            "font-family:%s; font-size:12px; }" %
            (theme.SUBTLE, theme.TEXT, theme.MONO_STACK))
        log_lay.addWidget(self.log, 1)
        right.addWidget(log_card, 1)

        right_inner = QWidget()
        right_inner.setLayout(right)
        body.addWidget(right_inner, 1)

        outer.addLayout(body, 1)

        # timers
        self._elapsed_timer = QTimer(self)
        self._elapsed_timer.setInterval(1000)
        self._elapsed_timer.timeout.connect(self._tick_elapsed)
        self._res_timer = QTimer(self)
        self._res_timer.setInterval(2000)
        self._res_timer.timeout.connect(self._tick_resources)

        # wire queue signals
        queue.step_changed.connect(self.on_step_changed)
        queue.log_line.connect(self.append_log)
        queue.sample_changed.connect(self.on_sample_changed)

    def restyle(self):
        """Re-apply the colours baked into labels and the log on a theme
        change; the log keeps its accumulated text."""
        self.sample_label.setStyleSheet("color:%s;" % theme.TEXT)
        self.elapsed_label.setStyleSheet("color:%s;" % theme.MUTED)
        self.step_count.setStyleSheet("color:%s; font-size:11px;" % theme.MUTED)
        self.log.setStyleSheet(
            "QPlainTextEdit { background:%s; color:%s; border:none; "
            "font-family:%s; font-size:12px; }" %
            (theme.SUBTLE, theme.TEXT, theme.MONO_STACK))
        self._live_status.setStyleSheet(
            "color:%s; font-size:11px;" % theme.MUTED)
        # Repaint the coverage grid so its status chips pick up the new palette.
        if self._live_mode:
            self._render_cover_table()
            self._update_live_status()

    # -- run lifecycle ---------------------------------------------------
    def begin(self, job, config_name="", barcodes=None):
        self.title.setText("Running: %s" %
                           (config_name or "Job %s" % str(job["id"])[:8]))
        self._output_dir = job.get("output_dir")
        self._log_path = job.get("log_path")
        self._start_time = time.time()
        self._current_bc = None
        self._in_final = False
        self._cloud_mode = False
        self._cloud_units_seen = False
        self._step_started = {}
        self._final_started = {}
        self._bc_started = {}
        self.log.clear()

        # Leaving any prior live-run mode: hide the coverage grid and restore
        # the normal local-run detail cards (a prior run may have been cloud or
        # a live watch handing off to finalize).
        self._live_mode = False
        self._live_card.setVisible(False)
        self.overall.setRange(0, 1)
        for c in (self._steps_card, self._final_card, self._gauges_card):
            c.setVisible(True)
        self._set_barcodes(barcodes or [])
        for row in self._steps.values():
            row.set_state("pending")
            row.set_duration("")
        for row in self._final.values():
            row.set_state("pending")
            row.set_duration("")
        self.sample_label.setText("Barcode \u2014")
        self._recompute_overall()

        self.stop_btn.setEnabled(True)
        self._elapsed_timer.start()
        self._res_timer.start()
        self._tick_resources()

    def end(self):
        self._elapsed_timer.stop()
        self._res_timer.stop()
        self.stop_btn.setEnabled(False)

    # -- barcode list ----------------------------------------------------
    def _set_barcodes(self, barcodes):
        # clear existing rows
        while self._bc_container.count():
            item = self._bc_container.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._barcodes = list(barcodes)
        self._bc_rows = {}
        if not self._barcodes:
            self.barcodes_card.setVisible(False)
            return
        self.barcodes_card.setVisible(True)
        for i, name in enumerate(self._barcodes):
            if i:
                self._bc_container.addWidget(hrule())
            row = StepRow(name)
            self._bc_rows[name] = row
            self._bc_container.addWidget(row)

    def _ensure_barcode_row(self, name):
        """Add a row for a barcode that wasn't pre-discovered (fallback)."""
        if name in self._bc_rows:
            return
        if self._barcodes:
            self._bc_container.addWidget(hrule())
        self._barcodes.append(name)
        row = StepRow(name)
        self._bc_rows[name] = row
        self._bc_container.addWidget(row)
        self.barcodes_card.setVisible(True)

    # -- signal handlers -------------------------------------------------
    def on_sample_changed(self, sample):
        now = time.time()
        self._ensure_barcode_row(sample)
        # finalise the previous barcode
        if self._current_bc and self._current_bc in self._bc_rows:
            prev = self._bc_rows[self._current_bc]
            if prev.state != "error":
                prev.set_state("done")
            started = self._bc_started.get(self._current_bc)
            if started:
                prev.set_duration(self._fmt_dur(now - started))

        self._current_bc = sample
        self._bc_started[sample] = now
        self._bc_rows[sample].set_state("running")

        # reset the per-barcode step detail for the new barcode
        self._step_started = {}
        for row in self._steps.values():
            row.set_state("pending")
            row.set_duration("")

        idx = self._barcodes.index(sample) + 1 if sample in self._barcodes else 0
        total = len(self._barcodes)
        self.sample_label.setText(
            "Barcode %d / %d \u00b7 %s" % (idx, total, sample))
        self._recompute_overall()

    def on_step_changed(self, step, state):
        now = time.time()
        if step in FINAL_STEPS:
            self._enter_final()
            row = self._final.get(step)
            if not row:
                return
            row.set_state(state)
            if state == "running":
                self._final_started[step] = now
                fidx = FINAL_STEPS.index(step)
                for prev in FINAL_STEPS[:fidx]:
                    if self._final[prev].state == "pending":
                        self._final[prev].set_state("done")
            elif state in ("done", "error"):
                started = self._final_started.get(step)
                if started:
                    row.set_duration(self._fmt_dur(now - started))
            self._recompute_overall()
            return

        row = self._steps.get(step)
        if not row:
            return
        row.set_state(state)
        if state == "running":
            self._step_started[step] = now
            idx = PER_BARCODE_STEPS.index(step)
            for prev in PER_BARCODE_STEPS[:idx]:
                if self._steps[prev].state == "pending":
                    self._steps[prev].set_state("done")
        elif state in ("done", "error"):
            started = self._step_started.get(step)
            if started:
                row.set_duration(self._fmt_dur(now - started))
        self._recompute_overall()

    # Coarse cloud handoff phases (mirrors cloud_queue.CLOUD_STEPS), shown
    # before the remote run starts reporting real per-unit progress.
    _CLOUD_PHASES = ["Package", "Upload", "Submit", "Remote run", "Download"]
    _CLOUD_PHASE_LABEL = {
        "Package": "Packaging input\u2026",
        "Upload": "Uploading input\u2026",
        "Submit": "Submitting run\u2026",
        "Remote run": "Running on server\u2026",
        "Download": "Downloading results\u2026",
    }

    def on_cloud_phase(self, phase, state):
        """Surface the cloud handoff phases on the progress screen.

        These phases don't map onto the local per-barcode step rows, so drive a
        coarse phase bar and a status label from them. This keeps the screen
        alive during packaging/upload/submit (which can be long) and until the
        remote run begins emitting real ``progress`` units.
        """
        self._enter_cloud_mode()
        if state == "error":
            self.sample_label.setText("%s \u2014 failed" % phase)
            return
        if state == "done" and phase == "Download":
            self.overall.setRange(0, 1)
            self.overall.setValue(1)
            self.step_count.setText("Complete")
            return
        if state != "running":
            return
        self.sample_label.setText(self._CLOUD_PHASE_LABEL.get(phase, phase))
        # Coarse phase bar, only until the remote run reports real units.
        if not self._cloud_units_seen and phase in self._CLOUD_PHASES:
            idx = self._CLOUD_PHASES.index(phase)
            n = len(self._CLOUD_PHASES)
            self.overall.setRange(0, n)
            self.overall.setValue(idx)
            self.step_count.setText(
                "%s \u00b7 phase %d / %d" % (phase, idx + 1, n))

    def _enter_cloud_mode(self):
        """Collapse the screen to the useful signals for a remote run.

        Cloud work runs on the server and reports only a coarse polled snapshot,
        so the local-run detail cards convey nothing: the per-barcode checklist,
        the current-barcode sub-steps and the Reports rows have no live event
        stream to drive them, and the System-resources gauges measure the local
        machine (which is merely polling). Hide all four and stop the resource
        timer; the Overview (bar + ``stage`` label) and the Live log remain.
        """
        if self._cloud_mode:
            return
        self._cloud_mode = True
        for c in (self.barcodes_card, self._steps_card,
                  self._final_card, self._gauges_card):
            c.setVisible(False)
        self._res_timer.stop()

    def on_cloud_progress(self, completed, total, stage):
        """Render a remote run's ``progress`` snapshot.

        Shows only the two signals the pipeline API provides: the authoritative
        ``completed_units`` / ``total_units`` count on the overall bar and the
        human ``stage`` string, shown verbatim (no parsing).
        """
        self._enter_cloud_mode()
        self._cloud_units_seen = True
        if stage:
            self.sample_label.setText(stage)
        if total > 0:
            done = max(0, min(completed, total))
            self.overall.setRange(0, total)
            self.overall.setValue(done)
            self.step_count.setText("%d / %d steps" % (done, total))

    # -- live (folder-watch) mode ---------------------------------------
    def enter_live_mode(self, barcodes, watch_dir):
        """Collapse the screen to the live coverage grid for a folder-watch run.

        Mirrors :meth:`_enter_cloud_mode`: the per-barcode sub-steps and Reports
        cards have no live event stream in a watch loop, so hide them and show
        the Coverage grid + Finalize button instead. The Overview, Live log and
        System-resources cards remain.
        """
        self._live_mode = True
        self._live_watch_dir = watch_dir
        self._live_cycle = 0
        self._live_cover = {}
        self._live_genes = []
        self.title.setText("Live run")
        # Hide the loop/step/report cards; keep Overview, resources, log.
        for c in (self.barcodes_card, self._steps_card, self._final_card):
            c.setVisible(False)
        self._live_card.setVisible(True)
        self.finalize_btn.setEnabled(False)
        self._render_cover_table()
        self._update_live_status()
        # The overall bar is meaningless for an open-ended watch loop; show a
        # busy sweep instead of a fixed step count.
        self.overall.setRange(0, 0)
        self.step_count.setText("Watching for reads\u2026")
        self.sample_label.setText("Live coverage")

    def on_coverage_updated(self, cover):
        """Render a fresh live coverage snapshot from the controller.

        ``cover`` is ``{barcode: {gene: (depth, status)}}``. A new cycle is
        counted each time this fires so the status line reads "cycle N".
        """
        self._live_cycle += 1
        self._live_cover = cover or {}
        # Preserve first-seen gene column order, appending any new genes.
        for genes in self._live_cover.values():
            for g in genes:
                if g not in self._live_genes:
                    self._live_genes.append(g)
        self._render_cover_table()
        self._update_live_status()

    def set_finalize_enabled(self, enabled):
        """Enable/disable the Finalize button (driven by ``saturated``)."""
        self.finalize_btn.setEnabled(bool(enabled))
        if enabled:
            self.finalize_btn.setToolTip(
                "All amplicons have adequate depth \u2014 finalize to run the "
                "full pipeline (variant calling + report).")

    def _update_live_status(self):
        n = len(self._live_cover)
        watch = self._live_watch_dir or "\u2014"
        if self._live_cycle == 0:
            self._live_status.setText("Watching %s \u2014 waiting for reads\u2026"
                                      % watch)
        else:
            self._live_status.setText(
                "Watching %s \u00b7 cycle %d \u00b7 %d barcode%s"
                % (watch, self._live_cycle, n, "" if n == 1 else "s"))

    def _render_cover_table(self):
        """Paint the depth grid: rows = barcodes, cols = amplicon genes.

        Each cell is a coloured *chip* (a styled ``QLabel`` cell widget) showing
        the amplicon depth, tinted by coverage status with the same semantic
        palette as the dashboard resistance overview (green OK / amber LOW /
        grey NO / neutral absent). Chips are used rather than item background
        roles because the app stylesheet's ``QTableWidget::item`` rule overrides
        ``QTableWidgetItem.setBackground`` — a widget's own stylesheet always
        wins, so the status colour is guaranteed to render.
        """
        t = self.cover_table
        barcodes = sorted(self._live_cover)
        genes = list(self._live_genes)
        t.clear()
        t.setColumnCount(len(genes))
        t.setHorizontalHeaderLabels(genes)
        t.setRowCount(len(barcodes))
        t.setVerticalHeaderLabels(barcodes)
        t.verticalHeader().setVisible(bool(barcodes))
        for r, bc in enumerate(barcodes):
            row = self._live_cover.get(bc, {})
            for c, gene in enumerate(genes):
                t.setCellWidget(r, c, self._cover_chip(bc, gene, row.get(gene)))

    @staticmethod
    def _cover_chip(bc, gene, cell):
        """A single coverage cell rendered as a coloured, rounded chip."""
        chip = QLabel()
        chip.setAlignment(Qt.AlignCenter)
        if cell is None:
            chip.setText("\u2014")
            chip.setStyleSheet(
                "QLabel { color:%s; margin:3px; }" % theme.MUTED)
            return chip
        depth, status = cell
        key = COVERAGE_COLORS.get((status or "").upper(), "no")
        color = PALETTE.get(key, theme.MUTED)
        # Amber (LOW) is a light swatch, so use dark text; the green (OK) and
        # grey (NO) swatches are dark enough for white text.
        text_col = "#1a1a1a" if key == "low" else "#ffffff"
        chip.setText("%.0f" % depth)
        chip.setStyleSheet(
            "QLabel { background:%s; color:%s; border-radius:6px; margin:3px;"
            " padding:6px 4px; font-weight:600; }" % (color, text_col))
        chip.setToolTip("%s \u00b7 %s \u00b7 depth %.1f \u00b7 %s"
                        % (bc, gene, depth, status))
        return chip

    def _enter_final(self):
        if self._in_final:
            return
        self._in_final = True
        now = time.time()
        # the last barcode is now fully processed
        if self._current_bc and self._current_bc in self._bc_rows:
            last = self._bc_rows[self._current_bc]
            if last.state != "error":
                last.set_state("done")
            started = self._bc_started.get(self._current_bc)
            if started:
                last.set_duration(self._fmt_dur(now - started))
        self.sample_label.setText("Finalising reports")

    def _recompute_overall(self):
        per = len(PER_BARCODE_STEPS)
        n = max(1, len(self._barcodes))
        total = n * per + len(FINAL_STEPS)
        done = 0
        for name in self._barcodes:
            if self._bc_rows[name].state in ("done", "error"):
                done += per
        if (self._current_bc and not self._in_final
                and self._bc_rows.get(self._current_bc)
                and self._bc_rows[self._current_bc].state == "running"):
            done += sum(1 for s in PER_BARCODE_STEPS
                        if self._steps[s].state in ("done", "error"))
        done += sum(1 for s in FINAL_STEPS
                    if self._final[s].state in ("done", "error"))
        self.overall.setRange(0, total)
        self.overall.setValue(done)
        self.step_count.setText("%d / %d steps" % (done, total))

    def append_log(self, line):
        self.log.appendPlainText(line)

    # -- timers ----------------------------------------------------------
    def _tick_elapsed(self):
        if self._start_time:
            self.elapsed_label.setText(
                "Elapsed " + self._fmt_dur(time.time() - self._start_time))

    def _tick_resources(self):
        if psutil is None:
            return
        try:
            self.cpu_gauge.set_value(psutil.cpu_percent(interval=None))
            vm = psutil.virtual_memory()
            self.ram_gauge.set_value(vm.percent)
            target = self._output_dir or os.path.expanduser("~")
            path = target if os.path.isdir(target) else os.path.expanduser("~")
            du = psutil.disk_usage(path)
            self.disk_gauge.set_value(du.percent)
        except Exception:
            pass

    # -- buttons ---------------------------------------------------------
    def _open_logs(self):
        if self._log_path and os.path.isfile(self._log_path):
            _open_path(self._log_path)

    def _open_output(self):
        if self._output_dir and os.path.isdir(self._output_dir):
            _open_path(self._output_dir)

    @staticmethod
    def _fmt_dur(seconds):
        seconds = int(seconds)
        m, s = divmod(seconds, 60)
        h, m = divmod(m, 60)
        if h:
            return "%d:%02d:%02d" % (h, m, s)
        return "%02d:%02d" % (m, s)


def _open_path(path):
    if sys.platform == "darwin":
        subprocess.Popen(["open", path])
    elif sys.platform.startswith("linux"):
        subprocess.Popen(["xdg-open", path])
