"""Sync a locally-executed, successful run into the cloud surveillance surface.

The desktop runs the Clair3 pipeline locally; this module ingests a *completed*
job into the server via the local-run sync contract:

    initiate  -> POST /pipeline/runs/sync      (register run, get presigned PUTs)
    upload    -> PUT each result artifact to its presigned URL
    finalize  -> POST /pipeline/runs/sync/{run_id}/finalize

State is tracked per local job in the ``run_sync`` table (the local job id is
the server's ``client_run_id`` idempotency key), so a crashed sync resumes and
the whole thing is safe to retry: the server upsert is idempotent on
``client_run_id``.

Local artifact layout is the shipped pipeline's
(``bin/pf-drug-resistance-pipeline.sh``): the three CSVs under
``<output_dir>/final_reports/``, QC assembled on the fly from
``<output_dir>/qc_trimmed`` (+ ``qc_raw`` when pretrim QC ran), and the run log
from the job's ``log_path``. The server assigns its own deterministic S3 keys,
so only the artifact *names* cross the wire — never local paths.
"""

import datetime
import json
import os
import shutil
import tarfile
import tempfile
import time

from . import cloud_client, config_bridge, db, ref_catalog

# Artifact name -> the Content-Type the server signed the presigned PUT with.
# These MUST be sent verbatim on upload or S3 returns SignatureDoesNotMatch.
ARTIFACT_CONTENT_TYPES = {
    "resistance_calls": "text/csv",
    "variant_detail": "text/csv",
    "coverage_report": "text/csv",
    "qc_report": "application/gzip",
    "log": "text/plain",
}

# The three combined CSVs the pipeline writes under <output_dir>/final_reports/.
_FINAL_REPORT_CSVS = {
    "resistance_calls": "resistance_calls.csv",
    "variant_detail": "variant_detail.csv",
    "coverage_report": "coverage_report.csv",
}

# PipelineRunParams keys the server persists (pydantic extra="ignore" drops the
# rest). reference_set is intentionally NOT here — it isn't a params field; its
# identity already rides in input_fingerprint (and, for audit, the manifest).
_PARAM_KEYS = ("threads", "min_qual", "min_dp", "min_mq")


def _iso(epoch):
    """Epoch seconds -> RFC3339/ISO-8601 UTC string, or ``None``."""
    if not epoch:
        return None
    return (datetime.datetime.fromtimestamp(
        epoch, tz=datetime.timezone.utc).isoformat().replace("+00:00", "Z"))


def _build_qc_tar(output_dir, dest_dir):
    """Assemble ``qc.tar.gz`` mirroring the cloud path's ``_archive_qc``.

    Adds ``qc_trimmed/`` (always produced) and ``qc_raw/`` (only when pretrim
    QC ran), each with its directory name as the top-level arcname. Returns the
    tarball path, or ``None`` if neither QC dir exists.
    """
    present = [d for d in ("qc_trimmed", "qc_raw")
               if os.path.isdir(os.path.join(output_dir, d))]
    if not present:
        return None
    dest = os.path.join(dest_dir, "qc.tar.gz")
    with tarfile.open(dest, "w:gz") as tar:
        for d in present:
            tar.add(os.path.join(output_dir, d), arcname=d)
    return dest


def discover_artifacts(output_dir, log_path=None, work_dir=None):
    """Map present artifact names to local file paths for a completed run.

    Locates the three ``final_reports`` CSVs, the run ``log`` (from the job's
    ``log_path``, which lives outside ``output_dir``), and assembles
    ``qc_report`` into ``work_dir``. Only artifacts that actually exist are
    returned, so ``artifacts`` on the wire reflects reality.
    """
    work_dir = work_dir or tempfile.mkdtemp(prefix="pf_sync_")
    found = {}
    final_reports = os.path.join(output_dir, "final_reports")
    for name, filename in _FINAL_REPORT_CSVS.items():
        p = os.path.join(final_reports, filename)
        if os.path.isfile(p):
            found[name] = p
    if log_path and os.path.isfile(log_path):
        found["log"] = log_path
    qc = _build_qc_tar(output_dir, work_dir)
    if qc:
        found["qc_report"] = qc
    return found


def _load_manifest(output_dir):
    """Opaque manifest passthrough: the pipeline's own provenance/manifest JSON.

    The server stores ``manifest`` as nullable JSON it never parses, so this is
    a best-effort audit snapshot (reference release, catalog version, commit).
    This build emits ``provenance.json``; a ``manifest.json`` build is also
    accepted. Returns a dict or ``None``.
    """
    for name in ("provenance.json", "manifest.json"):
        p = os.path.join(output_dir, name)
        if os.path.isfile(p):
            try:
                with open(p) as fh:
                    return json.load(fh)
            except (OSError, ValueError):
                return None
    return None


