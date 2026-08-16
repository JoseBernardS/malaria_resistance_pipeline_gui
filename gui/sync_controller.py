"""Background auto-sync of completed local runs into the cloud surface.

``SyncController`` is the Qt driver around :func:`gui.sync.sync_job`. When the
user signs in it sweeps every locally-executed ``completed`` job that hasn't
been finalized on the server yet and pushes each one on a ``QThread`` worker so
the UI never blocks. Sync is idempotent and resumable (keyed on the local job
id as ``client_run_id``), so a partial or failed pass is safe to repeat on the
next sign-in.

This runs independently of :class:`gui.cloud_queue.CloudJobController`: that
controller *executes* remote jobs, whereas this one *uploads results of local
runs*. They share the session but never touch the same rows.
"""

from PyQt5.QtCore import QObject, QThread, pyqtSignal

from . import db, sync


class _SyncWorker(QObject):
    """Sync a batch of completed local jobs, one after another, off the UI thread."""

    job_status = pyqtSignal(str, str)   # (job_id, sync_status)
    finished = pyqtSignal(int, int)     # (synced_ok, failed)

    def __init__(self, job_ids, session):
        super().__init__()
        self._job_ids = list(job_ids)
        self._session = session
        self._cancel = False

    def cancel(self):
        """Ask the sweep to stop after the current job (checked between jobs)."""
        self._cancel = True

    def run(self):
        ok = 0
        failed = 0
        for job_id in self._job_ids:
            # Stop between jobs on shutdown, or if the session dropped mid-sweep
            # (sign-out): the presigned URLs need the token, and there's no point
            # uploading for a signed-out user.
            if self._cancel:
                break
            if self._session is None or not self._session.is_authenticated():
                break
            try:
                sync.sync_job(
                    self._session, job_id,
                    on_status=lambda status, jid=job_id:
                        self.job_status.emit(jid, status))
                ok += 1
            except Exception:
                # sync_job already recorded FAILED + last_error on the row; keep
                # going so one bad run doesn't stall the rest of the batch.
                failed += 1
        self.finished.emit(ok, failed)


class SyncController(QObject):
    """Drive auto-sync of local runs on sign-in, exposing UI status signals."""

    started = pyqtSignal(int)           # (job_count)
    job_status = pyqtSignal(str, str)   # (job_id, sync_status)
    finished = pyqtSignal(int, int)     # (synced_ok, failed)

    def __init__(self, session, parent=None):
        super().__init__(parent)
        self._session = session
        self._thread = None
        self._worker = None

    def is_busy(self):
        return self._thread is not None

    def shutdown(self):
        """Stop the sweep after the in-flight job and join the worker thread.

        For clean app exit: signals the worker to stop between jobs, then quits
        and waits the thread so Qt doesn't tear down a running ``QThread``.
        """
        if self._worker is not None:
            self._worker.cancel()
        self._teardown_thread()

    def resume(self):
        """Sweep and sync pending local runs, if signed in and not already busy.

        Safe to call on every ``session.changed`` and at launch: it no-ops when
        signed out, when a sweep is already running, or when nothing is pending.
        """
        if self._thread is not None:
            return
        if self._session is None or not self._session.is_authenticated():
            return
        try:
            jobs = db.list_syncable_jobs()
        except Exception:
            return
        if not jobs:
            return
        self._start(jobs)

    def _start(self, jobs):
        job_ids = [j["id"] for j in jobs]
        self._thread = QThread(self)
        self._worker = _SyncWorker(job_ids, self._session)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.job_status.connect(self.job_status)
        self._worker.finished.connect(self._on_finished)
        self._thread.start()
        self.started.emit(len(job_ids))

    def _on_finished(self, ok, failed):
        self._teardown_thread()
        self.finished.emit(ok, failed)

    def _teardown_thread(self):
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait()
        self._thread = None
        self._worker = None
