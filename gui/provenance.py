"""Extract instrument provenance from ONT FASTQ read headers.

MinKNOW writes one space-delimited ``key=value`` header per read (line 1 of
each FASTQ record), carrying the acquisition ``runid``, flow-cell id, protocol
group, basecall model and barcode. That header is the only reliable provenance
this pipeline's inputs always have — there are no run-level summary files in the
delivered ``barcode*`` tarballs — so it is the anchor for cross-device dedupe.

Everything here is pure stdlib (``gzip`` for the common ``.fastq.gz``, plain
open otherwise) and read-only: one header line per sampled chunk, never a full
scan. Extraction is best-effort — any failure returns ``None`` so a run still
enqueues without provenance rather than crashing.
"""

import gzip
import os

from . import paths

# Header tokens we lift, mapped to the provenance field names used downstream.
# Order-independent: MinKNOW's token order drifts across versions, so we key on
# the ``=`` rather than position.
_RUN_TOKENS = {
    "runid": "sequencing_run_id",
    "flow_cell_id": "flow_cell_id",
    "protocol_group_id": "protocol_group_id",
    "start_time": "run_start_time",
    "basecall_model_version_id": "basecall_model",
}
_SAMPLE_TOKENS = {
    "barcode": "barcode",
    "barcode_alias": "barcode_alias",
    "sample_id": "sample_id",
}

# provenance_source discriminator: HEADER = runid present (full confidence),
# FINGERPRINT_ONLY = no runid (dedupe degrades to input_fingerprint, never
# auto-merged across devices).
SOURCE_HEADER = "HEADER"
SOURCE_FINGERPRINT_ONLY = "FINGERPRINT_ONLY"


def parse_header_tokens(line):
    """Space-delimited ``key=value`` tokens of one FASTQ header, as a dict.

    Skips the leading ``@<read_id>`` token and keys each remaining token on its
    first ``=`` (values never contain the read id, but may contain ``=`` — so
    ``partition`` not ``split``).
    """
    out = {}
    for tok in line.rstrip("\n").split(" ")[1:]:
        if "=" in tok:
            key, _, value = tok.partition("=")
            out[key] = value
    return out


def first_header(path):
    """First line of a FASTQ file, transparently gz-or-plain, or ``None``.

    Sniffs the ``1f 8b`` gzip magic rather than trusting the extension (some
    deliveries ship uncompressed ``.fastq``). Reads only the first line: gzip
    decompresses just the opening block, so this is near-instant regardless of
    file size.
    """
    try:
        with open(path, "rb") as fh:
            is_gz = fh.read(2) == b"\x1f\x8b"
        opener = gzip.open if is_gz else open
        with opener(path, "rt", errors="replace") as fh:
            return fh.readline()
    except OSError:
        return None


def _chunk_index(name):
    """Trailing ``_<N>`` chunk index of a MinKNOW FASTQ filename, or ``None``.

    ``FAW10642_pass_barcode01_3b605670_5d3c067f_0.fastq.gz`` -> ``0``. Used to
    order chunks numerically so the first/last homogeneity probe hits the true
    extremes, not a lexical ``_10`` < ``_2`` mis-sort.
    """
    stem = name
    for ext in (".fastq.gz", ".fq.gz", ".fastq", ".fq"):
        if stem.endswith(ext):
            stem = stem[: -len(ext)]
            break
    tail = stem.rsplit("_", 1)[-1]
    return int(tail) if tail.isdigit() else None


def _sorted_chunks(barcode_dir):
    """FASTQ chunk paths in a barcode dir, ordered by chunk index then name."""
    hits = []
    for name in os.listdir(barcode_dir):
        if name.endswith((".fastq.gz", ".fq.gz", ".fastq", ".fq")):
            hits.append(name)
    # (index-present-first, index, name) keeps indexed chunks in numeric order
    # and pushes any oddly-named file to a stable tail.
    hits.sort(key=lambda n: (_chunk_index(n) is None, _chunk_index(n) or 0, n))
    return [os.path.join(barcode_dir, n) for n in hits]


def barcode_provenance(barcode_dir):
    """Provenance of one ``barcode*`` dir from its first and last chunk.

    Reads the header of the lowest- and highest-index chunk and compares
    ``runid`` + ``basecall_model_version_id``; a mismatch (a hand-merged dir)
    sets ``_homogeneous`` False but never fails. Returns the merged token dict
    (run + sample fields) plus ``_homogeneous``, or ``None`` if the dir has no
    readable FASTQ.
    """
    chunks = _sorted_chunks(barcode_dir)
    if not chunks:
        return None
    first_line = first_header(chunks[0])
    if first_line is None:
        return None
    first = parse_header_tokens(first_line)
    if len(chunks) > 1:
        last_line = first_header(chunks[-1])
        last = parse_header_tokens(last_line) if last_line else first
    else:
        last = first
    homogeneous = (
        first.get("runid") == last.get("runid")
        and first.get("basecall_model_version_id")
        == last.get("basecall_model_version_id"))
    rec = {**first, "_homogeneous": homogeneous}
    return rec


def run_provenance(fastq_dir):
    """Run + per-barcode provenance for a FASTQ dir, or ``None``.

    Extracts each ``barcode*`` dir, derives run-level fields from the first
    barcode that carries a ``runid``, and sets ``source`` to ``HEADER`` when a
    ``runid`` was found anywhere, else ``FINGERPRINT_ONLY``. Best-effort: any
    error yields ``None`` so enqueue is never blocked. Shape::

        {"run": {sequencing_run_id, flow_cell_id, protocol_group_id,
                 run_start_time, basecall_model},
         "source": "HEADER" | "FINGERPRINT_ONLY",
         "barcodes": {barcode: {barcode, barcode_alias, sample_id,
                                homogeneous}}}
    """
    try:
        barcodes = paths.discover_barcodes(fastq_dir)
        if not barcodes:
            return None
        per_barcode = {}
        run = {v: None for v in _RUN_TOKENS.values()}
        have_run = False
        for bc in barcodes:
            rec = barcode_provenance(os.path.join(fastq_dir, bc))
            if rec is None:
                continue
            per_barcode[bc] = {
                out: rec.get(tok) for tok, out in _SAMPLE_TOKENS.items()}
            per_barcode[bc]["homogeneous"] = bool(rec.get("_homogeneous", True))
            if not have_run and rec.get("runid"):
                run = {out: rec.get(tok) for tok, out in _RUN_TOKENS.items()}
                have_run = True
        if not per_barcode:
            return None
        return {
            "run": run,
            "source": SOURCE_HEADER if have_run else SOURCE_FINGERPRINT_ONLY,
            "barcodes": per_barcode,
        }
    except Exception:
        return None
