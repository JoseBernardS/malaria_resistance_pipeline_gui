"""CSV-backed, cross-run data layer for the Trends screen.

Qt-free and unit-testable: the Trends screen stays thin by delegating all
loading and aggregation here. Rather than the orphaned ``sample_outcome`` DB
tables (which the surveillance map used), this reads each completed run's
``final_reports/`` CSVs directly, tags every row with the run's ``job_id`` and
``run_date``, and returns concatenated lists spanning *all* runs. Runs whose
output dir was deleted simply don't appear.

Barcodes are reused across sequencing runs, so the same ``barcode07`` can mean
two different samples. Rows are therefore keyed on ``(job_id, sample)``
internally so identical barcodes from different runs never collide.
"""

import csv
import datetime
import os
import re
import sys

from .. import db, paths

# Reuse the report module's tier classifier so tiers match the dashboard
# exactly. Same fallback pattern as gui/queue.py.
sys.path.insert(0, paths.src_dir())
try:
    from generate_report import classify_tier as _classify_tier
except Exception:  # pragma: no cover - fallback if src not importable
    _classify_tier = None


def classify_tier(classification):
    """Tier key for a resistance Classification string ("validated", ...)."""
    if _classify_tier is not None:
        return _classify_tier(classification)
    c = (classification or "").lower()
    if "validated" in c:
        return "validated"
    if "candidate" in c:
        return "candidate"
    return "potential"


# The three CSVs the pipeline writes per run, in final_reports/.
_CALLS_FILE = "resistance_calls.csv"
_VARIANTS_FILE = "variant_detail.csv"
_COVERAGE_FILE = "coverage_report.csv"

# A sample counts as carrying a marker if its worst tier is a resistance tier.
_RESISTANT_TIERS = ("validated", "candidate", "potential")


class RunBundle(object):
    """Concatenated, run-tagged rows across every completed run.

    ``calls``, ``variants`` and ``coverage`` are ``list[dict]`` where every row
    carries an added ``job_id``, ``run_date`` (the run's ``YYYY-MM-DD`` finish
    date) and ``date`` — the sample's *effective* surveillance date: its
    ``collection_date`` when recorded, else the run date. ``dates`` is the
    sorted distinct set of effective dates seen (drives the trend x-axis and the
    From/To picker span).
    """

    def __init__(self, calls, variants, coverage, dates):
        self.calls = calls
        self.variants = variants
        self.coverage = coverage
        self.dates = dates


def _run_date(job):
    """Format a job's completion time as ``YYYY-MM-DD``, or "" if unknown."""
    ts = job.get("finished_at")
    if not ts:
        return ""
    try:
        return datetime.datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def _reports_dir(output_dir):
    """The final_reports dir for a run, falling back to the output dir."""
    fr = os.path.join(output_dir, "final_reports")
    return fr if os.path.isdir(fr) else output_dir


def _read_csv(path):
    """Best-effort stdlib CSV read; [] on any missing file or error."""
    if not os.path.isfile(path):
        return []
    try:
        with open(path, newline="") as fh:
            return [dict(r) for r in csv.DictReader(fh)]
    except Exception:
        return []


def _valid_iso_date(value):
    """True iff ``value`` is a strict ``YYYY-MM-DD`` date string."""
    if not value:
        return False
    try:
        datetime.date.fromisoformat(str(value))
        return True
    except (ValueError, TypeError):
        return False


def _collection_dates(job_id):
    """``{sample: collection_date}`` for a job, from the ``sample_meta`` DB.

    The date is the epidemiological one an operator recorded for the sample
    (job form or Results editor); ``{}`` on any lookup error so a DB hiccup
    just means we fall back to run dates.
    """
    try:
        meta = db.list_sample_meta(job_id)
    except Exception:
        return {}
    return {s: row.get("collection_date") for s, row in meta.items()}


def _tag(rows, job_id, run_date, coll):
    """Stamp provenance on each row plus its effective surveillance ``date``.

    ``date`` is the sample's ``collection_date`` when it is a valid ISO date,
    otherwise the run's finish date — so a sample always plots somewhere, on
    its collection day when known and on the analysis day when not.
    """
    for r in rows:
        r["job_id"] = job_id
        r["run_date"] = run_date
        cd = coll.get(r.get("Sample"))
        r["date"] = cd if _valid_iso_date(cd) else run_date
    return rows


