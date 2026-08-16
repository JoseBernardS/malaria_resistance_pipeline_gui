"""Cloud job controller: mirror the local queue against the remote service.

``CloudJobController`` is the cloud analogue of :class:`gui.queue.JobQueue`. It
exposes the *same* Qt signals so ``app.py`` can wire cloud jobs into the exact
Progress/Results flow used for local runs, but instead of spawning the bash
pipeline it drives the pipeline API: tar the input, upload it (resumably),
submit a run, poll it to completion, then download the result CSVs into the
job's own ``output_dir/final_reports`` so the dashboard opens it unchanged.

Network work runs on a ``QThread`` worker so the UI never blocks. Jobs are
processed one at a time to match the single-active-job Progress screen. The
remote ``run_id`` and ``input_s3_key`` are persisted on the ``job`` row, so an
in-flight run is reconciled/resumed after a desktop restart rather than lost.
"""

import json
import os
import tarfile
import time

from PyQt5.QtCore import QObject, QThread, pyqtSignal

from . import cloud_client, config_bridge, db, paths, ref_catalog
from .cloud_client import CloudApiError, CloudClient, MultipartUploader
from .queue import _compute_outcomes

# Remote statuses that mean the run is finished (no more polling).
_TERMINAL = {"SUCCEEDED", "PARTIAL", "FAILED", "CANCELED"}

# Seconds between status polls.
_POLL_INTERVAL = 5

# Coarse cloud phases surfaced to the Progress step widget. These are distinct
# from the local per-barcode steps: remote execution only exposes run-level
# status, so the desktop shows the handoff phases it *can* observe.
STEP_PACKAGE = "Package"
STEP_UPLOAD = "Upload"
STEP_SUBMIT = "Submit"
STEP_REMOTE = "Remote run"
STEP_DOWNLOAD = "Download"
CLOUD_STEPS = [STEP_PACKAGE, STEP_UPLOAD, STEP_SUBMIT, STEP_REMOTE,
               STEP_DOWNLOAD]

# Map the results-endpoint URL fields to the canonical filenames the dashboard
# reads from ``final_reports/``.
_RESULT_FILES = {
    "resistance_calls_url": "resistance_calls.csv",
    "variant_detail_url": "variant_detail.csv",
    "coverage_report_url": "coverage_report.csv",
}


def _safe_extract(tar, dest):
    """Extract ``tar`` into ``dest``, refusing any member that escapes it.

    Guards against path-traversal (``../`` / absolute paths) in a remote
    archive before writing to disk.
    """
    dest = os.path.abspath(dest)
    for member in tar.getmembers():
        target = os.path.abspath(os.path.join(dest, member.name))
        if target != dest and not target.startswith(dest + os.sep):
            raise tarfile.TarError("unsafe path in QC bundle: %s" % member.name)
    tar.extractall(dest)


def _remote_to_local_status(remote_status):
    if remote_status in ("SUCCEEDED", "PARTIAL"):
        return "completed"
    if remote_status == "CANCELED":
        return "stopped"
    return "failed"


