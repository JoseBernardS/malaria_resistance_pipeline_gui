"""Translate GUI job settings into a per-run pipeline ``.conf`` file.

The bash pipeline sources a single config file. Rather than mutate the
shared ``config/pipeline.conf``, we read it as a base, apply the per-job
overrides coming from the GUI, and write a fresh temp ``.conf`` into the
writable user-data dir. The runner then points the pipeline at it via the
``PIPELINE_CONFIG`` env var / first CLI arg.

GUI field  ->  conf key:
    FASTQ dir       -> FASTQ_PASS_DIR
    Output dir      -> OUTPUT_DIR
    Threads         -> THREADS
    Min QUAL        -> MIN_QUAL
    Min DP          -> MIN_DP
    Min MAPQ        -> MIN_MQ
    Reference set   -> REFERENCE / ORIGINAL_BED_FILE / RESISTANCE_CATALOG
                       (+ ANNOTATION_GFF where the set defines one)
"""

import os
import re
import time

from . import paths
from . import ref_catalog
from . import refsets

# Canonical name stamped on runs that used the pipeline's bundled-in reference
# data (no custom bundle selected). MUST match the backend's
# ``settings.NATIVE_REFERENCE_SET_NAME`` verbatim: the server resolves/backfills
# a run's ``reference_set_id`` by exact-name match against a bundle registered
# under this name, so a typo would silently orphan provenance. Never hand-typed.
NATIVE_REFERENCE_SET_NAME = "WHO 2025 / PlasmoDB-68"

# Names older builds recorded for the same bundled defaults; mapped onto the
# native constant at provenance time so their runs still resolve/backfill.
_LEGACY_NATIVE_NAMES = {"PlasmoDB-68 (genome)"}

# Reference-set presets. Paths are relative to the app root and resolved to
# absolute when written, so they work both from source and inside a bundle.
# The native set mirrors the shipped default config.
REFERENCE_SETS = {
    NATIVE_REFERENCE_SET_NAME: {
        "REFERENCE":
            "data/external/pf-ref/genome/"
            "PlasmoDB-68_Pfalciparum3D7_Genome.fasta",
        "ORIGINAL_BED_FILE":
            "data/interim/targets/pf_snp_targets.PlasmoDB-68.bed",
        "ANNOTATION_GFF":
            "data/external/pf-ref/annotation/"
            "PlasmoDB-68_Pfalciparum3D7.gff",
        "RESISTANCE_CATALOG":
            "data/interim/catalog/pf_resistance_catalog.PlasmoDB-68.tsv",
    },
}

DEFAULT_REFERENCE_SET = NATIVE_REFERENCE_SET_NAME

# Fallback Clair3 model name (mirrors config/pipeline.conf). Threaded into the
# per-run .conf as CLAIR3_MODEL; the bash pipeline resolves the full path from
# this name under data/clair3_models/.
DEFAULT_CLAIR3_MODEL = "r941_prom_sup_g5014"

# GUI scalar field -> conf key
SCALAR_KEYS = {
    "threads": "THREADS",
    "min_qual": "MIN_QUAL",
    "min_dp": "MIN_DP",
    "min_mq": "MIN_MQ",
}


def reference_set_names():
    """Builtin set names first, then the names in the local synced catalog.

    The catalog lives in a local config file (:mod:`gui.ref_catalog`) that the
    user can refresh from the cloud on demand, so this list is available fully
    offline and stays out of any per-job record.
    """
    names = list(REFERENCE_SETS.keys())
    for n in ref_catalog.list_names():
        if n not in names:
            names.append(n)
    return names


def default_reference_set_name():
    """The chosen default reference set, or the builtin default.

    Reads the local catalog file's ``default`` pointer, falling back to
    :data:`DEFAULT_REFERENCE_SET` when nothing is chosen or the chosen name no
    longer resolves (e.g. a preset that is no longer offered).
    """
    name = ref_catalog.default_name()
    if name and name in reference_set_names():
        return name
    return DEFAULT_REFERENCE_SET


def set_default_reference_set(name):
    """Persist ``name`` as the app-wide default in the local catalog file."""
    ref_catalog.set_default_name(name)


def resolve_reference_version(name):
    """The provenance name to stamp on a run for reference set ``name``.

    Empty (bundled defaults, no set selected) or a legacy alias for those
    defaults collapses to :data:`NATIVE_REFERENCE_SET_NAME`; any other selected
    set name passes through unchanged. This is the exact string the backend
    matches on to resolve/backfill ``reference_set_id``.
    """
    if not name or name in _LEGACY_NATIVE_NAMES:
        return NATIVE_REFERENCE_SET_NAME
    return name


