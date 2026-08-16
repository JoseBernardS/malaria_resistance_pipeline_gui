"""Clinical report designer settings: one source of truth for the PDF's
branding, page size and section options.

Settings live in the ``app_config`` key-value store under ``report_*`` keys
(SQLite has no booleans, so flags are persisted as ``"1"``/``"0"``).
:func:`load` reads them into a plain dict matching the JSON contract that
``src/generate_report.py`` consumes via ``--settings``; :func:`write_sidecar`
serialises that dict to a JSON file the generator reads. Keeping the field
contract here keeps the Report-settings page and the Samples-page "Report"
action in lock-step without duplicating key names.
"""

import json
import os

from . import db, paths

# name -> (kind, default). ``kind`` is "str" or "bool". Names match the JSON
# keys in ``src/generate_report.py``'s DEFAULT_SETTINGS exactly.
FIELDS = [
    ("org_name",          "str",  ""),
    ("title",             "str",  ""),
    ("logo_show",         "bool", False),
    ("logo_path",         "str",  ""),
    ("logo_pos",          "str",  "left"),
    ("page_size",         "str",  "A4"),
    ("color_mode",        "str",  "color"),
    ("include_treatment", "bool", True),
    ("include_variants",  "bool", True),
    ("include_qc",        "bool", True),
    ("include_coverage",  "bool", True),
    ("include_site",      "bool", True),
    ("footer",            "str",  ""),
]

_PREFIX = "report_"

# Each report scope stores its own complete, independent settings set. Keys are
# namespaced ``report_<scope>_<name>``; the "sample" scope also falls back to the
# legacy un-namespaced ``report_<name>`` keys so existing installs keep values.
SCOPES = ("sample", "overview")


def _scope_key(scope, name):
    return "%s%s_%s" % (_PREFIX, scope, name)


def defaults():
    """A fresh dict of the built-in defaults (shared across scopes)."""
    return {name: default for name, _kind, default in FIELDS}


def load(scope="sample"):
    """Return the saved settings for ``scope`` as a plain dict.

    Unset keys fall back to defaults. For the "sample" scope, an unset
    namespaced key also falls back to the legacy ``report_<name>`` key so
    pre-existing sample settings survive the migration to scoped storage.
    """
    out = {}
    for name, kind, default in FIELDS:
        raw = db.get_app_config(_scope_key(scope, name), None)
        if raw is None and scope == "sample":
            raw = db.get_app_config(_PREFIX + name, None)
        if raw is None:
            out[name] = default
        elif kind == "bool":
            out[name] = raw == "1"
        else:
            out[name] = raw
    return out


def save(settings, scope="sample"):
    """Persist a settings dict for ``scope`` (only the known FIELDS)."""
    for name, kind, _default in FIELDS:
        if name not in settings:
            continue
        val = settings[name]
        key = _scope_key(scope, name)
        if kind == "bool":
            db.set_app_config(key, "1" if val else "0")
        else:
            db.set_app_config(key, "" if val is None else str(val))


def write_sidecar(scope="sample", out_dir=None):
    """Serialise ``scope``'s settings to JSON for ``--settings``; return path."""
    out_dir = out_dir or paths.configs_dir()
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "report_settings_%s.json" % scope)
    with open(path, "w") as fh:
        json.dump(load(scope), fh, indent=2)
    return path


def store_logo(src_path):
    """Copy a chosen logo into the app-data dir; return the stored path or "".

    Copying keeps the mark stable even if the user later moves the original.
    Falls back to the source path if the copy fails for any reason.
    """
    if not src_path or not os.path.isfile(src_path):
        return ""
    import shutil
    ext = os.path.splitext(src_path)[1].lower() or ".png"
    dest = os.path.join(paths.user_data_dir(), "report_logo" + ext)
    try:
        shutil.copy(src_path, dest)
        return dest
    except Exception:
        return src_path