class _CloudWorker(QObject):
    """Runs one cloud job to completion on a worker thread."""

    step_changed = pyqtSignal(str, str)     # (phase, state)
    progress = pyqtSignal(int, int, str)    # (completed_units, total_units, stage)
    log_line = pyqtSignal(str)
    finished = pyqtSignal(str, int)         # (job_id, exit_code)
    error = pyqtSignal(str)                 # fatal message

    def __init__(self, job, session):
        super().__init__()
        self._job = job
        self._session = session
        self._client = CloudClient(session)
        self._cancel = False

    def cancel(self):
        self._cancel = True

    # -- entry -----------------------------------------------------------
    def run(self):
        job_id = self._job["id"]
        try:
            exit_code = self._run_inner()
            self.finished.emit(job_id, exit_code)
        except InterruptedError:
            self.log_line.emit("[INFO] Cloud job cancelled")
            self.finished.emit(job_id, -1)
        except PermissionError:
            self.error.emit("not signed in")
        except CloudApiError as e:
            code = (" [%s]" % e.error_code) if e.error_code else ""
            self.error.emit("%s%s" % (e.detail, code))
        except Exception as e:  # pragma: no cover - defensive
            self.error.emit(str(e))

    # -- phases ----------------------------------------------------------
    def _run_inner(self):
        job = self._job
        cfg = db.get_config(job["config_id"])
        if not cfg:
            raise CloudApiError("no config for job")

        # 1) Package the fastq_pass tree into a single tar.gz.
        self.step_changed.emit(STEP_PACKAGE, "running")
        tar_path = self._package(cfg["fastq_dir"], job["id"])
        self.step_changed.emit(STEP_PACKAGE, "done")
        self._check_cancel()

        # 2) Resumable multipart upload of the tar.
        self.step_changed.emit(STEP_UPLOAD, "running")
        input_s3_key = self._upload(tar_path, job["id"])
        db.update_job(job["id"], input_s3_key=input_s3_key)
        self.step_changed.emit(STEP_UPLOAD, "done")
        self._check_cancel()

        # 3) Resolve model + submit (idempotent: reconcile a prior submit for
        #    this exact key before creating a new run).
        self.step_changed.emit(STEP_SUBMIT, "running")
        run = self._submit(cfg, input_s3_key, job)
        run_id = run["id"]
        db.update_job(job["id"], remote_run_id=run_id, status="running",
                      started_at=time.time())
        self.log_line.emit("[INFO] Submitted cloud run %s" % run_id)
        self.step_changed.emit(STEP_SUBMIT, "done")

        # 4) Poll to terminal status.
        self.step_changed.emit(STEP_REMOTE, "running")
        final = self._poll(run_id)
        status = final.get("status")
        if status in ("FAILED", "CANCELED"):
            self.step_changed.emit(STEP_REMOTE, "error")
            msg = final.get("error_message") or status
            self.log_line.emit("[ERROR] Cloud run %s: %s" % (status, msg))
            return 1 if status == "FAILED" else -1
        self.step_changed.emit(STEP_REMOTE, "done")

        # 5) Download result CSVs into the job's final_reports.
        self.step_changed.emit(STEP_DOWNLOAD, "running")
        self._download_results(run_id, job["output_dir"], job.get("log_path"))
        self.step_changed.emit(STEP_DOWNLOAD, "done")
        return 0

    # -- helpers ---------------------------------------------------------
    def _check_cancel(self):
        if self._cancel:
            raise InterruptedError()

    def _package(self, fastq_dir, job_id):
        """Tar ``fastq_dir`` as ``fastq_pass/`` into the uploads staging dir."""
        self.log_line.emit("[INFO] Packaging input from %s" % fastq_dir)
        tar_path = os.path.join(paths.uploads_dir(), "input_%s.tar.gz" % job_id)
        with tarfile.open(tar_path, "w:gz") as tar:
            tar.add(fastq_dir, arcname="fastq_pass")
        size = os.path.getsize(tar_path)
        self.log_line.emit("[INFO] Packaged %d MiB" % (size // (1024 * 1024)))
        return tar_path

    def _upload(self, tar_path, job_id):
        def on_progress(done, total):
            pct = (100 * done // total) if total else 0
            self.log_line.emit("[INFO] Upload %d%% (%d/%d MiB)"
                               % (pct, done // (1024 * 1024),
                                  total // (1024 * 1024)))

        uploader = MultipartUploader(
            self._client, tar_path, state_key=job_id,
            on_progress=on_progress, should_cancel=lambda: self._cancel,
            log=lambda m: self.log_line.emit("[INFO] %s" % m))
        key = uploader.upload()
        # The staged tar is no longer needed once the object exists remotely.
        try:
            os.remove(tar_path)
        except OSError:
            pass
        return key

    def _submit(self, cfg, input_s3_key, job):
        # Reconcile: if a prior attempt already created a run for this key
        # (e.g. a lost 201), reuse it instead of double-submitting.
        try:
            existing = self._client.find_runs(input_s3_key=input_s3_key)
        except CloudApiError:
            existing = []
        if existing:
            self.log_line.emit(
                "[INFO] Reusing existing cloud run for this input")
            return existing[0]

        model_id = self._client.resolve_model_id(cfg.get("clair3_model"))
        if not model_id:
            raise CloudApiError(
                "cloud model %r is not in the registry"
                % cfg.get("clair3_model"))
        params = self._params(cfg)
        prov = self._provenance_block(job)
        # Reference-set provenance, mirroring the local-sync payload: always send
        # the set name (native constant for bundled defaults), and the bundle id
        # only when known (else the server backfills it by name).
        reference_version = config_bridge.resolve_reference_version(
            cfg.get("reference_set"))
        reference_set_id = ref_catalog.id_for(reference_version)
        return self._client.submit_run(
            model_id, input_s3_key, params,
            reference_version=reference_version,
            reference_set_id=reference_set_id, **prov)

    def _provenance_block(self, job):
        """Optional fingerprint/provenance/samples kwargs for ``submit_run``.

        Reads the provenance already extracted at enqueue (same rows the local
        sync path uses) and shapes them exactly like
        :func:`gui.sync.build_sync_payload` so a cloud run and a synced local
        run of the same data produce identical dedupe keys. Best-effort: any
        read/shape failure yields an empty block, so a missing provenance row
        never blocks the submit.
        """
        try:
            run_prov = db.get_run_provenance(job["id"]) or {}
            sample_prov = db.list_sample_provenance(job["id"]) or {}
        except Exception:
            return {}
        provenance = {
            "source": run_prov.get("provenance_source", "FINGERPRINT_ONLY"),
            "sequencing_run_id": run_prov.get("sequencing_run_id"),
            "flow_cell_id": run_prov.get("flow_cell_id"),
            "protocol_group_id": run_prov.get("protocol_group_id"),
            "run_start_time": run_prov.get("run_start_time"),
            "basecall_model": run_prov.get("basecall_model"),
        }
        samples = [
            {"barcode": bc,
             "barcode_alias": rec.get("barcode_alias"),
             "sample_id": rec.get("sample_id"),
             "homogeneous": bool(rec.get("homogeneous", 1))}
            for bc, rec in sorted(sample_prov.items())]
        return {
            "input_fingerprint": job.get("input_fingerprint"),
            "provenance": provenance,
            "samples": samples,
            "sample_count": len(samples) or None,
        }

    def _params(self, cfg):
        """Map the saved config to ``PipelineRunParams`` (server fills defaults)."""
        try:
            extra = json.loads(cfg.get("extra_json") or "{}") or {}
        except (ValueError, TypeError):
            extra = {}
        # qc_tool is pinned to nanostat for cloud unless explicitly overridden;
        # the desktop QC view reads NanoStat output.
        qc_tool = (extra.get("QC_TOOL") or "nanostat").lower()
        if qc_tool not in ("nanostat", "nanoplot"):
            qc_tool = "nanostat"
        params = {
            "min_qual": int(cfg["min_qual"]),
            "min_dp": int(cfg["min_dp"]),
            "min_mq": int(cfg["min_mq"]),
            "qc_tool": qc_tool,
        }
        if cfg.get("threads"):
            params["threads"] = int(cfg["threads"])
        return params

    def _poll(self, run_id):
        last = None
        while True:
            self._check_cancel_remote(run_id)
            run = self._client.get_run(run_id)
            status = run.get("status")
            if status != last:
                self.log_line.emit("[INFO] Cloud run status: %s" % status)
                last = status
            prog = run.get("progress")
            if prog:
                self.progress.emit(int(prog.get("completed_units") or 0),
                                   int(prog.get("total_units") or 0),
                                   prog.get("stage") or "")
            if status in _TERMINAL:
                return run
            # Sleep in short slices so cancellation stays responsive.
            for _ in range(_POLL_INTERVAL * 2):
                if self._cancel:
                    self._check_cancel_remote(run_id)
                time.sleep(0.5)

    def _check_cancel_remote(self, run_id):
        """If the user asked to stop, cancel the remote run then bail out."""
        if not self._cancel:
            return
        try:
            self._client.cancel_run(run_id)
        except CloudApiError:
            pass
        raise InterruptedError()

    def _download_results(self, run_id, output_dir, log_path):
        results = self._client.run_results(run_id) or {}
        reports = os.path.join(output_dir, "final_reports")
        os.makedirs(reports, exist_ok=True)
        got = 0
        for field, filename in _RESULT_FILES.items():
            url = results.get(field)
            if not url:
                continue
            cloud_client.download(url, os.path.join(reports, filename))
            got += 1
        # QC bundle: qc_report_url is a .tar.gz of the qc_trimmed/ (+qc_raw/)
        # tree. Extract it into output_dir so the dashboard finds the per-sample
        # NanoStat files at output_dir/qc_trimmed/<sample>/ exactly like a local
        # run (saving it as one file is what left the Read-quality view empty).
        if results.get("qc_report_url"):
            try:
                self._download_qc_bundle(results["qc_report_url"], output_dir)
            except (CloudApiError, tarfile.TarError, OSError) as e:
                self.log_line.emit("[WARN] QC bundle: %s" % e)
        if results.get("log_url") and log_path:
            try:
                cloud_client.download(results["log_url"], log_path)
            except CloudApiError:
                pass
        self.log_line.emit("[INFO] Downloaded %d result file(s)" % got)

    def _download_qc_bundle(self, url, output_dir):
        """Fetch the QC ``.tar.gz`` and extract it under ``output_dir``."""
        tar_path = os.path.join(output_dir, "qc.tar.gz")
        cloud_client.download(url, tar_path)
        with tarfile.open(tar_path, "r:gz") as tar:
            _safe_extract(tar, output_dir)
        os.remove(tar_path)
        self.log_line.emit("[INFO] Extracted QC bundle")


class CloudJobController(QObject):
    """Sequential cloud queue exposing the ``JobQueue`` signal contract."""

    job_started = pyqtSignal(str)
    job_finished = pyqtSignal(str, int)
    queue_changed = pyqtSignal()
    step_changed = pyqtSignal(str, str)
    progress = pyqtSignal(int, int, str)    # (completed_units, total_units, stage)
    log_line = pyqtSignal(str)

    def __init__(self, session, parent=None):
        super().__init__(parent)
        self._session = session
        self._active_job_id = None
        self._thread = None
        self._worker = None
        # Peer queue (the local JobQueue). Only one job may run across both
        # queues at a time; see set_peer / _maybe_start_next.
        self._peer = None

    # -- public API (mirrors JobQueue) -----------------------------------
    @property
    def active_job_id(self):
        return self._active_job_id

    def set_peer(self, peer):
        """Register the other queue for cross-queue mutual exclusion."""
        self._peer = peer

    def is_busy(self):
        return self._active_job_id is not None

    def add_job(self, config_id):
        """Create a queued cloud job (own output dir + log) and maybe start it."""
        cfg = db.get_config(config_id)
        if not cfg:
            raise ValueError("no such config: %s" % config_id)
        job_id = db.enqueue_job(config_id, cfg["output_dir"])
        run_dir = os.path.join(cfg["output_dir"], "job_%s" % job_id)
        log_path = "%s/job_%s.log" % (paths.logs_dir(), job_id)
        db.update_job(job_id, output_dir=run_dir, log_path=log_path)
        self.queue_changed.emit()
        self._maybe_start_next()
        return job_id

    def stop_active(self):
        if self._worker is not None:
            self.log_line.emit("[INFO] Requesting cloud run cancellation\u2026")
            self._worker.cancel()

    def resume(self):
        """On launch: re-attach in-flight runs, then pick up queued cloud jobs.

        A job that already has a ``remote_run_id`` is still executing on the
        server, so we resume it (the worker's idempotent submit reconciles via
        ``input_s3_key`` and resumes polling); otherwise we start the oldest
        queued cloud job fresh.
        """
        self._maybe_start_next()

    # -- internals -------------------------------------------------------
    def _maybe_start_next(self):
        if self._active_job_id is not None:
            return
        # Cross-queue lock: never run a cloud job while a local job is active.
        # The job stays queued and auto-starts when the peer goes idle (the
        # peer pokes us via resume() from its finish handler).
        if self._peer is not None and self._peer.is_busy():
            return
        if self._session is None or not self._session.is_authenticated():
            # Can't run cloud jobs while signed out; they stay queued and are
            # picked up after the next successful sign-in.
            return
        job = db.list_resumable_cloud_jobs()
        job = job[0] if job else db.next_queued_cloud_job()
        if not job:
            return
        self._start_job(job)

    def _start_job(self, job):
        self._active_job_id = job["id"]
        self.queue_changed.emit()
        self.job_started.emit(job["id"])

        self._thread = QThread(self)
        self._worker = _CloudWorker(job, self._session)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.log_line.connect(self.log_line)
        self._worker.step_changed.connect(self.step_changed)
        self._worker.progress.connect(self.progress)
        self._worker.finished.connect(self._on_worker_finished)
        self._worker.error.connect(self._on_worker_error)
        self._thread.start()

    def _teardown_thread(self):
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait()
        self._thread = None
        self._worker = None

    def _on_worker_finished(self, job_id, exit_code):
        status = _remote_to_local_status(
            "SUCCEEDED" if exit_code == 0
            else ("CANCELED" if exit_code < 0 else "FAILED"))
        db.update_job(job_id, status=status, exit_code=exit_code,
                      finished_at=time.time())
        if exit_code == 0:
            try:
                job = db.get_job(job_id)
                outcomes = _compute_outcomes(job["output_dir"]) if job else {}
                for sample, (tier, n_calls, assessed) in outcomes.items():
                    db.upsert_sample_outcome(
                        job_id, sample, tier, n_calls, assessed)
            except Exception as e:
                self.log_line.emit("[WARN] outcome capture: %s" % e)
        self._teardown_thread()
        self._active_job_id = None
        self.queue_changed.emit()
        self.job_finished.emit(job_id, exit_code)
        self._maybe_start_next()
        if self._active_job_id is None and self._peer is not None:
            self._peer.resume()

    def _on_worker_error(self, message):
        job_id = self._active_job_id
        if job_id is not None:
            db.update_job(job_id, status="failed", finished_at=time.time())
        self.log_line.emit("[ERROR] cloud runner: %s" % message)
        self._teardown_thread()
        self._active_job_id = None
        self.queue_changed.emit()
        if job_id is not None:
            self.job_finished.emit(job_id, 1)
        self._maybe_start_next()
        if self._active_job_id is None and self._peer is not None:
            self._peer.resume()
