#!/usr/bin/env python3
"""Render a clinical/surveillance PDF report for the PF drug-resistance pipeline.

Standalone, re-runnable step that consumes the three CSVs written by
``combine_haplotype.py`` (resistance_calls.csv, variant_detail.csv,
coverage_report.csv) and renders a combined and/or per-sample PDF.

Design
------
The report is built around a *drug-status panel*: for every drug in the panel it
reports one of five verdicts per sample --

    Resistant / Candidate / Potential   (a WHO marker was called)
    No marker detected                   (informing gene was covered, nothing found)
    Not assessed                         (informing gene had no coverage)

The last distinction is the whole point of the pipeline: a drug with no call is
only reassuring if its gene was actually sequenced. Drugs are shown even when
nothing was flagged, so "checked and clear" is never confused with "not looked at".

Usage
-----
    python3 src/generate_report.py \
        --reports_dir results/analysis_output/final_reports \
        --output_dir  results/analysis_output/final_reports \
        --mode combined            # combined | per-sample | both
"""

import argparse
import csv
import json
import os
import sys
from collections import OrderedDict
from datetime import datetime

# ReportLab is only needed for PDF generation. Import it lazily so the data
# loaders and helpers below (used by the desktop GUI to render the on-screen
# results) keep working in environments without ReportLab installed; the PDF
# build functions raise a clear error if it is genuinely missing.
try:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, LETTER, landscape
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import cm
    from reportlab.platypus import (
        Image, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table,
        TableStyle,
    )
    _HAVE_REPORTLAB = True
except ImportError:
    colors = A4 = LETTER = landscape = ParagraphStyle = getSampleStyleSheet = cm = None
    Image = KeepTogether = Paragraph = SimpleDocTemplate = Spacer = Table = None
    TableStyle = None
    _HAVE_REPORTLAB = False


# Single version label for the whole external reference dataset — WHO marker
# compendium edition AND genome release, not the genome alone. Set per-run via
# the REFERENCE_SET_VERSION env var when a reference set is selected; falls back
# to the native bundled default (the data shipped in this image).
REFERENCE_VERSION = os.environ.get(
    "REFERENCE_SET_VERSION", "WHO 2025 / PlasmoDB-68"
)

# Friendly P. falciparum gene symbols, keyed by PlasmoDB gene ID.
GENE_SYMBOLS = {
    "PF3D7_0417200": "pfdhfr",
    "PF3D7_0810800": "pfdhps",
    "PF3D7_0523000": "pfmdr1",
    "PF3D7_0709000": "pfcrt",
    "PF3D7_1343700": "pfk13",
    "PF3D7_MIT02300": "pfcytb",
    "PF3D7_1251200": "pfcoronin",
}

# Drug panel: each drug -> the gene(s) whose markers inform it (documented panel,
# see README). A drug is "assessed" if at least one informing gene was covered.
DRUG_GENES = OrderedDict([
    ("Chloroquine",   ["PF3D7_0709000", "PF3D7_0523000"]),   # pfcrt, pfmdr1
    ("Amodiaquine",   ["PF3D7_0523000"]),                    # pfmdr1
    ("Pyrimethamine", ["PF3D7_0417200"]),                    # pfdhfr
    ("Cycloguanil",   ["PF3D7_0417200"]),                    # pfdhfr
    ("Sulfadoxine",   ["PF3D7_0810800"]),                    # pfdhps
    ("Artemisinin",   ["PF3D7_1343700", "PF3D7_1251200"]),   # pfk13 (+pfcoronin)
    ("Atovaquone",    ["PF3D7_MIT02300"]),                   # pfcytb
])

CATALOG_DISPLAY = {
    "known_marker_component": "Known marker",
    "uncharacterized": "Novel",
}

# Monochrome palette. Status is conveyed by *words and font weight*, never by
# fill colour, so every semantic key here is repointed to greyscale. The keys
# are kept defined (theme/widgets/charts import them) but no longer drive a
# coloured status fill anywhere; emphasis is bold weight instead of hue.
PALETTE = {
    "validated": "#1a1a1a",   # dark ink  - rendered bold (words carry meaning)
    "candidate": "#1a1a1a",   # dark ink  - rendered bold
    "potential": "#1a1a1a",   # dark ink
    "nomarker":  "#1a1a1a",   # dark ink
    "notassessed": "#5a6472", # grey ink  - absence of data
    "ok":        "#1a1a1a",
    "low":       "#5a6472",
    "no":        "#5a6472",
    "known":     "#1a1a1a",
    "novel":     "#5a6472",
    "band":      "#ffffff",   # was navy header band -> white (no fill)
    "rowalt":    "#ffffff",   # was pale row tint -> white (no alternating)
    "grid":      "#dde2e8",   # soft hairline rule (lightened for a calmer look)
    "text":      "#1a1a1a",
    "muted":     "#5a6472",
}

# Clinical status colours, mirroring the on-screen overview grid
# (gui/theme.py). Used only when the report's colour mode is "color": each
# status/coverage key maps to (soft chip fill, readable ink). In monochrome mode
# these are ignored and status is carried by words + bold weight, exactly as
# before, so the report still prints cleanly in black & white.
CHIP_COLORS = {
    "validated":   ("#f6dade", "#b2182b"),  # red    - resistant / validated
    "candidate":   ("#fbe3d2", "#bf531a"),  # orange - candidate marker
    "potential":   ("#f8ecc9", "#8a6d0b"),  # amber  - potential marker
    "nomarker":    ("#daf0e2", "#12703a"),  # green  - assessed, none found
    "notassessed": ("#ecedf0", "#4b5563"),  # grey   - no coverage
    "ok":          ("#daf0e2", "#12703a"),  # green  - adequate coverage
    "low":         ("#f8ecc9", "#8a6d0b"),  # amber  - low coverage
    "no":          ("#ecedf0", "#4b5563"),  # grey   - no coverage
}

# Beautify tints shared by both colour modes: a faint header wash and an even
# fainter zebra stripe lift dense tables off the page without adding weight.
HEADER_TINT = "#eef1f5"
ZEBRA_TINT = "#f8fafb"

# Resistance tiers, highest concern first.
TIER_ORDER = ["validated", "candidate", "potential"]

# status key -> (short label for cells, long label for panel, palette key, white text?)
STATUS_META = OrderedDict([
    ("validated",   ("Resistant",    "Validated marker",    "validated",   True)),
    ("candidate",   ("Candidate",    "Candidate marker",    "candidate",   True)),
    ("potential",   ("Potential",    "Potential marker",    "potential",   False)),
    ("nomarker",    ("No marker",    "No marker detected",  "nomarker",    False)),
    ("notassessed", ("Not assessed", "Not assessed",        "notassessed", True)),
])