def build_sync_payload(job, cfg, run_prov, sample_prov, artifacts,
                       manifest=None):
    """Assemble the ``LocalRunSyncCreate`` body from local rows.

    ``job`` is the local job row (its ``id`` is ``client_run_id`` and
    ``input_fingerprint`` carries the manifest+params hash), ``cfg`` the config
    row, ``run_prov``/``sample_prov`` the extracted provenance, ``artifacts``
    the discovered ``{name: path}`` map.
    """
    params = {k: cfg.get(k) for k in _PARAM_KEYS if cfg.get(k) is not None}
    rp = run_prov or {}
    provenance = {
        "source": rp.get("provenance_source", "FINGERPRINT_ONLY"),
        "sequencing_run_id": rp.get("sequencing_run_id"),
        "flow_cell_id": rp.get("flow_cell_id"),
        "protocol_group_id": rp.get("protocol_group_id"),
        "run_start_time": rp.get("run_start_time"),
        "basecall_model": rp.get("basecall_model"),
    }
    samples = [
        {"barcode": bc,
         "barcode_alias": rec.get("barcode_alias"),
         "sample_id": rec.get("sample_id"),
         "homogeneous": bool(rec.get("homogeneous", 1))}
        for bc, rec in sorted((sample_prov or {}).items())]
    # Both fields are non-nullable on the backend LocalRunSyncCreate. Guard
    # against a null config/row so the serializer can never emit null: fall back
    # to the pipeline's own runtime default model, and recompute the input
    # fingerprint from the config when the job row lacks one.
    model_name = cfg.get("clair3_model") or config_bridge.DEFAULT_CLAIR3_MODEL
    input_fingerprint = job.get("input_fingerprint") or db.compute_input_fingerprint(
        cfg.get("fastq_dir"), cfg)
    # Reference-set provenance. The name is always sent (it is the source of
    # truth the server matches/backfills on); the bundle id is sent only when we
    # actually have it (a cloud-published set), else omitted so the server
    # backfills it later by name. Bundled-defaults / legacy names collapse to
    # the native constant.
    reference_version = config_bridge.resolve_reference_version(
        cfg.get("reference_set"))
    reference_set_id = ref_catalog.id_for(reference_version)
    payload = {
        "client_run_id": job["id"],
        "status": "SUCCEEDED",
        "model_name": model_name,
        "params": params,
        "input_fingerprint": input_fingerprint,
        "sample_count": len(samples) or None,
        "manifest": manifest,
        "finished_at": _iso(job.get("finished_at")),
        "provenance": provenance,
        "samples": samples,
        "artifacts": sorted(artifacts),
        "reference_version": reference_version,
    }
    if reference_set_id:
        payload["reference_set_id"] = reference_set_id
    return payload


def sync_job(session, job_id, path=None, on_status=None):
    """Sync one completed local job to the cloud; return the server run dict.

    Drives initiate -> upload -> finalize, advancing the ``run_sync`` row at
    each step. Idempotent and resumable via ``client_run_id``. Raises on
    failure after recording ``FAILED`` + ``last_error``; the caller decides
    whether to surface or retry. ``on_status(status)`` is an optional UI hook.
    """
    def _status(status, **fields):
        db.upsert_run_sync(job_id, path=path, sync_status=status, **fields)
        if on_status:
            on_status(status)

    job = db.get_job(job_id, path)
    if job is None:
        raise ValueError("no such job: %s" % job_id)
    if job.get("status") != "completed":
        raise ValueError("only completed runs can be synced")
    cfg = db.get_config(job["config_id"], path)
    run_prov = db.get_run_provenance(job_id, path)
    sample_prov = db.list_sample_provenance(job_id, path)

    work_dir = tempfile.mkdtemp(prefix="pf_sync_")
    try:
        artifacts = discover_artifacts(
            job["output_dir"], job.get("log_path"), work_dir)
        manifest = _load_manifest(job["output_dir"])
        payload = build_sync_payload(
            job, cfg, run_prov, sample_prov, artifacts, manifest)

        client = cloud_client.CloudClient(session)
        try:
            _status("INITIATED")
            resp = client.sync_local_run(payload)
            server_run_id = resp["run"]["id"]
            upload_urls = {u["artifact"]: u["url"]
                           for u in resp.get("upload_urls", [])}
            expires_at = None
            if resp.get("expires_in"):
                expires_at = time.time() + resp["expires_in"]

            state = {name: {"uploaded": False} for name in artifacts}
            _status("UPLOADING", server_run_id=server_run_id,
                    expires_at=expires_at, artifacts_json=json.dumps(state),
                    last_error=None)

            for name, local_path in artifacts.items():
                url = upload_urls.get(name)
                if not url:
                    continue
                etag = cloud_client.put_artifact(
                    url, local_path, ARTIFACT_CONTENT_TYPES[name])
                state[name] = {"uploaded": True, "etag": etag}
                db.upsert_run_sync(job_id, path=path,
                                   artifacts_json=json.dumps(state))

            _status("UPLOADED")
            run = client.finalize_local_run(
                server_run_id, sorted(artifacts))
            _status("FINALIZED")
            return run
        except Exception as exc:
            _status("FAILED", last_error=str(exc))
            raise
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
