"""Run the bash pipeline via QProcess and parse its progress from stdout.

``PipelineRunner`` sets the bundled-env ``PATH``/``PYTHONPATH`` (so the bash
script finds Clair3, minimap2, samtools, etc. inside the .app) and starts
``bin/pf-drug-resistance-pipeline.sh`` with a per-run config file.

Progress is inferred by regex over the pipeline's own banners:
    print_header  -> "==== <msg> ===="   (two banner lines wrap the message)
    print_section -> "=== <msg> ==="
    log levels    -> "[INFO]/[SUCCESS]/[WARNING]/[ERROR] <msg>"

Each ``print_section`` message is mapped to one of the canonical steps below.
"""

import os
import re

from PyQt5.QtCore import QObject, QProcess, QProcessEnvironment, pyqtSignal

from . import paths

# The pipeline loops these steps once per barcode folder...
PER_BARCODE_STEPS = [
    "QC (raw)",
    "Trim",
    "QC (trimmed)",
    "Align",
    "Coverage",
    "Variant calling",
    "Filter",
]

# ...then runs these once, after every barcode is processed.
FINAL_STEPS = [
    "Combine",
    "Report",
]

# Canonical, user-facing step order (per-barcode loop + final phase).
CANONICAL_STEPS = PER_BARCODE_STEPS + FINAL_STEPS

# Section-banner keyword (lowercased substring) -> canonical step.
# Order matters: first match wins.
_SECTION_MAP = [
    ("initial qc",           "QC (raw)"),
    ("trimming adapters",    "Trim"),
    ("post-trimming qc",     "QC (trimmed)"),
    ("alignment",            "Align"),
    ("coverage",             "Coverage"),
    ("variant calling",      "Variant calling"),
    ("with clair3",          "Variant calling"),
    ("variant filtering",    "Filter"),
    ("annotating",           "Filter"),
    ("combined reports",     "Combine"),
    ("pdf report",           "Report"),
]

# Regexes over a single (ANSI-stripped) output line.
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_SECTION = re.compile(r"^===\s+(.*?)\s+===$")
_PROCESSING = re.compile(r"Processing\s+(barcode\w+)", re.IGNORECASE)
_LEVEL = re.compile(r"^\[(INFO|SUCCESS|WARNING|ERROR)\]")


def section_to_step(message):
    msg = message.lower()
    for keyword, step in _SECTION_MAP:
        if keyword in msg:
            return step
    return None


def _strip_ansi(text):
    return _ANSI.sub("", text)


class PipelineRunner(QObject):
    """Wrap a single pipeline run in a QProcess with progress parsing."""

    step_changed = pyqtSignal(str, str)     # (canonical step, state)
    log_line = pyqtSignal(str)              # one cleaned stdout line
    sample_changed = pyqtSignal(str)       # current barcode
    finished = pyqtSignal(int)             # exit code
    error = pyqtSignal(str)                # spawn / runtime error message

    STATE_PENDING = "pending"
    STATE_RUNNING = "running"
    STATE_DONE = "done"
    STATE_ERROR = "error"

    def __init__(self, parent=None):
        super().__init__(parent)
        self._proc = None
        self._buffer = ""
        self._log_fh = None
        self._current_step = None
        self._stopped = False

    # -- lifecycle -------------------------------------------------------
    def start(self, config_path, log_path=None):
        """Launch the pipeline using ``config_path`` as its per-run config."""
        self._stopped = False
        self._buffer = ""
        self._current_step = None
        if log_path:
            os.makedirs(os.path.dirname(log_path), exist_ok=True)
            self._log_fh = open(log_path, "w")

        self._proc = QProcess(self)
        self._proc.setProcessChannelMode(QProcess.MergedChannels)
        self._proc.setProcessEnvironment(self._build_environment(config_path))
        self._proc.readyReadStandardOutput.connect(self._on_output)
        self._proc.finished.connect(self._on_finished)
        self._proc.errorOccurred.connect(self._on_error)

        script = paths.pipeline_script()
        self._proc.setWorkingDirectory(paths.app_root())
        # Pass the config as the first CLI arg (script also honors PIPELINE_CONFIG).
        self._proc.start("bash", [script, config_path])

    def stop(self):
        """Terminate the running pipeline."""
        self._stopped = True
        if self._proc and self._proc.state() != QProcess.NotRunning:
            self._proc.terminate()
            if not self._proc.waitForFinished(3000):
                self._proc.kill()

    def is_running(self):
        return (self._proc is not None
                and self._proc.state() != QProcess.NotRunning)

    # -- environment -----------------------------------------------------
    def _build_environment(self, config_path):
        env = QProcessEnvironment.systemEnvironment()
        env.insert("PIPELINE_CONFIG", config_path)

        env_bin = paths.bundled_env_bin()
        if env_bin:
            existing = env.value("PATH")
            env.insert("PATH", env_bin + os.pathsep + existing)
            # Help the bundled python find the bundled site-packages and src/.
            env.insert("PYTHONPATH",
                       paths.app_root() + os.pathsep + env.value("PYTHONPATH"))
            root = paths.bundled_env_root()
            if root:
                env.insert("CONDA_PREFIX", root)
        else:
            # Source mode: still make src/ importable for report generation.
            existing_pp = env.value("PYTHONPATH")
            env.insert("PYTHONPATH",
                       paths.app_root() + (os.pathsep + existing_pp
                                           if existing_pp else ""))
        return env

    # -- output parsing --------------------------------------------------
    def _on_output(self):
        data = bytes(self._proc.readAllStandardOutput()).decode(
            "utf-8", errors="replace")
        self._buffer += data
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            self._handle_line(line)

    def _handle_line(self, raw):
        clean = _strip_ansi(raw).rstrip()
        if self._log_fh:
            self._log_fh.write(clean + "\n")
        if not clean:
            return
        self.log_line.emit(clean)

        # Current sample (from "Processing barcodeXX" header). A new barcode
        # restarts the per-barcode loop, so forget the previous step: otherwise
        # _advance_to would mark the new barcode's first step's predecessor done.
        m = _PROCESSING.search(clean)
        if m:
            self._current_step = None
            self.sample_changed.emit(m.group(1))

        # Section banner -> canonical step transition.
        sec = _SECTION.match(clean)
        if sec:
            step = section_to_step(sec.group(1))
            if step and step != self._current_step:
                self._advance_to(step)
            return

        # Error level marks the current step failed (non-fatal warnings ignored).
        lvl = _LEVEL.match(clean)
        if lvl and lvl.group(1) == "ERROR" and self._current_step:
            self.step_changed.emit(self._current_step, self.STATE_ERROR)

    def _advance_to(self, step):
        # Mark previously-running step done, new one running.
        if self._current_step and self._current_step in CANONICAL_STEPS:
            self.step_changed.emit(self._current_step, self.STATE_DONE)
        self._current_step = step
        self.step_changed.emit(step, self.STATE_RUNNING)

    # -- termination -----------------------------------------------------
    def _on_finished(self, exit_code, _exit_status):
        # flush any trailing partial line
        if self._buffer.strip():
            self._handle_line(self._buffer)
            self._buffer = ""
        if self._log_fh:
            self._log_fh.close()
            self._log_fh = None
        if self._current_step:
            state = self.STATE_DONE if exit_code == 0 else self.STATE_ERROR
            self.step_changed.emit(self._current_step, state)
        code = exit_code if not self._stopped else -1
        self.finished.emit(code)

    def _on_error(self, _err):
        if self._proc:
            self.error.emit(self._proc.errorString())
