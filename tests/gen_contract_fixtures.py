#!/usr/bin/env python3
"""Regenerate the cross-service contract fixtures under ``contracts/``.

These fixtures pin the *wire shape* of the desktop -> backend cloud-submit
contract so the two independently-tested services can't silently drift. The
backend's cross-origin dedupe test consumes ``request_body`` from the emitted
JSON verbatim instead of hand-rolling the cloud submit dict, so a field-name or
nesting change on either side turns a green suite red.

Each body is produced by the **real** desktop serializers -- there is no
hand-written shape here:

    gui.cloud_queue._CloudWorker._provenance_block  (local rows -> block)
    gui.cloud_client.CloudClient.submit_run         (block -> POST /runs body)
    gui.metadata_sync.build_metadata_payload        (sample_meta rows -> samples)
    gui.cloud_client.CloudClient.put_sample_metadata(samples -> PUT body)

Run from the repo root::

    python tests/gen_contract_fixtures.py

then commit the changed ``tests/contracts/*.json``. Do not hand-edit the JSON.
"""

import json
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gui.cloud_client as cc      # noqa: E402
import gui.cloud_queue as cq       # noqa: E402
import gui.metadata_sync as ms     # noqa: E402
import gui.sync as sync            # noqa: E402

_CONTRACTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "contracts")
_OUT_SUBMIT = os.path.join(_CONTRACTS, "cloud_submit_run.golden.json")
_OUT_METADATA = os.path.join(_CONTRACTS, "sample_metadata_upsert.golden.json")
_OUT_SYNC = os.path.join(_CONTRACTS, "local_run_sync.golden.json")

# Canonical golden run: a real ONT-HEADER run with two barcodes (one
# homogeneous, one not) so every provenance/sample field is exercised.
_JOB = {
    "id": "job-golden-0001",
    "input_fingerprint":
        "3f2a1c9e7b4d6a8f0c2e5d7a9b1f3e5c"
        "7a9d1b3f5e7c9a1d3b5f7e9c1a3d5b7f",
}
_PARAMS = {"min_qual": 10, "min_dp": 20, "min_mq": 30,
           "qc_tool": "nanostat", "threads": 8}
_MODEL_ID = "11111111-1111-1111-1111-111111111111"
_INPUT_S3_KEY = "orgs/acme/inputs/job-golden-0001/input.tar.gz"
_RUN_PROV = {
    "provenance_source": "HEADER",
    "sequencing_run_id": "b6a1e0f2c3d4e5f60718293a4b5c6d7e8f90a1b2",
    "flow_cell_id": "FAX00000",
    "protocol_group_id": "malaria_surveillance_2024",
    "run_start_time": "2024-05-14T09:12:33Z",
    "basecall_model": "dna_r9.4.1_450bps_sup",
}
_SAMPLE_PROV = {
    "barcode01": {"barcode_alias": "MW-KA-001", "sample_id": "MW-KA-001",
                  "homogeneous": 1},
    "barcode02": {"barcode_alias": "MW-KA-002", "sample_id": "MW-KA-002",
                  "homogeneous": 0},
}

_COMMENT = (
    "GOLDEN CONTRACT FIXTURE - cross-service dedupe. Produced by the desktop "
    "client's REAL serializers (gui.cloud_queue._CloudWorker._provenance_block "
    "+ gui.cloud_client.CloudClient.submit_run). The backend cross-origin "
    "dedupe test MUST consume request_body verbatim instead of hand-rolling "
    "the cloud submit dict. Any field-name or nesting drift on either side "
    "turns a green suite red. Regenerate with scripts/gen_contract_fixtures.py; "
    "do not hand-edit.")


def build_golden():
    """Return the golden fixture dict driven by the real serializers."""
    # _provenance_block reads the local rows via the module-level ``db``; stub
    # it to the canonical rows so no real DB/session is needed.
    cq.db = types.SimpleNamespace(
        get_run_provenance=lambda jid: _RUN_PROV,
        list_sample_provenance=lambda jid: _SAMPLE_PROV,
    )
    block = cq._CloudWorker._provenance_block(types.SimpleNamespace(), _JOB)

    # Capture the exact POST /runs body submit_run puts on the wire.
    captured = {}

    def _fake_request(method, path, token=None, json_body=None, **kwargs):
        captured["method"] = method
        captured["path"] = path
        captured["body"] = json_body
        return {"id": "run-golden"}

    cc._request = _fake_request
    client = cc.CloudClient.__new__(cc.CloudClient)
    client._token = lambda: "test-token"
    client._p = lambda s: "/pipeline" + s
    client.submit_run(_MODEL_ID, _INPUT_S3_KEY, _PARAMS, **block)

    return {
        "_comment": _COMMENT,
        "endpoint": {"method": captured["method"], "path": captured["path"]},
        "request_body": captured["body"],
        "provenance_block": block,
        "source_rows": {
            "job": _JOB,
            "params": _PARAMS,
            "model_id": _MODEL_ID,
            "input_s3_key": _INPUT_S3_KEY,
            "run_provenance": _RUN_PROV,
            "sample_provenance": _SAMPLE_PROV,
        },
    }


