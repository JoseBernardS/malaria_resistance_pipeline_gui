"""Resolve bundle-relative resources and the writable user-data directory.

Works both when running from source (``python -m gui.app`` inside the repo)
and from inside a packaged ``.app``/AppImage where the repo lives in
``Contents/Resources/app`` and the relocatable conda env in
``Contents/Resources/env``.

The key idea: everything *read-only* (pipeline script, refs, model, base
config) is resolved relative to the app root, while everything *writable*
(SQLite DB, default job outputs, temp per-run configs) lives under the
platform user-data directory so it survives the read-only bundle and
macOS app-translocation.
"""

import glob
import os
import subprocess
import sys

APP_NAME = "PfDrugResistance"
ORG_NAME = "PfDrugResistance"


def app_root():
    """Directory that holds gui/, src/, bin/, config/, data/.

    From source this is the repo root (parent of the ``gui`` package).
    Inside a bundle it is ``Contents/Resources/app`` (still the parent of
    ``gui``), so the same logic works in both cases.
    """
    here = os.path.dirname(os.path.abspath(__file__))   # .../app/gui
    return os.path.dirname(here)                          # .../app


def pipeline_script():
    return os.path.join(app_root(), "bin", "pf-drug-resistance-pipeline.sh")


def base_config():
    return os.path.join(app_root(), "config", "pipeline.conf")


def annotation_gff():
    """The bundled PlasmoDB GFF3 the pipeline annotates with, or None.

    Release-agnostic: globs the annotation dir so the exact PlasmoDB version
    in the filename never needs hardcoding. Used to derive true protein
    lengths for the mutation lollipop backbone.
    """
    ann = os.path.join(app_root(), "data", "external", "pf-ref", "annotation")
    for pattern in ("*.gff", "*.gff3"):
        hits = sorted(glob.glob(os.path.join(ann, pattern)))
        if hits:
            return hits[0]
    return None


def src_dir():
    return os.path.join(app_root(), "src")


def clair3_models_dir():
    """On-disk Clair3 model registry: ``<app_root>/data/clair3_models``.

    The bash pipeline resolves ``$PROJECT_ROOT/data/clair3_models/$CLAIR3_MODEL``,
    so this is the fixed base whose subdir *names* are the selectable models.
    This is the *bundled* (read-only) registry; user-imported models live under
    :func:`user_clair3_models_dir` so they survive a read-only packaged ``.app``.
    """
    return os.path.join(app_root(), "data", "clair3_models")


def user_clair3_models_dir():
    """Writable Clair3 model registry: ``<user_data_dir>/clair3_models``.

    Holds models the user imports at runtime. Kept separate from the bundled
    registry so a read-only ``.app`` can still gain new models, and so we never
    write into ``app_root/data``.
    """
    path = os.path.join(user_data_dir(), "clair3_models")
    os.makedirs(path, exist_ok=True)
    return path


def _is_valid_model_dir(d):
    """A dir is a usable Clair3 model iff it holds both weight files."""
    return (os.path.isdir(d)
            and os.path.isfile(os.path.join(d, "pileup.pt"))
            and os.path.isfile(os.path.join(d, "full_alignment.pt")))


def list_clair3_models():
    """Sorted names of usable Clair3 models across bundled + user registries.

    A subdir counts only if it holds both weight files the caller (Clair3)
    needs — ``pileup.pt`` and ``full_alignment.pt`` — so half-populated dirs
    are never offered. Names are the union of the two registries, deduped so a
    user model shadows a bundled one of the same name. Returns an empty list
    when both registries are absent.
    """
    names = set()
    for base in (clair3_models_dir(), user_clair3_models_dir()):
        if not os.path.isdir(base):
            continue
        for name in os.listdir(base):
            if _is_valid_model_dir(os.path.join(base, name)):
                names.add(name)
    return sorted(names)


def resolve_clair3_model(name):
    """Absolute path to a named Clair3 model dir, or None.

    Prefers the user registry (so a user import shadows a bundled model of the
    same name), then falls back to the bundled registry. The bash pipeline
    accepts this absolute path directly.
    """
    if not name:
        return None
    for base in (user_clair3_models_dir(), clair3_models_dir()):
        d = os.path.join(base, name)
        if _is_valid_model_dir(d):
            return d
    return None