# ---------------------------------------------------------------------------
# Report settings (branding / layout / content), designed in the GUI's Report
# settings page and passed in as a small JSON sidecar via --settings. Every key
# has a safe default so the report renders identically to before when no
# settings file is supplied.
# ---------------------------------------------------------------------------
DEFAULT_SETTINGS = {
    "org_name": "",
    "title": "",
    "logo_show": False,
    "logo_path": "",
    "logo_pos": "left",              # left | center | right
    "page_size": "A4",              # A4 | Letter
    "color_mode": "color",          # color | mono (resistance-grid rendering)
    "include_treatment": True,       # clinical interpretation block (per-sample)
    "include_variants": True,
    "include_qc": True,
    "include_coverage": True,
    "include_site": True,
    "footer": "",                    # blank -> the default research-use notice
}


def load_settings(path):
    """Merge a JSON settings sidecar over DEFAULT_SETTINGS; tolerant of errors."""
    settings = dict(DEFAULT_SETTINGS)
    if not path or not os.path.isfile(path):
        return settings
    try:
        with open(path) as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            for k in DEFAULT_SETTINGS:
                if k in data and data[k] is not None:
                    settings[k] = data[k]
    except Exception:
        pass
    return settings


def page_size(settings):
    base = LETTER if str(settings.get("page_size")).lower() == "letter" else A4
    return base


# Per-drug clinical interpretation, keyed by the resistance status tier. This
# translates a molecular verdict into plain, actionable wording for a treating
# clinician. It is decision-support only: ACTs remain first-line per national
# policy and markers are supportive surveillance evidence, not a prescription.
TREATMENT_META = OrderedDict([
    ("validated",   ("Avoid",            "Reduced efficacy likely; prefer an "
                                          "alternative per national guidelines.")),
    ("candidate",   ("Use with caution", "Possible reduced efficacy; monitor "
                                          "response.")),
    ("potential",   ("Monitor",          "Uncertain marker; monitor response.")),
    ("nomarker",    ("Likely effective", "No marker detected.")),
    ("notassessed", ("No data",          "Not assessed \u2013 no coverage.")),
])


def hx(key):
    return colors.HexColor(PALETTE[key])


def _c(hexstr):
    """A ReportLab colour from a raw hex string (for the chip/beautify tints)."""
    return colors.HexColor(hexstr)


def _color_on(settings):
    """True when the resistance grid should render in colour (the default).

    Monochrome mode ("mono") preserves the print-safe word+weight rendering.
    """
    mode = str((settings or DEFAULT_SETTINGS).get("color_mode", "color")).lower()
    return mode != "mono"


# Absence-of-data states get no chip fill (only grey ink), mirroring the
# overview grid's hollow "Not assessed" tile — this keeps the grid light when
# most cells are simply uncovered.
_NO_FILL = ("notassessed", "no")


def _chip_bg(key):
    """Soft chip fill for a status/coverage key, or None when it should stay
    unfilled (unknown key, or an absence-of-data state)."""
    if key in _NO_FILL:
        return None
    entry = CHIP_COLORS.get(key)
    return _c(entry[0]) if entry else None


def _decorate(cmds, n_rows, zebra=True):
    """Append the shared beautify commands (header wash + zebra stripes).

    ``n_rows`` is the total row count including the header at row 0; data rows
    are 1..n-1 and every second data row gets a faint stripe. Chip backgrounds,
    when used, are appended *after* this so they win on their own cells.
    """
    cmds.append(("BACKGROUND", (0, 0), (-1, 0), _c(HEADER_TINT)))
    if zebra:
        for r in range(2, n_rows, 2):
            cmds.append(("BACKGROUND", (0, r), (-1, r), _c(ZEBRA_TINT)))


def classify_tier(classification):
    c = (classification or "").lower()
    if "validated" in c:
        return "validated"
    if "candidate" in c:
        return "candidate"
    if "potential" in c:
        return "potential"
    return "potential"


def gene_display(gene_id, gene_name=None):
    sym = GENE_SYMBOLS.get(gene_id)
    if sym:
        return sym
    if gene_name and gene_name != gene_id:
        return gene_name
    return gene_id


def catalog_display(status):
    return CATALOG_DISPLAY.get((status or "").strip().lower(), status or "-")


# ---------------------------------------------------------------------------
# CSV loading
# ---------------------------------------------------------------------------
def load_csv(path):
    if not os.path.isfile(path):
        return []
    with open(path, newline="") as fh:
        return [dict(r) for r in csv.DictReader(fh)]


def load_data(reports_dir):
    calls = load_csv(os.path.join(reports_dir, "resistance_calls.csv"))
    variants = load_csv(os.path.join(reports_dir, "variant_detail.csv"))
    coverage = load_csv(os.path.join(reports_dir, "coverage_report.csv"))
    if not (calls or variants or coverage):
        sys.exit("ERROR: no input CSVs found in %s" % reports_dir)
    return calls, variants, coverage


def all_samples(calls, variants, coverage):
    seen = OrderedDict()
    for row in coverage + variants + calls:
        s = row.get("Sample")
        if s:
            seen.setdefault(s, None)
    return list(seen.keys())


def coverage_index(coverage):
    return {(r.get("Sample"), r.get("Gene_ID")): (r.get("Status", "") or "").upper()
            for r in coverage}


# ---------------------------------------------------------------------------
# Optional sample metadata (alias + collection site), written by the GUI
# ---------------------------------------------------------------------------
def load_sample_meta(path):
    """Load the optional sample-metadata sidecar into ``{Sample: {...}}``.

    The GUI writes a CSV keyed by ``Sample`` with optional ``Sample_UID``,
    ``Alias``, ``Region``, ``District``, ``Latitude``, ``Longitude``,
    ``Collection_date``, ``Case_classification``, ``Age_years`` and ``Notes``
    columns. Missing file / column -> empty mapping, so the report renders
    exactly as before when no metadata supplied.
    """
    meta = {}
    if not path or not os.path.isfile(path):
        return meta
    for row in load_csv(path):
        s = (row.get("Sample") or "").strip()
        if s:
            meta[s] = row
    return meta


def sample_label(sample, meta):
    """Display label for a sample: ``alias (sample)`` when an alias is set."""
    info = meta.get(sample) if meta else None
    alias = (info.get("Alias") or "").strip() if info else ""
    if alias:
        return "%s (%s)" % (alias, sample)
    return sample


