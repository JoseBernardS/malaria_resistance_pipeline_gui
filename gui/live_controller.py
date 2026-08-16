"""Folder-watch controller for the "Live run" mode.

While a sequencing run is still in progress, this controller polls the
MinKNOW output folder (the config's FASTQ dir) and, whenever a barcode's
files change, runs a cheap align + coverage pass over just that barcode via
the bash pipeline's ``LIVE_SCAN`` fast path. Parsed per-gene coverage is
emitted so the Progress screen can paint a live depth grid, letting the
operator see when each amplicon crosses the depth bar (``MIN_DP``) and stop
the flow cell early.

The expensive Clair3 variant calling + PDF report are deliberately deferred
to a single **finalize** pass: :meth:`finalize` stops the watch loop and
hands the same saved config to the normal :class:`gui.queue.JobQueue`, which
runs the full pipeline (LIVE_SCAN off) and lands on the Results dashboard.

Design notes:
- Polling (``QTimer``), not ``QFileSystemWatcher`` — MinKNOW commonly writes
  to a network share where inotify-style watches are unreliable.
- A separate ``job_<id>_live/`` scratch output dir keeps live BAMs/coverage
  out of the finalize job's own directory.
- Only one thing runs at a time: a tick is skipped while a scan is in flight
  or either normal queue (local/cloud) is busy.
"""

import json
import os

from PyQt5.QtCore import QObject, QTimer, pyqtSignal

from . import config_bridge, db, paths
from .runner import PipelineRunner

# How often to poll the watch folder for new/changed FASTQs (ms). ~20 s is a
# good balance over a network share: frequent enough to feel live, infrequent
# enough not to hammer a remote mount while reads trickle in.
POLL_INTERVAL_MS = 20000


