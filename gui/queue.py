"""Sequential job queue backed by SQLite.

``JobQueue`` owns a single ``PipelineRunner`` and runs queued jobs one at a
time. When a job finishes it persists the outcome and automatically starts
the next ``queued`` job. The queue is rebuilt from the DB on launch, so the
job list and history survive restarts.
"""

import csv
import json
import os
import sys
import time

from PyQt5.QtCore import QObject, pyqtSignal

from . import config_bridge, db, paths
from .runner import PipelineRunner

# Make src/ importable so we can reuse the report's tier classifier.
sys.path.insert(0, paths.src_dir())

# Reuse the report module's tier classifier so the persisted "worst_tier"
# matches the dashboard/PDF exactly. Fall back to a tiny local classifier if
# the report module is not importable (keeps outcome capture best-effort).
try:
    from generate_report import classify_tier as _classify_tier
except Exception:  # pragma: no cover
    _classify_tier = None

# Highest concern first; a smaller index is "worse".
_TIER_ORDER = ["validated", "candidate", "potential"]


def _classify(classification):
    if _classify_tier is not None:
        return _classify_tier(classification)
    c = (classification or "").lower()
    if "validated" in c:
        return "validated"
    if "candidate" in c:
        return "candidate"
    return "potential"


def _compute_outcomes(output_dir):
    """Parse a finished run into ``{sample: (worst_tier, n_calls, assessed)}``.

    Pure stdlib-csv read of ``final_reports/resistance_calls.csv`` and
    ``coverage_report.csv``. ``assessed`` is True when the sample has at
    least one gene covered (OK/LOW_COVERAGE). Best-effort: returns whatever
    it can parse and never raises for missing files.
    """
    reports = os.path.join(output_dir, "final_reports")
    if not os.path.isdir(reports):
        reports = output_dir
    calls_path = os.path.join(reports, "resistance_calls.csv")
    cov_path = os.path.join(reports, "coverage_report.csv")

    outcomes = {}

    def _read(path):
        if not os.path.isfile(path):
            return []
        with open(path, newline="") as fh:
            return list(csv.DictReader(fh))

    for row in _read(calls_path):
        s = row.get("Sample")
        if not s:
            continue
        worst, n = outcomes.get(s, (None, 0))
        tier = _classify(row.get("Classification", ""))
        if worst is None or _TIER_ORDER.index(tier) < _TIER_ORDER.index(worst):
            worst = tier
        outcomes[s] = (worst, n + 1)

    assessed = set()
    for row in _read(cov_path):
        s = row.get("Sample")
        if not s:
            continue
        outcomes.setdefault(s, (None, 0))
        if (row.get("Status", "") or "").upper() in ("OK", "LOW_COVERAGE"):
            assessed.add(s)

    return {s: (worst, n, s in assessed)
            for s, (worst, n) in outcomes.items()}


def backfill_outcomes():
    """One-off: persist outcomes for completed jobs whose dirs still exist.

    Safe to call repeatedly (INSERT OR REPLACE). Returns the number of jobs
    whose outcomes were (re)written.
    """
    done = 0
    for job in db.list_jobs():
        if job.get("status") != "completed":
            continue
        out = job.get("output_dir")
        if not out or not os.path.isdir(out):
            continue
        try:
            outcomes = _compute_outcomes(out)
            for sample, (tier, n_calls, assessed) in outcomes.items():
                db.upsert_sample_outcome(
                    job["id"], sample, tier, n_calls, assessed)
            if outcomes:
                done += 1
        except Exception:
            continue
    return done