# ---------------------------------------------------------------------------
# Read-level QC (NanoStat) loading
# ---------------------------------------------------------------------------
def parse_nanostat(path):
    """Parse a NanoStat report into a {metric: value} dict (tab-separated)."""
    metrics = {}
    with open(path) as fh:
        for line in fh:
            parts = line.rstrip("\n").split("\t")
            if len(parts) >= 2:
                metrics[parts[0].strip()] = parts[1].strip()
    return metrics


# NanoPlot's ``NanoStats.txt`` uses a human-readable "General summary:" block
# (colon-separated, thousands-separated values) with different labels than
# NanoStat's ``--tsv`` output. Map its labels onto the same canonical keys the
# report/GUI already consume so either QC tool populates the same table.
_NANOPLOT_LABELS = {
    "number of reads": "number_of_reads",
    "total bases": "number_of_bases",
    "median read length": "median_read_length",
    "mean read length": "mean_read_length",
    "read length n50": "n50",
    "mean read quality": "mean_qual",
    "median read quality": "median_qual",
}


def parse_nanoplot_stats(path):
    """Parse NanoPlot's ``NanoStats.txt`` into the canonical metric keys.

    Reads the leading "General summary:" lines (``Label:   value``), strips the
    thousands separators NanoPlot prints, and renames the labels to match
    :func:`parse_nanostat` so downstream code is tool-agnostic.
    """
    metrics = {}
    with open(path) as fh:
        for line in fh:
            if ":" not in line:
                continue
            label, _, value = line.partition(":")
            key = _NANOPLOT_LABELS.get(label.strip().lower())
            if not key:
                continue
            value = value.strip().replace(",", "")
            if value:
                metrics[key] = value
    return metrics


def load_qc(qc_dir, samples):
    """Load post-trim read QC metrics per sample, tool-agnostically.

    Looks under ``qc_dir/<sample>/`` for NanoStat's ``<sample>_nanostat.txt``
    first, then NanoPlot's ``NanoStats.txt``; whichever is present is parsed
    into the same canonical keys. Missing samples are skipped.
    """
    qc = {}
    if not qc_dir or not os.path.isdir(qc_dir):
        return qc
    for s in samples:
        nanostat = os.path.join(qc_dir, s, "%s_nanostat.txt" % s)
        nanoplot = os.path.join(qc_dir, s, "NanoStats.txt")
        if os.path.isfile(nanostat):
            qc[s] = parse_nanostat(nanostat)
        elif os.path.isfile(nanoplot):
            qc[s] = parse_nanoplot_stats(nanoplot)
    return qc


def panel_drugs(calls):
    """Full documented panel, plus any extra drugs that appear in the calls."""
    drugs = list(DRUG_GENES.keys())
    for c in calls:
        d = c.get("Drug")
        if d and d not in drugs:
            drugs.append(d)
    return drugs


def drug_status(sample, drug, calls, cov_idx):
    """Return (status_key, genes_label, finding_text) for one drug in one sample."""
    sc = [c for c in calls if c.get("Sample") == sample and c.get("Drug") == drug]
    if sc:
        tier = min((classify_tier(c.get("Classification", "")) for c in sc),
                   key=TIER_ORDER.index)
        alts = []
        for c in sc:
            a = c.get("Alteration", "")
            if a and a not in alts:
                alts.append(a)
        genes = []
        for c in sc:
            g = c.get("Genes", "")
            if g and g not in genes:
                genes.append(g)
        return tier, ", ".join(genes), " / ".join(alts)

    gids = DRUG_GENES.get(drug, [])
    genes_label = ", ".join(gene_display(g) for g in gids) or "\u2013"
    covered = [g for g in gids if cov_idx.get((sample, g)) in ("OK", "LOW_COVERAGE")]
    if covered:
        return "nomarker", genes_label, "\u2013"
    return "notassessed", genes_label, "\u2013"


# ---------------------------------------------------------------------------
# Styles
# ---------------------------------------------------------------------------
def build_styles():
    s = getSampleStyleSheet()
    # Title/org head: bold black paragraphs, no navy band behind them.
    s.add(ParagraphStyle("BandTitle", parent=s["Title"], textColor=hx("text"),
                         fontSize=16, leading=19, spaceAfter=2))
    s.add(ParagraphStyle("BandSub", parent=s["Normal"], textColor=hx("muted"),
                         fontSize=8.5, leading=11))
    s.add(ParagraphStyle("H2", parent=s["Heading2"], textColor=hx("text"),
                         fontSize=12, spaceBefore=10, spaceAfter=3))
    s.add(ParagraphStyle("H3", parent=s["Heading3"], textColor=hx("text"),
                         fontSize=10.5, spaceBefore=7, spaceAfter=2))
    s.add(ParagraphStyle("Body", parent=s["Normal"], fontSize=9, leading=12,
                         textColor=hx("text")))
    s.add(ParagraphStyle("Cell", parent=s["Normal"], fontSize=8, leading=10,
                         textColor=hx("text")))
    # Bold dark cell, used for emphasised status words (validated/candidate).
    s.add(ParagraphStyle("CellBold", parent=s["Normal"], fontSize=8, leading=10,
                         textColor=hx("text"), fontName="Helvetica-Bold"))
    s.add(ParagraphStyle("CellR", parent=s["Normal"], fontSize=8, leading=10,
                         textColor=hx("text"), alignment=2))
    s.add(ParagraphStyle("Muted", parent=s["Normal"], fontSize=8, leading=11,
                         textColor=hx("muted")))
    # Small grey definition line printed under a table/diagram.
    s.add(ParagraphStyle("Footnote", parent=s["Normal"], fontSize=7.5,
                         leading=9.5, textColor=hx("muted"), spaceBefore=2))
    return s


# Status tiers rendered with emphasis (bold); others render in plain weight.
_EMPHASISED_STATUS = ("validated", "candidate")


def status_para(key, styles, short=True, settings=None):
    """A status word; coloured ink in colour mode, word+weight in mono.

    Bold is kept for validated/candidate in both modes so the highest-concern
    tiers still stand out on a black-and-white printout.
    """
    label = STATUS_META[key][0 if short else 1]
    bold = key in _EMPHASISED_STATUS
    if _color_on(settings):
        ink = CHIP_COLORS.get(key, (None, PALETTE["text"]))[1]
        body = ("<b>%s</b>" % label) if bold else label
        return Paragraph('<font color="%s">%s</font>' % (ink, body),
                         styles["Cell"])
    return Paragraph(label, styles["CellBold"] if bold else styles["Cell"])


