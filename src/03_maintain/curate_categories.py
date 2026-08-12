#!/usr/bin/env python3
"""
Algorithmic amenity-category curation.

Replaces the interactive scripts (deleter.py, deleter2.py) that removed
categories one at a time via keyboard prompts, leaving no record of what was
dropped or why. Category selection is now a pure function of

    data/06_catchments/  +  category_rules.json  ->  data/07_curated/

so it is deterministic, reviewable in version control, and reproducible.

Modes
-----
  --report      Amenity counts per category and what the rules would do. Read-only.
  --verify      Compare the rule-derived set against the categories actually
                present in a reference .pkl, and report every divergence.
  --reconcile   Emit a rules file whose force_keep/force_drop exactly reproduce
                a reference .pkl. Converts an undocumented historical selection
                into an explicit, auditable rule set.
  --apply       Write data/07_curated/ plus outputs/category_manifest.csv.

Uses only the standard library, so it runs without the geospatial stack.
"""

import argparse
import csv
import json
import pickle
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CATCHMENTS = REPO_ROOT / "data" / "06_catchments"
CURATED = REPO_ROOT / "data" / "07_curated"
MANIFEST = REPO_ROOT / "outputs" / "category_manifest.csv"
RULES = Path(__file__).resolve().parent / "category_rules.json"
REFERENCE_PKL = REPO_ROOT / "outputs" / "preprocessed_grid_scores_with_wards.pkl"

KEEP, DROP, MERGED = "keep", "drop", "merged_away"


# ── loading ───────────────────────────────────────────────────────────────────

def load_counts(catchments=CATCHMENTS):
    """category -> number of catchment polygons (one per amenity)."""
    if not catchments.is_dir():
        sys.exit(f"error: catchment folder not found: {catchments}")
    counts = {}
    for fp in sorted(catchments.glob("*.geojson")):
        with open(fp, encoding="utf-8") as f:
            counts[fp.stem] = len(json.load(f)["features"])
    if not counts:
        sys.exit(f"error: no .geojson files in {catchments}")
    return counts


def load_rules(path=RULES):
    with open(path, encoding="utf-8") as f:
        rules = json.load(f)
    return {
        "min_amenity_count": int(rules.get("min_amenity_count", 0)),
        "merges": {k: list(v) for k, v in rules.get("merges", {}).items()},
        "force_keep": set(rules.get("force_keep", [])),
        "force_drop": set(rules.get("force_drop", [])),
    }


def load_reference_categories(pkl=REFERENCE_PKL):
    if not pkl.exists():
        sys.exit(f"error: reference pkl not found: {pkl}")
    with open(pkl, "rb") as f:
        data = pickle.load(f)
    meta_cats = data.get("metadata", {}).get("categories")
    return set(meta_cats) if meta_cats else set(data["grid_scores"].keys())


# ── the rule engine ───────────────────────────────────────────────────────────

def curate(counts, rules):
    """Apply rules in fixed order. Returns a per-category decision dict."""
    merges, force_keep, force_drop = rules["merges"], rules["force_keep"], rules["force_drop"]
    threshold = rules["min_amenity_count"]

    # validate the rules against reality before doing anything
    known = set(counts)
    unknown = (
        {s for srcs in merges.values() for s in srcs}
        | set(merges) | force_keep | force_drop
    ) - known
    if unknown:
        sys.exit("error: rules reference categories with no catchment file: "
                 + ", ".join(sorted(unknown)))

    source_of = {s: tgt for tgt, srcs in merges.items() for s in srcs}
    overlap = set(source_of) & set(merges)
    if overlap:
        sys.exit("error: category is both a merge source and target (chained "
                 "merges are not supported): " + ", ".join(sorted(overlap)))

    # step 1: merge — effective count is target + all its sources
    effective = {}
    for cat, n in counts.items():
        if cat in source_of:
            continue
        effective[cat] = n + sum(counts[s] for s in merges.get(cat, []))

    decisions = {}
    for cat in sorted(counts):
        raw = counts[cat]

        if cat in source_of:
            decisions[cat] = dict(action=MERGED, raw_count=raw, effective_count=0,
                                  merged_into=source_of[cat],
                                  reason=f"merged into {source_of[cat]}")
            continue

        eff = effective[cat]
        row = dict(raw_count=raw, effective_count=eff, merged_into="")
        if merges.get(cat):
            row["merged_into"] = ""  # this one is a target, not a source

        # step 2: force_drop wins over everything
        if cat in force_drop:
            row.update(action=DROP, reason="force_drop")
        # step 3/4: threshold, with force_keep as the only escape
        elif eff < threshold:
            if cat in force_keep:
                row.update(action=KEEP,
                           reason=f"force_keep (count {eff} < min {threshold})")
            else:
                row.update(action=DROP, reason=f"count {eff} < min {threshold}")
        else:
            gained = eff - raw
            row.update(action=KEEP,
                       reason=(f"count {eff} >= min {threshold}"
                               + (f" (+{gained} merged in)" if gained else "")))
        decisions[cat] = row

    return decisions