def load_all_runs():
    """Read every completed run's CSVs into one cross-run bundle.

    Enumerates jobs via ``db.list_jobs()``; for each ``status == "completed"``
    job with an existing ``output_dir`` reads the three ``final_reports/`` CSVs,
    tagging each row with the run's ``job_id``/``run_date`` and each sample's
    effective ``date`` (its ``collection_date`` when recorded, else the run
    date). Guarded per file and per job — a bad run is skipped, never raised.
    """
    calls, variants, coverage = [], [], []
    try:
        jobs = db.list_jobs()
    except Exception:
        jobs = []
    for job in jobs:
        if job.get("status") != "completed":
            continue
        output_dir = job.get("output_dir")
        if not output_dir or not os.path.isdir(output_dir):
            continue
        job_id = job.get("id")
        run_date = _run_date(job)
        coll = _collection_dates(job_id)
        rd = _reports_dir(output_dir)
        calls += _tag(_read_csv(os.path.join(rd, _CALLS_FILE)),
                      job_id, run_date, coll)
        variants += _tag(_read_csv(os.path.join(rd, _VARIANTS_FILE)),
                         job_id, run_date, coll)
        coverage += _tag(_read_csv(os.path.join(rd, _COVERAGE_FILE)),
                         job_id, run_date, coll)
    dates = {r["date"] for rows in (calls, variants, coverage)
             for r in rows if r.get("date")}
    return RunBundle(calls, variants, coverage, sorted(dates))


# ---------------------------------------------------------------------------
# Pure date scoping
# ---------------------------------------------------------------------------
def _date_ok(row, date_from, date_to):
    """Keep rows whose effective ``date`` lies within ``[date_from, date_to]``.

    Bounds are inclusive zero-padded ISO ``YYYY-MM-DD`` strings (either may be
    None for an open end). Lexical comparison is valid for ISO dates. Rows with
    a blank date are kept only when no bound is set.
    """
    if date_from is None and date_to is None:
        return True
    rd = row.get("date") or ""
    if not rd:
        return False
    if date_from is not None and rd < date_from:
        return False
    if date_to is not None and rd > date_to:
        return False
    return True


def filter_rows(rows, *, date_from=None, date_to=None):
    """Rows whose effective ``date`` falls in ``[date_from, date_to]`` (any when
    both bounds are None). Works for calls, variants and coverage alike."""
    if date_from is None and date_to is None:
        return list(rows)
    return [r for r in rows if _date_ok(r, date_from, date_to)]


def date_span(run_dates):
    """The ``(min, max)`` ISO run date across a bundle, or ``(None, None)``.

    Drives the dynamic From/To date range: the pickers open on the real span
    of completed runs instead of an arbitrary calendar window.
    """
    dates = [d for d in (run_dates or []) if d]
    if not dates:
        return None, None
    return min(dates), max(dates)


# ---------------------------------------------------------------------------
# Aggregation helpers (pure, testable)
# ---------------------------------------------------------------------------
def _worst_tier(tiers):
    """Most-concerning tier among ``tiers`` (validated < candidate < ...)."""
    for t in _RESISTANT_TIERS:
        if t in tiers:
            return t
    return "nomarker"


def worst_tier_by_sample(calls):
    """Map each ``(job_id, sample)`` to its worst resistance tier.

    Ranks validated > candidate > potential; a sample with no resistant call
    is ``"nomarker"``. Keyed on ``(job_id, sample)`` so a barcode reused across
    runs stays distinct.
    """
    tiers = {}
    for c in calls:
        k = (c.get("job_id"), c.get("Sample"))
        tier = classify_tier(c.get("Classification"))
        tiers.setdefault(k, set()).add(tier)
    return {k: _worst_tier(ts) for k, ts in tiers.items()}


def _rows_by_run(rows):
    """``{job_id: {sample, ...}}`` — the distinct samples seen per run."""
    runs = {}
    for r in rows:
        runs.setdefault(r.get("job_id"), set()).add(r.get("Sample"))
    return runs


