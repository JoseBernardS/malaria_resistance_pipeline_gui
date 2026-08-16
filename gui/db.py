"""SQLite persistence for saved configs and the job queue / run history.

Uses only the stdlib ``sqlite3``. The DB lives in the user-data dir (see
``gui.paths.db_path``) so it survives a read-only app bundle. All DAO
functions open and close their own connection for simplicity and thread
safety (the GUI and queue may touch the DB from the same thread, but short
connections avoid cross-thread handle sharing issues).
"""

import hashlib
import json
import os
import sqlite3
import time
import uuid

from . import paths, provenance

# Identifiers are random UUID4 strings (not auto-incrementing integers) so a
# job/config id is globally unique and never reused across runs or machines.
SCHEMA = """
CREATE TABLE IF NOT EXISTS job_config (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    created_at    REAL NOT NULL,
    fastq_dir     TEXT NOT NULL,
    output_dir    TEXT NOT NULL,
    reference_set TEXT NOT NULL,
    threads       INTEGER NOT NULL,
    min_qual      INTEGER NOT NULL,
    min_dp        INTEGER NOT NULL,
    min_mq        INTEGER NOT NULL,
    extra_json    TEXT,
    execution_target TEXT,
    clair3_model  TEXT
);

CREATE TABLE IF NOT EXISTS job (
    id            TEXT PRIMARY KEY,
    config_id     TEXT NOT NULL REFERENCES job_config(id),
    queued_at     REAL NOT NULL,
    started_at    REAL,
    finished_at   REAL,
    status        TEXT NOT NULL DEFAULT 'queued',
    output_dir    TEXT NOT NULL,
    exit_code     INTEGER,
    log_path      TEXT,
    remote_run_id TEXT,
    input_s3_key  TEXT,
    input_fingerprint TEXT
);

-- Surveillance sample metadata: one editable row per (job, barcode). The
-- alias and collection site travel into the dashboard, the PDF and the map.
CREATE TABLE IF NOT EXISTS sample_meta (
    job_id          TEXT NOT NULL,
    sample          TEXT NOT NULL,
    alias           TEXT,
    internal_id     TEXT,
    region          TEXT,
    district        TEXT,
    latitude        REAL,
    longitude       REAL,
    collection_date TEXT,
    case_class      TEXT,
    age_years       INTEGER,
    notes           TEXT,
    updated_at      REAL,
    PRIMARY KEY (job_id, sample)
);

-- Append-only audit of every field change. Written inside the DAO so no
-- write path can bypass it; the autoincrement id is intentional here (this
-- is a log, not an identity-stable entity). ``source`` records provenance
-- (creation / edit / ...), the foundation for a future permissions backend.
CREATE TABLE IF NOT EXISTS sample_meta_audit (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id     TEXT NOT NULL,
    sample     TEXT NOT NULL,
    field      TEXT NOT NULL,
    old_value  TEXT,
    new_value  TEXT,
    changed_at REAL,
    source     TEXT
);

-- Compact per-sample outcome persisted at job completion. The map reads
-- this (joined with sample_meta) instead of re-parsing disposable output
-- dirs, so it stays fast and survives deleted runs.
CREATE TABLE IF NOT EXISTS sample_outcome (
    job_id     TEXT NOT NULL,
    sample     TEXT NOT NULL,
    worst_tier TEXT,
    n_calls    INTEGER,
    assessed   INTEGER,
    computed_at REAL,
    PRIMARY KEY (job_id, sample)
);

CREATE INDEX IF NOT EXISTS idx_sample_meta_region
    ON sample_meta(region);
CREATE INDEX IF NOT EXISTS idx_sample_audit_job_sample
    ON sample_meta_audit(job_id, sample);

-- Simple key/value store for application settings (report designer branding,
-- section toggles, page size). Values are always strings; callers encode
-- booleans as "1"/"0" (see gui.reportcfg).
CREATE TABLE IF NOT EXISTS app_config (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at REAL
);

-- Machine-extracted instrument provenance, kept OUT of the user-editable
-- sample_meta so the audit log never narrates edits to facts no user touched.
-- One row per job: the acquisition anchor plus reconciliation fields lifted
-- from the ONT FASTQ headers at enqueue time (see gui.provenance). The
-- ``provenance_source`` enum records confidence: 'HEADER' (runid present) vs
-- 'FINGERPRINT_ONLY' (no runid; dedupe degrades to input_fingerprint).
CREATE TABLE IF NOT EXISTS run_provenance (
    job_id            TEXT PRIMARY KEY,
    sequencing_run_id TEXT,
    basecall_model    TEXT,
    flow_cell_id      TEXT,
    protocol_group_id TEXT,
    run_start_time    TEXT,
    provenance_source TEXT NOT NULL DEFAULT 'FINGERPRINT_ONLY',
    extracted_at      REAL
);

-- Per (job, barcode) provenance child. ``homogeneous`` is 0 when a barcode
-- dir's first/last chunk disagree on runid/basecall model (a hand-merged dir):
-- surfaced as lower-confidence, never a hard block.
CREATE TABLE IF NOT EXISTS sample_provenance (
    job_id        TEXT NOT NULL,
    barcode       TEXT NOT NULL,
    barcode_alias TEXT,
    sample_id     TEXT,
    homogeneous   INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (job_id, barcode)
);

-- Local-run cloud-sync state (see gui.sync). Keyed on the local job id, which
-- doubles as the server's ``client_run_id`` idempotency key. The lifecycle
-- (initiate -> upload N artifacts -> finalize) has retryable sub-states, so it
-- lives in its own table rather than as columns on ``job``; ``remote_run_id``
-- there means "cloud-executed", a different thing. This row is a local
-- convenience — the server remains the source of truth, reconcilable by
-- re-POSTing the idempotent sync with the same client_run_id.
CREATE TABLE IF NOT EXISTS run_sync (
    client_run_id  TEXT PRIMARY KEY,
    server_run_id  TEXT,
    sync_status    TEXT NOT NULL DEFAULT 'NOT_SYNCED',
    artifacts_json TEXT,
    expires_at     REAL,
    last_error     TEXT,
    updated_at     REAL
);

-- Per-job high-water mark for pushing sample_meta to the cloud. Unlike run_sync
-- (which tracks the one-shot result upload), this tracks *mutable* metadata:
-- ``pushed_through`` is the greatest ``sample_meta.updated_at`` confirmed
-- server-side. A job is "dirty" (has edits to push) whenever any of its
-- sample_meta rows has a newer ``updated_at`` than ``pushed_through`` — so an
-- edit made offline is naturally picked up on the next signed-in sweep, no
-- explicit flag to keep in step with the data.
CREATE TABLE IF NOT EXISTS metadata_sync (
    job_id         TEXT PRIMARY KEY,
    pushed_through REAL,
    last_status    TEXT,
    last_error     TEXT,
    updated_at     REAL
);
"""