def kept_categories(decisions):
    return {c for c, d in decisions.items() if d["action"] == KEEP}


# ── modes ─────────────────────────────────────────────────────────────────────

def do_report(counts, rules, decisions):
    kept = kept_categories(decisions)
    dropped = {c for c, d in decisions.items() if d["action"] == DROP}
    merged = {c for c, d in decisions.items() if d["action"] == MERGED}

    print(f"catchment categories : {len(counts)}")
    print(f"min_amenity_count    : {rules['min_amenity_count']}")
    print(f"merge rules          : {len(rules['merges'])}")
    print(f"\n  keep {len(kept)}   drop {len(dropped)}   merged away {len(merged)}\n")

    print(f"{'count':>7}  {'eff':>5}  {'action':<11} category / reason")
    print("-" * 78)
    for cat in sorted(decisions, key=lambda c: (-decisions[c]["effective_count"], c)):
        d = decisions[cat]
        print(f"{d['raw_count']:>7}  {d['effective_count']:>5}  {d['action']:<11} "
              f"{cat}  ({d['reason']})")


def do_verify(decisions, reference):
    derived = kept_categories(decisions)
    only_derived = sorted(derived - reference)
    only_reference = sorted(reference - derived)

    print(f"rule-derived kept : {len(derived)}")
    print(f"reference pkl     : {len(reference)}")
    print(f"agreement         : {len(derived & reference)} categories\n")

    if not only_derived and not only_reference:
        print("EXACT MATCH - the rules reproduce the reference set.")
        return True

    print(f"DIVERGENCE: {len(only_derived) + len(only_reference)} categories\n")
    if only_derived:
        print(f"  kept by rules but ABSENT from reference ({len(only_derived)}):")
        for c in only_derived:
            print(f"    + {c}  (count {decisions[c]['effective_count']})")
    if only_reference:
        print(f"  in reference but DROPPED by rules ({len(only_reference)}):")
        for c in only_reference:
            d = decisions.get(c)
            n = d["effective_count"] if d else "?"
            why = d["reason"] if d else "no catchment file"
            print(f"    - {c}  (count {n}; {why})")
    print("\nTo pin the reference set exactly, run --reconcile.")
    return False