class JobQueue(QObject):
    """Persisted, run-one-at-a-time job queue."""

    # Re-emitted runner signals, tagged with the active job id where useful.
    job_started = pyqtSignal(str)          # job id
    job_finished = pyqtSignal(str, int)    # job id, exit code
    queue_changed = pyqtSignal()           # list of jobs changed
    step_changed = pyqtSignal(str, str)    # (step, state) for active job
    log_line = pyqtSignal(str)
    sample_changed = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.runner = PipelineRunner(self)
        self.runner.step_changed.connect(self.step_changed)
        self.runner.log_line.connect(self.log_line)
        self.runner.sample_changed.connect(self.sample_changed)
        self.runner.finished.connect(self._on_job_finished)
        self.runner.error.connect(self._on_runner_error)
        self._active_job_id = None
        # Peer queue (the cloud controller). Only one job may run across both
        # queues at a time, so we won't start while the peer is busy, and we
        # poke the peer to pick up its waiting jobs once we go idle.
        self._peer = None

    # -- public API ------------------------------------------------------
    @property
    def active_job_id(self):
        return self._active_job_id

    def set_peer(self, peer):
        """Register the other queue for cross-queue mutual exclusion."""
        self._peer = peer

    def add_job(self, config_id):
        """Create a queued job for a saved config and (maybe) start it.

        Each job gets its **own** output directory — a ``job_<id>`` subfolder
        under the config's chosen output root — so successive runs never
        overwrite one another's ``final_reports``. Reopening a job then shows
        that specific run's results.
        """
        cfg = db.get_config(config_id)
        if not cfg:
            raise ValueError("no such config: %s" % config_id)
        # Enqueue first to mint the job id, then point the job at its own
        # per-run subfolder (and matching log).
        job_id = db.enqueue_job(config_id, cfg["output_dir"])
        run_dir = os.path.join(cfg["output_dir"], "job_%s" % job_id)
        log_path = "%s/job_%s.log" % (paths.logs_dir(), job_id)
        db.update_job(job_id, output_dir=run_dir, log_path=log_path)
        self.queue_changed.emit()
        self._maybe_start_next()
        return job_id

    def is_busy(self):
        return self._active_job_id is not None

    def stop_active(self):
        if self._active_job_id is not None:
            self.runner.stop()

    def resume(self):
        """Called on launch: pick up any leftover queued jobs."""
        self._maybe_start_next()

    # -- internals -------------------------------------------------------
    def _maybe_start_next(self):
        if self._active_job_id is not None:
            return
        # Cross-queue lock: never run a local job while a cloud job is active.
        # The job stays queued and auto-starts when the peer goes idle (the
        # peer pokes us via resume() from its finish handler).
        if self._peer is not None and self._peer.is_busy():
            return
        job = db.next_queued_job()
        if not job:
            return
        self._start_job(job)

    def _start_job(self, job):
        cfg = db.get_config(job["config_id"])
        # QC tool / pre-trim QC / report mode were stashed as conf-keyed extras
        # on the saved config; pass them straight through to the per-run .conf.
        try:
            extra = json.loads(cfg.get("extra_json") or "{}") or None
        except (ValueError, TypeError):
            extra = None
        # Use the job's own per-run output dir (set in add_job), not the
        # config's shared root, so each run writes to its own folder.
        config_path = config_bridge.write_run_config(
            fastq_dir=cfg["fastq_dir"],
            output_dir=job["output_dir"],
            reference_set=cfg["reference_set"],
            threads=cfg["threads"],
            min_qual=cfg["min_qual"],
            min_dp=cfg["min_dp"],
            min_mq=cfg["min_mq"],
            extra=extra,
            job_id=job["id"],
            clair3_model=cfg.get("clair3_model"))

        self._active_job_id = job["id"]
        db.update_job(job["id"], status="running", started_at=time.time())
        self.queue_changed.emit()
        self.job_started.emit(job["id"])
        self.runner.start(config_path, log_path=job.get("log_path"))

    def _on_job_finished(self, exit_code):
        job_id = self._active_job_id
        self._active_job_id = None
        if job_id is None:
            return
        if exit_code == 0:
            status = "completed"
        elif exit_code < 0:
            status = "stopped"
        else:
            status = "failed"
        db.update_job(job_id, status=status, exit_code=exit_code,
                      finished_at=time.time())
        # Persist a compact per-sample outcome so the surveillance map has a
        # durable source that survives deleting the output dir. Fully guarded:
        # this must never fail the job or block queue chaining.
        if exit_code == 0:
            try:
                job = db.get_job(job_id)
                outcomes = _compute_outcomes(job["output_dir"]) if job else {}
                for sample, (tier, n_calls, assessed) in outcomes.items():
                    db.upsert_sample_outcome(
                        job_id, sample, tier, n_calls, assessed)
            except Exception as e:
                self.log_line.emit("[WARN] outcome capture: %s" % e)
        self.queue_changed.emit()
        self.job_finished.emit(job_id, exit_code)
        # Chain to the next queued job; if we stay idle, let the peer (cloud)
        # start any job it deferred while we were running.
        self._maybe_start_next()
        if self._active_job_id is None and self._peer is not None:
            self._peer.resume()

    def _on_runner_error(self, message):
        job_id = self._active_job_id
        if job_id is not None:
            db.update_job(job_id, status="failed", finished_at=time.time())
            self._active_job_id = None
            self.queue_changed.emit()
            self.job_finished.emit(job_id, 1)
        self.log_line.emit("[ERROR] runner: %s" % message)
        self._maybe_start_next()
        if self._active_job_id is None and self._peer is not None:
            self._peer.resume()