def clair3_model_names():
    """Local Clair3 model names for the picker.

    Reads the on-disk registry via ``paths.list_clair3_models()`` and falls
    back to the shipped default when the registry is empty, so the combo is
    never blank.
    """
    names = paths.list_clair3_models()
    return names if names else [DEFAULT_CLAIR3_MODEL]


def _abs(path):
    if not path:
        return path
    return path if os.path.isabs(path) else os.path.join(paths.app_root(), path)


def read_base_config():
    """Parse the shipped conf into an ordered list of (raw_line, key) tuples
    so we can rewrite values in place and preserve comments/order."""
    lines = []
    base = paths.base_config()
    if os.path.isfile(base):
        with open(base) as fh:
            for line in fh:
                m = re.match(r'^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=', line)
                key = m.group(1) if m else None
                lines.append((line.rstrip("\n"), key))
    return lines


def _format_value(value):
    """Quote strings, leave bare numbers/bools unquoted (shell-friendly)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return '"%s"' % value


def build_overrides(fastq_dir, output_dir, reference_set, threads,
                    min_qual, min_dp, min_mq, extra=None, clair3_model=None):
    """Return an ordered dict of conf-key -> formatted value string."""
    overrides = {}
    overrides["FASTQ_PASS_DIR"] = _format_value(_abs(fastq_dir))
    overrides["OUTPUT_DIR"] = _format_value(output_dir)

    # Reference lookup: a builtin preset first (paths relative to app_root, so
    # they need _abs), else a user-imported set (registry paths already
    # absolute), falling back to the builtin default. ``_resolved`` marks a
    # user set so we skip the _abs() join on its already-absolute paths.
    if reference_set in REFERENCE_SETS:
        ref = REFERENCE_SETS[reference_set]
        resolved = False
    else:
        ref = refsets.resolve(reference_set)
        resolved = ref is not None
        if ref is None:
            ref = REFERENCE_SETS[DEFAULT_REFERENCE_SET]
    abspath = (lambda p: p) if resolved else _abs
    overrides["REFERENCE"] = _format_value(abspath(ref["REFERENCE"]))
    overrides["ORIGINAL_BED_FILE"] = _format_value(abspath(ref["ORIGINAL_BED_FILE"]))
    overrides["RESISTANCE_CATALOG"] = _format_value(abspath(ref["RESISTANCE_CATALOG"]))
    if ref.get("ANNOTATION_GFF"):
        overrides["ANNOTATION_GFF"] = _format_value(abspath(ref["ANNOTATION_GFF"]))
    # Human-readable provenance label the pipeline stamps into the run manifest.
    # Bundled-defaults / legacy names collapse to the native constant — the exact
    # string the backend resolves/backfills reference_set_id on.
    overrides["REFERENCE_SET_VERSION"] = _format_value(
        resolve_reference_version(reference_set))

    overrides["THREADS"] = _format_value(int(threads))
    overrides["MIN_QUAL"] = _format_value(int(min_qual))
    overrides["MIN_DP"] = _format_value(int(min_dp))
    overrides["MIN_MQ"] = _format_value(int(min_mq))

    # Model: resolve the name to an absolute dir (user registry shadows
    # bundled), so a user-imported model in the writable user-data dir works
    # inside a read-only bundle. Falls back to the bare name if it can't be
    # resolved — the bash pipeline still looks it up under $PROJECT_ROOT.
    if clair3_model:
        overrides["CLAIR3_MODEL"] = _format_value(
            paths.resolve_clair3_model(clair3_model) or clair3_model)

    for key, value in (extra or {}).items():
        overrides[key] = _format_value(value)
    return overrides


def write_run_config(fastq_dir, output_dir, reference_set, threads,
                     min_qual, min_dp, min_mq, extra=None, job_id=None,
                     clair3_model=None):
    """Write a per-run .conf into the user-data configs dir; return its path."""
    overrides = build_overrides(
        fastq_dir, output_dir, reference_set, threads,
        min_qual, min_dp, min_mq, extra=extra, clair3_model=clair3_model)

    base_lines = read_base_config()
    seen = set()
    out_lines = []
    for raw, key in base_lines:
        if key and key in overrides:
            out_lines.append("%s=%s" % (key, overrides[key]))
            seen.add(key)
        else:
            out_lines.append(raw)

    # Append any override keys that were not present in the base config.
    extras = [k for k in overrides if k not in seen]
    if extras:
        out_lines.append("")
        out_lines.append("# --- GUI per-run overrides ---")
        for k in extras:
            out_lines.append("%s=%s" % (k, overrides[k]))

    tag = job_id if job_id is not None else int(time.time())
    out_path = os.path.join(paths.configs_dir(), "job_%s.conf" % tag)
    with open(out_path, "w") as fh:
        fh.write("\n".join(out_lines) + "\n")
    return out_path