STATUSES = ("queued", "running", "completed", "failed", "stopped")

# Analysis parameters that define a run's identity for duplicate detection.
# Deliberately excludes ``threads`` (perf-only), ``execution_target`` (Local vs
# Cloud of the same data+params is still a duplicate) and QC/report cosmetics.
FINGERPRINT_KEYS = ("reference_set", "min_qual", "min_dp", "min_mq",
                    "clair3_model")


def compute_input_fingerprint(fastq_dir, fields, path=None):
    """A cheap SHA-256 identifying a run by its inputs + analysis params.

    Combines the canonical JSON of the :data:`FINGERPRINT_KEYS` drawn from
    ``fields`` with :func:`paths.input_manifest` (relpath|size per input file,
    no content reads). Returns ``None`` on any error so a fingerprint failure
    never blocks enqueue — no fingerprint simply means no dedup.
    """
    try:
        params = {k: fields.get(k) for k in FINGERPRINT_KEYS}
        payload = (json.dumps(params, sort_keys=True) + "\x00"
                   + paths.input_manifest(fastq_dir))
        return hashlib.sha256(payload.encode()).hexdigest()
    except Exception:
        return None


def find_duplicate_jobs(fingerprint, path=None):
    """Non-terminal jobs sharing ``fingerprint`` (newest first), or ``[]``.

    Excludes ``failed``/``stopped`` jobs so re-running after a failure doesn't
    nag. Returns ``[]`` when ``fingerprint`` is falsy (nothing to match).
    """
    if not fingerprint:
        return []
    conn = _connect(path)
    try:
        rows = conn.execute(
            """SELECT job.id, job.queued_at, job.status, c.name AS name
                 FROM job JOIN job_config c ON c.id = job.config_id
                WHERE job.input_fingerprint = ?
                  AND job.status NOT IN ('failed','stopped')
                ORDER BY job.queued_at DESC""",
            (fingerprint,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def new_id():
    """A fresh random identifier for a config or job row."""
    return uuid.uuid4().hex


# Fixed namespace so a sample's short code is deterministic and reproducible
# from (job, barcode) on any machine — never random, never reused.
_UID_NAMESPACE = uuid.UUID("6f4d2c8a-1b3e-5a7c-9d0f-2e4a6c8b0d1f")


def sample_uid(job_id, sample):
    """A short, label-friendly unique code for a sample, e.g. ``GHA-7F3A2C``.

    Deterministic UUIDv5 derivative of ``job_id`` + ``barcode``: the same
    sample always yields the same code, which disambiguates barcodes reused
    across runs. Six uppercase hex chars (~16M space) keep it short enough to
    print on a tube label.
    """
    digest = uuid.uuid5(_UID_NAMESPACE, "%s/%s" % (job_id, sample))
    return "GHA-%s" % digest.hex[:6].upper()


def _connect(path=None):
    conn = sqlite3.connect(path or paths.db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(path=None):
    """Create tables if they do not exist. Safe to call on every launch."""
    os.makedirs(os.path.dirname(path or paths.db_path()), exist_ok=True)
    conn = _connect(path)
    try:
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.commit()
    finally:
        conn.close()


def _migrate(conn):
    """Additive schema migrations for databases created by older versions.

    ``CREATE TABLE IF NOT EXISTS`` never alters an existing table, so columns
    added after a user's DB was first created must be back-filled here. Each
    step is guarded on the live column set, so it is a no-op on a fresh DB and
    idempotent on every launch.
    """
    cols = {r["name"] for r
            in conn.execute("PRAGMA table_info(sample_meta)").fetchall()}
    if "internal_id" not in cols:
        conn.execute("ALTER TABLE sample_meta ADD COLUMN internal_id TEXT")
    if "case_class" not in cols:
        conn.execute("ALTER TABLE sample_meta ADD COLUMN case_class TEXT")
    if "age_years" not in cols:
        conn.execute("ALTER TABLE sample_meta ADD COLUMN age_years INTEGER")

    cfg_cols = {r["name"] for r
                in conn.execute("PRAGMA table_info(job_config)").fetchall()}
    if "execution_target" not in cfg_cols:
        conn.execute(
            "ALTER TABLE job_config ADD COLUMN execution_target TEXT")
    if "clair3_model" not in cfg_cols:
        conn.execute("ALTER TABLE job_config ADD COLUMN clair3_model TEXT")

    job_cols = {r["name"] for r
                in conn.execute("PRAGMA table_info(job)").fetchall()}
    if "remote_run_id" not in job_cols:
        conn.execute("ALTER TABLE job ADD COLUMN remote_run_id TEXT")
    if "input_s3_key" not in job_cols:
        conn.execute("ALTER TABLE job ADD COLUMN input_s3_key TEXT")
    if "input_fingerprint" not in job_cols:
        conn.execute("ALTER TABLE job ADD COLUMN input_fingerprint TEXT")


# ---------------------------------------------------------------------------
# job_config DAO
# ---------------------------------------------------------------------------
def save_config(name, fastq_dir, output_dir, reference_set, threads,
                min_qual, min_dp, min_mq, execution_target="local",
                clair3_model=None, extra=None, path=None):
    """Insert a reusable configuration; returns its new id."""
    config_id = new_id()
    conn = _connect(path)
    try:
        conn.execute(
            """INSERT INTO job_config
                 (id, name, created_at, fastq_dir, output_dir, reference_set,
                  threads, min_qual, min_dp, min_mq, extra_json,
                  execution_target, clair3_model)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (config_id, name, time.time(), fastq_dir, output_dir,
             reference_set, int(threads), int(min_qual), int(min_dp),
             int(min_mq), json.dumps(extra or {}),
             execution_target, clair3_model))
        conn.commit()
        return config_id
    finally:
        conn.close()


def list_configs(path=None):
    conn = _connect(path)
    try:
        rows = conn.execute(
            "SELECT * FROM job_config ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_config(config_id, path=None):
    conn = _connect(path)
    try:
        row = conn.execute(
            "SELECT * FROM job_config WHERE id=?", (config_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# job DAO
# ---------------------------------------------------------------------------
def enqueue_job(config_id, output_dir, log_path=None, path=None):
    """Create a 'queued' job for a config; returns its new id.

    Computes and stores the run's input fingerprint here so both the local and
    cloud providers get it from one place. The config row already carries the
    :data:`FINGERPRINT_KEYS` columns, so it doubles as the ``fields`` source.
    Instrument provenance (ONT header extraction) is stored alongside; both are
    best-effort and never block enqueue.
    """
    job_id = new_id()
    cfg = get_config(config_id, path)
    fp = (compute_input_fingerprint(cfg["fastq_dir"], cfg, path)
          if cfg else None)
    conn = _connect(path)
    try:
        conn.execute(
            """INSERT INTO job
                 (id, config_id, queued_at, status, output_dir, log_path,
                  input_fingerprint)
               VALUES (?,?,?,?,?,?,?)""",
            (job_id, config_id, time.time(), "queued", output_dir, log_path,
             fp))
        conn.commit()
    finally:
        conn.close()
    if cfg:
        _store_provenance(job_id, cfg["fastq_dir"], path)
    return job_id


def _store_provenance(job_id, fastq_dir, path=None):
    """Extract and persist ONT header provenance for a job. Best-effort.

    Writes one ``run_provenance`` row plus a ``sample_provenance`` row per
    barcode. Any failure is swallowed: provenance is a nice-to-have for
    cross-device dedupe, never a reason to fail a queued run.
    """
    try:
        prov = provenance.run_provenance(fastq_dir)
        if not prov:
            return
        run = prov["run"]
        conn = _connect(path)
        try:
            conn.execute(
                """INSERT OR REPLACE INTO run_provenance
                     (job_id, sequencing_run_id, basecall_model, flow_cell_id,
                      protocol_group_id, run_start_time, provenance_source,
                      extracted_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (job_id, run.get("sequencing_run_id"),
                 run.get("basecall_model"), run.get("flow_cell_id"),
                 run.get("protocol_group_id"), run.get("run_start_time"),
                 prov["source"], time.time()))
            for bc, rec in prov["barcodes"].items():
                conn.execute(
                    """INSERT OR REPLACE INTO sample_provenance
                         (job_id, barcode, barcode_alias, sample_id,
                          homogeneous)
                       VALUES (?,?,?,?,?)""",
                    (job_id, bc, rec.get("barcode_alias"),
                     rec.get("sample_id"),
                     1 if rec.get("homogeneous", True) else 0))
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def get_run_provenance(job_id, path=None):
    """The ``run_provenance`` row for a job, or ``None``."""
    conn = _connect(path)
    try:
        row = conn.execute(
            "SELECT * FROM run_provenance WHERE job_id=?", (job_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_sample_provenance(job_id, path=None):
    """All ``sample_provenance`` rows for a job, keyed by barcode."""
    conn = _connect(path)
    try:
        rows = conn.execute(
            "SELECT * FROM sample_provenance WHERE job_id=? ORDER BY barcode",
            (job_id,)).fetchall()
        return {r["barcode"]: dict(r) for r in rows}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# run_sync DAO (local-run cloud-sync state)
# ---------------------------------------------------------------------------
# Sync lifecycle sub-states. NOT_SYNCED is the implicit state of a job with no
# row; the rest track progress through initiate -> upload -> finalize so a
# crashed sync can be resumed and the UI can show where a run stands.
SYNC_STATUSES = ("NOT_SYNCED", "INITIATED", "UPLOADING", "UPLOADED",
                 "FINALIZED", "FAILED")


def get_run_sync(client_run_id, path=None):
    """The ``run_sync`` row for a local job, or ``None`` (never synced)."""
    conn = _connect(path)
    try:
        row = conn.execute(
            "SELECT * FROM run_sync WHERE client_run_id=?",
            (client_run_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def upsert_run_sync(client_run_id, path=None, **fields):
    """Insert or patch a job's sync row; stamps ``updated_at``.

    Only the columns named in ``fields`` are written, so callers advance the
    row incrementally (status, server_run_id, artifacts_json, expires_at,
    last_error) without clobbering the rest.
    """
    fields["updated_at"] = time.time()
    conn = _connect(path)
    try:
        exists = conn.execute(
            "SELECT 1 FROM run_sync WHERE client_run_id=?",
            (client_run_id,)).fetchone()
        if exists is None:
            cols = ["client_run_id"] + list(fields)
            conn.execute(
                "INSERT INTO run_sync (%s) VALUES (%s)"
                % (", ".join(cols), ", ".join("?" * len(cols))),
                [client_run_id] + list(fields.values()))
        else:
            sets = ", ".join("%s=?" % k for k in fields)
            conn.execute(
                "UPDATE run_sync SET %s WHERE client_run_id=?" % sets,
                list(fields.values()) + [client_run_id])
        conn.commit()
    finally:
        conn.close()


def list_run_sync(path=None):
    """All sync rows, newest first (for a sync dashboard / reconciliation)."""
    conn = _connect(path)
    try:
        rows = conn.execute(
            "SELECT * FROM run_sync ORDER BY updated_at DESC").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def list_syncable_jobs(path=None):
    """Completed **local** jobs whose cloud sync isn't FINALIZED yet, oldest first.

    These are the runs the auto-sync pass ingests on sign-in: locally-executed
    ``completed`` jobs with no ``run_sync`` row (never attempted) or a row that
    stalled before ``FINALIZED`` (a crashed/failed sync is resumable and
    idempotent on ``client_run_id``). Cloud-target jobs are excluded — their
    results already live server-side, so there is nothing to sync back.
    """
    conn = _connect(path)
    try:
        rows = conn.execute(
            "SELECT job.* FROM job "
            "JOIN job_config c ON job.config_id = c.id "
            "LEFT JOIN run_sync s ON s.client_run_id = job.id "
            "WHERE job.status='completed' "
            "  AND (c.execution_target IS NULL OR c.execution_target != 'cloud') "
            "  AND (s.sync_status IS NULL OR s.sync_status != 'FINALIZED') "
            "ORDER BY job.queued_at ASC").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# metadata_sync DAO (push mutable sample_meta to the cloud, keyed on server run)
# ---------------------------------------------------------------------------
def resolve_server_run_id(job_id, path=None):
    """The server ``PipelineRun`` id a local job maps to, or ``None``.

    A locally-executed run that was synced carries it on its ``run_sync`` row
    (``server_run_id``); a cloud-executed run carries it on the job itself
    (``remote_run_id``). Metadata attaches to that id regardless of origin, so
    both paths resolve through here. Prefers the sync row (the sync surface is
    the source of truth for local runs) and falls back to the job.
    """
    conn = _connect(path)
    try:
        row = conn.execute(
            "SELECT COALESCE(s.server_run_id, j.remote_run_id) AS run_id "
            "FROM job j "
            "LEFT JOIN run_sync s ON s.client_run_id = j.id "
            "WHERE j.id = ?", (job_id,)).fetchone()
        return row["run_id"] if row and row["run_id"] else None
    finally:
        conn.close()


def list_metadata_syncable_jobs(path=None):
    """Jobs with sample_meta edits not yet confirmed on their cloud run.

    A job qualifies when it (a) resolves to a server run id (has been synced or
    executed in the cloud) and (b) has at least one ``sample_meta`` row whose
    ``updated_at`` is newer than the job's ``metadata_sync.pushed_through``
    high-water mark. This is what makes offline edits self-heal: the edit bumps
    ``updated_at`` above the mark, so the next signed-in sweep re-pushes without
    any explicit dirty flag. Returns ``[{job_id, server_run_id}, ...]``.
    """
    conn = _connect(path)
    try:
        rows = conn.execute(
            "SELECT j.id AS job_id, "
            "       COALESCE(s.server_run_id, j.remote_run_id) AS server_run_id "
            "FROM job j "
            "JOIN sample_meta m ON m.job_id = j.id "
            "LEFT JOIN run_sync s ON s.client_run_id = j.id "
            "LEFT JOIN metadata_sync ms ON ms.job_id = j.id "
            "WHERE COALESCE(s.server_run_id, j.remote_run_id) IS NOT NULL "
            "GROUP BY j.id "
            "HAVING MAX(m.updated_at) > COALESCE(ms.pushed_through, 0) "
            "ORDER BY j.queued_at ASC").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_metadata_sync(job_id, path=None):
    """The ``metadata_sync`` row for a job, or ``None``."""
    conn = _connect(path)
    try:
        row = conn.execute(
            "SELECT * FROM metadata_sync WHERE job_id=?", (job_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def mark_metadata_pushed(job_id, pushed_through, last_status="OK",
                         last_error=None, path=None):
    """Record how far a job's sample_meta has been confirmed server-side.

    ``pushed_through`` is the greatest ``sample_meta.updated_at`` the server has
    accepted; advancing it is what clears the job from
    :func:`list_metadata_syncable_jobs` until the next edit.
    """
    conn = _connect(path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO metadata_sync "
            "  (job_id, pushed_through, last_status, last_error, updated_at) "
            "VALUES (?,?,?,?,?)",
            (job_id, pushed_through, last_status, last_error, time.time()))
        conn.commit()
    finally:
        conn.close()


def next_queued_job(path=None):
    """Oldest still-queued **local** job, or None.

    Cloud-target jobs are excluded so the in-process local queue never tries to
    run a job destined for the remote service; the cloud controller claims
    those via :func:`next_queued_cloud_job`.
    """
    conn = _connect(path)
    try:
        row = conn.execute(
            "SELECT job.* FROM job "
            "JOIN job_config c ON job.config_id = c.id "
            "WHERE job.status='queued' "
            "  AND (c.execution_target IS NULL OR c.execution_target != 'cloud') "
            "ORDER BY job.queued_at ASC LIMIT 1").fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def next_queued_cloud_job(path=None):
    """Oldest still-queued **cloud** job (execution_target='cloud'), or None."""
    conn = _connect(path)
    try:
        row = conn.execute(
            "SELECT job.* FROM job "
            "JOIN job_config c ON job.config_id = c.id "
            "WHERE job.status='queued' AND c.execution_target='cloud' "
            "ORDER BY job.queued_at ASC LIMIT 1").fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def update_job(job_id, path=None, **fields):
    """Update arbitrary columns of a job row (status, timestamps, etc.)."""
    if not fields:
        return
    cols = ", ".join("%s=?" % k for k in fields)
    vals = list(fields.values()) + [job_id]
    conn = _connect(path)
    try:
        conn.execute("UPDATE job SET %s WHERE id=?" % cols, vals)
        conn.commit()
    finally:
        conn.close()


def get_job(job_id, path=None):
    conn = _connect(path)
    try:
        row = conn.execute("SELECT * FROM job WHERE id=?", (job_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_jobs(path=None):
    """All jobs joined with their config name, newest first."""
    conn = _connect(path)
    try:
        rows = conn.execute(
            """SELECT job.*, job_config.name AS config_name,
                      job_config.reference_set AS reference_set
                 FROM job JOIN job_config ON job.config_id = job_config.id
                ORDER BY job.queued_at DESC""").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def latest_completed_job(path=None):
    conn = _connect(path)
    try:
        row = conn.execute(
            "SELECT * FROM job WHERE status='completed' "
            "ORDER BY finished_at DESC LIMIT 1").fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def reset_running_jobs(path=None):
    """On launch, any *local* job left 'running' from a crash is stopped.

    Cloud jobs (those with a ``remote_run_id``) are deliberately excluded: the
    remote run keeps executing on the server across a desktop restart, so we
    resume polling it instead of marking it stopped (see
    ``list_resumable_cloud_jobs``).
    """
    conn = _connect(path)
    try:
        conn.execute(
            "UPDATE job SET status='stopped' "
            "WHERE status='running' AND remote_run_id IS NULL")
        conn.commit()
    finally:
        conn.close()


def list_resumable_cloud_jobs(path=None):
    """Cloud jobs still in flight (submitted, non-terminal) to re-attach on launch.

    A job qualifies once it has a ``remote_run_id`` and its local status is
    still ``queued`` or ``running`` — i.e. the server may still be working on
    it. Newest first.
    """
    conn = _connect(path)
    try:
        rows = conn.execute(
            "SELECT * FROM job "
            "WHERE remote_run_id IS NOT NULL "
            "  AND status IN ('queued','running') "
            "ORDER BY queued_at DESC").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# sample_meta DAO (surveillance metadata + append-only audit)
# ---------------------------------------------------------------------------
# Editable columns of sample_meta, in the order they are presented. Used by
# the upsert to diff/persist and by callers to know the schema.
SAMPLE_META_FIELDS = ("alias", "internal_id", "region", "district", "latitude",
                      "longitude", "collection_date", "case_class", "age_years",
                      "notes")


def get_sample_meta(job_id, sample, path=None):
    """The metadata row for one (job, sample), or None."""
    conn = _connect(path)
    try:
        row = conn.execute(
            "SELECT * FROM sample_meta WHERE job_id=? AND sample=?",
            (job_id, sample)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_sample_meta(job_id, path=None):
    """All metadata rows for a job, keyed by sample."""
    conn = _connect(path)
    try:
        rows = conn.execute(
            "SELECT * FROM sample_meta WHERE job_id=? ORDER BY sample",
            (job_id,)).fetchall()
        return {r["sample"]: dict(r) for r in rows}
    finally:
        conn.close()


def _norm(value):
    """Normalise a field value for comparison/storage: blanks become None."""
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return value


def upsert_sample_meta(job_id, sample, fields, source="edit", path=None):
    """Insert or update a sample's metadata, auditing every changed field.

    Diffs ``fields`` against the current row and appends one audit row per
    changed field (with old/new values and ``source`` provenance) *before*
    the INSERT OR REPLACE, all in one transaction so the log can never drift
    from the data. Only keys in ``SAMPLE_META_FIELDS`` are considered;
    ``updated_at`` is set to now. Returns the number of audited changes.
    """
    conn = _connect(path)
    try:
        cur = conn.execute(
            "SELECT * FROM sample_meta WHERE job_id=? AND sample=?",
            (job_id, sample)).fetchone()
        current = dict(cur) if cur else {}

        merged = {f: current.get(f) for f in SAMPLE_META_FIELDS}
        now = time.time()
        changes = 0
        for f in SAMPLE_META_FIELDS:
            if f not in fields:
                continue
            new_val = _norm(fields[f])
            old_val = _norm(current.get(f))
            if new_val == old_val:
                merged[f] = new_val
                continue
            conn.execute(
                """INSERT INTO sample_meta_audit
                     (job_id, sample, field, old_value, new_value,
                      changed_at, source)
                   VALUES (?,?,?,?,?,?,?)""",
                (job_id, sample, f,
                 None if old_val is None else str(old_val),
                 None if new_val is None else str(new_val),
                 now, source))
            merged[f] = new_val
            changes += 1

        conn.execute(
            """INSERT OR REPLACE INTO sample_meta
                 (job_id, sample, alias, internal_id, region, district,
                  latitude, longitude, collection_date, case_class, age_years,
                  notes, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (job_id, sample, merged["alias"], merged["internal_id"],
             merged["region"], merged["district"], merged["latitude"],
             merged["longitude"], merged["collection_date"],
             merged["case_class"], merged["age_years"], merged["notes"], now))
        conn.commit()
        return changes
    finally:
        conn.close()


def list_sample_audit(job_id, sample=None, path=None):
    """Audit rows for a job (optionally one sample), newest first."""
    conn = _connect(path)
    try:
        if sample is None:
            rows = conn.execute(
                "SELECT * FROM sample_meta_audit WHERE job_id=? "
                "ORDER BY changed_at DESC, id DESC", (job_id,)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM sample_meta_audit WHERE job_id=? AND sample=? "
                "ORDER BY changed_at DESC, id DESC",
                (job_id, sample)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# sample_outcome DAO (compact per-sample result, persisted at completion)
# ---------------------------------------------------------------------------
def upsert_sample_outcome(job_id, sample, worst_tier, n_calls, assessed,
                          path=None):
    """Persist a sample's compact outcome (durable source for the map)."""
    conn = _connect(path)
    try:
        conn.execute(
            """INSERT OR REPLACE INTO sample_outcome
                 (job_id, sample, worst_tier, n_calls, assessed, computed_at)
               VALUES (?,?,?,?,?,?)""",
            (job_id, sample, worst_tier, int(n_calls or 0),
             1 if assessed else 0, time.time()))
        conn.commit()
    finally:
        conn.close()


def find_barcode_reuse(barcodes, exclude_job_id=None, path=None):
    """Which of ``barcodes`` already appear in *other* runs.

    Barcodes are reused between sequencing runs, so the same label (e.g.
    ``barcode01``) can mean different samples. Returns
    ``{barcode: [{"job_id","name","queued_at"}, ...]}`` for every barcode also
    seen in another job, drawn from both the labels (``sample_meta``) and the
    completed outcomes (``sample_outcome``). Empty dict if none overlap.
    """
    barcodes = [b for b in (barcodes or []) if b]
    if not barcodes:
        return {}
    placeholders = ",".join("?" * len(barcodes))
    conn = _connect(path)
    try:
        rows = conn.execute(
            """SELECT DISTINCT s.sample AS sample, j.id AS job_id,
                      c.name AS name, j.queued_at AS queued_at
                 FROM (SELECT job_id, sample FROM sample_meta
                       UNION SELECT job_id, sample FROM sample_outcome) s
                 JOIN job j ON j.id = s.job_id
                 JOIN job_config c ON c.id = j.config_id
                WHERE s.sample IN (%s)""" % placeholders,
            barcodes).fetchall()
    finally:
        conn.close()
    out = {}
    for r in rows:
        if exclude_job_id and r["job_id"] == exclude_job_id:
            continue
        out.setdefault(r["sample"], []).append({
            "job_id": r["job_id"], "name": r["name"],
            "queued_at": r["queued_at"]})
    return out


# ---------------------------------------------------------------------------
# app_config DAO (key/value settings store)
# ---------------------------------------------------------------------------
def get_app_config(key, default=None, path=None):
    """Return the string value stored under ``key``, or ``default`` if unset."""
    conn = _connect(path)
    try:
        row = conn.execute(
            "SELECT value FROM app_config WHERE key=?", (key,)).fetchone()
        return row["value"] if row is not None else default
    finally:
        conn.close()


def set_app_config(key, value, path=None):
    """Insert or update the value stored under ``key``."""
    conn = _connect(path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO app_config (key, value, updated_at) "
            "VALUES (?,?,?)", (key, value, time.time()))
        conn.commit()
    finally:
        conn.close()


def list_all_outcomes_with_meta(path=None):
    """Every persisted outcome left-joined with its sample metadata.

    A single read powering the surveillance map across all of the user's
    runs. Output dirs may be long gone; this never touches the filesystem.
    """
    conn = _connect(path)
    try:
        rows = conn.execute(
            """SELECT o.job_id, o.sample, o.worst_tier, o.n_calls,
                      o.assessed, m.alias, m.region, m.district,
                      m.latitude, m.longitude, m.collection_date
                 FROM sample_outcome o
                 LEFT JOIN sample_meta m
                   ON o.job_id = m.job_id AND o.sample = m.sample""").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def list_all_samples_for_search(path=None):
    """Every persisted specimen across all runs, for the patient registry.

    Same LEFT JOIN as :func:`list_all_outcomes_with_meta`, but selecting the
    extra identity fields the searchable registry shows and searches over
    (barcode, internal id, case class, collection date). No ``WHERE``/``LIKE``
    here: the specimen UID is *derived* (see :func:`sample_uid`) and the user
    searches across every field, so filtering happens in Python over a
    concatenation of the visible fields plus the computed UID. The dataset is
    run-scale (dozens–hundreds of rows), so a full read is cheap and keeps UID
    matching correct. Never touches the filesystem.
    """
    conn = _connect(path)
    try:
        rows = conn.execute(
            """SELECT o.job_id, o.sample, o.worst_tier, o.n_calls,
                      o.assessed, m.alias, m.internal_id, m.case_class,
                      m.region, m.district, m.collection_date
                 FROM sample_outcome o
                 LEFT JOIN sample_meta m
                   ON o.job_id = m.job_id AND o.sample = m.sample""").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
