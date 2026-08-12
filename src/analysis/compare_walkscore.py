#!/usr/bin/env python3
"""
Controlled comparison against Walk Score and cumulative opportunity.

Answers EC2 / R1.2 / R2.1. Every method consumes the SAME routed network, the
same amenity set and the same grid; only the aggregation differs. Correlating
against Walk Score's own published numbers would instead confound method with
data source (Google/Factual vs OSM) and geography, which is why the comparison
is done by reimplementation.

Walk Score is patented and its full weight set is unpublished. We therefore use
the formalisation of Dai et al., "Walkability Optimization" (arXiv:2212.05192),
which specifies:

  weighted distance   l_i = SUM_a SUM_p  w_a^p * D_i,a^p
  score               f_i = PiecewiseLinear(l_i) through
                            (0, 100) (400, 95) (1800, 10) (2400, 0)
  rank weights        Grocery     [3]
                      Restaurants [.75 .45 .25 .25 .225 .225 .225 .225 .2 .2]
                      Schools     [1]

Their restaurant vector IS a diminishing-returns mechanism ("depth of choice"),
so the paper must NOT claim existing tools ignore redundancy. What differs is the
SHAPE: their weights flatten to a floor near 0.2 of the first instance, ours decay
geometrically toward zero.

Only three categories are instantiated because that is all Dai et al. publish
weights for. State this as a limitation.

Arms
----
  walkscore     Dai et al. formalisation
  ours_as_ws    our aggregation, THEIR categories and weights   <- isolates method
  cumulative    count within tau, equal weights
  ours_uniform  our aggregation, all categories, equal weights
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

# Dai et al. Table 5
WS_BREAKPOINTS = [(0.0, 100.0), (400.0, 95.0), (1800.0, 10.0), (2400.0, 0.0)]
D_INF = 2400.0

# Dai et al. Appendix D rank weights
WS_WEIGHTS = {
    "grocery":     [3.0],
    "restaurants": [.75, .45, .25, .25, .225, .225, .225, .225, .2, .2],
    "schools":     [1.0],
}

# Mapping onto our OSM taxonomy. A judgement call, and there is no neutral
# answer: A is faithful to an index built for North American retail, C to how
# Bengaluru actually provisions itself. Select with --mapping; C is the default
# and the one reported in the paper.
WS_MAPPINGS = {
    "A": {  # supermarkets only
        "grocery":     ["shops+food+supermarket"],
        "restaurants": ["food+drink+restaurant"],
        "schools":     ["Schools+school"],
    },
    "B": {  # + greengrocer
        "grocery":     ["shops+food+supermarket", "shops+food+greengrocer"],
        "restaurants": ["food+drink+restaurant"],
        "schools":     ["Schools+school"],
    },
    "C": {  # + general and convenience stores
        "grocery":     ["shops+food+supermarket", "shops+food+greengrocer",
                        "shops+food+general", "shops+food+convenience"],
        "restaurants": ["food+drink+restaurant"],
        "schools":     ["Schools+school"],
    },
    "D": {  # C, plus cafes and fast food counted as restaurants
        "grocery":     ["shops+food+supermarket", "shops+food+greengrocer",
                        "shops+food+general", "shops+food+convenience"],
        "restaurants": ["food+drink+restaurant", "food+drink+cafe",
                        "food+drink+fast_food"],
        "schools":     ["Schools+school"],
    },
}
WS_CATEGORIES = WS_MAPPINGS["C"]


def ws_piecewise(l):
    """Interpolate the Walk Score curve at weighted distance l."""
    pts = WS_BREAKPOINTS
    if l <= pts[0][0]:
        return pts[0][1]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if l <= x1:
            return y0 + (y1 - y0) * (l - x0) / (x1 - x0)
    return 0.0


def merged_distances(dist, cats, cell):
    """Sorted distances across all our categories mapped to one WS category."""
    out = []
    for c in cats:
        out.extend(dist.get(c, {}).get(cell, ()))
    out.sort()
    return out


def score_walkscore(dist, cells):
    out = {}
    for cid in cells:
        l = 0.0
        for ws_cat, ranks in WS_WEIGHTS.items():
            ds = merged_distances(dist, WS_CATEGORIES[ws_cat], cid)
            for p, w in enumerate(ranks):
                l += w * (ds[p] if p < len(ds) else D_INF)
        out[cid] = ws_piecewise(l / sum(sum(v) for v in WS_WEIGHTS.values()))
    return out


def score_ours_as_ws(grid, cells, tau, lam):
    """Our aggregation, restricted to Walk Score's categories and weights."""
    ti = grid.levels.index(tau)
    groups = [(ws, sum(WS_WEIGHTS[ws]), WS_CATEGORIES[ws]) for ws in WS_WEIGHTS]
    den = sum(w for _, w, _ in groups)
    out = {}
    for cid in cells:
        s = 0.0
        for _, w, cats in groups:
            k = sum((grid.scores.get(c, {}).get(cid) or [0] * len(grid.levels))[ti]
                    for c in cats)
            if k:
                s += w * (1 - math.exp(-lam * k))
        out[cid] = s / den
    return out


