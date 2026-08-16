"""Background push of edited surveillance metadata to the cloud.

``MetadataSyncController`` is the Qt driver around
:func:`gui.metadata_sync.push_metadata`. It sweeps every job whose
``sample_meta`` has edits newer than what the server has confirmed
(:func:`gui.db.list_metadata_syncable_jobs`) and pushes each on a ``QThread``
worker so the UI never blocks.

It runs independently of :class:`gui.sync_controller.SyncController`: that one
uploads *result artifacts* of local runs once; this one pushes *mutable
metadata* for any run that already has a server id (local-synced or
cloud-executed), and re-runs whenever the user edits a sample. Both are
idempotent, resumable, and safe to call on every ``session.changed`` -- the
push is a no-op when signed out, unconfigured, or nothing has changed.
"""

from PyQt5.QtCore import QObject, QThread, pyqtSignal

from . import db, metadata_sync


class _MetadataWorker(QObject):
    """Push metadata for a batch of jobs, one after another, off the UI thread."""

    job_status = pyqtSignal(str, str)   # (job_id, push_status)
    finished = pyqtSignal(int, int)     # (pushed_ok, failed)

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
            # Stop between jobs on shutdown, or if the session dropped mid-sweep:
            # the upsert needs the token, and there's no point pushing for a
            # signed-out user.
            if self._cancel:
                break
            if self._session is None or not self._session.is_authenticated():
                break
            try:
                pushed = metadata_sync.push_metadata(
                    self._session, job_id,
                    on_status=lambda status, jid=job_id:
                        self.job_status.emit(jid, status))
                if pushed:
                    ok += 1
            except Exception:
                # push_metadata already recorded FAILED + last_error on the row;
                # keep going so one bad job doesn't stall the rest of the batch.
                failed += 1
        self.finished.emit(ok, failed)


class MetadataSyncController(QObject):
    """Drive background metadata pushes, exposing UI status signals."""

    started = pyqtSignal(int)           # (job_count)
    job_status = pyqtSignal(str, str)   # (job_id, push_status)
    finished = pyqtSignal(int, int)     # (pushed_ok, failed)

    def __init__(self, session, parent=None):
        super().__init__(parent)
        self._session = session
        self._thread = None
        self._worker = None

    def is_busy(self):
        return self._thread is not None

    def shutdown(self):
        """Stop the sweep after the in-flight job and join the worker thread."""
        if self._worker is not None:
            self._worker.cancel()
        self._teardown_thread()

    def resume(self):
        """Sweep and push jobs with pending metadata edits, if signed in.

        Safe to call on every ``session.changed``, after a run finishes, and on
        every metadata edit: it no-ops when signed out, when a sweep is already
        running, or when nothing is pending.
        """
        if self._thread is not None:
            return
        if self._session is None or not self._session.is_authenticated():
            return
        try:
            jobs = db.list_metadata_syncable_jobs()
        except Exception:
            return
        if not jobs:
            return
        self._start(jobs)

    def _start(self, jobs):
        job_ids = [j["job_id"] for j in jobs]
        self._thread = QThread(self)
        self._worker = _MetadataWorker(job_ids, self._session)
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
