"""Local, cloud-syncable catalog of reference-set names.

The offered reference sets (which targets BED + resistance catalog a run is
called against) are not hard-coded in the GUI: they live in a small JSON config
file in the writable user-data dir. The list can be refreshed from the cloud,
but only when the user chooses to (the Data sources "Update from cloud" action)
-- never automatically. The Data sources page reads the offered names from here,
each run then picks its own set, and the user's chosen default is persisted here
too -- one file, list + local default, independent of any individual job.

File: ``<user_data_dir>/config/reference_sets.json``::

    {
      "sets": [                                       # cloud-published presets
        {"name": "PlasmoDB-68 + kelch13 2026", "id": "uuid"},
        ...
      ],
      "default": "WHO 2025 / PlasmoDB-68"             # user's default (local)
    }

Each set carries the backend bundle ``id`` when one was published, so a synced
run can send ``reference_set_id`` directly; when it is unknown the id is null
and the backend backfills it later by matching on the name. Legacy string-only
entries are still read (as ``{name, id: null}``).

The bundled builtin set(s) are always offered by :mod:`gui.config_bridge`; this
file holds only the *additional* synced presets plus the default pointer, so a
fresh install with no internet still works entirely offline. Auto-sync replaces
``sets`` from the service but never touches the locally chosen ``default``.
"""

import json
import os

from . import paths


def _read():
    """Parsed catalog, or an empty skeleton on any read/parse error."""
    try:
        with open(paths.reference_sets_file()) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {"sets": [], "default": None}
    if not isinstance(data, dict):
        return {"sets": [], "default": None}
    sets = data.get("sets")
    return {
        "sets": sets if isinstance(sets, list) else [],
        "default": data.get("default"),
    }


def _write(data):
    path = paths.reference_sets_file()
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)


def _entries():
    """Normalized ``[{name, id}]`` list (tolerates legacy string entries)."""
    out = []
    seen = set()
    for e in _read()["sets"]:
        if isinstance(e, str) and e:
            name, sid = e, None
        elif isinstance(e, dict) and e.get("name"):
            name, sid = e["name"], e.get("id")
        else:
            continue
        if name in seen:
            continue
        seen.add(name)
        out.append({"name": name, "id": sid})
    return out


def list_names():
    """Synced preset names (the bundled builtins are added by config_bridge)."""
    return [e["name"] for e in _entries()]


def id_for(name):
    """The backend bundle id recorded for ``name``, or ``None`` if unknown."""
    for e in _entries():
        if e["name"] == name:
            return e["id"]
    return None


def set_entries(items):
    """Replace the synced preset list from cloud ``{name, id}`` items.

    Returns True if the stored list changed. The local ``default`` is preserved
    untouched. Items missing a name are skipped; duplicates keep the first.
    """
    clean = []
    seen = set()
    for it in items or []:
        name = (it.get("name") if isinstance(it, dict) else None) or (
            it if isinstance(it, str) else None)
        if not name or name in seen:
            continue
        seen.add(name)
        sid = it.get("id") if isinstance(it, dict) else None
        clean.append({"name": name, "id": sid})
    data = _read()
    if _entries() == clean:
        return False
    data["sets"] = clean
    _write(data)
    return True


def default_name():
    """The user's chosen default reference set name, or ``None``."""
    return _read().get("default")


def set_default_name(name):
    """Persist ``name`` as the default reference set."""
    data = _read()
    if data.get("default") == name:
        return
    data["default"] = name
    _write(data)
