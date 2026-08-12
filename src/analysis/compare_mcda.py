#!/usr/bin/env python3
"""
Comparison against MCDA methods given IDENTICAL preference data.

Answers Reviewer 1: "many other methods can also incorporate agent-based or
individual preference data and inputs". That objection is not met by showing
personalised scores differ from non-personalised baselines - R1 already grants
that. The fair test gives a competing personalisable method the SAME weights,
categories and thresholds, then asks whether our aggregation still differs.

This framework IS a GIS-MCDA: alternatives = grid cells, criteria = amenity
categories, normalisation = 1-exp(-lambda*k), weights = user-set, aggregation =
weighted mean. So the comparison is against other members of the same family.

Methods (all fed each profile's own categories, weights and per-category tau):

  ours          saturating normalisation 1-exp(-lambda*k) + required filter
  wlc           weighted linear combination, max-min normalisation (Malczewski)
  wlc_required  WLC plus our required filter   -> isolates NORMALISATION alone
  ours_norequired                              -> isolates the REQUIRED FILTER alone
  topsis        distance to ideal / anti-ideal, vector normalisation

The 2x2 (saturating vs linear) x (required vs not) decomposes the difference
between our method and standard WLC into its two components.
"""

import argparse
import json
import math
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))

PKL = REPO_ROOT / "outputs" / "preprocessed_grid_scores_routed.pkl"
PRESETS = REPO_ROOT / "src" / "analysis" / "presets_generated.json"
OUT = REPO_ROOT / "outputs" / "benchmark"


def group_k(grid, preset, cells):
    """Per group: raw k at that group's tau, for every cell."""
    out = []
    for g in preset["groups"]:
        ti = g["ti"]
        vals = {}
        for cid in cells:
            k = 0
            for c in g["categories"]:
                v = grid.scores.get(c, {}).get(cid)
                if v:
                    k += v[ti]
            vals[cid] = k
        out.append({"w": g["w"], "required": g["required"], "k": vals,
                    "excluded": g["excluded"], "negative": g["negative"],
                    "lam": g["lam"], "label": g["label"]})
    return out


def blocked(groups, cid):
    """Either hard constraint disqualifies a cell."""
    return any((g["required"] and g["k"][cid] == 0)
               or (g["excluded"] and g["k"][cid] > 0) for g in groups)


def m_ours(groups, cells, use_required=True):
    # Only wanted groups set the scale; avoided ones subtract without entering
    # the denominator, so they can lower a score but never raise it.
    den = sum(g["w"] for g in groups if not g["negative"])
    if den == 0:
        return {cid: 0.0 for cid in cells}
    out = {}
    for cid in cells:
        if use_required and blocked(groups, cid):
            out[cid] = 0.0
            continue
        total = 0.0
        for g in groups:
            if g["excluded"]:
                continue                       # filter only, contributes nothing
            term = g["w"] * (1 - math.exp(-g["lam"] * g["k"][cid]))
            total += -term if g["negative"] else term
        out[cid] = max(0.0, total / den)
    return out


def m_wlc(groups, cells, use_required=False):
    """Weighted linear combination with max-min normalisation.

    Avoided groups enter as cost criteria, the standard MCDA treatment: the
    normalisation is inverted so more of an unwanted thing scores lower. This
    hands WLC the same preference information we use, in its own idiom.
    """
    use = [g for g in groups if not g["excluded"]]
    den = sum(g["w"] for g in use)
    if den == 0:
        return {cid: 0.0 for cid in cells}
    norm = []
    for g in use:
        vs = [g["k"][c] for c in cells]
        lo, hi = min(vs), max(vs)
        rng = (hi - lo) or 1.0
        if g["negative"]:
            norm.append({cid: (hi - g["k"][cid]) / rng for cid in cells})
        else:
            norm.append({cid: (g["k"][cid] - lo) / rng for cid in cells})
    out = {}
    for cid in cells:
        if use_required and blocked(groups, cid):
            out[cid] = 0.0
            continue
        out[cid] = sum(g["w"] * n[cid] for g, n in zip(use, norm)) / den
    return out


