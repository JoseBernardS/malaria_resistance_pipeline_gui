"""User-managed reference-set registry (BED + resistance catalog).

The pipeline needs, per run, a genome FASTA, a targets BED, a resistance
catalog TSV and (optionally) an annotation GFF. The GUI ships one builtin set
(:data:`gui.config_bridge.REFERENCE_SETS`), but labs need to add their own —
e.g. an updated target BED + catalog for a new marker — without an OTA update
and without writing into the read-only bundle.

This module persists such user sets under the writable user-data dir:

    <user_data_dir>/reference_sets/<slug>/
        manifest.json     # name + relative filenames (+ inherited defaults)
        targets.bed       # copied import
        catalog.tsv       # copied import
        reference.fasta   # only if the user overrode the genome
        annotation.gff    # only if the user overrode the GFF

A set inherits the bundled genome/GFF (same PlasmoDB build the BED coords are
called against) unless the user explicitly overrides them, so v1 never has to
index a fresh FASTA on the fly.

:func:`resolve` returns absolute paths ready to drop into a per-run ``.conf``.
Only imports :mod:`gui.paths` and reads the builtin default lazily to avoid an
import cycle with :mod:`gui.config_bridge`.
"""

import json
import os
import re
import shutil
import tarfile
import tempfile

from . import paths

MANIFEST = "manifest.json"
_BED_NAME = "targets.bed"
_CATALOG_NAME = "catalog.tsv"
_REF_NAME = "reference.fasta"
_GFF_NAME = "annotation.gff"

# Recognized keys in a downloaded cloud bundle's ``manifest.json`` ``files`` map
# -> the local on-disk filename we store each under. Mirrors the backend bundle
# format (docs/pipeline-reference-sets-bundle-format.md): a cloud bundle is a
# complete snapshot, so all four are required.
_BUNDLE_FILE_KEYS = {
    "targets": _BED_NAME,
    "catalog": _CATALOG_NAME,
    "reference": _REF_NAME,
    "annotation": _GFF_NAME,
}


def reference_sets_dir():
    """Writable root holding one subdir per user reference set."""
    path = os.path.join(paths.user_data_dir(), "reference_sets")
    os.makedirs(path, exist_ok=True)
    return path


def _slug(name):
    """A filesystem-safe directory name derived from a display name."""
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", name.strip()).strip("_")
    return s or "set"


def _builtin_defaults():
    """Absolute genome + GFF from the bundled default set, for inheritance.

    Imported lazily so this module doesn't import :mod:`gui.config_bridge` at
    module load (which imports back here), avoiding a cycle.
    """
    from . import config_bridge as cb

    ref = cb.REFERENCE_SETS[cb.DEFAULT_REFERENCE_SET]
    return {
        "REFERENCE": cb._abs(ref["REFERENCE"]),
        "ANNOTATION_GFF": cb._abs(ref.get("ANNOTATION_GFF")),
    }


def _builtin_names():
    from . import config_bridge as cb

    return set(cb.REFERENCE_SETS.keys())


# -- listing ------------------------------------------------------------
def _read_manifest(set_dir):
    try:
        with open(os.path.join(set_dir, MANIFEST)) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("name"):
        return None
    data["_dir"] = set_dir
    return data


def list_sets():
    """All valid user reference sets as manifest dicts, sorted by name."""
    base = reference_sets_dir()
    out = []
    for entry in sorted(os.listdir(base)):
        set_dir = os.path.join(base, entry)
        if not os.path.isdir(set_dir):
            continue
        man = _read_manifest(set_dir)
        if man:
            out.append(man)
    out.sort(key=lambda m: m["name"].lower())
    return out


def list_names():
    """Display names of all user reference sets."""
    return [m["name"] for m in list_sets()]


def _find_dir(name):
    """The directory of the user set with this display name, or None."""
    for man in list_sets():
        if man["name"] == name:
            return man["_dir"]
    return None


# -- validation ---------------------------------------------------------
def _validate_bed(path):
    """Light check: first non-comment line must have >= 3 tab-separated cols."""
    if not path or not os.path.isfile(path):
        raise ValueError("BED file not found.")
    with open(path) as fh:
        for line in fh:
            line = line.rstrip("\n")
            if not line or line.startswith(("#", "track", "browser")):
                continue
            if len(line.split("\t")) < 3:
                raise ValueError(
                    "BED file looks malformed: the first data line needs at "
                    "least 3 tab-separated columns (chrom, start, end).")
            return
    raise ValueError("BED file has no data rows.")