def score_cumulative(grid, cells, tau):
    ti = grid.levels.index(tau)
    cats = [c for v in WS_CATEGORIES.values() for c in v]
    return {cid: sum((grid.scores.get(c, {}).get(cid) or [0] * len(grid.levels))[ti]
                     for c in cats) for cid in cells}


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
    p.add_argument("--tau", type=int, default=15)
    p.add_argument("--lam", type=float, default=math.log(2))
    p.add_argument("--out", type=Path, default=OUT)
    p.add_argument("--mapping", choices=sorted(WS_MAPPINGS), default="C",
                   help="grocery/restaurant mapping variant. default C")
    args = p.parse_args()

    global WS_CATEGORIES
    WS_CATEGORIES = WS_MAPPINGS[args.mapping]

    from scoring import Grid
    from ward_assign import assign

    grid = Grid(PKL)
    d = pickle.load(open(DIST, "rb"))
    dist = d["distances"]
    wards = assign(grid.cells)
    cells = sorted(wards)
    print(f"{len(cells):,} ward-assigned cells | Walk Score cutoff "
          f"{d['metadata']['max_distance_m']:.0f} m | tau={args.tau} | "
          f"mapping {args.mapping}\n")

    arms = {
        "walkscore": score_walkscore(dist, cells),
        "ours_as_ws": score_ours_as_ws(grid, cells, args.tau, args.lam),
        "cumulative": score_cumulative(grid, cells, args.tau),
    }
    names = list(arms)

    print("GRID CELLS")
    print(f"{'pair':<34}{'pearson':>10}{'spearman':>10}")
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            x = [arms[names[i]][c] for c in cells]
            y = [arms[names[j]][c] for c in cells]
            print(f"{names[i]+' vs '+names[j]:<34}{pear(x,y):>10.3f}{spear(x,y):>10.3f}")

    wagg = {}
    for m in names:
        acc = {}
        for c in cells:
            acc.setdefault(wards[c], []).append(arms[m][c])
        wagg[m] = {w: sum(v) / len(v) for w, v in acc.items()}
    wk = sorted(wagg["walkscore"])
    base = [wagg["walkscore"][w] for w in wk]
    rb = sorted(range(len(wk)), key=lambda i: -base[i])
    pb = {wk[i]: r for r, i in enumerate(rb)}

    print(f"\nWARDS (n={len(wk)}) vs walkscore")
    print(f"{'method':<16}{'pearson':>10}{'spearman':>10}{'shift med':>11}{'p90':>6}"
          f"{'max':>6}{'top20':>8}")
    for m in names[1:]:
        v = [wagg[m][w] for w in wk]
        rm = sorted(range(len(wk)), key=lambda i: -v[i])
        pm = {wk[i]: r for r, i in enumerate(rm)}
        sh = sorted(abs(pb[w] - pm[w]) for w in wk)
        top = len({wk[i] for i in rb[:20]} & {wk[i] for i in rm[:20]})
        print(f"{m:<16}{pear(v,base):>10.3f}{spear(v,base):>10.3f}"
              f"{sh[len(sh)//2]:>11}{sh[int(len(sh)*.9)]:>6}{sh[-1]:>6}{top:>6}/20")

    # where do walkscore and ours disagree? condition on redundancy
    ti = grid.levels.index(args.tau)
    allc = [c for v in WS_CATEGORIES.values() for c in v]
    totk = {cid: sum((grid.scores.get(c, {}).get(cid) or [0] * len(grid.levels))[ti]
                     for c in allc) for cid in cells}
    srt = sorted(cells, key=lambda c: -totk[c])
    print(f"\nwalkscore vs ours_as_ws, conditioned on redundancy")
    print(f"{'subset':<26}{'cells':>8}{'spearman':>10}{'mean k':>9}")
    for name, idx in [("all", cells), ("top 50% by k", srt[:len(srt)//2]),
                      ("top 20% by k", srt[:len(srt)//5]),
                      ("top 10% by k", srt[:len(srt)//10])]:
        x = [arms["walkscore"][c] for c in idx]
        y = [arms["ours_as_ws"][c] for c in idx]
        print(f"{name:<26}{len(idx):>8,}{spear(x,y):>10.3f}"
              f"{sum(totk[c] for c in idx)/len(idx):>9.1f}")

    args.out.mkdir(parents=True, exist_ok=True)
    with open(args.out / "walkscore_comparison.json", "w", encoding="utf-8") as f:
        json.dump({"wards": wk,
                   "scores": {m: [wagg[m][w] for w in wk] for m in names},
                   "source": "Dai et al. arXiv:2212.05192 formalisation of Walk Score",
                   "categories": WS_CATEGORIES, "weights": WS_WEIGHTS}, f, indent=2)
    print(f"\nwrote {args.out / 'walkscore_comparison.json'}")


if __name__ == "__main__":
    main()
