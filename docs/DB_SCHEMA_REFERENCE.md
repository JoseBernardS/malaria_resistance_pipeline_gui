# Database schema reference

This documents every SQLite table the desktop GUI persists. The authoritative
source is the `SCHEMA` string in [`gui/db.py`](gui/db.py); the tables below are
transcribed from those live `CREATE TABLE` statements, so if a column here
disagrees with the code, the code wins.

The database file lives in the writable user-data directory
(`gui.paths.db_path()` → `.../PfDrugResistance/pipeline.db`) so it survives a
read-only app bundle. All identifiers are random UUID4 hex strings
(`gui.db.new_id()`), never auto-incrementing integers, so a config/job id is
globally unique and never reused across runs or machines.

## How inputs and outputs flow

- A **`job_config`** row captures the *inputs* of a run: where the FASTQ lives,
  the reference panel, thresholds, QC/reporting choices, the execution target
  and the Clair3 model.
- Adding a job creates one **`job`** row per run (a config can be run many
  times). The job tracks status and timestamps and points at its own per-run
  output directory.
- While editing samples, **`sample_meta`** holds the editable surveillance
  metadata per `(job, barcode)`; every field change is logged append-only in
  **`sample_meta_audit`**.
- At completion the queue distils each sample's result into a compact
  **`sample_outcome`** row so the surveillance map has a durable source that
  survives deletion of the output directory.

---

## `job_config` — run inputs

One reusable configuration. Holds everything the pipeline needs to run plus the
new execution routing fields.

| Column             | Type    | Nullable | Purpose |
|--------------------|---------|----------|---------|
| `id`               | TEXT    | no (PK)  | Random UUID4 hex; primary key. |
| `name`             | TEXT    | no       | Human label for the run. |
| `created_at`       | REAL    | no       | Unix timestamp the config was saved. |
| `fastq_dir`        | TEXT    | no       | Folder of `barcode*` sub-folders → `FASTQ_PASS_DIR`. |
| `output_dir`       | TEXT    | no       | Chosen output root → `OUTPUT_DIR`. |
| `reference_set`    | TEXT    | no       | Reference-panel preset name (see `config_bridge.REFERENCE_SETS`). |
| `threads`          | INTEGER | no       | `THREADS`. |
| `min_qual`         | INTEGER | no       | `MIN_QUAL`. |
| `min_dp`           | INTEGER | no       | `MIN_DP`. |
| `min_mq`           | INTEGER | no       | `MIN_MQ`. |
| `extra_json`       | TEXT    | yes      | JSON of extra conf keys: `QC_TOOL`, `RUN_PRETRIM_QC`, `REPORT_MODE`. |
| `execution_target` | TEXT    | yes      | `"local"` or `"cloud"`; selects the execution provider in `app.py`. |
| `clair3_model`     | TEXT    | yes      | Selected Clair3 model **name** → `CLAIR3_MODEL`; the bash pipeline resolves the path from `data/clair3_models/<name>`. |

**Primary key:** `id`.

**Inputs/outputs note:** this is the sole *input* record. `config_bridge`
translates these columns into a per-run `.conf`; `execution_target` and
`clair3_model` are the two columns added for cloud routing and model selection.
Both are nullable so pre-existing databases upgrade non-destructively via the
`ALTER TABLE ... ADD COLUMN` guards in `_migrate()`.

---

## `job` — one row per run

| Column        | Type    | Nullable | Purpose |
|---------------|---------|----------|---------|
| `id`          | TEXT    | no (PK)  | Random UUID4 hex; primary key. |
| `config_id`   | TEXT    | no       | FK → `job_config(id)` (the inputs used). |
| `queued_at`   | REAL    | no       | Unix timestamp the job was enqueued. |
| `started_at`  | REAL    | yes      | When the runner began (null while queued). |
| `finished_at` | REAL    | yes      | When the run ended (null while queued/running). |
| `status`      | TEXT    | no       | One of the STATUSES enum; defaults to `'queued'`. |
| `output_dir`  | TEXT    | no       | This run's own `job_<id>` output folder. |
| `exit_code`   | INTEGER | yes      | Process exit code once finished. |
| `log_path`    | TEXT    | yes      | Path to this run's log file. |

**Primary key:** `id`. **Foreign key:** `config_id → job_config(id)`.

**Inputs/outputs note:** each run gets its own `output_dir` (`job_<id>`
subfolder) so successive runs of the same config never overwrite one another's
`final_reports`.

### `STATUSES` enum

Defined in `gui/db.py` as `STATUSES`:

`queued` · `running` · `completed` · `failed` · `stopped`

- `queued` → waiting in the sequential queue.
- `running` → currently executing.
- `completed` → exit code 0.
- `failed` → non-zero exit code, or a runner error.
- `stopped` → cancelled by the user, or reset from `running` on relaunch after
  a crash (`reset_running_jobs()`).

---