def cov_para(status, styles, settings=None):
    """A coverage word (OK / Low / None); coloured in colour mode, plain in mono."""
    key = cov_key(status)
    label = cov_text(status)
    if _color_on(settings):
        ink = CHIP_COLORS.get(key, (None, PALETTE["text"]))[1]
        return Paragraph('<font color="%s">%s</font>' % (ink, label),
                         styles["Cell"])
    return Paragraph(label, styles["Cell"])


# ---------------------------------------------------------------------------
# Header band + legend
# ---------------------------------------------------------------------------
_ALIGN = {"left": "LEFT", "center": "CENTER", "right": "RIGHT"}
_TA = {"left": 0, "center": 1, "right": 2}


def _logo_image(settings, max_h=1.15):
    """A scaled ReportLab Image for the configured logo, or None.

    Scales to ``max_h`` cm tall, preserving the source aspect ratio. Returns
    None when the logo is disabled, unset or unreadable so the caller can fall
    back to a text-only header.
    """
    if not settings.get("logo_show"):
        return None
    path = settings.get("logo_path") or ""
    if not path or not os.path.isfile(path):
        return None
    try:
        from reportlab.lib.utils import ImageReader
        iw, ih = ImageReader(path).getSize()
        if not iw or not ih:
            return None
        h = max_h * cm
        w = h * (iw / float(ih))
        img = Image(path, width=w, height=h)
        img.hAlign = _ALIGN.get(settings.get("logo_pos"), "LEFT")
        return img
    except Exception:
        return None


def letterhead(styles, settings):
    """Optional branded letterhead (logo + organisation name) above the band.

    Returns a list of flowables (possibly empty). The logo and org name align
    per ``logo_pos`` so a lab can place their mark left/centre/right.
    """
    flow = []
    img = _logo_image(settings)
    if img is not None:
        flow.append(img)
    org = (settings.get("org_name") or "").strip()
    if org:
        align = _TA.get(settings.get("logo_pos"), 0)
        org_style = ParagraphStyle(
            "Org", parent=styles["Body"], fontSize=10.5, leading=13,
            textColor=hx("text"), alignment=align, spaceBefore=3)
        flow.append(Paragraph("<b>%s</b>" % org, org_style))
    if flow:
        flow.append(Spacer(1, 8))
    return flow


def header_band(styles, title, n_samples, settings=None):
    """Plain title block: bold black title + a meta line, then one thin rule.

    No filled band; the title/meta honour ``logo_pos`` alignment so they sit
    under a left/centre/right logo consistently.
    """
    settings = settings or DEFAULT_SETTINGS
    title = (settings.get("title") or "").strip() or title
    align = _TA.get(settings.get("logo_pos"), 0)
    meta = ("%s  &middot;  %d sample(s)  &middot;  reference %s"
            % (datetime.now().strftime("%Y-%m-%d"), n_samples, REFERENCE_VERSION))
    title_style = ParagraphStyle("BandTitleAligned", parent=styles["BandTitle"],
                                 alignment=align)
    meta_style = ParagraphStyle("BandSubAligned", parent=styles["BandSub"],
                                alignment=align)
    tbl = Table([[Paragraph(title, title_style)],
                 [Paragraph(meta, meta_style)]])
    tbl.setStyle(TableStyle([
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, 0), 0),
        ("BOTTOMPADDING", (0, -1), (-1, -1), 6),
        # One thin horizontal rule under the header, no filled band.
        ("LINEBELOW", (0, -1), (-1, -1), 0.75, hx("grid")),
    ]))
    return tbl


def header_block(styles, title, n_samples, settings=None):
    """Full report header: optional letterhead followed by the title band."""
    settings = settings or DEFAULT_SETTINGS
    return letterhead(styles, settings) + [header_band(styles, title, n_samples,
                                                        settings)]


def legend(styles, settings=None):
    """The status key, matching the on-screen overview legend.

    In colour mode each tier is prefixed with its coloured dot (the same
    red/orange/amber/green/grey as the grid); in monochrome mode the dots are
    dropped so the words alone still read correctly in black & white.
    """
    items = [
        ("validated", "<b>Resistant</b>: validated marker"),
        ("candidate", "<b>Candidate</b>: candidate marker"),
        ("potential", "Potential: uncertain marker"),
        ("nomarker", "No marker: assessed, clear"),
        ("notassessed", "Not assessed: no coverage"),
    ]
    sep = " &nbsp;&middot;&nbsp; "
    if _color_on(settings):
        parts = ['<font color="%s">&#9679;</font>&nbsp;%s'
                 % (CHIP_COLORS[key][1], text) for key, text in items]
    else:
        parts = [text for _key, text in items]
    return Paragraph(sep.join(parts), styles["Cell"])


# ---------------------------------------------------------------------------
# Drug x Sample status matrix (combined overview, full panel)
# ---------------------------------------------------------------------------
def status_matrix(calls, coverage, samples, styles, meta=None, settings=None):
    """Drug x sample status matrix.

    In colour mode each verdict is a soft tinted chip (matching the overview
    grid); in monochrome mode it is the status *word* (bold for validated/
    candidate) over faint zebra stripes. Returns [table, footnote].
    """
    color = _color_on(settings)
    cov_idx = coverage_index(coverage)
    drugs = panel_drugs(calls)
    header = [Paragraph("<b>Drug</b>", styles["Cell"])]
    header += [Paragraph("<b>%s</b>" % sample_label(s, meta), styles["Cell"])
               for s in samples]
    data = [header]
    cmds = [
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (1, 0), (-1, -1), "CENTER"),
        ("LINEBELOW", (0, 0), (-1, 0), 0.75, hx("grid")),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]
    _decorate(cmds, len(drugs) + 1, zebra=not color)
    chips = []
    for r, drug in enumerate(drugs, start=1):
        cells = [Paragraph(drug, styles["Cell"])]
        for col, s in enumerate(samples, start=1):
            key, _, _ = drug_status(s, drug, calls, cov_idx)
            cells.append(status_para(key, styles, short=True, settings=settings))
            if color:
                bg = _chip_bg(key)
                if bg is not None:
                    chips.append(("BACKGROUND", (col, r), (col, r), bg))
        data.append(cells)
    cmds += chips
    n = len(samples)
    first = 3.0 * cm
    rest = min(3.6 * cm, (24 * cm - first) / max(1, n))
    tbl = Table(data, colWidths=[first] + [rest] * n, hAlign="LEFT")
    tbl.setStyle(TableStyle(cmds))
    return [tbl]