def sample_dates(*rowlists):
    """``{(job_id, sample): effective date}`` across any of the given row lists.

    The effective date is what ``load_all_runs`` stamped on each row: the
    sample's ``collection_date`` when known, else the run date. Keyed per
    ``(job_id, sample)`` so prevalence buckets on the day each sample was
    collected rather than the day its run happened to finish.
    """
    dates = {}
    for rows in rowlists:
        for r in rows:
            k = (r.get("job_id"), r.get("Sample"))
            if k not in dates:
                dates[k] = r.get("date") or ""
    return dates


def sample_universe(coverage, calls=None, variants=None):
    """``{job_id: {sample, ...}}`` of every *sequenced* sample per run.

    The pipeline's ``resistance_calls.csv`` lists only flagged samples, so it
    is the wrong denominator for prevalence. Coverage carries a row per sample
    per gene and therefore spans every sequenced sample; we union in
    calls/variants so a run missing its coverage CSV still contributes its
    known samples rather than vanishing.
    """
    universe = _rows_by_run(coverage)
    for rows in (calls or [], variants or []):
        for job_id, samples in _rows_by_run(rows).items():
            universe.setdefault(job_id, set()).update(samples)
    return universe


def universe_keys(universe):
    """Flatten a universe to the set of ``(job_id, sample)`` keys."""
    return {(job_id, s) for job_id, samples in universe.items()
            for s in samples}


def flagged_keys(calls, drug=None):
    """``(job_id, sample)`` keys with a resistant call (optionally for a drug).

    A "resistant call" is any call whose tier is validated/candidate/potential.
    Pass ``drug`` to restrict to samples flagged for that one drug.
    """
    keys = set()
    for c in calls:
        if drug is not None and (c.get("Drug") or "") != drug:
            continue
        if classify_tier(c.get("Classification")) in _RESISTANT_TIERS:
            keys.add((c.get("job_id"), c.get("Sample")))
    return keys


def carrier_keys(variants, gene, aa_change):
    """``(job_id, sample)`` keys carrying a specific ``gene``/``aa_change``."""
    keys = set()
    for v in variants:
        g = v.get("Gene") or v.get("Gene_ID") or ""
        if g != gene:
            continue
        if (v.get("AA_Change") or "").strip() != aa_change:
            continue
        keys.add((v.get("job_id"), v.get("Sample")))
    return keys


def _series_over_universe(positive_keys, universe, sample_dates):
    """Per-date fraction of universe samples that are in ``positive_keys``.

    Returns ``[(date, frac, n_samples)]`` sorted by date, where each sample is
    placed on its effective date (``collection_date`` when known, else the run
    date) so samples pool by *collection day* across runs, not by run. ``n`` is
    that day's sequenced-sample count.
    """
    by_date = {}                 # date -> [n_samples, n_positive]
    for job_id, samples in universe.items():
        for s in samples:
            date = sample_dates.get((job_id, s), "")
            if not date:
                continue
            acc = by_date.setdefault(date, [0, 0])
            acc[0] += 1
            if (job_id, s) in positive_keys:
                acc[1] += 1
    out = [(date, (pos / n) if n else 0.0, n)
           for date, (n, pos) in by_date.items()]
    out.sort(key=lambda t: t[0])
    return out


def prevalence_series(calls, universe, sample_dates):
    """Per-date share of *sequenced* samples carrying any resistance marker.

    Feeds ``TrendChart.set_data(points, mode="prevalence")``.
    """
    return _series_over_universe(flagged_keys(calls), universe, sample_dates)


def drug_prevalence_series(calls, drug, universe, sample_dates):
    """Per-date share of sequenced samples flagged resistant to one drug."""
    return _series_over_universe(
        flagged_keys(calls, drug=drug), universe, sample_dates)


def mutation_prevalence_series(variants, gene, aa_change, universe,
                               sample_dates):
    """Per-date share of sequenced samples carrying one gene/AA change."""
    return _series_over_universe(
        carrier_keys(variants, gene, aa_change), universe, sample_dates)