def import_clair3_model_files(pileup_path, full_alignment_path, name):
    """Assemble a Clair3 model in the user registry from its two weight files.

    Takes the two weight files individually and writes them under canonical
    names (``pileup.pt`` and ``full_alignment.pt``) into a fresh
    ``user_clair3_models_dir()/<name>`` dir. This lets the UI collect each file
    in its own labelled slot so users can't mis-structure the folder.

    Both paths must be existing files and ``name`` must be non-empty and not
    already taken in the user registry. On any copy error the partially created
    destination is removed so a failed import never leaves a half model that
    would then be offered as valid. Returns the imported model's name.
    """
    import shutil

    name = (name or "").strip()
    if not name:
        raise ValueError("Model name cannot be empty.")
    if not pileup_path or not os.path.isfile(pileup_path):
        raise ValueError("Select the model's 'pileup.pt' weight file.")
    if not full_alignment_path or not os.path.isfile(full_alignment_path):
        raise ValueError(
            "Select the model's 'full_alignment.pt' weight file.")
    dest = os.path.join(user_clair3_models_dir(), name)
    if os.path.exists(dest):
        raise ValueError("A model named '%s' already exists." % name)
    os.makedirs(dest)
    try:
        shutil.copyfile(pileup_path, os.path.join(dest, "pileup.pt"))
        shutil.copyfile(full_alignment_path,
                        os.path.join(dest, "full_alignment.pt"))
    except OSError:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    return name


def delete_clair3_model(name):
    """Remove a user-imported model. Never touches the bundled registry.

    Returns True if a user model was removed, False if there was nothing to
    remove (e.g. the name only exists as a bundled model).
    """
    import shutil

    if not name:
        return False
    d = os.path.join(user_clair3_models_dir(), name)
    if os.path.isdir(d):
        shutil.rmtree(d)
        return True
    return False