# ---------------------------------------------------------------------------
# Per-sample drug panel (full panel, the clinical view)
# ---------------------------------------------------------------------------
def drug_panel(sample, calls, coverage, styles, settings=None):
    """Per-sample drug panel: verdict as a tinted chip (colour mode) or word
    (mono), header wash and faint stripes. Returns [table, footnote]."""
    color = _color_on(settings)
    cov_idx = coverage_index(coverage)
    drugs = panel_drugs(calls)
    header = ["Drug", "Status", "Informing gene(s)", "Finding"]
    data = [[Paragraph("<b>%s</b>" % h, styles["Cell"]) for h in header]]
    cmds = [
        ("LINEBELOW", (0, 0), (-1, 0), 0.75, hx("grid")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]
    _decorate(cmds, len(drugs) + 1, zebra=not color)
    for r, drug in enumerate(drugs, start=1):
        key, genes, finding = drug_status(sample, drug, calls, cov_idx)
        data.append([
            Paragraph(drug, styles["Cell"]),
            status_para(key, styles, short=False, settings=settings),
            Paragraph(genes, styles["Cell"]),
            Paragraph(finding, styles["Cell"]),
        ])
        if color:
            bg = _chip_bg(key)
            if bg is not None:
                cmds.append(("BACKGROUND", (1, r), (1, r), bg))
    tbl = Table(data, colWidths=[3.2 * cm, 5.4 * cm, 3.6 * cm, 4.4 * cm],
                hAlign="LEFT")
    tbl.setStyle(TableStyle(cmds))
    return [tbl]


# ---------------------------------------------------------------------------
# Coverage matrix (sample x gene)
# ---------------------------------------------------------------------------
def cov_key(status):
    st = (status or "").upper()
    return "ok" if st == "OK" else "low" if st == "LOW_COVERAGE" else "no"


def cov_text(status):
    st = (status or "").upper()
    return "OK" if st == "OK" else "Low" if st == "LOW_COVERAGE" else "None"


def coverage_matrix(coverage, samples, styles, max_width=24.0, meta=None,
                    settings=None):
    color = _color_on(settings)
    genes, label, by = [], {}, {}
    for r in coverage:
        gid = r.get("Gene_ID", "")
        if gid not in genes:
            genes.append(gid)
            label[gid] = gene_display(gid, r.get("Gene"))
        by[(r.get("Sample"), gid)] = r.get("Status", "")
    header = [Paragraph("<b>Sample</b>", styles["Cell"])]
    header += [Paragraph("<b>%s</b>" % label[g], styles["Cell"]) for g in genes]
    data = [header]
    cmds = [
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (1, 0), (-1, -1), "CENTER"),
        ("LINEBELOW", (0, 0), (-1, 0), 0.75, hx("grid")),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]
    _decorate(cmds, len(samples) + 1, zebra=not color)
    for r, sample in enumerate(samples, start=1):
        cells = [Paragraph(sample_label(sample, meta), styles["Cell"])]
        for col, g in enumerate(genes, start=1):
            st = by.get((sample, g), "")
            cells.append(cov_para(st, styles, settings=settings))
            if color:
                bg = _chip_bg(cov_key(st))
                if bg is not None:
                    cmds.append(("BACKGROUND", (col, r), (col, r), bg))
        data.append(cells)
    n = len(genes)
    first = 3.0 * cm
    rest = min(3.0 * cm, (max_width * cm - first) / max(1, n))
    tbl = Table(data, colWidths=[first] + [rest] * n, hAlign="LEFT")
    tbl.setStyle(TableStyle(cmds))
    return [tbl, Paragraph(
        "Per-amplicon depth: OK \u2265\u200910\u00d7 \u00b7 Low 1\u20139\u00d7 "
        "\u00b7 None 0\u00d7.", styles["Footnote"])]


# ---------------------------------------------------------------------------
# Read-level QC table (NanoStat)
# ---------------------------------------------------------------------------
def _qc_int(metrics, key):
    try:
        return "{:,}".format(int(float(metrics.get(key, ""))))
    except (TypeError, ValueError):
        return "\u2013"


def _qc_mb(metrics, key):
    try:
        return "%.1f" % (float(metrics.get(key, "")) / 1e6)
    except (TypeError, ValueError):
        return "\u2013"


def _qc_num(metrics, key, fmt="%.0f"):
    try:
        return fmt % float(metrics.get(key, ""))
    except (TypeError, ValueError):
        return "\u2013"


