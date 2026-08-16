"""Protein lengths for the resistance gene panel, derived from the GFF3.

The mutation lollipop plot draws each gene's protein backbone to scale. The
faithful length (in amino acids) is the summed CDS span / 3 with the trailing
stop codon removed. We read it straight from the same PlasmoDB GFF the
pipeline annotates with (``bcftools csq``), so the axis matches the reference
exactly instead of stopping at the last observed mutation.

Parsing is lazy and cached for the process. When the GFF is absent (e.g. an
old run viewed away from the reference data) callers fall back to the observed
mutation range, so the plot still renders.
"""

import json
import os
import re

from . import paths

_LENGTHS = None                 # cache: {gene_id: protein_length_aa}
_DOMAINS = None                 # cache: {gene_id: [(start, end, name), ...]}
_GENE_ID_RE = re.compile(r"gene_id=([^;]+)")


def _parse_gff(path):
    """Sum CDS spans per ``gene_id`` and convert to amino-acid length.

    Each CDS line carries ``gene_id=PF3D7_...`` in the attributes column.
    Protein length = total coding bp / 3 - 1 (drop the stop codon).
    """
    cds_bp = {}
    with open(path) as fh:
        for line in fh:
            if line.startswith("#"):
                continue
            cols = line.rstrip("\n").split("\t")
            if len(cols) < 9 or cols[2] != "CDS":
                continue
            try:
                span = int(cols[4]) - int(cols[3]) + 1
            except ValueError:
                continue
            m = _GENE_ID_RE.search(cols[8])
            if m:
                cds_bp[m.group(1)] = cds_bp.get(m.group(1), 0) + span
    lengths = {}
    for gid, bp in cds_bp.items():
        aa = bp // 3 - 1            # exclude the stop codon
        if aa > 0:
            lengths[gid] = aa
    return lengths


def protein_lengths():
    """Cached ``{gene_id: protein_length_aa}`` from the bundled GFF (or {})."""
    global _LENGTHS
    if _LENGTHS is None:
        gff = paths.annotation_gff()
        try:
            _LENGTHS = _parse_gff(gff) if gff else {}
        except OSError:
            _LENGTHS = {}
    return _LENGTHS


def protein_length(gene_id):
    """Protein length (aa) for a PF3D7 gene id, or None when unknown."""
    if not gene_id:
        return None
    return protein_lengths().get(gene_id)


def _load_domains():
    """Parse the shipped ``protein_domains.json`` into the cache (or {})."""
    path = os.path.join(os.path.dirname(__file__), "assets",
                        "protein_domains.json")
    out = {}
    try:
        with open(path) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return out
    for gid, entry in (data.get("genes") or {}).items():
        bands = []
        for d in entry.get("domains", []):
            try:
                start, end = int(d["start"]), int(d["end"])
            except (KeyError, ValueError, TypeError):
                continue
            if end > start:
                bands.append((start, end, str(d.get("name", ""))))
        if bands:
            out[gid] = bands
    return out


def protein_domains(gene_id):
    """Domain bands ``[(start, end, name), ...]`` for a PF3D7 gene id.

    Sourced from the bundled ``protein_domains.json`` (UniProt/Pfam). Returns
    an empty list when the gene or the file is absent, so the lollipop simply
    draws a bare backbone.
    """
    global _DOMAINS
    if _DOMAINS is None:
        _DOMAINS = _load_domains()
    if not gene_id:
        return []
    return _DOMAINS.get(gene_id, [])
