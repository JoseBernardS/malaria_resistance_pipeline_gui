"""Push mutable surveillance metadata (``sample_meta``) to the cloud.

Unlike run provenance and the input fingerprint -- frozen at enqueue and
carried once on submit/sync -- ``sample_meta`` (region, district, GPS,
collection date, corrected alias/id, notes) is *edited after the run completes*
and edited repeatedly. So it can't ride the one-shot submit/sync payload; it
gets its own upsert path, callable any time, for both origins:

    PUT /pipeline/runs/{run_id}/sample-metadata

The server upserts per ``(run_id, barcode)`` with last-write-wins on each row's
``updated_at`` and leaves un-listed barcodes untouched (partial upsert), so a
re-push is idempotent and a stale retry can't clobber a newer edit. ``run_id``
is the server ``PipelineRun`` id, resolved from either origin via
:func:`gui.db.resolve_server_run_id` (local runs carry it on ``run_sync``,
cloud runs on the job's ``remote_run_id``).

Two contract rules the client enforces so it never knowingly trips the server's
strict validation (which 422s bad input rather than coercing it):

- coordinates must be in range and ``collection_date`` a strict ISO date -- the
  desktop entry path already guarantees this (map-picker pin + ``QDateEdit``),
  but a legacy/imported row is validated here and its bad field dropped to null
  (with a warning) rather than sent;
- ``notes`` is bounded (:data:`NOTES_MAX_BYTES`); over-cap notes are dropped to
  null rather than risking a 422 that would stall the whole batch.

Everything degrades quietly: an unconfigured endpoint, a run with no server id
yet, or nothing newer than the last push is a clean no-op, so the desktop stays
fully usable offline.
"""

import datetime
import logging

from . import cloud_client, db

log = logging.getLogger(__name__)

# Cap on the ``notes`` blob (bytes, UTF-8). The rich-text editor emits a tiny
# whitelisted subset (<b>/<i>/<u>/<br/>), so a human annotation never
# approaches this; the cap only guards a pathological/legacy value from 422-ing
# the batch. Kept in step with the server's bound.
NOTES_MAX_BYTES = 16 * 1024

# Local sample_meta column -> wire field. alias/internal_id are namespaced so
# they never collide with provenance's barcode_alias/sample_id on the wire, in
# storage, or in the surveillance read (metadata wins as display precedence).
_ALIAS_FIELD = "sample_alias"
_INTERNAL_ID_FIELD = "sample_internal_id"

# WHO clinical case classification — the controlled vocabulary the editor offers
# (gui.screens.dashboard.WHO_CASE_CLASSES). The server validates against the same
# enum, so an out-of-vocabulary legacy value is dropped to null here rather than
# risking a 422 that would stall the whole batch.
_WHO_CASE_CLASSES = ("Asymptomatic", "Uncomplicated", "Severe")

# Patient age in whole years. The server accepts an integer in this inclusive
# range or null; an out-of-range or non-integer legacy value is dropped to null
# here rather than risking a 422 that would stall the whole batch.
_AGE_YEARS_MIN = 0
_AGE_YEARS_MAX = 120


def _iso(epoch):
    """Epoch seconds -> RFC3339/ISO-8601 UTC string, or ``None``."""
    if not epoch:
        return None
    return (datetime.datetime.fromtimestamp(
        epoch, tz=datetime.timezone.utc).isoformat().replace("+00:00", "Z"))


def _valid_coords(lat, lon):
    """True iff both coords are present and in range; a pair is all-or-nothing."""
    if lat is None or lon is None:
        return False
    try:
        return -90.0 <= float(lat) <= 90.0 and -180.0 <= float(lon) <= 180.0
    except (TypeError, ValueError):
        return False


def _valid_iso_date(value):
    """True iff ``value`` is a strict ``YYYY-MM-DD`` date string."""
    if not value:
        return False
    try:
        datetime.date.fromisoformat(str(value))
        return True
    except ValueError:
        return False


