"""HTTP client for the cloud pipeline service (``/api/v1``).

A dependency-free (stdlib ``urllib``) client implementing the pipeline API
contract: bearer auth, the Clair3 model registry, resumable multipart upload
of the run input, run submission/polling, and presigned result downloads.

The service base URL comes from the ``PF_CLOUD_API_URL`` environment variable
(e.g. ``https://pipeline.example.org/api/v1``). When it is unset the client is
"unconfigured": callers should treat cloud execution as unavailable and fall
back to the local queue. This keeps the desktop app fully functional offline
while making the whole cloud path live the moment an endpoint is provided.

All ``4xx`` pipeline responses carry a stable ``error_code`` beside the human
``detail``; :class:`CloudApiError` surfaces both so callers branch on the code
(``MODEL_INACTIVE``, ``INPUT_NOT_FOUND``, ``UPLOAD_NOT_FOUND``, …) and fall
back to ``detail`` for display.
"""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from . import paths

# Seconds to wait on any single HTTP call. Uploads use a longer timeout since a
# part PUT streams real bytes; metadata calls are quick.
DEFAULT_TIMEOUT = 30
UPLOAD_TIMEOUT = 600

# Default cloud endpoint: the pipeline API served locally during development.
# Override with PF_CLOUD_API_URL to point at a deployed service.
DEFAULT_API_URL = "http://localhost:8000/api/v1"


def api_base():
    """Configured service base URL (no trailing slash).

    Falls back to the local dev endpoint (:data:`DEFAULT_API_URL`) when
    ``PF_CLOUD_API_URL`` is unset, so cloud sign-in works out of the box.
    """
    base = (os.environ.get("PF_CLOUD_API_URL") or "").strip() or DEFAULT_API_URL
    return base.rstrip("/") or None


def is_configured():
    """True when a cloud endpoint is configured."""
    return api_base() is not None


class CloudApiError(Exception):
    """A non-2xx response from the pipeline API (or a transport failure).

    ``error_code`` is the stable machine code from the body when present;
    ``status`` is the HTTP status; ``detail`` is the human message.
    """

    def __init__(self, message, status=None, error_code=None, detail=None):
        super().__init__(message)
        self.status = status
        self.error_code = error_code
        self.detail = detail or message


def _join(path):
    base = api_base()
    if base is None:
        raise CloudApiError("cloud endpoint not configured (PF_CLOUD_API_URL)")
    if not path.startswith("/"):
        path = "/" + path
    return base + path