def m_topsis(groups, cells):
    """Vector normalisation, weighted, distance to ideal / anti-ideal.

    Avoided groups are cost criteria, so their ideal is the column minimum and
    their anti-ideal the maximum. TOPSIS takes negative preferences natively.
    """
    groups = [g for g in groups if not g["excluded"]]
    if not groups:
        return {cid: 0.0 for cid in cells}
    cols, cost = [], []
    for g in groups:
        vs = [float(g["k"][c]) for c in cells]
        denom = math.sqrt(sum(v * v for v in vs)) or 1.0
        cols.append([g["w"] * (v / denom) for v in vs])
        cost.append(g["negative"])
    best = [min(c) if k else max(c) for c, k in zip(cols, cost)]
    worst = [max(c) if k else min(c) for c, k in zip(cols, cost)]
    out = {}
    for i, cid in enumerate(cells):
        dp = math.sqrt(sum((cols[j][i] - best[j]) ** 2 for j in range(len(cols))))
        dm = math.sqrt(sum((cols[j][i] - worst[j]) ** 2 for j in range(len(cols))))
        out[cid] = dm / (dp + dm) if (dp + dm) else 0.0
    return out


def pear(x, y):
    n = len(x); mx, my = sum(x) / n, sum(y) / n
    vx = sum((i - mx) ** 2 for i in x); vy = sum((j - my) ** 2 for j in y)
    if vx * vy == 0:
        return float("nan")
    return sum((i - mx) * (j - my) for i, j in zip(x, y)) / math.sqrt(vx * vy)


def ranks(v):
    n = len(v); o = sorted(range(n), key=lambda i: v[i]); r = [0.0] * n; i = 0
    while i < n:
        j = i
        while j + 1 < n and v[o[j + 1]] == v[o[i]]:
            j += 1
        for q in range(i, j + 1):
            r[o[q]] = (i + j) / 2 + 1
        i = j + 1
    return r


def spear(x, y):
    return pear(ranks(x), ranks(y))


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--presets", type=Path, default=PRESETS)
    p.add_argument("--pkl", type=Path, default=PKL)
    p.add_argument("--out", type=Path, default=OUT)
    args = p.parse_args()

    from scoring import Grid, load_presets
    from ward_assign import assign

    grid = Grid(args.pkl)
    presets = load_presets(grid, args.presets)
    wards = assign(grid.cells)
    cells = sorted(wards)
    print(f"{len(cells):,} ward-assigned cells | {len(presets)} profiles\n")

    def wardmean(vals):
        acc = {}
        for cid in cells:
            acc.setdefault(wards[cid], []).append(vals[cid])
        ks = sorted(acc)
        return ks, [sum(acc[w]) / len(acc[w]) for w in ks]

    results = {}
    print("Ward-level Spearman vs OURS, with identical weights / categories / tau")
    print(f"{'profile':<24}{'wlc':>9}{'wlc+req':>10}{'topsis':>9}"
          f"{'ours_noreq':>12}{'n_req':>7}")
    for key, pr in presets.items():
        groups = group_k(grid, pr, cells)
        base = m_ours(groups, cells, True)
        arms = {"wlc": m_wlc(groups, cells, False),
                "wlc+req": m_wlc(groups, cells, True),
                "topsis": m_topsis(groups, cells),
                "ours_noreq": m_ours(groups, cells, False)}
        _, b = wardmean(base)
        row = f"{key:<24}"
        vals = {}
        for name, a in arms.items():
            _, v = wardmean(a)
            vals[name] = spear(v, b)
            row += f"{vals[name]:>{10 if name=='wlc+req' else (12 if name=='ours_noreq' else 9)}.3f}"
        nreq = sum(1 for g in groups if g["required"])
        row += f"{nreq:>7}"
        print(row)
        results[key] = vals

    print("\nDecomposition (ward Spearman vs ours):")
    print("  ours vs wlc          = normalisation + required filter combined")
    print("  ours vs wlc+req      = NORMALISATION alone (required held constant)")
    print("  ours vs ours_noreq   = REQUIRED FILTER alone (normalisation constant)")
    for key, v in results.items():
        gap_norm = 1 - v["wlc+req"]
        gap_req = 1 - v["ours_noreq"]
        tot = 1 - v["wlc"]
        print(f"  {key:<24} total {tot:.3f}   normalisation {gap_norm:.3f}   "
              f"required {gap_req:.3f}")

    args.out.mkdir(parents=True, exist_ok=True)
    with open(args.out / "mcda_comparison.json", "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)
    print(f"\nwrote {args.out / 'mcda_comparison.json'}")


if __name__ == "__main__":
    main()
