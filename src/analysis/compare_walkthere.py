#!/usr/bin/env python3
"""
Head-to-head against WalkTHERE (Roper, Ng & Pettit 2023) - the closest academic
prior art and the framework this paper states it builds on.

WalkTHERE, Journal of Transport and Land Use 16(1):361-387, Eq. 1-2:

    A_im = SUM_k  W_k * SUM_j  U_jk * f_k(c_ijm)
    U_jk = exp(-lam * SUM_1..j-1 O_jk) - exp(-lam * SUM_1..j O_jk)      (Eq. 2)
    f_k(c) = exp(-lam_k * c)                                            (Eq. 3)

with lam_k = 0.001 per metre (Gunn et al. 2017; Iacono et al. 2008; Millward et
al. 2013; Yang & Diez-Roux 2012) and O_jk = 1 for all non-employment categories
(their Table 1).

KEY IDENTITY. Equation 2 telescopes: with O_jk = 1, summing marginal utilities
over the first n destinations gives exactly 1 - exp(-lam*n), which is this
paper's saturating curve. The proposed measure is therefore WalkTHERE with a
RECTANGULAR impedance f_k(c) = 1[c <= v*tau_k] substituted for the negative
exponential. That substitution collapses the inner sum to a count of
opportunities within a threshold, which is precomputable per category and
threshold and hence reweightable at query time; a continuous impedance is not.

So this script does not test whether the decay is novel - it is not. It tests
what the impedance substitution costs, and what user weighting adds on top.

Arms
----
  walkthere        Roper et al. as published (exponential impedance)
  ours_rect        same weights and categories, rectangular impedance  <- isolates
                   the substitution alone
  ours_profile     rectangular impedance + user weights + required tier
  cumulative       count within tau, equal weights (field default)
"""

import argparse
import json
import math
import pickle
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))

DIST = REPO_ROOT / "outputs" / "nearest_distances.pkl"
PKL = REPO_ROOT / "outputs" / "preprocessed_grid_scores_routed.pkl"
OUT = REPO_ROOT / "outputs" / "benchmark"

LAMBDA_IMPEDANCE = 0.001      # per metre, Roper et al. section 4.1
LAMBDA_SATURATION = math.log(2)   # our balanced setting, applied to both arms

# WalkTHERE builds categories from household-travel-survey trip purposes weighted
# by trip frequency. We use the same principle with Bangalore frequencies from
# Rahul et al. (2021) Table 1: work 38%, non-work 62%.
CATEGORY_SETS = {
    "work":      (0.38, ["metro station"]),
    "education": (0.62 / 4, ["Schools+school", "library+library"]),
    "shopping":  (0.62 / 4, ["shops+food+supermarket", "shops+food+greengrocer",
                             "shops+food+convenience", "shops+food+general"]),
    "errands":   (0.62 / 4, ["money+bank", "money+atm", "Pharmacy+pharmacy",
                             "Hospitals+hospital"]),
    "recreation": (0.62 / 4, ["park+park", "food+drink+restaurant",
                              "food+drink+cafe", "leisure+swimming_pool"]),
}


def merged_sorted(dist, cats, cell):
    out = []
    for c in cats:
        out.extend(dist.get(c, {}).get(cell, ()))
    out.sort()
    return out


def m_walkthere(dist, cells):
    """Eq. 1-3 as published: marginal utility x exponential impedance."""
    out = {}
    for cid in cells:
        a = 0.0
        for _, (w, cats) in CATEGORY_SETS.items():
            ds = merged_sorted(dist, cats, cid)
            sub = 0.0
            for j, c in enumerate(ds, start=1):
                u = (math.exp(-LAMBDA_SATURATION * (j - 1))
                     - math.exp(-LAMBDA_SATURATION * j))
                sub += u * math.exp(-LAMBDA_IMPEDANCE * c)
            a += w * sub
        out[cid] = a
    return out


def m_ours_rect(grid, cells, tau):
    """Same weights and categories; rectangular impedance -> count within tau."""
    ti = grid.levels.index(tau)
    out = {}
    for cid in cells:
        a = 0.0
        for _, (w, cats) in CATEGORY_SETS.items():
            k = sum((grid.scores.get(c, {}).get(cid) or [0] * len(grid.levels))[ti]
                    for c in cats)
            if k:
                a += w * (1 - math.exp(-LAMBDA_SATURATION * k))
        out[cid] = a
    return out


