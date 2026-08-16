"""Execution-provider abstraction for the job queue.

Jobs can, in principle, run **locally** (the shipped sequential ``JobQueue``)
or be handed off to a **cloud** queue that executes the pipeline remotely.
This module is the seam that keeps those two worlds interchangeable: the app
routes every "add job" request through a ``JobProvider`` selected by the
saved config's ``execution_target``.

Only the local path is wired up today. The cloud side is a deliberate stub —
a provider that refuses to run plus a client whose network calls are
``# TODO(cloud)`` placeholders — so a real remote backend can drop in later
without touching the local queue or the UI's submit flow.
"""

from . import cloud_client


class JobProvider:
    """Base execution provider: turn a saved config into a running job."""

    def add_job(self, config_id):
        """Enqueue/submit the config and return an opaque job id."""
        raise NotImplementedError

    def is_busy(self):
        """True while a job is actively running under this provider."""
        return False

    def stop_active(self):
        """Stop the currently active job, if any."""
        raise NotImplementedError


class LocalProvider(JobProvider):
    """Thin delegate to the in-process sequential ``JobQueue``.

    Behaviour is unchanged from calling the queue directly; the wrapper only
    exists so ``app.py`` can treat local and cloud execution uniformly.
    """

    def __init__(self, queue):
        self._queue = queue

    def add_job(self, config_id):
        return self._queue.add_job(config_id)

    def is_busy(self):
        return self._queue.is_busy()

    def stop_active(self):
        self._queue.stop_active()


class CloudProvider(JobProvider):
    """Remote execution provider backed by a :class:`CloudJobController`.

    Cloud runs are org-scoped and bearer-authenticated, so ``add_job`` enforces
    two gates before touching the network:

    - **not signed in** → ``PermissionError`` (the app maps this to a "sign in
      to run cloud jobs" prompt); every cloud call would otherwise 401.
    - **no server configured** (``PF_CLOUD_API_URL`` unset) → ``NotImplementedError``
      so the app shows its "not enabled yet" notice while still persisting the
      config.

    Once both gates pass it delegates to the injected ``controller`` (a
    :class:`gui.cloud_queue.CloudJobController`), whose Qt signals ``app.py``
    has wired into the same Progress/Results flow as local jobs.
    """

    def __init__(self, session=None, controller=None):
        self._session = session
        self._controller = controller

    def add_job(self, config_id):
        if self._session is not None and not self._session.is_authenticated():
            raise PermissionError("sign in to run cloud jobs")
        if self._controller is None or not cloud_client.is_configured():
            raise NotImplementedError("cloud execution coming soon")
        return self._controller.add_job(config_id)

    def is_busy(self):
        return bool(self._controller and self._controller.is_busy())

    def stop_active(self):
        if self._controller is not None:
            self._controller.stop_active()


def cloud_model_names(session=None):
    """Cloud Clair3 model names, or ``[]`` when the service can't be reached.

    Swallowing errors keeps the model combo safe to populate: the UI shows an
    empty/placeholder list when cloud is unconfigured or the fetch fails, and
    returns real names once a signed-in session reaches the ``/models`` route.
    """
    if session is None or not cloud_client.is_configured():
        return []
    try:
        client = cloud_client.CloudClient(session)
        return [m.get("name") for m in client.list_models() if m.get("name")]
    except Exception:
        return []


def cloud_reference_set_names(session=None):
    """Cloud reference-set names, or ``[]`` when the service can't be reached.

    Mirrors :func:`cloud_model_names`: errors are swallowed so the reference
    combo stays safe to populate when cloud is unconfigured, signed out, or the
    fetch fails, and returns real names once a signed-in session reaches the
    ``/reference-sets`` route.
    """
    if session is None or not cloud_client.is_configured():
        return []
    try:
        client = cloud_client.CloudClient(session)
        return [r.get("name") for r in client.list_reference_sets()
                if r.get("name")]
    except Exception:
        return []