# Canonical server run id the metadata upsert targets (a real PipelineRun id on
# the backend; here a fixed UUID so the fixture is stable).
_METADATA_RUN_ID = "22222222-2222-2222-2222-222222222222"

# Canonical sample_meta rows, shaped exactly as db.list_sample_meta values:
# a full row (every geo/date/label field populated) plus a sparse row (only the
# required barcode+updated_at, everything else null) so the fixture exercises
# both the fully-populated and the mostly-omitted wire shapes. updated_at is the
# epoch the LWW merge key is derived from; _iso turns it into the wire string.
_METADATA_COMMENT = (
    "GOLDEN CONTRACT FIXTURE - sample-metadata upsert. Produced by the desktop "
    "client's REAL serializers (gui.metadata_sync.build_metadata_payload + "
    "gui.cloud_client.CloudClient.put_sample_metadata). The backend "
    "sample-metadata upsert test MUST consume request_body verbatim instead of "
    "hand-rolling the samples list. alias -> sample_alias and internal_id -> "
    "sample_internal_id on the wire (namespaced off provenance); updated_at is "
    "the per-row last-write-wins key. Any field-name or nesting drift on either "
    "side turns a green suite red. Regenerate with "
    "scripts/gen_contract_fixtures.py; do not hand-edit.")

_META_ROWS = [
    {
        "sample": "barcode01",
        "region": "Southern",
        "district": "Zomba",
        "latitude": -15.3833,
        "longitude": 35.3188,
        "collection_date": "2024-05-10",
        "case_class": "Uncomplicated",
        "age_years": 7,
        "alias": "MW-ZA-001",
        "internal_id": "ZA-2024-001",
        "notes": "Febrile patient, <b>day 3</b> follow-up.",
        "updated_at": 1715731200.0,
    },
    {
        "sample": "barcode02",
        "region": None,
        "district": None,
        "latitude": None,
        "longitude": None,
        "collection_date": None,
        "case_class": None,
        "age_years": None,
        "alias": None,
        "internal_id": None,
        "notes": None,
        "updated_at": 1715734800.0,
    },
]


def build_metadata_golden():
    """Return the sample-metadata golden fixture driven by the real serializers."""
    warnings = []
    samples = ms.build_metadata_payload(_META_ROWS, on_warn=warnings.append)

    # Capture the exact PUT body put_sample_metadata puts on the wire.
    captured = {}

    def _fake_request(method, path, token=None, json_body=None, **kwargs):
        captured["method"] = method
        captured["path"] = path
        captured["body"] = json_body
        return {"upserted": len(samples)}

    cc._request = _fake_request
    client = cc.CloudClient.__new__(cc.CloudClient)
    client._token = lambda: "test-token"
    client._p = lambda s: "/pipeline" + s
    client.put_sample_metadata(_METADATA_RUN_ID, samples)

    return {
        "_comment": _METADATA_COMMENT,
        "endpoint": {"method": captured["method"], "path": captured["path"]},
        "request_body": captured["body"],
        "source_rows": {
            "run_id": _METADATA_RUN_ID,
            "sample_meta": _META_ROWS,
        },
    }