def drug_counts(calls, keys=None):
    """Distinct samples flagged per drug, sorted desc.

    Returns ``[(drug, n_distinct_samples)]`` — a sample counts once per drug it
    has any resistance call for. Pass ``keys`` to restrict to a sample subset
    (e.g. the carriers of a focused mutation). Feeds ``BarChart.set_data``.
    """
    by_drug = {}
    for c in calls:
        drug = (c.get("Drug") or "").strip()
        if not drug:
            continue
        k = (c.get("job_id"), c.get("Sample"))
        if keys is not None and k not in keys:
            continue
        if classify_tier(c.get("Classification")) not in _RESISTANT_TIERS:
            continue
        by_drug.setdefault(drug, set()).add(k)
    pairs = [(drug, len(ks)) for drug, ks in by_drug.items() if ks]
    pairs.sort(key=lambda p: (-p[1], p[0]))
    return pairs


def distinct_drugs(calls):
    """Drugs with at least one resistant call, most-flagged first."""
    return [drug for drug, _n in drug_counts(calls)]


# Resistance-tier legend for the drug chart, most concerning first. The tier
# keys double as PALETTE keys, so the StackedBarChart segments pick up the same
# semantic tier colours the dashboard uses and follow the light/dark toggle.
_DRUG_TIERS = [
    ("validated", "Validated"),
    ("candidate", "Candidate"),
    ("potential", "Potential"),
]


def drug_tier_prevalence(calls, universe, keys=None, top=None):
    """Per-drug share of sequenced samples flagged resistant, split by tier.

    For each drug, buckets the distinct samples flagged for it by the *worst*
    tier they reach for that drug (validated > candidate > potential), then
    expresses each bucket as a percentage of the whole sequenced cohort
    (``distinct_samples(universe)``) — the same denominator the mutation
    distribution uses, so both charts read on one prevalence scale. A sample
    counts once per drug (in its worst tier for that drug), so a drug's segments
    sum to its true flagged prevalence.

    Returns ``(categories, groups, data, group_labels)`` for
    ``StackedBarChart.set_data``: ``categories`` the drug names ordered by total
    prevalence desc, ``groups`` the tiers present (worst first), ``data`` a
    mapping ``{drug: {tier: percent}}`` and ``group_labels`` the tier captions.
    Pass ``keys`` to restrict to a sample subset (e.g. a focused mutation's
    carriers); ``top`` caps the number of drug rows.
    """
    total = distinct_samples(universe)
    if not total:
        return [], [], {}, {}

    # drug -> {(job_id, sample): worst tier rank seen for this drug}
    worst = {}
    for c in calls:
        drug = (c.get("Drug") or "").strip()
        if not drug:
            continue
        k = (c.get("job_id"), c.get("Sample"))
        if keys is not None and k not in keys:
            continue
        tier = classify_tier(c.get("Classification"))
        if tier not in _RESISTANT_TIERS:
            continue
        rank = _RESISTANT_TIERS.index(tier)
        cur = worst.setdefault(drug, {})
        if k not in cur or rank < cur[k]:
            cur[k] = rank

    if not worst:
        return [], [], {}, {}

    # Distinct samples per (drug, worst tier), as a % of the sequenced cohort.
    data, totals = {}, {}
    for drug, samples in worst.items():
        counts = {}
        for rank in samples.values():
            tier = _RESISTANT_TIERS[rank]
            counts[tier] = counts.get(tier, 0) + 1
        data[drug] = {t: 100.0 * n / total for t, n in counts.items()}
        totals[drug] = len(samples)

    ranked = sorted(data, key=lambda d: (-totals[d], d))
    if top:
        ranked = ranked[:top]
    data = {d: data[d] for d in ranked}

    shown = {t for seg in data.values() for t in seg}
    groups = [t for t, _lbl in _DRUG_TIERS if t in shown]
    group_labels = {t: lbl for t, lbl in _DRUG_TIERS}
    return ranked, groups, data, group_labels


def distinct_mutations(variants):
    """Distinct mutations as ``[(label, gene, aa_change)]``.

    ``label`` is ``"<gene> <aa_change>"``; ordered by gene then AA position.
    Only mutations with a parseable AA position are listed (they are the ones
    the prevalence series can place).
    """
    seen = {}                    # (gene, aa) -> aa_pos
    for v in variants:
        aa = (v.get("AA_Change") or "").strip()
        pos = _aa_pos(aa)
        if pos is None:
            continue
        gene = v.get("Gene") or v.get("Gene_ID") or "?"
        seen.setdefault((gene, aa), pos)
    items = sorted(seen.items(), key=lambda kv: (kv[0][0], kv[1]))
    return [("%s %s" % (gene, aa), gene, aa) for (gene, aa), _pos in items]


