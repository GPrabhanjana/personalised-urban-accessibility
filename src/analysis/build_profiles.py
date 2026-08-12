#!/usr/bin/env python3
"""
Generate concrete presets from evidence-based rules.

    category_attributes.json  (shared, cited)
  + evidence_profiles.json    (per-profile: scope / required / elevated)
  -> presets_generated.json   (what scoring.py consumes)

A profile never sets tau or decay directly. tau derives from the category's
trip purpose and the empirical acceptable distance for that purpose; decay
derives from the category's substitutability. So every parameter in the output
is traceable to a stated rule, and the number of free choices per profile drops
from roughly 24 to 4.

  --report   show the derivation for every group, and what got clamped
  --write    emit presets_generated.json
"""

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ATTRS = HERE / "category_attributes.json"
PROFILES = HERE / "evidence_profiles_v3.json"
OUT = HERE / "presets_v3.json"


def load(attrs_path=None, profiles_path=None):
    attrs = json.load(open(attrs_path or ATTRS, encoding="utf-8"))
    profs = json.load(open(profiles_path or PROFILES, encoding="utf-8"))
    return attrs, {k: v for k, v in profs.items() if not k.startswith("_")}


def invert(mapping):
    out = {}
    for key, cats in mapping.items():
        for c in cats:
            if c in out:
                sys.exit(f"error: {c!r} listed under both {out[c]!r} and {key!r}")
            out[c] = key
    return out


def snap(minutes, grid):
    return min(grid, key=lambda g: abs(g - minutes))


# Tiers that subtract rather than add. Kept in step with scoring.NEGATIVE.
NEGATIVE_WEIGHTS = {"avoided", "preferred_avoided", "excluded"}
# Avoidance carries no "how many nearby" setting, so its rate is fixed.
AVOID_DECAY = "balanced"


def derive(attrs, profiles):
    purpose_of = invert(attrs["purpose_of"])
    subst_of = invert(attrs["substitutability_of"])
    dist = attrs["trip_purpose_distance_m"]
    speed = attrs["walking_speed_m_per_min"]
    grid = attrs["tau_grid"]
    to_decay = attrs["substitutability_to_decay"]
    buffers = attrs.get("disamenity_buffer_m", {})
    disamenity_of = invert(attrs.get("disamenity_of", {}))

    presets, trace = {}, []
    for key, spec in profiles.items():
        groups = []
        for g in spec["groups"]:
            cats = g["categories"]
            weight = g.get("weight", "standard")

            if weight in NEGATIVE_WEIGHTS:
                # Nobody travels to something they are trying to avoid, so trip
                # purpose cannot set the threshold. The evidenced exposure radius
                # does: every member must belong to one named disamenity, and
                # that disamenity's buffer becomes tau.
                kinds = {disamenity_of.get(c) for c in cats}
                if None in kinds or len(kinds) != 1:
                    sys.exit(f"error: {key}/{g['label']}: an avoided group must list "
                             f"categories of exactly one disamenity named in "
                             f"disamenity_of; got {sorted(map(str, kinds))}")
                kind = kinds.pop()
                raw = buffers[kind] / speed
                tau = snap(raw, grid)
                decay = AVOID_DECAY
                purpose, distance_m, subst = kind, buffers[kind], "n/a"
            else:
                missing = [c for c in cats if c not in purpose_of or c not in subst_of]
                if missing:
                    sys.exit(f"error: {key}/{g['label']}: no attributes for {missing}")

                # tau: from trip purpose. All members of a group must share a purpose,
                # otherwise the group's threshold would be ambiguous.
                purposes = {purpose_of[c] for c in cats}
                if len(purposes) > 1:
                    sys.exit(f"error: {key}/{g['label']} mixes trip purposes {sorted(purposes)}; "
                             "split the group or reassign the categories")
                purpose = purposes.pop()
                raw = dist[purpose] / speed
                tau = snap(raw, grid)
                distance_m = dist[purpose]

                # decay: from substitutability. Mixed groups take the most substitutable
                # member, since grouping already asserts the user treats them as swappable.
                rank = {"low": 0, "medium": 1, "high": 2}
                subst = max((subst_of[c] for c in cats), key=lambda s: rank[s])
                decay = to_decay[subst]

            groups.append({"label": g["label"], "categories": cats,
                           "weight": weight, "decay": decay, "tau": tau})
            trace.append({"profile": key, "group": g["label"], "n_cats": len(cats),
                          "purpose": purpose, "distance_m": distance_m,
                          "raw_min": round(raw, 1), "tau": tau,
                          "clamped": raw < grid[0], "subst": subst,
                          "decay": decay, "weight": weight})
        presets[key] = {"label": spec["label"], "basis": spec.get("basis", ""),
                        "groups": groups}
    return presets, trace


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--report", action="store_true")
    p.add_argument("--write", action="store_true")
    p.add_argument("--attrs", type=Path, default=None)
    p.add_argument("--profiles", type=Path, default=None)
    p.add_argument("--out", type=Path, default=None)
    args = p.parse_args()
    if not (args.report or args.write):
        p.error("give --report and/or --write")

    attrs, profiles = load(args.attrs, args.profiles)
    presets, trace = derive(attrs, profiles)

    if args.report:
        print(f"{'profile':<22}{'group':<18}{'n':>3}  {'purpose':<26}"
              f"{'m':>5}{'raw':>7}  {'tau':>4}  {'decay':<10}{'weight':<10}")
        print("-" * 112)
        for t in trace:
            flag = " *" if t["clamped"] else ""
            print(f"{t['profile']:<22}{t['group']:<18}{t['n_cats']:>3}  {t['purpose']:<26}"
                  f"{t['distance_m']:>5}{t['raw_min']:>6.1f}m{t['tau']:>5}{flag:<2}"
                  f"{t['decay']:<10}{t['weight']:<10}")
        n_cl = sum(1 for t in trace if t["clamped"])
        print(f"\n* {n_cl} of {len(trace)} groups clamped up to the 5-minute floor: the "
              f"empirical\n  acceptable distance is SHORTER than the framework's minimum "
              f"threshold.")
        used = {c for pr in presets.values() for g in pr["groups"] for c in g["categories"]}
        print(f"\ncategories used by at least one profile: {len(used)}")
        for k, pr in presets.items():
            cats = {c for g in pr["groups"] for c in g["categories"]}
            req = [g["label"] for g in pr["groups"] if g["weight"] == "required"]
            print(f"  {k:<22} {len(pr['groups'])} groups, {len(cats)} categories, "
                  f"required: {', '.join(req) or 'none'}")
        # overlap between profiles - lower is better for discrimination
        keys = list(presets)
        print("\npairwise category overlap (Jaccard; lower = more distinct):")
        for i in range(len(keys)):
            for j in range(i + 1, len(keys)):
                a = {c for g in presets[keys[i]]["groups"] for c in g["categories"]}
                b = {c for g in presets[keys[j]]["groups"] for c in g["categories"]}
                print(f"  {keys[i][:20]:<21} vs {keys[j][:20]:<21} "
                      f"{len(a & b) / len(a | b):.2f}")

    if args.write:
        doc = {"_generated_by": "build_profiles.py from category_attributes.json + "
                               "evidence_profiles.json - do not edit by hand",
               "_aliases": {"metro": "metro station"}}
        doc.update(presets)
        out_path = args.out or OUT
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=2)
            f.write("\n")
        print(f"\nwrote {out_path}")


if __name__ == "__main__":
    main()