def assets_dir():
    """Bundled GUI assets (logo, etc.), shipped inside the ``gui`` package."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")


def logo_path(dark=False):
    if dark:
        p = os.path.join(assets_dir(), "logo_dark.png")
        if os.path.exists(p):
            return p
    return os.path.join(assets_dir(), "logo.png")


def app_icon_path():
    """Square app/Dock icon (mosquito+DNA emblem on a rounded card)."""
    return os.path.join(assets_dir(), "app_icon.png")


def leaflet_dir():
    """Directory holding the vendored Leaflet 1.9.4 shell (js/css/images).

    Kept local so the web-map shell loads offline; only tiles/geocoding
    reach the network.
    """
    return os.path.join(assets_dir(), "leaflet")


def leaflet_asset(name):
    """Absolute path to one vendored Leaflet file (e.g. ``"leaflet.js"``)."""
    return os.path.join(leaflet_dir(), name)


def ghana_geojson_path():
    """Bundled Ghana admin-1 (16-region) boundary GeoJSON."""
    return os.path.join(assets_dir(), "ghana_regions.geojson")


def ghana_districts_path():
    """Bundled ``{region: [districts...]}`` lookup for dropdown convenience."""
    return os.path.join(assets_dir(), "ghana_districts.json")


def discover_barcodes(fastq_dir):
    """Barcode folders the pipeline will loop over (matches its ``barcode*``).

    Returns the sorted basenames so callers (progress screen, sample sheet)
    can lay out the loop before any output arrives.
    """
    if not fastq_dir or not os.path.isdir(fastq_dir):
        return []
    found = [os.path.basename(p.rstrip("/"))
             for p in glob.glob(os.path.join(fastq_dir, "barcode*"))
             if os.path.isdir(p)]
    return sorted(found)


def resolve_barcode_root(fastq_dir):
    """Return the directory that *directly* holds ``barcode*`` folders.

    ONT delivers reads as ``<run>/fastq_pass/barcode*`` but operators commonly
    pick the run folder (or another wrapper) rather than ``fastq_pass`` itself,
    leaving the sample sheet empty because ``barcode*`` sits one level down.
    Probe the chosen dir first, then a single ``fastq_pass`` child, then any
    lone immediate sub-folder that contains the barcodes. Returns the original
    path unchanged when nothing matches (so the pipeline still gets what the
    operator typed and the empty-state hint still fires).
    """
    if not fastq_dir or not os.path.isdir(fastq_dir):
        return fastq_dir
    fastq_dir = os.path.normpath(fastq_dir)
    if discover_barcodes(fastq_dir):
        return fastq_dir
    # Operator picked a single ``barcodeNN`` folder itself. MinKNOW always
    # nests barcodes under a parent (``fastq_pass``), so step up one level and
    # run against that parent (which the pipeline then loops over as usual).
    if os.path.basename(fastq_dir).lower().startswith("barcode"):
        parent = os.path.dirname(fastq_dir)
        if parent and discover_barcodes(parent):
            return parent
    cand = os.path.join(fastq_dir, "fastq_pass")
    if os.path.isdir(cand) and discover_barcodes(cand):
        return cand
    # Fall back to a scan of immediate children: if exactly one holds the
    # barcodes, descend into it (covers oddly-named wrappers without guessing).
    hits = [p for p in sorted(glob.glob(os.path.join(fastq_dir, "*")))
            if os.path.isdir(p) and discover_barcodes(p)]
    if len(hits) == 1:
        return hits[0]
    return fastq_dir


def input_manifest(fastq_dir):
    """A cheap, content-free fingerprint of the run's input files.

    For every regular file under each ``barcode*`` dir in ``fastq_dir`` emits a
    ``"<relpath-from-fastq_dir>|<size>"`` line (size via ``os.path.getsize`` — a
    stat, never a content read), returning the sorted lines joined by newlines.
    Relpaths keep the manifest stable if the parent folder is moved. Returns an
    empty string when the dir is missing or holds no barcode files. Per-file
    stats are wrapped so a transient error skips one file rather than aborting.
    """
    if not fastq_dir or not os.path.isdir(fastq_dir):
        return ""
    lines = []
    for bc in glob.glob(os.path.join(fastq_dir, "barcode*")):
        if not os.path.isdir(bc):
            continue
        for root, _dirs, files in os.walk(bc):
            for name in files:
                full = os.path.join(root, name)
                try:
                    size = os.path.getsize(full)
                except OSError:
                    continue
                rel = os.path.relpath(full, fastq_dir)
                lines.append("%s|%d" % (rel, size))
    return "\n".join(sorted(lines))


def generate_report_script():
    return os.path.join(src_dir(), "generate_report.py")


def bundled_env_root():
    """Relocatable conda env root inside the bundle, or None when running
    from a normal (already-activated) environment in source mode."""
    # Inside the .app: Contents/Resources/env, app is Contents/Resources/app
    candidate = os.path.join(os.path.dirname(app_root()), "env")
    if os.path.isdir(os.path.join(candidate, "bin")):
        return candidate
    return None


def bundled_env_bin():
    root = bundled_env_root()
    return os.path.join(root, "bin") if root else None


# --- interpreter discovery for PDF generation --------------------------
# The dashboard's data loaders work in any interpreter, but the PDF report
# needs ReportLab. The GUI may be launched from an interpreter that lacks it
# (e.g. a bare conda *base*), so we locate one that has it: the bundled env
# first, then the launching interpreter, then sibling conda envs.

def _conda_env_roots():
    """Candidate conda environment prefixes near the running interpreter."""
    bases = set()
    for prefix in (os.path.dirname(os.path.dirname(sys.executable)),
                   os.environ.get("CONDA_PREFIX")):
        if not prefix:
            continue
        parent = os.path.dirname(prefix)
        # If prefix is .../envs/<name>, the conda base is two levels up.
        bases.add(os.path.dirname(parent)
                  if os.path.basename(parent) == "envs" else prefix)
    roots = []
    for base in bases:
        envs = os.path.join(base, "envs")
        if os.path.isdir(envs):
            roots += [os.path.join(envs, n) for n in sorted(os.listdir(envs))]
    return roots


def _env_pythons(bin_dir):
    """Interpreter paths to try in a conda ``bin`` dir, preferring plain
    ``python`` but falling back to ``python3`` — some envs (e.g. a Python-3.11
    env) ship only ``python3``, so probing ``python`` alone misses ReportLab.
    """
    return [os.path.join(bin_dir, name) for name in ("python", "python3")]


def _has_module(python, module):
    try:
        return subprocess.run(
            [python, "-c", "import %s" % module],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    except Exception:
        return False


_REPORT_PYTHON = False  # cache (False = not yet probed; None = none found)


def report_python():
    """A Python interpreter that has ReportLab installed, or None.

    Probes the bundled env, the launching interpreter, then sibling conda
    envs, returning the first whose ``import reportlab`` succeeds. The result
    is cached for the process.
    """
    global _REPORT_PYTHON
    if _REPORT_PYTHON is not False:
        return _REPORT_PYTHON
    candidates = []
    env_bin = bundled_env_bin()
    if env_bin:
        candidates += _env_pythons(env_bin)
    candidates.append(sys.executable)
    for r in _conda_env_roots():
        candidates += _env_pythons(os.path.join(r, "bin"))
    seen = set()
    for py in candidates:
        if not py or py in seen:
            continue
        seen.add(py)
        if os.path.isfile(py) and _has_module(py, "reportlab"):
            _REPORT_PYTHON = py
            return py
    _REPORT_PYTHON = None
    return None


def user_data_dir():
    """Writable per-user directory for DB, outputs and temp configs.

    Uses QStandardPaths when a QApplication/Qt is importable; falls back to
    platform conventions otherwise so this module is import-safe without Qt.
    """
    try:
        from PyQt5.QtCore import QStandardPaths
        path = QStandardPaths.writableLocation(
            QStandardPaths.AppDataLocation)
        if path:
            os.makedirs(path, exist_ok=True)
            return path
    except Exception:
        pass

    if sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get(
            "XDG_DATA_HOME", os.path.expanduser("~/.local/share"))
    path = os.path.join(base, APP_NAME)
    os.makedirs(path, exist_ok=True)
    return path


def db_path():
    return os.path.join(user_data_dir(), "pipeline.db")


def default_output_root():
    """Default parent dir for job outputs (writable, NO spaces).

    Job outputs must live on a space-free path: Clair3's helper scripts and
    parts of the bash pipeline word-split unquoted paths, so a directory like
    ``~/Library/Application Support/...`` (which contains a space) breaks the
    variant-calling step. The DB/configs/logs can stay under the standard
    user-data dir, but run outputs go to a clean home-relative path.
    """
    path = os.path.join(os.path.expanduser("~"), APP_NAME, "runs")
    os.makedirs(path, exist_ok=True)
    return path


def configs_dir():
    """Where per-run temporary .conf files are written (writable)."""
    path = os.path.join(user_data_dir(), "configs")
    os.makedirs(path, exist_ok=True)
    return path


def config_dir():
    """Writable dir for app settings files that may be cloud-synced."""
    path = os.path.join(user_data_dir(), "config")
    os.makedirs(path, exist_ok=True)
    return path


def reference_sets_file():
    """Local, cloud-syncable reference-set catalog (JSON).

    Holds the offered reference-set names (auto-synced from the service when
    online) plus the user's chosen default, so the Data sources picker works
    fully offline and the list stays current without touching per-job records.
    """
    return os.path.join(config_dir(), "reference_sets.json")


def logs_dir():
    path = os.path.join(user_data_dir(), "logs")
    os.makedirs(path, exist_ok=True)
    return path


def uploads_dir():
    """Staging + resumable-state dir for cloud run-input uploads (writable).

    Holds the tarred ``fastq_pass`` archive being uploaded and a small JSON
    of already-uploaded part ETags per upload, so a crashed multipart upload
    can resume without re-sending completed parts.
    """
    path = os.path.join(user_data_dir(), "uploads")
    os.makedirs(path, exist_ok=True)
    return path