# Tier -> (palette key, legend label), most concerning first.
_STATUS_LABELS = [
    ("validated", "Validated"),
    ("candidate", "Candidate"),
    ("potential", "Potential"),
    ("nomarker", "No marker"),
]


def status_mix(calls, keys):
    """Worst-tier sample counts over an explicit ``(job_id, sample)`` key set.

    Returns ``[(label, value, palette_key)]`` over validated/candidate/
    potential/nomarker, summing to ``len(keys)``. Keys absent from ``calls``
    (sequenced but never flagged) count as ``nomarker``, so the donut reflects
    the whole sequenced cohort, not just the flagged rows. Feeds
    ``DonutChart.set_data``.
    """
    worst = worst_tier_by_sample(calls)
    counts = {}
    for k in keys:
        tier = worst.get(k, "nomarker")
        counts[tier] = counts.get(tier, 0) + 1
    return [(label, counts.get(key, 0), key)
            for key, label in _STATUS_LABELS]


def _aa_pos(aa_change):
    """First integer in an AA-change string (``C59R`` -> 59), or None."""
    m = re.search(r"(\d+)", aa_change or "")
    return int(m.group(1)) if m else None


def _split_aa_change(aa_change):
    """Split an AA-change string into ``(ref, pos, alt)``.

    Handles the pipeline's variety: ``C59R`` -> ``("C", 59, "R")``,
    ``142K`` -> ``("", 142, "K")`` (no reference residue), and multi-residue
    references like ``DN651D`` -> ``("DN", 651, "D")``. Returns ``None`` when
    there is no parseable position.
    """
    m = re.match(r"^\s*([A-Za-z*]*?)(\d+)([A-Za-z*]*)\s*$", aa_change or "")
    if not m:
        return None
    ref, pos, alt = m.group(1), int(m.group(2)), m.group(3)
    return ref, pos, alt


# Legend group order + display labels for the mutation-distribution chart.
_MUT_GROUPS = [("known", "Known marker"), ("novel", "Novel")]


def _catalog_group(catalog_status):
    """Bin a variant's ``Catalog_status`` into ``"known"`` vs ``"novel"``."""
    st = (catalog_status or "").lower()
    return "known" if st == "known_marker_component" else "novel"


def mutation_distribution(variants, universe, top=12):
    """Per-mutation prevalence for the "mutation distribution" chart.

    Pools variants across every run and, for each distinct mutation (a
    ``gene``/AA change, labelled ``"<gene> <aa_change>"`` — e.g.
    ``"PPPK-DHPS 142K"``), measures the share of *sequenced* samples carrying
    it. A sample is counted once per mutation via its ``(job_id, sample)`` key,
    and the denominator is the whole sequenced cohort
    (``distinct_samples(universe)``), so bar heights read as true prevalence.

    Bars are coloured by whether the mutation is a **known** resistance marker
    (``Catalog_status == known_marker_component``) or a **novel**/
    uncharacterised variant — the actionable surveillance question, and one the
    x-axis label doesn't already answer. A mutation's catalogue status is a
    property of the variant, so each bar is a single colour.

    Returns ``(categories, groups, data, group_labels)`` for
    ``StackedBarChart.set_data``: ``categories`` are the mutation labels ordered
    by prevalence desc (top ``top``), ``groups`` the marker classes present
    (known before novel), ``data`` a mapping ``{category: {group: percent}}``,
    and ``group_labels`` the human-readable legend text per group. Variants
    with no parseable AA change are skipped.
    """
    total = distinct_samples(universe)
    if not total:
        return [], [], {}, {}

    # mutation label -> (group, set of (job_id, sample))
    muts = {}
    for v in variants:
        if _split_aa_change(v.get("AA_Change")) is None:
            continue
        gene = v.get("Gene") or v.get("Gene_ID") or "?"
        aa = (v.get("AA_Change") or "").strip()
        label = "%s %s" % (gene, aa)
        group = _catalog_group(v.get("Catalog_status"))
        k = (v.get("job_id"), v.get("Sample"))
        entry = muts.setdefault(label, [group, set()])
        entry[1].add(k)
        if group == "known":         # a catalogued hit wins the colour
            entry[0] = "known"

    if not muts:
        return [], [], {}, {}

    # Rank mutations by distinct carriers desc, take top N.
    ranked = sorted(muts.items(),
                    key=lambda kv: (-len(kv[1][1]), kv[0]))[:top]
    categories = [label for label, _ in ranked]

    data = {}
    for label, (group, ks) in ranked:
        data[label] = {group: 100.0 * len(ks) / total}

    shown = {g for seg in data.values() for g in seg}
    groups = [g for g, _lbl in _MUT_GROUPS if g in shown]
    group_labels = {g: lbl for g, lbl in _MUT_GROUPS}
    return categories, groups, data, group_labels


