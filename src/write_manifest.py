#!/usr/bin/env python3
"""
write_manifest.py — emit the run's manifest into the output dir.

The pipeline's completed output folder is the immutable record of "what
produced this". This script stamps a single ``manifest.json`` at the output root
so downstream consumers can read straight from the artifact, independent of any
database. It is the ONE writer for every outcome (success / partial / error),
called by ``emit_manifest`` in the bash pipeline (and by its error trap), so the
JSON shape is identical however a run ends.

The manifest merges two concerns into one file:

  - Run status (read by the backend poller as a completion/outcome signal):
    top-level ``status`` (success|partial|error), ``stage`` (last stage
    reached), ``sample_count``, ``samples`` and ``outputs``.
  - Provenance (read by the desktop GUI Results view and forwarded opaquely by
    the local-run sync as the run's audit snapshot): top-level
    ``reference_version`` plus the nested ``reference`` / ``variant_calling`` /
    ``filters`` / ``qc`` blocks.

The top-level ``reference_version`` is the offline-capable provenance token —
the human-readable reference-set name the backend matches/backfills a run's
``reference_set_id`` on. Values are read from the environment (the caller — the
bash pipeline — passes the resolved shell variables), so this stays a thin,
side-effect-free writer. ``status``/``stage``/``samples``/pdf outputs come from
the run-state ``MANIFEST_*`` env vars the ``emit_manifest`` wrapper sets.

Usage:
    write_manifest.py <output_path>

Never hard-fails the run: on any error it prints a warning and exits 0 so a
missing manifest degrades gracefully rather than sinking an otherwise good run.
"""

import json
import os
import sys
from datetime import datetime, timezone

# Native default, kept in lockstep with config/pipeline.conf and
# gui.config_bridge.NATIVE_REFERENCE_SET_NAME.
_NATIVE_REFERENCE_SET_VERSION = "WHO 2025 / PlasmoDB-68"


def _env(name):
    """Return a stripped env value, or None when unset/blank."""
    value = os.environ.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _int(name):
    raw = _env(name)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _float(name):
    raw = _env(name)
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def build_manifest(out_dir):
    reference_version = _env("REFERENCE_SET_VERSION") or _NATIVE_REFERENCE_SET_VERSION
    # Run-state fields set by the emit_manifest wrapper. samples/pdfs are passed
    # as space-joined strings (barcodes and file paths never contain spaces).
    samples = (_env("MANIFEST_SAMPLES") or "").split()
    pdf_reports = (_env("MANIFEST_PDFS") or "").split()
    final_dir = os.path.join(out_dir, "final_reports")
    return {
        "schema": "pf-manifest/1",
        # --- Run status (read by the backend poller as a completion signal) ---
        "status": _env("MANIFEST_STATUS") or "success",
        "stage": _env("MANIFEST_STAGE"),
        "run_started": _env("MANIFEST_RUN_STARTED"),
        "run_finished": datetime.now(timezone.utc).isoformat(),
        "generated": datetime.now(timezone.utc).isoformat(),
        "sample_count": len(samples),
        "samples": samples,
        "report_mode": _env("REPORT_MODE"),
        "outputs": {
            "final_reports_dir": final_dir,
            "resistance_calls_csv": os.path.join(final_dir, "resistance_calls.csv"),
            "variant_detail_csv": os.path.join(final_dir, "variant_detail.csv"),
            "coverage_report_csv": os.path.join(final_dir, "coverage_report.csv"),
            "pdf_reports": pdf_reports,
        },
        # --- Provenance (read by the GUI Results view; forwarded by sync) ---
        # Offline-capable provenance token (the reference-set name). Kept at the
        # top level so consumers don't need to know the nested layout.
        "reference_version": reference_version,
        "reference": {
            "version": reference_version,
            "genome": _env("REFERENCE"),
            "targets_bed": _env("ORIGINAL_BED_FILE"),
            "annotation_gff": _env("ANNOTATION_GFF"),
            "resistance_catalog": _env("RESISTANCE_CATALOG"),
        },
        "variant_calling": {
            "caller": "Clair3",
            "clair3_model": _env("CLAIR3_MODEL"),
            "clair3_qual": _int("CLAIR3_QUAL"),
            "min_coverage": _int("MIN_COVERAGE"),
        },
        "filters": {
            "min_qual": _int("MIN_QUAL"),
            "min_dp": _int("MIN_DP"),
            "min_mapping_quality": _int("MIN_MQ"),
            "min_read_length": _int("MIN_READ_LENGTH"),
            "max_read_length": _int("MAX_READ_LENGTH"),
            "coverage_min_breadth": _float("COV_MIN_BREADTH"),
        },
        "qc": {
            "qc_tool": _env("QC_TOOL"),
        },
    }


def main():
    if len(sys.argv) != 2:
        sys.stderr.write("usage: write_manifest.py <output_path>\n")
        return 0  # non-fatal: don't sink the run over a manifest arg slip
    out_path = sys.argv[1]
    # The manifest sits at the output root; derive the output dir from its path
    # so callers don't pass it twice.
    out_dir = os.path.dirname(out_path) or "."
    try:
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
        # Write atomically (temp + replace) so a reader never sees a half file.
        tmp_path = "%s.tmp" % out_path
        with open(tmp_path, "w") as fh:
            json.dump(build_manifest(out_dir), fh, indent=2)
        os.replace(tmp_path, out_path)
    except OSError as exc:
        sys.stderr.write("WARNING: failed to write manifest: %s\n" % exc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