def qc_table(qc, samples, styles, max_width=24.0, meta=None):
    """Per-sample read-level QC (reads, yield, length, quality) after trimming."""
    cols = ["Sample", "Reads", "Yield (Mb)", "Median len (bp)", "N50 (bp)", "Mean Q"]
    data = [[Paragraph("<b>%s</b>" % h, styles["Cell"]) for h in cols]]
    cmds = [
        ("LINEBELOW", (0, 0), (-1, 0), 0.75, hx("grid")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (1, 1), (-1, -1), "RIGHT"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]
    _decorate(cmds, len(samples) + 1)
    for s in samples:
        m = qc.get(s, {})
        data.append([
            Paragraph(sample_label(s, meta), styles["Cell"]),
            Paragraph(_qc_int(m, "number_of_reads"), styles["CellR"]),
            Paragraph(_qc_mb(m, "number_of_bases"), styles["CellR"]),
            Paragraph(_qc_num(m, "median_read_length"), styles["CellR"]),
            Paragraph(_qc_num(m, "n50"), styles["CellR"]),
            Paragraph(_qc_num(m, "mean_qual", "%.1f"), styles["CellR"]),
        ])
    n = len(cols) - 1
    first = 3.0 * cm
    rest = min(3.4 * cm, (max_width * cm - first) / max(1, n))
    tbl = Table(data, colWidths=[first] + [rest] * n, hAlign="LEFT")
    tbl.setStyle(TableStyle(cmds))
    return [tbl, Paragraph(
        "Post-trim reads retained at 300\u20138000 bp.", styles["Footnote"])]


# ---------------------------------------------------------------------------
# Collection sites (optional, from sample metadata sidecar)
# ---------------------------------------------------------------------------
def _has_sites(samples, meta):
    """True if any sample carries a region/district/GPS/date worth tabulating."""
    if not meta:
        return False
    fields = ("Region", "District", "Latitude", "Longitude", "Collection_date",
              "Case_classification", "Age_years")
    for s in samples:
        info = meta.get(s) or {}
        if any((info.get(f) or "").strip() for f in fields):
            return True
    return False


def collection_sites_table(samples, meta, styles, max_width=24.0):
    """Per-sample collection metadata: ID, alias, region, district, GPS, date.

    ``ID`` is the stable short label code (``Sample_UID``) the GUI mints for
    each barcode; it is what gets written onto a tube/plate so the row links
    back to a physical sample.
    """
    cols = ["Sample", "ID", "Alias", "Region", "District", "Case", "Age", "Lat",
            "Lon", "Collected"]
    data = [[Paragraph("<b>%s</b>" % h, styles["Cell"]) for h in cols]]
    cmds = [
        ("LINEBELOW", (0, 0), (-1, 0), 0.75, hx("grid")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]
    _decorate(cmds, len(samples) + 1)

    def _cell(info, key):
        return (info.get(key) or "").strip() or "\u2013"

    for s in samples:
        info = meta.get(s) or {}
        data.append([
            Paragraph(s, styles["Cell"]),
            Paragraph(_cell(info, "Sample_UID"), styles["Cell"]),
            Paragraph(_cell(info, "Alias"), styles["Cell"]),
            Paragraph(_cell(info, "Region"), styles["Cell"]),
            Paragraph(_cell(info, "District"), styles["Cell"]),
            Paragraph(_cell(info, "Case_classification"), styles["Cell"]),
            Paragraph(_cell(info, "Age_years"), styles["CellR"]),
            Paragraph(_cell(info, "Latitude"), styles["CellR"]),
            Paragraph(_cell(info, "Longitude"), styles["CellR"]),
            Paragraph(_cell(info, "Collection_date"), styles["Cell"]),
        ])
    n = len(cols) - 1
    first = 2.4 * cm
    rest = min(2.9 * cm, (max_width * cm - first) / max(1, n))
    tbl = Table(data, colWidths=[first] + [rest] * n, hAlign="LEFT")
    tbl.setStyle(TableStyle(cmds))
    return tbl


# Inline tags reportlab Paragraph understands; anything else from the notes
# field is escaped so stored markup can never break the PDF build.
_NOTES_ALLOWED = ("b", "i", "u", "br", "br/")


def _safe_notes_html(text):
    """Keep our bold/italic/underline/break subset; escape everything else."""
    import re
    out = []
    pos = 0
    for m in re.finditer(r"<(/?)([a-zA-Z/]+)\s*/?>", text or ""):
        out.append(_xml_escape(text[pos:m.start()]))
        tag = m.group(2).lower()
        if tag in _NOTES_ALLOWED:
            out.append(m.group(0))
        else:
            out.append(_xml_escape(m.group(0)))
        pos = m.end()
    out.append(_xml_escape((text or "")[pos:]))
    return "".join(out)


def _xml_escape(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def collection_notes_block(samples, meta, styles):
    """Flowables rendering per-sample rich-text notes, or [] when none.

    Notes are stored as a small HTML subset (``<b>/<i>/<u>/<br/>``) which
    reportlab Paragraph renders directly; any other markup is escaped first.
    """
    rows = []
    for s in samples:
        info = meta.get(s) or {}
        note = (info.get("Notes") or "").strip()
        if note:
            rows.append((s, info, note))
    if not rows:
        return []
    flow = [Paragraph("Field notes", styles["H3"])]
    for s, info, note in rows:
        head = sample_label(s, meta)
        uid = (info.get("Sample_UID") or "").strip()
        if uid:
            head = "%s (%s)" % (head, uid)
        flow.append(Paragraph("<b>%s</b>" % _xml_escape(head), styles["Cell"]))
        flow.append(Paragraph(_safe_notes_html(note), styles["Cell"]))
        flow.append(Spacer(1, 4))
    return flow


# ---------------------------------------------------------------------------
# Molecular evidence (variant table)
# ---------------------------------------------------------------------------
def truncate(text, limit=24):
    if text and len(text) > limit:
        return text[:limit] + "\u2026"
    return text or ""


def change_display(aa_change, consequence):
    """Compact amino-acid change. Frameshifts retranslate the whole downstream
    protein, so the raw AA string is noise here -- the Effect column already
    says 'frameshift', and the full translation stays in variant_detail.csv."""
    if "frameshift" in (consequence or "").lower():
        return "\u2013"
    return truncate(aa_change, 18)


def fmt_af(af):
    try:
        return "%.0f%%" % (float(af) * 100)
    except (TypeError, ValueError):
        return "-"


def variants_table(rows, styles):
    if not rows:
        return [Paragraph("No coding variants detected.", styles["Muted"])]
    header = ["Gene", "Change", "Effect", "Catalog", "AF", "Depth", "Qual"]
    data = [[Paragraph("<b>%s</b>" % h, styles["Cell"]) for h in header]]
    cmds = [
        ("LINEBELOW", (0, 0), (-1, 0), 0.75, hx("grid")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (4, 1), (-1, -1), "RIGHT"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]
    _decorate(cmds, len(rows) + 1)
    for r in rows:
        cat = (r.get("Catalog_status", "") or "").lower()
        cat_style = styles["CellBold"] if "known" in cat else styles["Cell"]
        data.append([
            Paragraph(gene_display(r.get("Gene_ID", ""), r.get("Gene")), styles["Cell"]),
            Paragraph(change_display(r.get("AA_Change", ""), r.get("Consequence", "")),
                      styles["Cell"]),
            Paragraph(r.get("Consequence", ""), styles["Cell"]),
            Paragraph(catalog_display(r.get("Catalog_status", "")), cat_style),
            Paragraph(fmt_af(r.get("AF", "")), styles["CellR"]),
            Paragraph(r.get("DP", ""), styles["CellR"]),
            Paragraph(r.get("QUAL", ""), styles["CellR"]),
        ])
    tbl = Table(data, colWidths=[2.4 * cm, 3.4 * cm, 2.2 * cm, 2.6 * cm,
                                 1.6 * cm, 1.8 * cm, 1.6 * cm], hAlign="LEFT")
    tbl.setStyle(TableStyle(cmds))
    return [tbl, Paragraph(
        "Pass filters: QUAL \u226515, depth \u226510, MQ \u226520. "
        "Known = WHO marker (bold); Novel otherwise.", styles["Footnote"])]


def variants_caption(styles):
    """One-line legend for the variant table columns."""
    return Paragraph(
        "Coding changes in the panel genes. "
        "<b>Catalog</b> = known WHO marker vs novel \u00b7 "
        "<b>AF</b> = variant read fraction \u00b7 "
        "<b>Depth</b> = reads at site \u00b7 "
        "<b>Qual</b> = caller confidence.", styles["Muted"])


# ---------------------------------------------------------------------------
# Summary line
# ---------------------------------------------------------------------------
def summary_line(calls, samples, styles):
    flagged = sorted({c.get("Drug", "?") for c in calls
                      if classify_tier(c.get("Classification", "")) in TIER_ORDER})
    if flagged:
        txt = ("<b>%d</b> sample(s) analysed. Resistance markers detected for: "
               "<b>%s</b>." % (len(samples), ", ".join(flagged)))
    else:
        txt = ("<b>%d</b> sample(s) analysed. No resistance markers detected."
               % len(samples))
    return Paragraph(txt, styles["Body"])


# ---------------------------------------------------------------------------
# Clinical interpretation & treatment considerations (per-sample)
# ---------------------------------------------------------------------------
def treatment_block(sample, calls, coverage, styles, settings=None):
    """Decision-support treatment considerations for one sample.

    Maps each drug's molecular status to a plain-language consideration
    (Avoid / Use with caution / Monitor / Likely effective / No data) via
    ``TREATMENT_META``. Monochrome: the consideration is a word only, bold when
    the underlying status is validated/candidate. This is supportive evidence,
    not a prescription: artemisinin-based combination therapies (ACTs) remain
    first-line and every choice must be confirmed against current national
    treatment guidelines.
    """
    color = _color_on(settings)
    cov_idx = coverage_index(coverage)
    drugs = panel_drugs(calls)
    header = ["Drug", "Consideration", "Clinical guidance"]
    data = [[Paragraph("<b>%s</b>" % h, styles["Cell"]) for h in header]]
    cmds = [
        ("LINEBELOW", (0, 0), (-1, 0), 0.75, hx("grid")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]
    _decorate(cmds, len(drugs) + 1, zebra=not color)
    art_flag = False
    for r, drug in enumerate(drugs, start=1):
        key, _genes, _finding = drug_status(sample, drug, calls, cov_idx)
        short, guidance = TREATMENT_META.get(key, TREATMENT_META["notassessed"])
        if key == "validated" and "Artemisinin" in drug:
            art_flag = True
        bold = key in _EMPHASISED_STATUS
        if color:
            ink = CHIP_COLORS.get(key, (None, PALETTE["text"]))[1]
            body = ("<b>%s</b>" % short) if bold else short
            consideration = Paragraph('<font color="%s">%s</font>' % (ink, body),
                                      styles["Cell"])
            bg = _chip_bg(key)
            if bg is not None:
                cmds.append(("BACKGROUND", (1, r), (1, r), bg))
        else:
            consideration = Paragraph(
                short, styles["CellBold"] if bold else styles["Cell"])
        data.append([
            Paragraph(drug, styles["Cell"]),
            consideration,
            Paragraph(guidance, styles["Cell"]),
        ])
    tbl = Table(data, colWidths=[3.2 * cm, 3.2 * cm, 10.2 * cm], hAlign="LEFT")
    tbl.setStyle(TableStyle(cmds))

    flow = [tbl, Spacer(1, 5)]
    if art_flag:
        flow.append(Paragraph(
            "<b>Note:</b> validated artemisinin marker (pfk13) detected. ACT "
            "remains first-line; monitor day-3 parasitaemia and escalate per "
            "national guidelines.", styles["Muted"]))
    else:
        flow.append(Paragraph(
            "ACT remains first-line unless a validated marker or clinical "
            "failure indicates otherwise. Confirm against current national "
            "guidelines.", styles["Muted"]))
    return flow


# ---------------------------------------------------------------------------
# Footer
# ---------------------------------------------------------------------------
def make_footer(styles, footer_text=None):
    default = ("Research / surveillance use; confirm before clinical action. "
               "'Not assessed' is not evidence of sensitivity. Reference %s."
               % REFERENCE_VERSION)
    note = Paragraph((footer_text or "").strip() or default, styles["Muted"])

    def footer(canvas, doc):
        canvas.saveState()
        w, _ = doc.pagesize
        note.wrapOn(canvas, w - 3 * cm, 2 * cm)
        note.drawOn(canvas, 1.5 * cm, 0.9 * cm)
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(hx("muted"))
        canvas.drawRightString(w - 1.5 * cm, 0.9 * cm, "Page %d" % doc.page)
        canvas.restoreState()

    return footer


# ---------------------------------------------------------------------------
# Document builders
# ---------------------------------------------------------------------------
def build_combined(calls, variants, coverage, qc, samples, out_path, run_name,
                   meta=None, settings=None):
    settings = settings or DEFAULT_SETTINGS
    styles = build_styles()
    doc = SimpleDocTemplate(
        out_path, pagesize=landscape(page_size(settings)),
        leftMargin=1.5 * cm, rightMargin=1.5 * cm,
        topMargin=1.2 * cm, bottomMargin=1.7 * cm,
        title="Resistance Report")
    flow = header_block(styles, run_name or "Drug-Resistance Report",
                        len(samples), settings)
    flow += [Spacer(1, 6), legend(styles, settings), Spacer(1, 8),
             summary_line(calls, samples, styles), Spacer(1, 10)]

    flow.append(Paragraph("Drug resistance overview", styles["H2"]))
    flow.extend(status_matrix(calls, coverage, samples, styles, meta=meta,
                              settings=settings))
    flow.append(Spacer(1, 14))

    flow.append(Paragraph("Per-sample detail", styles["H2"]))
    for i, sample in enumerate(samples):
        block = [
            Paragraph("Sample: %s" % sample_label(sample, meta), styles["H3"]),
        ]
        block.extend(drug_panel(sample, calls, coverage, styles,
                                settings=settings))
        if settings.get("include_treatment", True):
            block += [
                Spacer(1, 4),
                Paragraph("Clinical interpretation & treatment considerations",
                          styles["H3"]),
            ]
            block.extend(treatment_block(sample, calls, coverage, styles,
                                         settings=settings))
        if settings.get("include_variants", True):
            block += [
                Spacer(1, 4),
                Paragraph("Supporting variants", styles["H3"]),
                variants_caption(styles),
                Spacer(1, 3),
            ]
            block.extend(variants_table(
                [r for r in variants if r.get("Sample") == sample], styles))
        flow.append(KeepTogether(block))
        if i != len(samples) - 1:
            flow.append(Spacer(1, 12))
    flow.append(Spacer(1, 14))

    if settings.get("include_qc", True) or settings.get("include_coverage", True):
        qc_block = [Paragraph("Quality control", styles["H2"])]
        if qc and settings.get("include_qc", True):
            qc_block.append(Paragraph("Sequencing quality (after trimming)",
                                      styles["H3"]))
            qc_block.extend(qc_table(qc, samples, styles, meta=meta))
            qc_block.append(Spacer(1, 8))
        if coverage and settings.get("include_coverage", True):
            qc_block.append(Paragraph("Target gene coverage", styles["H3"]))
            qc_block.extend(coverage_matrix(coverage, samples, styles, meta=meta,
                                            settings=settings))
        if len(qc_block) > 1:
            flow.append(KeepTogether(qc_block))

    if settings.get("include_site", True) and _has_sites(samples, meta):
        flow.append(Spacer(1, 14))
        flow.append(KeepTogether([
            Paragraph("Collection sites", styles["H2"]),
            collection_sites_table(samples, meta, styles),
        ]))
    notes_flow = collection_notes_block(samples, meta, styles)
    if notes_flow:
        flow.append(Spacer(1, 12))
        flow.append(KeepTogether(notes_flow))

    footer = make_footer(styles, settings.get("footer"))
    doc.build(flow, onFirstPage=footer, onLaterPages=footer)
    return out_path


def build_per_sample(sample, calls, variants, coverage, qc, out_path, run_name,
                     meta=None, settings=None):
    settings = settings or DEFAULT_SETTINGS
    styles = build_styles()
    doc = SimpleDocTemplate(
        out_path, pagesize=page_size(settings),
        leftMargin=1.5 * cm, rightMargin=1.5 * cm,
        topMargin=1.2 * cm, bottomMargin=1.7 * cm,
        title="Resistance Report %s" % sample)
    s_calls = [r for r in calls if r.get("Sample") == sample]
    s_cov = [r for r in coverage if r.get("Sample") == sample]
    s_qc = {sample: qc[sample]} if sample in qc else {}
    flow = header_block(
        styles, run_name or ("Sample %s" % sample_label(sample, meta)), 1,
        settings)
    flow += [Spacer(1, 6), legend(styles, settings), Spacer(1, 8),
             summary_line(s_calls, [sample], styles), Spacer(1, 10)]

    flow.append(Paragraph("Drug resistance summary", styles["H2"]))
    flow.extend(drug_panel(sample, calls, coverage, styles, settings=settings))
    flow.append(Spacer(1, 12))

    if settings.get("include_treatment", True):
        flow.append(Paragraph("Clinical interpretation & treatment "
                              "considerations", styles["H2"]))
        flow.extend(treatment_block(sample, calls, coverage, styles,
                                    settings=settings))
        flow.append(Spacer(1, 12))

    if settings.get("include_variants", True):
        flow.append(Paragraph("Supporting variants", styles["H2"]))
        flow.append(variants_caption(styles))
        flow.append(Spacer(1, 3))
        flow.extend(variants_table(
            [r for r in variants if r.get("Sample") == sample], styles))
        flow.append(Spacer(1, 12))

    if settings.get("include_qc", True) or settings.get("include_coverage", True):
        flow.append(Paragraph("Quality control", styles["H2"]))
        if s_qc and settings.get("include_qc", True):
            flow.append(Paragraph("Sequencing quality (after trimming)",
                                  styles["H3"]))
            flow.extend(qc_table(s_qc, [sample], styles, max_width=17.5,
                                 meta=meta))
            flow.append(Spacer(1, 8))
        if s_cov and settings.get("include_coverage", True):
            flow.append(Paragraph("Target gene coverage", styles["H3"]))
            flow.extend(coverage_matrix(s_cov, [sample], styles, max_width=17.5,
                                        meta=meta, settings=settings))

    if settings.get("include_site", True) and _has_sites([sample], meta):
        flow.append(Spacer(1, 12))
        flow.append(Paragraph("Collection site", styles["H2"]))
        flow.append(collection_sites_table([sample], meta, styles,
                                            max_width=17.5))
    notes_flow = collection_notes_block([sample], meta, styles)
    if notes_flow:
        flow.append(Spacer(1, 10))
        flow.extend(notes_flow)

    footer = make_footer(styles, settings.get("footer"))
    doc.build(flow, onFirstPage=footer, onLaterPages=footer)
    return out_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(
        description="Render a color-coded PDF report from the pipeline CSVs.")
    ap.add_argument("--reports_dir", required=True)
    ap.add_argument("--output_dir", required=True)
    ap.add_argument("--mode", choices=["combined", "per-sample", "both"],
                    default="combined")
    ap.add_argument("--run-name", default=None)
    ap.add_argument("--qc_dir", default=None,
                    help="Dir with <sample>/<sample>_nanostat.txt files. "
                         "Defaults to a 'qc_trimmed' sibling of --reports_dir; "
                         "skipped if absent.")
    ap.add_argument("--sample_meta", default=None,
                    help="Optional CSV of per-sample metadata (Sample, Alias, "
                         "Region, District, Latitude, Longitude, "
                         "Collection_date). Adds aliases and a collection-sites "
                         "table; omitted -> unchanged report.")
    ap.add_argument("--settings", default=None,
                    help="Optional JSON with report branding/layout/section "
                         "settings (org_name, title, logo_show, logo_path, "
                         "logo_pos, page_size, include_*, footer). Missing keys "
                         "fall back to defaults.")
    ap.add_argument("--sample", default=None,
                    help="In per-sample mode, render only this sample barcode "
                         "(on-demand single report). Ignored otherwise.")
    args = ap.parse_args()

    if not _HAVE_REPORTLAB:
        sys.exit("ERROR: PDF generation requires ReportLab, which is not "
                 "installed in this environment. Install it with "
                 "'pip install reportlab' (or 'conda install reportlab').")

    os.makedirs(args.output_dir, exist_ok=True)
    calls, variants, coverage = load_data(args.reports_dir)
    samples = all_samples(calls, variants, coverage)
    if not samples:
        sys.exit("ERROR: no samples found in the input CSVs.")

    qc_dir = args.qc_dir or os.path.join(
        os.path.dirname(os.path.abspath(args.reports_dir)), "qc_trimmed")
    qc = load_qc(qc_dir, samples)
    meta = load_sample_meta(args.sample_meta)
    settings = load_settings(args.settings)

    written = []
    if args.mode in ("combined", "both"):
        out = os.path.join(args.output_dir, "resistance_report.pdf")
        build_combined(calls, variants, coverage, qc, samples, out,
                       args.run_name, meta=meta, settings=settings)
        written.append(out)
    if args.mode in ("per-sample", "both"):
        targets = samples
        if args.sample:
            if args.sample not in samples:
                sys.exit("ERROR: sample %r not found in the input CSVs."
                         % args.sample)
            targets = [args.sample]
        for sample in targets:
            out = os.path.join(args.output_dir, "report_%s.pdf" % sample)
            build_per_sample(sample, calls, variants, coverage, qc, out,
                             args.run_name, meta=meta, settings=settings)
            written.append(out)

    for path in written:
        print("Wrote %s" % path)


if __name__ == "__main__":
    main()