def sample_cards(calls, variants, coverage, keys):
    """Per-sample metadata for a set of ``(job_id, sample)`` keys.

    Pulls together everything the Trends popup shows when you drill into a run's
    sample: its worst tier, the drugs it was flagged for (with the raw
    classification *and the mutations that justify it*, from the call's
    ``Alteration``), the mutations it carries (gene/AA, known-vs-novel, allele
    fraction and the read depth/quality/consequence that say how much to trust
    the call) and a coverage summary. Returns ``{key: card}`` where each
    ``card`` is::

        {"tier": "candidate",
         "drugs": [(drug, classification, tier, [alt, ...]), ...],  # worst first
         "mutations": [(gene, aa, "known"|"novel", af, dp, qual, cons), ...],
         "coverage": [(gene, status, mean_depth), ...]}             # by gene

    Pure and testable; the screen only formats these into widgets.
    """
    keys = set(keys)
    worst = worst_tier_by_sample(calls)

    drugs = {}
    for c in calls:
        k = (c.get("job_id"), c.get("Sample"))
        if k not in keys:
            continue
        tier = classify_tier(c.get("Classification"))
        if tier not in _RESISTANT_TIERS:
            continue
        alt = (c.get("Alteration") or "").strip()
        evidence = [m.strip() for m in alt.split("+") if m.strip()]
        drugs.setdefault(k, []).append(
            (c.get("Drug") or "?", c.get("Classification") or "", tier,
             evidence))

    muts = {}
    for v in variants:
        k = (v.get("job_id"), v.get("Sample"))
        if k not in keys:
            continue
        aa = (v.get("AA_Change") or "").strip()
        if not aa:
            continue
        gene = v.get("Gene") or v.get("Gene_ID") or "?"
        status = (v.get("Catalog_status") or "").lower()
        skey = "known" if status == "known_marker_component" else "novel"
        muts.setdefault(k, []).append(
            (gene, aa, skey, v.get("AF") or "", v.get("DP") or "",
             v.get("QUAL") or "", (v.get("Consequence") or "").strip()))

    cov = {}
    for r in coverage:
        k = (r.get("job_id"), r.get("Sample"))
        if k not in keys:
            continue
        cov.setdefault(k, []).append(
            (r.get("Gene") or r.get("Gene_ID") or "?",
             (r.get("Status") or "").upper(), r.get("Mean_Depth") or ""))

    def _tier_rank(t):
        return _RESISTANT_TIERS.index(t) if t in _RESISTANT_TIERS else 9

    cards = {}
    for k in keys:
        cards[k] = {
            "tier": worst.get(k, "nomarker"),
            "drugs": sorted(drugs.get(k, []), key=lambda d: _tier_rank(d[2])),
            "mutations": sorted(muts.get(k, []), key=lambda m: (m[0], m[1])),
            "coverage": sorted(cov.get(k, [])),
        }
    return cards


def distinct_samples(universe):
    """Total distinct sequenced samples across a ``sample_universe`` mapping."""
    return sum(len(samples) for samples in universe.values())


def distinct_runs(universe):
    """Number of runs in a ``sample_universe`` mapping."""
    return len(universe)