def _validate_catalog(path):
    """Light check: a header line plus at least one data row."""
    if not path or not os.path.isfile(path):
        raise ValueError("Catalog TSV not found.")
    rows = 0
    with open(path) as fh:
        for line in fh:
            if line.strip():
                rows += 1
            if rows >= 2:
                return
    raise ValueError(
        "Catalog TSV needs a header row plus at least one entry.")


# -- create / delete ----------------------------------------------------
def save_set(name, bed_path, catalog_path, reference_path=None,
             gff_path=None):
    """Validate + copy an imported reference set into the user registry.

    ``name`` is the display name (must not collide with a builtin set or an
    existing user set). ``bed_path`` and ``catalog_path`` are required; a
    genome/GFF override is optional (otherwise the bundled defaults are
    inherited at resolve time). Returns the stored name.
    """
    name = (name or "").strip()
    if not name:
        raise ValueError("Reference set name cannot be empty.")
    if name in _builtin_names():
        raise ValueError(
            "'%s' is a built-in set name; choose a different name." % name)
    if _find_dir(name) is not None:
        raise ValueError("A reference set named '%s' already exists." % name)

    _validate_bed(bed_path)
    _validate_catalog(catalog_path)
    if reference_path and not os.path.isfile(reference_path):
        raise ValueError("Genome FASTA not found.")
    if gff_path and not os.path.isfile(gff_path):
        raise ValueError("Annotation GFF not found.")

    base = reference_sets_dir()
    slug = _slug(name)
    set_dir = os.path.join(base, slug)
    # Disambiguate a slug collision (two names sluging to the same dir).
    n = 2
    while os.path.exists(set_dir):
        set_dir = os.path.join(base, "%s-%d" % (slug, n))
        n += 1
    os.makedirs(set_dir)

    manifest = {"name": name, "bed": _BED_NAME, "catalog": _CATALOG_NAME}
    try:
        shutil.copyfile(bed_path, os.path.join(set_dir, _BED_NAME))
        shutil.copyfile(catalog_path, os.path.join(set_dir, _CATALOG_NAME))
        if reference_path:
            shutil.copyfile(reference_path, os.path.join(set_dir, _REF_NAME))
            manifest["reference"] = _REF_NAME
            # A user genome needs its .fai alongside; copy it if present.
            fai = reference_path + ".fai"
            if os.path.isfile(fai):
                shutil.copyfile(fai, os.path.join(set_dir, _REF_NAME + ".fai"))
        if gff_path:
            shutil.copyfile(gff_path, os.path.join(set_dir, _GFF_NAME))
            manifest["annotation_gff"] = _GFF_NAME
        with open(os.path.join(set_dir, MANIFEST), "w") as fh:
            json.dump(manifest, fh, indent=2)
    except Exception:
        shutil.rmtree(set_dir, ignore_errors=True)
        raise
    return name


def _safe_members(tar):
    """Validated regular-file members that live at the archive **root**.

    The pipeline's bundle format keeps every file at the top level (no wrapping
    dir). Reject anything that isn't a plain root file — absolute paths, parent
    traversal (``..``), nested dirs, symlinks/devices — so a malicious or
    malformed archive can't write outside the extraction dir. Directory entries
    (incl. a bare ``./``) are ignored. Returns the list to hand to ``extractall``.
    """
    safe = []
    for m in tar.getmembers():
        if m.isdir():
            continue
        if not m.isfile():
            raise ValueError("bundle contains a non-file member: %s" % m.name)
        norm = os.path.normpath(m.name)
        if norm.startswith(("/", "..")) or os.path.dirname(norm):
            raise ValueError("unsafe or nested path in bundle: %s" % m.name)
        safe.append(m)
    return safe