## `sample_meta` — editable surveillance metadata

One editable row per `(job, barcode)`. The alias and collection site travel
into the dashboard, the PDF and the map.

| Column            | Type | Nullable | Purpose |
|-------------------|------|----------|---------|
| `job_id`          | TEXT | no (PK)  | Job this sample belongs to. |
| `sample`          | TEXT | no (PK)  | Barcode label (e.g. `barcode01`). |
| `alias`           | TEXT | yes      | Friendly sample name. |
| `internal_id`     | TEXT | yes      | Lab/internal identifier. |
| `region`          | TEXT | yes      | Ghana admin-1 region. |
| `district`        | TEXT | yes      | District within the region. |
| `latitude`        | REAL | yes      | Collection-site latitude. |
| `longitude`       | REAL | yes      | Collection-site longitude. |
| `collection_date` | TEXT | yes      | Collection date (ISO string). |
| `notes`           | TEXT | yes      | Free-text notes. |
| `updated_at`      | REAL | yes      | Unix timestamp of the last edit. |

**Primary key:** `(job_id, sample)`.

The editable field set is `SAMPLE_META_FIELDS` in `gui/db.py`:
`alias, internal_id, region, district, latitude, longitude, collection_date,
notes`. Upserts diff against the current row and audit each changed field.

### `sample_meta_audit` — append-only change log

Written inside the same transaction as the `sample_meta` upsert, so no write
path can bypass it.

| Column       | Type    | Nullable | Purpose |
|--------------|---------|----------|---------|
| `id`         | INTEGER | no (PK)  | Autoincrement log id (this is a log, not an identity-stable entity). |
| `job_id`     | TEXT    | no       | Job of the changed sample. |
| `sample`     | TEXT    | no       | Barcode of the changed sample. |
| `field`      | TEXT    | no       | Which `sample_meta` field changed. |
| `old_value`  | TEXT    | yes      | Previous value (stringified; null if unset). |
| `new_value`  | TEXT    | yes      | New value (stringified; null if cleared). |
| `changed_at` | REAL    | yes      | Unix timestamp of the change. |
| `source`     | TEXT    | yes      | Provenance: `creation`, `edit`, … (basis for a future permissions backend). |

**Primary key:** `id` (autoincrement).

---

## `sample_outcome` — compact per-sample outputs

The distilled per-sample *output* persisted at job completion. The map reads
this (joined with `sample_meta`) instead of re-parsing disposable output dirs,
so it stays fast and survives deleted runs.

| Column        | Type    | Nullable | Purpose |
|---------------|---------|----------|---------|
| `job_id`      | TEXT    | no (PK)  | Job that produced this outcome. |
| `sample`      | TEXT    | no (PK)  | Barcode label. |
| `worst_tier`  | TEXT    | yes      | Highest-concern tier seen for the sample (see tier vocabulary). |
| `n_calls`     | INTEGER | yes      | Number of resistance calls parsed for the sample. |
| `assessed`    | INTEGER | yes      | `1` if the sample had at least one gene covered (`OK`/`LOW_COVERAGE`), else `0`. |
| `computed_at` | REAL    | yes      | Unix timestamp the outcome was written. |

**Primary key:** `(job_id, sample)`.

**Inputs/outputs note:** computed by `queue._compute_outcomes()` from
`final_reports/resistance_calls.csv` and `coverage_report.csv`. Best-effort and
fully guarded — capturing an outcome must never fail the job.

### Tier vocabulary

`worst_tier` uses the report's classifier (`_TIER_ORDER` in `gui/queue.py`),
highest concern first:

`validated` (worst) · `candidate` · `potential`

The persisted `worst_tier` matches the dashboard/PDF classification exactly, so
the map and the reports agree.

---

## `sample_uid` derivation

Samples are labelled with a short, deterministic code derived in
`gui.db.sample_uid(job_id, sample)`:

- It is a UUIDv5 of `"<job_id>/<sample>"` under a fixed namespace, so the same
  `(job, barcode)` always yields the same code on any machine — never random,
  never reused.
- Format: `GHA-XXXXXX`, where `XXXXXX` is the first six uppercase hex characters
  of the digest (~16M space) — short enough to print on a tube label.

This disambiguates barcodes reused across sequencing runs (the same
`barcode01` can mean different samples in different runs).

## Indexes

Declared alongside the tables in `SCHEMA`:

- `idx_sample_meta_region` on `sample_meta(region)`.
- `idx_sample_audit_job_sample` on `sample_meta_audit(job_id, sample)`.

## Migrations

`CREATE TABLE IF NOT EXISTS` never alters an existing table, so columns added
after a user's DB was first created are back-filled in `_migrate()` with
PRAGMA-guarded `ALTER TABLE ... ADD COLUMN` steps (idempotent, no-ops on a fresh
DB):

- `sample_meta.internal_id`
- `job_config.execution_target`
- `job_config.clair3_model`