def m_cumulative(grid, cells, tau):
    ti = grid.levels.index(tau)
    cats = [c for _, (_, cs) in CATEGORY_SETS.items() for c in cs]
    return {cid: sum((grid.scores.get(c, {}).get(cid) or [0] * len(grid.levels))[ti]
                     for c in cats) for cid in cells}


def truncation_bound(top_r=10):
    """Share of a category's saturating curve unreachable with only top_r kept."""
    return math.exp(-LAMBDA_SATURATION * top_r)


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--tau", type=int, default=15)
    p.add_argument("--presets", type=Path,
                   default=REPO_ROOT / "src" / "analysis" / "presets_v2.json")
    p.add_argument("--out", type=Path, default=OUT)
    args = p.parse_args()

    import numpy as np
    from scipy.stats import spearmanr, pearsonr
    from scoring import Grid, load_presets, score_preset
    from ward_assign import assign

    grid = Grid(PKL)
    d = pickle.load(open(DIST, "rb"))
    dist = d["distances"]
    wards = assign(grid.cells)
    cells = sorted(wards)
    print(f"{len(cells):,} ward cells | tau={args.tau} | "
          f"impedance lambda={LAMBDA_IMPEDANCE}/m | top_r={d['metadata']['top_r']}")
    print(f"truncation bound at top_r=10: <= {truncation_bound()*100:.2f}% "
          f"of each category's curve unaccounted\n")

    arms = {
        "walkthere": m_walkthere(dist, cells),
        "ours_rect": m_ours_rect(grid, cells, args.tau),
        "cumulative": m_cumulative(grid, cells, args.tau),
    }
    pres = load_presets(grid, args.presets)
    for k, pr in pres.items():
        arms[f"ours_{k}"] = score_preset(grid, pr)

    names = list(arms)
    base = np.array([arms["walkthere"][c] for c in cells])
    print("Agreement with WalkTHERE (grid cells)")
    print(f"{'arm':<34}{'pearson':>10}{'spearman':>10}")
    for n in names[1:]:
        v = np.array([arms[n][c] for c in cells])
        print(f"{n:<34}{pearsonr(v,base).statistic:>10.3f}"
              f"{spearmanr(v,base).statistic:>10.3f}")

    def wm(vals):
        acc = {}
        for c in cells:
            acc.setdefault(wards[c], []).append(vals[c])
        ks = sorted(acc)
        return ks, np.array([sum(acc[w]) / len(acc[w]) for w in ks])

    wk, wb = wm(arms["walkthere"])
    print(f"\nAgreement with WalkTHERE (wards, n={len(wk)})")
    print(f"{'arm':<34}{'pearson':>10}{'spearman':>10}{'top20':>8}")
    rb = sorted(range(len(wk)), key=lambda i: -wb[i])
    topb = {wk[i] for i in rb[:20]}
    for n in names[1:]:
        _, v = wm(arms[n])
        rv = sorted(range(len(wk)), key=lambda i: -v[i])
        print(f"{n:<34}{pearsonr(v,wb).statistic:>10.3f}"
              f"{spearmanr(v,wb).statistic:>10.3f}"
              f"{len({wk[i] for i in rv[:20]} & topb):>6}/20")

    args.out.mkdir(parents=True, exist_ok=True)
    with open(args.out / "walkthere_comparison.json", "w", encoding="utf-8") as f:
        json.dump({"wards": wk,
                   "scores": {n: wm(arms[n])[1].tolist() for n in names},
                   "source": "Roper, Ng & Pettit (2023) JTLU 16(1):361-387, Eq. 1-3",
                   "lambda_impedance": LAMBDA_IMPEDANCE,
                   "category_sets": {k: v[1] for k, v in CATEGORY_SETS.items()}},
                  f, indent=2)
    print(f"\nwrote {args.out / 'walkthere_comparison.json'}")


if __name__ == "__main__":
    main()