# Canonical local-run-sync source rows. The job carries a real 64-hex
# input_fingerprint and the config a real clair3_model, so build_sync_payload
# emits both required (non-nullable) fields non-null off the row values alone --
# the defensive fallbacks never fire, keeping the fixture self-contained.
_SYNC_JOB = {
    "id": "job-golden-0001",
    "input_fingerprint":
        "3f2a1c9e7b4d6a8f0c2e5d7a9b1f3e5c"
        "7a9d1b3f5e7c9a1d3b5f7e9c1a3d5b7f",
    "finished_at": 1715738400.0,
    "output_dir": "/local/runs/job-golden-0001",
    "log_path": "/local/logs/job-golden-0001.log",
}
_SYNC_CFG = {
    "clair3_model": "r941_prom_sup_g5014",
    "fastq_dir": "/local/inputs/job-golden-0001/fastq_pass",
    "reference_set": "PlasmoDB-68 (genome)",
    "threads": 8,
    "min_qual": 10,
    "min_dp": 20,
    "min_mq": 30,
}
# The artifact set a completed run produces (see sync.discover_artifacts): the
# three final_reports CSVs, the assembled QC tarball, and the run log.
_SYNC_ARTIFACTS = {
    "resistance_calls": "/local/runs/job-golden-0001/final_reports/"
                        "resistance_calls.csv",
    "variant_detail": "/local/runs/job-golden-0001/final_reports/"
                      "variant_detail.csv",
    "coverage_report": "/local/runs/job-golden-0001/final_reports/"
                       "coverage_report.csv",
    "qc_report": "/tmp/pf_sync_golden/qc.tar.gz",
    "log": "/local/logs/job-golden-0001.log",
}
_SYNC_MANIFEST = {
    "reference_release": "PlasmoDB-68",
    "catalog_version": "2024.1",
    "pipeline_commit": "abc1234",
}

_SYNC_COMMENT = (
    "GOLDEN CONTRACT FIXTURE - local-run sync. Produced by the desktop "
    "client's REAL serializers (gui.sync.build_sync_payload + "
    "gui.cloud_client.CloudClient.sync_local_run). The backend local-run-sync "
    "test MUST consume request_body verbatim instead of hand-rolling the "
    "LocalRunSyncCreate body. input_fingerprint and model_name are "
    "non-nullable on the backend; build_sync_payload guarantees both non-null "
    "(model_name falls back to the pipeline's runtime-default clair3 model, "
    "input_fingerprint recomputes from the config when the job row lacks one). "
    "Any field-name or nesting drift on either side turns a green suite red. "
    "Regenerate with scripts/gen_contract_fixtures.py; do not hand-edit.")


def build_sync_golden():
    """Return the local-run-sync golden fixture driven by the real serializers."""
    payload = sync.build_sync_payload(
        _SYNC_JOB, _SYNC_CFG, _RUN_PROV, _SAMPLE_PROV,
        _SYNC_ARTIFACTS, _SYNC_MANIFEST)

    # Capture the exact POST /runs/sync body sync_local_run puts on the wire.
    captured = {}

    def _fake_request(method, path, token=None, json_body=None, **kwargs):
        captured["method"] = method
        captured["path"] = path
        captured["body"] = json_body
        return {"run": {"id": "run-golden"}, "upload_urls": [],
                "expires_in": 3600}

    cc._request = _fake_request
    client = cc.CloudClient.__new__(cc.CloudClient)
    client._token = lambda: "test-token"
    client._p = lambda s: "/pipeline" + s
    client.sync_local_run(payload)

    return {
        "_comment": _SYNC_COMMENT,
        "endpoint": {"method": captured["method"], "path": captured["path"]},
        "request_body": captured["body"],
        "source_rows": {
            "job": _SYNC_JOB,
            "config": _SYNC_CFG,
            "run_provenance": _RUN_PROV,
            "sample_provenance": _SAMPLE_PROV,
            "artifacts": _SYNC_ARTIFACTS,
            "manifest": _SYNC_MANIFEST,
        },
    }


def _serialize(golden):
    return json.dumps(golden, indent=2, sort_keys=True) + "\n"


# Every fixture this generator owns: (path, builder). Adding a contract here
# means it is written and --check-ed alongside the rest.
_FIXTURES = (
    (_OUT_SUBMIT, build_golden),
    (_OUT_METADATA, build_metadata_golden),
    (_OUT_SYNC, build_sync_golden),
)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    # --check: fail (non-zero) if any committed fixture has drifted from what
    # the live serializers now produce, so a desktop-side field/nesting change
    # turns CI red instead of silently breaking prod. Mirrors the backend
    # consuming request_body verbatim.
    if "--check" in argv:
        drift = False
        for out, build in _FIXTURES:
            fresh = _serialize(build())
            try:
                with open(out) as fh:
                    committed = fh.read()
            except OSError:
                print("MISSING %s - run without --check to create it"
                      % os.path.relpath(out))
                drift = True
                continue
            if committed != fresh:
                print("DRIFT: %s is stale - regenerate with "
                      "`python scripts/gen_contract_fixtures.py`"
                      % os.path.relpath(out))
                drift = True
                continue
            print("OK: %s matches the live serializers" % os.path.relpath(out))
        return 1 if drift else 0
    for out, build in _FIXTURES:
        fresh = _serialize(build())
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "w") as fh:
            fh.write(fresh)
        print("wrote %s" % os.path.relpath(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