class LiveRunController(QObject):
    """Watch a run's FASTQ folder and drive incremental coverage scans."""

    started = pyqtSignal(str)            # config id
    coverage_updated = pyqtSignal(object)   # {barcode: {gene: (depth, status)}}
    cycle_log = pyqtSignal(str)         # one status/log line for the console
    saturated = pyqtSignal(bool)        # every discovered amplicon is OK
    finalize_requested = pyqtSignal(str)    # config id -> hand to normal queue
    stopped = pyqtSignal()

    def __init__(self, queue, cloud, parent=None):
        super().__init__(parent)
        # The two normal controllers, used only for busy checks so a live scan
        # never overlaps a real run (which shares the same Progress console).
        self._queue = queue
        self._cloud = cloud

        self._runner = PipelineRunner(self)
        self._runner.log_line.connect(self.cycle_log)
        self._runner.finished.connect(self._on_scan_finished)
        self._runner.error.connect(self._on_scan_error)

        self._timer = QTimer(self)
        self._timer.setInterval(POLL_INTERVAL_MS)
        self._timer.timeout.connect(self._tick)

        self._config_id = None
        self._cfg = None
        self._watch_dir = None
        self._live_dir = None
        self._extra = None
        # Per-barcode input fingerprint from the last scan, so we only re-align
        # barcodes whose files actually changed.
        self._fingerprints = {}
        self._scanning = False
        self._cycle = 0
        # Barcodes queued for the in-flight scan (LIVE_ONLY_BARCODES), so we
        # only commit their fingerprints once the scan succeeds.
        self._pending = {}

    # -- lifecycle -------------------------------------------------------
    def is_busy(self):
        """True while a live run is active (timer running or scan in flight)."""
        return self._config_id is not None

    def start(self, config_id):
        """Begin watching the config's FASTQ folder for a live run."""
        cfg = db.get_config(config_id)
        if not cfg:
            raise ValueError("no such config: %s" % config_id)
        self._config_id = config_id
        self._cfg = cfg
        self._watch_dir = paths.resolve_barcode_root(cfg["fastq_dir"])
        # Scratch live output dir, kept separate from the finalize job's dir so
        # live estimates never mingle with the authoritative run's artifacts.
        self._live_dir = os.path.join(
            cfg["output_dir"], "job_%s_live" % config_id)
        os.makedirs(self._live_dir, exist_ok=True)
        try:
            self._extra = json.loads(cfg.get("extra_json") or "{}") or {}
        except (ValueError, TypeError):
            self._extra = {}
        self._fingerprints = {}
        self._pending = {}
        self._scanning = False
        self._cycle = 0

        self.started.emit(config_id)
        self.cycle_log.emit(
            "[INFO] Live run watching %s (every %ds)"
            % (self._watch_dir, POLL_INTERVAL_MS // 1000))
        self._timer.start()
        # Run one scan immediately rather than waiting a full interval.
        self._tick()

    def stop(self):
        """Stop the watch loop and any in-flight scan."""
        if self._config_id is None:
            return
        self._timer.stop()
        if self._runner.is_running():
            self._runner.stop()
        self._reset()
        self.stopped.emit()

    def finalize(self):
        """Stop watching and hand off to the normal full pipeline.

        Emits :attr:`finalize_requested` with the config id; the app enqueues a
        normal job (LIVE_SCAN off) which runs Clair3 + report and takes over the
        Progress/Results path.
        """
        if self._config_id is None:
            return
        config_id = self._config_id
        self._timer.stop()
        if self._runner.is_running():
            self._runner.stop()
        self._reset()
        self.finalize_requested.emit(config_id)

    def _reset(self):
        self._config_id = None
        self._cfg = None
        self._watch_dir = None
        self._live_dir = None
        self._extra = None
        self._fingerprints = {}
        self._pending = {}
        self._scanning = False

    # -- polling ---------------------------------------------------------
    def _tick(self):
        """One poll cycle: fingerprint barcodes, scan the changed ones."""
        if self._config_id is None or self._scanning:
            return
        # Never contend with a real run for the shared Progress console / CPU.
        if self._queue.is_busy() or self._cloud.is_busy():
            return

        current = self._fingerprint_all()
        changed = [bc for bc, fp in current.items()
                   if self._fingerprints.get(bc) != fp]
        if not changed:
            return

        self._cycle += 1
        self.cycle_log.emit(
            "[INFO] Live cycle %d: scanning %s"
            % (self._cycle, ", ".join(sorted(changed))))
        self._pending = {bc: current[bc] for bc in changed}
        self._scanning = True
        try:
            conf = config_bridge.write_run_config(
                fastq_dir=self._cfg["fastq_dir"],
                output_dir=self._live_dir,
                reference_set=self._cfg["reference_set"],
                threads=self._cfg["threads"],
                min_qual=self._cfg["min_qual"],
                min_dp=self._cfg["min_dp"],
                min_mq=self._cfg["min_mq"],
                extra=dict(self._extra,
                           LIVE_SCAN=True,
                           LIVE_ONLY_BARCODES=" ".join(sorted(changed))),
                job_id="%s_live" % self._config_id,
                clair3_model=self._cfg.get("clair3_model"))
        except Exception as e:  # pragma: no cover - defensive
            self._scanning = False
            self._pending = {}
            self.cycle_log.emit("[ERROR] Live config write failed: %s" % e)
            return
        self._runner.start(conf)

    def _fingerprint_all(self):
        """``{barcode: fingerprint}`` for every ``barcode*`` dir in the watch
        folder, where fingerprint is a stable string of each FASTQ's
        name+size+mtime (a cheap stat-only signal of "new reads arrived")."""
        out = {}
        if not self._watch_dir or not os.path.isdir(self._watch_dir):
            return out
        for bc in paths.discover_barcodes(self._watch_dir):
            bc_dir = os.path.join(self._watch_dir, bc)
            parts = []
            try:
                for entry in os.scandir(bc_dir):
                    name = entry.name
                    if not (name.endswith(".fastq")
                            or name.endswith(".fastq.gz")):
                        continue
                    try:
                        st = entry.stat()
                    except OSError:
                        continue
                    parts.append("%s|%d|%d" % (name, st.st_size,
                                               int(st.st_mtime)))
            except OSError:
                continue
            out[bc] = "\n".join(sorted(parts))
        return out

    # -- scan completion -------------------------------------------------
    def _on_scan_finished(self, exit_code):
        self._scanning = False
        if exit_code == 0:
            # Commit fingerprints only for barcodes we actually re-scanned.
            self._fingerprints.update(self._pending)
        else:
            self.cycle_log.emit(
                "[WARNING] Live cycle exited %d; will retry next poll"
                % exit_code)
        self._pending = {}

        cover = self._parse_coverage()
        self.coverage_updated.emit(cover)
        self.saturated.emit(self._is_saturated(cover))

    def _on_scan_error(self, message):
        self._scanning = False
        self._pending = {}
        self.cycle_log.emit("[ERROR] Live scan runner: %s" % message)

    def _parse_coverage(self):
        """Read all ``<live_dir>/reports/*_coverage.tsv`` into a nested dict.

        Returns ``{barcode: {gene: (amplicon_depth, status)}}``. Each TSV row is
        ``sample \\t gene \\t whole_mean \\t amplicon_depth \\t STATUS``.
        Best-effort: an unreadable/short row is skipped, never fatal.
        """
        cover = {}
        reports = os.path.join(self._live_dir or "", "reports")
        if not os.path.isdir(reports):
            return cover
        for name in os.listdir(reports):
            if not name.endswith("_coverage.tsv"):
                continue
            path = os.path.join(reports, name)
            try:
                with open(path) as fh:
                    for line in fh:
                        cols = line.rstrip("\n").split("\t")
                        if len(cols) < 5:
                            continue
                        sample, gene, _whole, ampd, status = cols[:5]
                        try:
                            depth = float(ampd)
                        except ValueError:
                            depth = 0.0
                        cover.setdefault(sample, {})[gene] = (depth, status)
            except OSError:
                continue
        return cover

    @staticmethod
    def _is_saturated(cover):
        """True when at least one barcode is present and every amplicon of
        every discovered barcode reads ``OK`` (adequate depth)."""
        if not cover:
            return False
        for genes in cover.values():
            if not genes:
                return False
            for _depth, status in genes.values():
                if status != "OK":
                    return False
        return True