def do_reconcile(counts, rules, reference, out_path):
    """Emit rules whose force_keep/force_drop exactly reproduce `reference`."""
    base = dict(rules)
    base["force_keep"], base["force_drop"] = set(), set()
    decisions = curate(counts, base)

    threshold = rules["min_amenity_count"]
    force_keep, force_drop = [], []
    for cat, d in decisions.items():
        if d["action"] == MERGED:
            continue
        in_ref = cat in reference
        would_keep = d["effective_count"] >= threshold
        if in_ref and not would_keep:
            force_keep.append(cat)
        elif not in_ref and would_keep:
            force_drop.append(cat)

    out = {
        "_comment": [
            "Generated by curate_categories.py --reconcile.",
            f"force_keep/force_drop are the explicit exceptions needed to reproduce",
            f"the category set in {REFERENCE_PKL.name} under min_amenity_count="
            f"{threshold}.",
            "These were previously undocumented manual choices. Review each and",
            "record why it is an exception, or change min_amenity_count instead.",
        ],
        "min_amenity_count": threshold,
        "merges": rules["merges"],
        "force_keep": sorted(force_keep),
        "force_drop": sorted(force_drop),
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)
        f.write("\n")

    print(f"wrote {out_path}")
    print(f"  force_keep ({len(force_keep)}): {', '.join(sorted(force_keep)) or '-'}")
    print(f"  force_drop ({len(force_drop)}): {', '.join(sorted(force_drop)) or '-'}")

    check = curate(counts, load_rules(out_path))
    ok = kept_categories(check) == reference
    print(f"\n  self-check: reproduces reference exactly -> {ok}")
    return ok


def do_apply(counts, rules, decisions, catchments=CATCHMENTS, curated=CURATED):
    """Write the curated catchment set, concatenating merge sources into targets."""
    merges = rules["merges"]
    kept = sorted(kept_categories(decisions))

    curated.mkdir(parents=True, exist_ok=True)
    for stale in curated.glob("*.geojson"):
        stale.unlink()

    total_in = total_out = 0
    for cat in kept:
        feats = []
        for part in [cat] + merges.get(cat, []):
            with open(catchments / f"{part}.geojson", encoding="utf-8") as f:
                feats.extend(json.load(f)["features"])
        expected = decisions[cat]["effective_count"]
        if len(feats) != expected:
            sys.exit(f"error: {cat} produced {len(feats)} features, expected {expected}")
        with open(curated / f"{cat}.geojson", "w", encoding="utf-8") as f:
            json.dump({"type": "FeatureCollection", "features": feats}, f)
        total_out += len(feats)
    total_in = sum(counts.values())

    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    with open(MANIFEST, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["category", "raw_count", "effective_count", "action",
                    "merged_into", "reason"])
        for cat in sorted(decisions):
            d = decisions[cat]
            w.writerow([cat, d["raw_count"], d["effective_count"], d["action"],
                        d["merged_into"], d["reason"]])

    dropped_feats = sum(d["raw_count"] for d in decisions.values() if d["action"] == DROP)
    print(f"wrote {len(kept)} categories -> {curated}")
    print(f"manifest -> {MANIFEST}")
    print(f"\n  amenities in  : {total_in}")
    print(f"  retained      : {total_out}")
    print(f"  dropped       : {dropped_feats}")
    print(f"  accounted for : {total_out + dropped_feats == total_in}")
    print(f"\nNext: point FOLDER_PATH in src/02_grid/mapgen2.py at data/07_curated,"
          f"\nthen rerun mapgen2 -> wardgen2 -> converter.")


# ── cli ───────────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    m = p.add_mutually_exclusive_group(required=True)
    m.add_argument("--report", action="store_true", help="show counts and decisions")
    m.add_argument("--verify", action="store_true", help="compare against reference pkl")
    m.add_argument("--reconcile", action="store_true", help="emit rules matching reference pkl")
    m.add_argument("--apply", action="store_true", help="write data/07_curated/ + manifest")
    p.add_argument("--rules", type=Path, default=RULES)
    p.add_argument("--reference", type=Path, default=REFERENCE_PKL)
    p.add_argument("--out", type=Path, default=RULES.with_name("category_rules.reconciled.json"))
    args = p.parse_args()

    counts = load_counts()
    rules = load_rules(args.rules)
    decisions = curate(counts, rules)

    if args.report:
        do_report(counts, rules, decisions)
    elif args.verify:
        sys.exit(0 if do_verify(decisions, load_reference_categories(args.reference)) else 1)
    elif args.reconcile:
        ok = do_reconcile(counts, rules, load_reference_categories(args.reference), args.out)
        sys.exit(0 if ok else 1)
    elif args.apply:
        do_apply(counts, rules, decisions)


if __name__ == "__main__":
    main()