def _sample_row(meta, on_warn):
    """Map one ``sample_meta`` row to a wire sample dict, or ``None`` to skip.

    Skips a row only when it has no ``updated_at`` (the required LWW key, so it
    can't participate in merge). Individually invalid fields are dropped to null
    -- with a warning -- so one bad legacy value never sinks the batch.
    """
    barcode = meta.get("sample")
    updated_at = _iso(meta.get("updated_at"))
    if not barcode or not updated_at:
        on_warn("skipping sample_meta row with no barcode/updated_at: %r"
                % (barcode,))
        return None

    lat, lon = meta.get("latitude"), meta.get("longitude")
    if (lat is not None or lon is not None) and not _valid_coords(lat, lon):
        on_warn("dropping out-of-range coords for %s: (%r, %r)"
                % (barcode, lat, lon))
        lat = lon = None

    collection_date = meta.get("collection_date")
    if collection_date is not None and not _valid_iso_date(collection_date):
        on_warn("dropping non-ISO collection_date for %s: %r"
                % (barcode, collection_date))
        collection_date = None

    notes = meta.get("notes")
    if notes is not None and len(notes.encode("utf-8")) > NOTES_MAX_BYTES:
        on_warn("dropping over-cap notes (%d bytes) for %s"
                % (len(notes.encode("utf-8")), barcode))
        notes = None

    case_class = meta.get("case_class")
    if case_class is not None and case_class not in _WHO_CASE_CLASSES:
        on_warn("dropping out-of-vocabulary case_class for %s: %r"
                % (barcode, case_class))
        case_class = None

    age_years = meta.get("age_years")
    if age_years is not None:
        try:
            age_years = int(age_years)
            if not _AGE_YEARS_MIN <= age_years <= _AGE_YEARS_MAX:
                raise ValueError
        except (TypeError, ValueError):
            on_warn("dropping out-of-range age_years for %s: %r"
                    % (barcode, meta.get("age_years")))
            age_years = None

    return {
        "barcode": barcode,
        "region": meta.get("region"),
        "district": meta.get("district"),
        "latitude": lat,
        "longitude": lon,
        "collection_date": collection_date,
        "case_classification": case_class,
        "age_years": age_years,
        _ALIAS_FIELD: meta.get("alias"),
        _INTERNAL_ID_FIELD: meta.get("internal_id"),
        "notes": notes,
        "updated_at": updated_at,
    }


def build_metadata_payload(meta_rows, on_warn=None):
    """Shape ``sample_meta`` rows into the ``sample-metadata`` upsert samples.

    Deterministic (sorted by barcode) so the pinned golden fixture is stable.
    ``on_warn(msg)`` receives one message per dropped field/skipped row.
    """
    warn = on_warn or (lambda _msg: None)
    out = []
    for meta in sorted(meta_rows, key=lambda m: m.get("sample") or ""):
        row = _sample_row(meta, warn)
        if row is not None:
            out.append(row)
    return out


def push_metadata(session, job_id, path=None, on_status=None):
    """Push a job's sample_meta to its cloud run; return ``True`` if sent.

    A clean no-op (returns ``False``) when: not signed in / no cloud endpoint;
    the job has no server run id yet (never synced/submitted); or there are no
    sample_meta rows. On success advances the ``metadata_sync`` high-water mark
    so the row is not re-pushed until the next edit. On a rejected push records
    ``FAILED`` + the error and re-raises, so the caller can count it.
    """
    def _emit(status):
        if on_status:
            on_status(status)

    if session is None or not session.is_authenticated():
        return False
    if not cloud_client.is_configured():
        return False

    run_id = db.resolve_server_run_id(job_id, path)
    if not run_id:
        return False

    # list_sample_meta returns {sample: row}; the rows are what we shape/push.
    meta_rows = list(db.list_sample_meta(job_id, path).values())
    if not meta_rows:
        return False

    def _warn(msg):
        log.warning("[metadata_sync %s] %s", job_id, msg)

    samples = build_metadata_payload(meta_rows, on_warn=_warn)
    if not samples:
        return False

    # The high-water mark to record on success: the newest edit we're pushing.
    # Uses the raw epoch values (what list_metadata_syncable_jobs compares), not
    # the ISO strings on the wire.
    pushed_through = max(m.get("updated_at") or 0 for m in meta_rows)

    client = cloud_client.CloudClient(session)
    try:
        _emit("PUSHING")
        client.put_sample_metadata(run_id, samples)
    except cloud_client.CloudApiError as exc:
        db.mark_metadata_pushed(
            job_id, pushed_through=None, last_status="FAILED",
            last_error=str(exc), path=path)
        _emit("FAILED")
        raise
    db.mark_metadata_pushed(
        job_id, pushed_through=pushed_through, last_status="OK", path=path)
    _emit("PUSHED")
    return True