def _request(method, path, token=None, json_body=None, form=None,
             query=None, timeout=DEFAULT_TIMEOUT):
    """Perform an API request and return the decoded JSON (or ``None``).

    Exactly one of ``json_body``/``form`` may be given as the request body.
    Non-2xx responses are raised as :class:`CloudApiError` with the parsed
    ``error_code``/``detail``. Transport errors become ``CloudApiError`` too,
    so callers only ever catch one exception type.
    """
    url = _join(path)
    if query:
        url = "%s?%s" % (url, urllib.parse.urlencode(
            {k: v for k, v in query.items() if v is not None}, doseq=True))

    data = None
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = "Bearer %s" % token
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    elif form is not None:
        data = urllib.parse.urlencode(form).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            if not body:
                return None
            return json.loads(body.decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace") if e.fp else ""
        error_code = None
        detail = raw
        try:
            payload = json.loads(raw)
            error_code = payload.get("error_code")
            detail = payload.get("detail") or raw
        except Exception:
            pass
        raise CloudApiError(
            detail or ("HTTP %s" % e.code), status=e.code,
            error_code=error_code, detail=detail)
    except urllib.error.URLError as e:
        raise CloudApiError("network error: %s" % e.reason)


# ---------------------------------------------------------------------------
# Auth (non-pipeline routers: plain {detail}, no error_code)
# ---------------------------------------------------------------------------
def login(email, password):
    """Exchange credentials for tokens via ``POST /login/access-token``.

    The login router uses OAuth2 password form encoding (``username`` field),
    not JSON. Returns the raw token payload
    (``{access_token, refresh_token, token_type}``).
    """
    return _request("POST", "/login/access-token",
                    form={"username": email, "password": password}) or {}


def refresh(refresh_token):
    """Rotate tokens via ``POST /login/refresh-token``."""
    return _request("POST", "/login/refresh-token",
                    json_body={"refresh_token": refresh_token}) or {}


def me(token):
    """Current user (incl. ``email``/``organization_id``) via ``GET /users/me``."""
    return _request("GET", "/users/me", token=token) or {}


class CloudClient:
    """Pipeline-scoped API calls, authorized from a :class:`gui.auth.Session`.

    Every call reads the bearer token from the session at call time, so a
    refreshed token is picked up automatically. Raises :class:`CloudApiError`
    (with ``error_code``) on any non-2xx, and ``PermissionError`` if the
    session is not signed in.
    """

    PIPELINE = "/pipeline"

    def __init__(self, session):
        self._session = session

    def _token(self):
        token = self._session.raw_token() if self._session is not None else None
        if not token:
            raise PermissionError("not signed in")
        return token

    def _p(self, path):
        return self.PIPELINE + path

    # -- model registry --------------------------------------------------
    def list_models(self, skip=0, limit=100):
        """Active Clair3 models: list of ``PipelineModelPublic`` dicts."""
        out = _request("GET", self._p("/models"), token=self._token(),
                       query={"skip": skip, "limit": limit})
        return (out or {}).get("data", [])

    def resolve_model_id(self, name):
        """Map a Clair3 model *name* to its ``model_id`` (uuid), or ``None``.

        Cloud submit takes a ``model_id`` but the desktop config stores the
        model *name* (the on-disk subdir), so the worker resolves it here
        against the live registry.
        """
        if not name:
            return None
        for m in self.list_models(limit=1000):
            if m.get("name") == name:
                return m.get("id")
        return None

    # -- reference-set presets -------------------------------------------
    def list_reference_sets(self, skip=0, limit=100):
        """Curated reference-set presets published by the service.

        Mirrors :meth:`list_models`: returns a list of dicts (each carrying at
        least a ``name``). The desktop app keys reference sets by name, so the
        name is all the picker needs. Returns ``[]`` when the endpoint is
        absent so an older server degrades to the bundled/local sets.
        """
        out = _request("GET", self._p("/reference-sets"), token=self._token(),
                       query={"skip": skip, "limit": limit})
        return (out or {}).get("data", [])

    def reference_set_bundle_url(self, reference_set_id):
        """Presigned GET URL for a finalized reference-set bundle archive.

        Returns the ``ReferenceSetDownloadURL`` dict (carrying
        ``bundle_download_url``) from ``GET /reference-sets/{id}/bundle-url``,
        so the desktop can pull ``bundle.tar.gz`` down and register it locally.
        Raises :class:`CloudApiError` (``REFERENCE_SET_NOT_FOUND`` /
        ``REFERENCE_SET_BUNDLE_MISSING``) when the set or its bundle is absent.
        """
        return _request(
            "GET", self._p("/reference-sets/%s/bundle-url" % reference_set_id),
            token=self._token())

    # -- multipart upload ------------------------------------------------
    def initiate_upload(self, size, part_size=None):
        return _request("POST", self._p("/runs/upload/initiate"),
                        token=self._token(),
                        json_body={"size": int(size), "part_size": part_size})

    def part_urls(self, input_s3_key, upload_id, part_numbers):
        return _request("POST", self._p("/runs/upload/part-urls"),
                        token=self._token(),
                        json_body={"input_s3_key": input_s3_key,
                                   "upload_id": upload_id,
                                   "part_numbers": list(part_numbers)})

    def complete_upload(self, input_s3_key, upload_id, parts):
        """``parts`` = ``[{"part_number": int, "etag": str}, ...]``."""
        return _request("POST", self._p("/runs/upload/complete"),
                        token=self._token(),
                        json_body={"input_s3_key": input_s3_key,
                                   "upload_id": upload_id, "parts": parts})

    def abort_upload(self, input_s3_key, upload_id):
        return _request("POST", self._p("/runs/upload/abort"),
                        token=self._token(),
                        json_body={"input_s3_key": input_s3_key,
                                   "upload_id": upload_id})

    # -- runs ------------------------------------------------------------
    def submit_run(self, model_id, input_s3_key, params=None,
                   input_fingerprint=None, provenance=None, samples=None,
                   sample_count=None, reference_version=None,
                   reference_set_id=None):
        """Create a cloud run.

        The optional ``input_fingerprint``/``provenance``/``samples`` block
        mirrors the local-run sync contract so a cloud-executed run lands on
        the same dedupe surveillance surface as a synced local run. The server
        accepts them optionally (``extra="ignore"``), so an older server simply
        drops them and a newer one joins this run to the deduped dataset.

        ``reference_version`` (the set name) records which reference produced the
        run; ``reference_set_id`` links it to a registered bundle when known
        (else the server resolves/backfills it by name).
        """
        body = {"model_id": model_id,
                "input_s3_key": input_s3_key,
                "params": params or {}}
        if input_fingerprint is not None:
            body["input_fingerprint"] = input_fingerprint
        if provenance is not None:
            body["provenance"] = provenance
        if samples is not None:
            body["samples"] = samples
        if sample_count is not None:
            body["sample_count"] = sample_count
        if reference_version is not None:
            body["reference_version"] = reference_version
        if reference_set_id is not None:
            body["reference_set_id"] = reference_set_id
        return _request("POST", self._p("/runs"), token=self._token(),
                        json_body=body)

    def find_runs(self, input_s3_key=None, status=None, skip=0, limit=100):
        out = _request("GET", self._p("/runs"), token=self._token(),
                       query={"input_s3_key": input_s3_key, "status": status,
                              "skip": skip, "limit": limit})
        return (out or {}).get("data", [])

    def get_run(self, run_id):
        return _request("GET", self._p("/runs/%s" % run_id),
                        token=self._token())

    def run_results(self, run_id):
        return _request("GET", self._p("/runs/%s/results" % run_id),
                        token=self._token())

    def cancel_run(self, run_id):
        return _request("POST", self._p("/runs/%s/cancel" % run_id),
                        token=self._token())

    # -- local-run sync (ingest a locally-executed successful run) -------
    def sync_local_run(self, payload):
        """Register/re-register a local run; returns ``LocalRunSyncInitiated``.

        Idempotent on ``payload['client_run_id']``: re-POSTing the same key
        upserts the same server run and returns fresh presigned ``PUT`` URLs
        (``run``, ``upload_urls``, ``expires_in``). Result keys are set later at
        :meth:`finalize_local_run`, after the client uploads.
        """
        return _request("POST", self._p("/runs/sync"), token=self._token(),
                        json_body=payload)

    def put_sample_metadata(self, run_id, samples):
        """Upsert per-barcode surveillance metadata onto a cloud run.

        ``samples`` is a list of ``{barcode, updated_at, ...}`` rows. The server
        upserts per ``(run_id, barcode)`` with last-write-wins on ``updated_at``
        and leaves un-listed barcodes untouched (partial upsert), so this is
        safe to re-send and safe to send for a subset. Returns the server
        response (or ``None``). Raises ``CloudApiError`` (``RUN_NOT_FOUND`` /
        ``NOT_AUTHORIZED`` / validation 422) on rejection.
        """
        return _request("PUT",
                        self._p("/runs/%s/sample-metadata" % run_id),
                        token=self._token(),
                        json_body={"samples": list(samples)})

    def finalize_local_run(self, run_id, artifacts=None):
        """Confirm uploaded artifacts for a synced run; returns the run.

        ``run_id`` is the server ``PipelineRunPublic.id`` from
        :meth:`sync_local_run`. When ``artifacts`` is given, every named
        artifact must exist server-side (else ``INPUT_NOT_FOUND``); when
        omitted, the server probes the full allowed set and sets those present.
        """
        body = {}
        if artifacts is not None:
            body["artifacts"] = list(artifacts)
        return _request("POST", self._p("/runs/sync/%s/finalize" % run_id),
                        token=self._token(), json_body=body)


# ---------------------------------------------------------------------------
# Presigned S3 part upload / result download (raw bytes, not JSON API calls)
# ---------------------------------------------------------------------------
def put_part(url, chunk, timeout=UPLOAD_TIMEOUT):
    """PUT one part's bytes to its presigned URL; return the ETag header.

    Content-Type is deliberately omitted: the presign signs only host, so
    adding it is unnecessary and parts are opaque bytes. The ETag response
    header is captured verbatim (quotes included) for ``complete_upload``.
    """
    req = urllib.request.Request(url, data=chunk, method="PUT")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        etag = resp.headers.get("ETag") or resp.headers.get("Etag")
    if not etag:
        raise CloudApiError("part upload returned no ETag")
    return etag


def put_artifact(url, file_path, content_type, timeout=UPLOAD_TIMEOUT):
    """PUT a whole result artifact to its presigned URL; return the ETag.

    Unlike :func:`put_part` (multipart, host-only presign), the local-run sync
    presigns each artifact URL **with** its ``Content-Type`` baked into the
    signature, so the header must be sent verbatim (``text/csv`` /
    ``application/gzip`` / ``text/plain``) — a wrong or missing Content-Type
    yields ``SignatureDoesNotMatch``. Artifacts are single, modest files
    (amplicon CSVs are KB, the qc tarball a few MB), so a single PUT is fine.
    """
    with open(file_path, "rb") as fh:
        data = fh.read()
    req = urllib.request.Request(
        url, data=data, method="PUT",
        headers={"Content-Type": content_type})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.headers.get("ETag") or resp.headers.get("Etag")


def download(url, dest_path, timeout=UPLOAD_TIMEOUT):
    """Stream a presigned URL to ``dest_path`` (creating parent dirs)."""
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp, \
            open(dest_path, "wb") as fh:
        while True:
            block = resp.read(1024 * 256)
            if not block:
                break
            fh.write(block)
    return dest_path


class MultipartUploader:
    """Resumable multipart upload of a single file against :class:`CloudClient`.

    Splits the file by the **server-returned** ``part_size``, PUTs each part,
    and records its ETag. Progress is reported through an optional
    ``on_progress(done_bytes, total_bytes)`` callback and an optional
    ``should_cancel()`` predicate is polled between parts for cooperative
    cancellation.

    Resumability: the upload_id, key and per-part ETags are persisted to a JSON
    sidecar in ``paths.uploads_dir()`` keyed by a caller-supplied ``state_key``
    (the local job id). A re-run with the same file + state reuses recorded
    ETags and only sends missing parts. There is no server "list parts"
    endpoint, so this local sidecar is the sole recovery path — if it is lost
    the upload re-initiates from scratch.
    """

    def __init__(self, client, file_path, state_key,
                 on_progress=None, should_cancel=None, log=None):
        self._client = client
        self._file_path = file_path
        self._state_key = state_key
        self._on_progress = on_progress or (lambda done, total: None)
        self._should_cancel = should_cancel or (lambda: False)
        self._log = log or (lambda msg: None)
        self._state_path = os.path.join(
            paths.uploads_dir(), "upload_%s.json" % state_key)

    # -- sidecar state ---------------------------------------------------
    def _load_state(self):
        try:
            with open(self._state_path) as fh:
                return json.load(fh)
        except Exception:
            return {}

    def _save_state(self, state):
        try:
            tmp = self._state_path + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(state, fh)
            os.replace(tmp, self._state_path)
        except Exception:
            pass

    def clear_state(self):
        try:
            os.remove(self._state_path)
        except OSError:
            pass

    # -- upload ----------------------------------------------------------
    def upload(self):
        """Run the upload to completion; return the final ``input_s3_key``.

        Raises :class:`CloudApiError` on API/transport failure or
        ``InterruptedError`` if ``should_cancel`` fires mid-flight.
        """
        total = os.path.getsize(self._file_path)
        state = self._load_state()

        # (Re)initiate unless a prior init for this exact file size is cached.
        if (state.get("upload_id") and state.get("input_s3_key")
                and state.get("size") == total):
            input_s3_key = state["input_s3_key"]
            upload_id = state["upload_id"]
            part_size = state["part_size"]
            part_count = state["part_count"]
            self._log("Resuming upload (%d parts, %d already done)"
                      % (part_count, len(state.get("etags", {}))))
        else:
            init = self._client.initiate_upload(total)
            input_s3_key = init["input_s3_key"]
            upload_id = init["upload_id"]
            part_size = init["part_size"]
            part_count = init["part_count"]
            state = {"input_s3_key": input_s3_key, "upload_id": upload_id,
                     "part_size": part_size, "part_count": part_count,
                     "size": total, "etags": {}}
            self._save_state(state)
            self._log("Initiated upload: %d part(s) of ~%d MiB"
                      % (part_count, part_size // (1024 * 1024)))

        etags = {int(k): v for k, v in state.get("etags", {}).items()}
        done_bytes = len(etags) * part_size

        with open(self._file_path, "rb") as fh:
            for part_number in range(1, part_count + 1):
                if part_number in etags:
                    continue
                if self._should_cancel():
                    raise InterruptedError("upload cancelled")
                fh.seek((part_number - 1) * part_size)
                chunk = fh.read(part_size)
                if not chunk:
                    break
                url = self._presign_one(input_s3_key, upload_id, part_number)
                etags[part_number] = put_part(url, chunk)
                done_bytes = min(total, done_bytes + len(chunk))
                state["etags"] = {str(k): v for k, v in etags.items()}
                self._save_state(state)
                self._on_progress(done_bytes, total)

        parts = [{"part_number": n, "etag": etags[n]}
                 for n in sorted(etags)]
        self._client.complete_upload(input_s3_key, upload_id, parts)
        self.clear_state()
        self._log("Upload complete")
        return input_s3_key

    def _presign_one(self, input_s3_key, upload_id, part_number):
        """Fetch a fresh presigned URL for a single part (TTL-safe per part)."""
        resp = self._client.part_urls(input_s3_key, upload_id, [part_number])
        for p in (resp or {}).get("part_urls", []):
            if p.get("part_number") == part_number:
                return p["url"]
        raise CloudApiError("no presigned URL for part %d" % part_number)
