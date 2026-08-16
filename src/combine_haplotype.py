#!/usr/bin/env python3
"""
combine_haplotype.py — match observed consequences against the resistance catalog.

Replaces the old hardcoded RESISTANCE_MAP. Joins on gene_id + amino-acid change,
which is stable across csq output, the BED, and build_targets.

Reads:
  --input    combined Step 8 reports (one or many barcodes), tab-separated:
             Sample  Gene_ID  Gene_Name  Consequence  AA_Change  QUAL  DP  AF
  --catalog  build_catalog.py output:
             marker_id  drug  classification  gene_symbol  gene_id  components

Writes in --output_dir:
  resistance_calls.csv  one row per matched marker (drug, classification, evidence)
  variant_detail.csv    every coding change, flagged in-catalog / uncharacterized

A marker is "called" only when every one of its gene-parts is fully satisfied,
so haplotypes need all components present and cross-gene markers need both genes.
"""

import argparse
import csv
import os
from collections import defaultdict

NON_CODING = {"intron", "3_prime_utr", "5_prime_utr", "synonymous",
              "non_coding", "intergenic"}


def load_catalog(path):
    markers = {}
    with open(path) as f:
        for row in csv.DictReader(f, delimiter="\t"):
            mid = row["marker_id"]
            m = markers.setdefault(mid, {
                "drug": row["drug"],
                "classification": row["classification"] or "unclassified",
                "parts": defaultdict(set),
                "genes": set(),
            })
            comps = [c for c in row["components"].split("+") if c]
            m["parts"][row["gene_id"]].update(comps)
            m["genes"].add(row["gene_symbol"])
    return markers


def load_report(path):
    observed = defaultdict(lambda: defaultdict(set))
    detail = []
    with open(path) as f:
        for line in f:
            p = line.rstrip("\n").split("\t")
            if len(p) < 5:
                continue
            rec = {
                "sample": p[0], "gene_id": p[1], "gene_name": p[2],
                "consequence": p[3], "aa": p[4],
                "qual": p[5] if len(p) > 5 else "",
                "dp": p[6] if len(p) > 6 else "",
                "af": p[7] if len(p) > 7 else "",
            }
            detail.append(rec)
            if rec["gene_id"] and rec["aa"]:
                observed[rec["sample"]][rec["gene_id"]].add(rec["aa"])
    return observed, detail


def match_sample(sample_obs, markers):
    called = []
    for m in markers.values():
        evidence, ok = [], True
        for gid, comps in m["parts"].items():
            have = sample_obs.get(gid, set())
            if not comps.issubset(have):
                ok = False
                break
            evidence += [f"{gid}:{c}" for c in sorted(comps)]
        if ok:
            called.append((m, evidence))
    return called


def marker_pairset(m):
    return frozenset((gid, c) for gid, comps in m["parts"].items() for c in comps)


def collapse_calls(calls):
    by_drug = defaultdict(list)
    for i, (m, _ev) in enumerate(calls):
        by_drug[m["drug"]].append(i)
    psets = [marker_pairset(m) for m, _ev in calls]
    keep = set(range(len(calls)))
    for idxs in by_drug.values():
        for i in idxs:
            if any(i != j and psets[i] < psets[j] for j in idxs):
                keep.discard(i)
    return [calls[i] for i in sorted(keep)]


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", required=True)
    ap.add_argument("--catalog", required=True)
    ap.add_argument("--output_dir", required=True)
    ap.add_argument("--collapse", action="store_true",
                    help="report only the maximal genotype per drug, dropping "
                         "markers whose components are a strict subset of another call")
    ap.add_argument("--coverage", default=None,
                    help="optional combined per-gene coverage TSV "
                         "(Sample, gene_id, mean_depth, Amplicon_Depth, status); "
                         "writes coverage_report.csv so silent genes are not "
                         "mistaken for wild-type")
    args = ap.parse_args()

    os.makedirs(args.output_dir, exist_ok=True)
    markers = load_catalog(args.catalog)
    observed, detail = load_report(args.input)

    catalog_changes = defaultdict(set)
    for m in markers.values():
        for gid, comps in m["parts"].items():
            catalog_changes[gid].update(comps)

    calls_path = os.path.join(args.output_dir, "resistance_calls.csv")
    with open(calls_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Sample", "Drug", "Classification", "Genes",
                    "Alteration", "Evidence"])
        for sample in sorted(observed):
            calls = match_sample(observed[sample], markers)
            if args.collapse:
                calls = collapse_calls(calls)
            for m, evidence in sorted(calls, key=lambda x: (x[0]["drug"],
                                                            x[0]["classification"])):
                alteration = " & ".join(
                    "+".join(sorted(c)) for c in m["parts"].values())
                w.writerow([sample, m["drug"], m["classification"],
                            ",".join(sorted(m["genes"])), alteration,
                            "; ".join(evidence)])

    detail_path = os.path.join(args.output_dir, "variant_detail.csv")
    with open(detail_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Sample", "Gene_ID", "Gene", "Consequence", "AA_Change",
                    "Catalog_status", "QUAL", "DP", "AF"])
        for d in detail:
            if d["consequence"] in NON_CODING or not d["gene_id"] or not d["aa"]:
                status = ""
            elif d["aa"] in catalog_changes.get(d["gene_id"], set()):
                status = "known_marker_component"
            else:
                status = "uncharacterized"
            w.writerow([d["sample"], d["gene_id"], d["gene_name"], d["consequence"],
                        d["aa"], status, d["qual"], d["dp"], d["af"]])

    if args.coverage:
        name_by_gid = {d["gene_id"]: d["gene_name"]
                       for d in detail if d["gene_id"]}
        cov_path = os.path.join(args.output_dir, "coverage_report.csv")
        with open(cov_path, "w", newline="") as f, open(args.coverage) as cf:
            w = csv.writer(f)
            w.writerow(["Sample", "Gene_ID", "Gene", "Mean_Depth",
                        "Amplicon_Depth", "Status"])
            for line in cf:
                p = line.rstrip("\n").split("\t")
                if len(p) < 5:
                    continue
                sample, gid, mean, breadth, status = p[:5]
                w.writerow([sample, gid, name_by_gid.get(gid, gid),
                            mean, breadth, status])

    n_calls = 0
    for s in observed:
        c = match_sample(observed[s], markers)
        if args.collapse:
            c = collapse_calls(c)
        n_calls += len(c)
    print(f"samples            : {len(observed)}")
    print(f"resistance calls   : {n_calls}")
    print(f"variant detail rows: {len(detail)}")
    print(f"reports -> {args.output_dir}")


if __name__ == "__main__":
    main()