def save_bundle(name, bundle_path, replace=True):
    """Extract a downloaded cloud ``bundle.tar.gz`` into the user registry.

    ``name`` is the **backend reference-set name** (authoritative) — the set is
    registered under it verbatim, not under the bundle manifest's own ``name``,
    so it matches the catalog id map and the server's name-keyed backfill. The
    bundle's ``manifest.json`` ``files`` map (``catalog``/``targets``/
    ``reference``/``annotation``, all required) locates each data file; they are
    copied in under this module's canonical filenames. A prior copy of the same
    name is replaced (``replace=True``) so a re-download refreshes in place.
    Returns the stored name; raises ``ValueError`` on any malformed bundle.
    """
    name = (name or "").strip()
    if not name:
        raise ValueError("Reference set name cannot be empty.")
    if name in _builtin_names():
        raise ValueError(
            "'%s' is a built-in set name; choose a different name." % name)

    tmp = tempfile.mkdtemp(prefix="refbundle-")
    try:
        with tarfile.open(bundle_path, "r:gz") as tar:
            tar.extractall(tmp, members=_safe_members(tar))

        man_path = os.path.join(tmp, MANIFEST)
        if not os.path.isfile(man_path):
            raise ValueError("bundle has no manifest.json at its root.")
        with open(man_path) as fh:
            try:
                bundle_man = json.load(fh)
            except ValueError:
                raise ValueError("bundle manifest.json is not valid JSON.")
        files = bundle_man.get("files") if isinstance(bundle_man, dict) else None
        if not isinstance(files, dict):
            raise ValueError("bundle manifest has no 'files' map.")
        missing = [k for k in _BUNDLE_FILE_KEYS if not files.get(k)]
        if missing:
            raise ValueError(
                "bundle manifest is incomplete; missing file(s): %s"
                % ", ".join(sorted(missing)))

        # Resolve each declared file to a concrete root-level path in the extract.
        resolved = {}
        for key in _BUNDLE_FILE_KEYS:
            declared = files[key]
            if os.path.dirname(os.path.normpath(declared)):
                raise ValueError(
                    "bundle file for '%s' must be at the archive root: %s"
                    % (key, declared))
            src = os.path.join(tmp, declared)
            if not os.path.isfile(src):
                raise ValueError(
                    "bundle is missing the declared '%s' file: %s"
                    % (key, declared))
            resolved[key] = src

        _validate_bed(resolved["targets"])
        _validate_catalog(resolved["catalog"])

        base = reference_sets_dir()
        existing = _find_dir(name)
        if existing is not None:
            if not replace:
                raise ValueError(
                    "A reference set named '%s' already exists." % name)
            shutil.rmtree(existing, ignore_errors=True)
        slug = _slug(name)
        set_dir = os.path.join(base, slug)
        # Disambiguate a slug collision with a *different* set's directory.
        n = 2
        while os.path.exists(set_dir):
            set_dir = os.path.join(base, "%s-%d" % (slug, n))
            n += 1
        os.makedirs(set_dir)

        # A cloud bundle is complete, so it always carries its own genome + GFF
        # (no inheritance of the bundled defaults). Record all four.
        manifest = {"name": name, "bed": _BED_NAME, "catalog": _CATALOG_NAME,
                    "reference": _REF_NAME, "annotation_gff": _GFF_NAME}
        try:
            shutil.copyfile(resolved["targets"],
                            os.path.join(set_dir, _BED_NAME))
            shutil.copyfile(resolved["catalog"],
                            os.path.join(set_dir, _CATALOG_NAME))
            shutil.copyfile(resolved["reference"],
                            os.path.join(set_dir, _REF_NAME))
            shutil.copyfile(resolved["annotation"],
                            os.path.join(set_dir, _GFF_NAME))
            with open(os.path.join(set_dir, MANIFEST), "w") as fh:
                json.dump(manifest, fh, indent=2)
        except Exception:
            shutil.rmtree(set_dir, ignore_errors=True)
            raise
        return name
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def resolve(name):
    """Absolute pipeline paths for a user set, or None if unknown.

    Returns a dict with ``REFERENCE``, ``ORIGINAL_BED_FILE``,
    ``RESISTANCE_CATALOG`` and ``ANNOTATION_GFF``. Genome and GFF fall back to
    the bundled defaults unless the set overrode them.
    """
    set_dir = _find_dir(name)
    if set_dir is None:
        return None
    man = _read_manifest(set_dir)
    if man is None:
        return None
    defaults = _builtin_defaults()
    ref = (os.path.join(set_dir, man["reference"])
           if man.get("reference") else defaults["REFERENCE"])
    gff = (os.path.join(set_dir, man["annotation_gff"])
           if man.get("annotation_gff") else defaults["ANNOTATION_GFF"])
    return {
        "REFERENCE": ref,
        "ORIGINAL_BED_FILE": os.path.join(set_dir, man["bed"]),
        "RESISTANCE_CATALOG": os.path.join(set_dir, man["catalog"]),
        "ANNOTATION_GFF": gff,
    }


def delete_set(name):
    """Remove a user reference set. Returns True if one was removed."""
    set_dir = _find_dir(name)
    if set_dir is None:
        return False
    shutil.rmtree(set_dir, ignore_errors=True)
    return True
