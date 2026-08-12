#!/usr/bin/env python3
"""
Controlled comparison against established accessibility methods.

Answers the editor's "compare more comprehensively, preferably quantitatively"
and Reviewer 1/2's objection that a uniform-weighting comparison and a single
visual Walk Score screenshot are insufficient.

DESIGN: every method consumes the SAME routed input - the same pedestrian graph,
the same catchments, the same grid. Only the aggregation differs. That isolates
the methodological contribution, which is what the paper actually claims, and it
avoids a fairness hole: if this framework used network routing while baselines
used Euclidean distance, it would win by construction.

Recoverable from the stored k-vectors: counts are non-decreasing across levels,
so differencing them gives how many amenities FIRST become reachable in each time
band - a per-cell distribution of network walking times, with no re-routing.

Methods
-------
  ours          weighted exponential decay on redundancy (this framework)
  uniform       same, all weights equal - the comparator already in the paper
  cumulative    count of opportunities within 15 min (the dominant approach)
  binary15      fraction of categories with >= 1 amenity within 15 min
                (15-minute-city toolkit logic)
  walkscore     fixed category weights x decay on distance to NEAREST amenity
                (Walk Score-style; an approximation, see WALKSCORE_WEIGHTS)
  gravity       Hansen: sum over amenities of w * exp(-beta * t)
"""

import argparse
import json
import math
import pickle
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))

PKL = REPO_ROOT / "outputs" / "preprocessed_grid_scores_routed.pkl"
OUT = REPO_ROOT / "outputs" / "benchmark"

# Walk Score publishes 13 weighted categories. Our OSM taxonomy does not map onto
# them one-to-one, so this is a Walk Score-STYLE reconstruction: the same shape
# (fixed weights, decay on distance to the nearest instance), with weights
# assigned by the published relative emphasis. Label it as such in the paper.
WALKSCORE_WEIGHTS = {
    "shops+food+supermarket": 3.0,
    "shops+food+greengrocer": 3.0,
    "food+drink+restaurant": 0.75,
    "food+drink+cafe": 0.75,
    "shops+food+convenience": 1.0,
    "shops+food+general": 1.0,
    "shops+clothes+clothes": 1.0,
    "money+bank": 1.0,
    "park+park": 1.0,
    "Schools+school": 1.0,
    "library+library": 1.0,
    "leisure+cinema": 1.0,
    "Pharmacy+pharmacy": 1.0,
}


def load(pkl=PKL):
    d = pickle.load(open(pkl, "rb"))
    return d, d["levels"], sorted(d["grid_scores"])


def first_reach_band(vec):
    """k-vector -> list of counts first reachable at each level (differencing)."""
    out = [vec[0]]
    for i in range(1, len(vec)):
        out.append(max(vec[i] - vec[i - 1], 0))
    return out


def nearest_level(vec, levels):
    """Smallest level at which at least one amenity is reachable, else None."""
    for i, v in enumerate(vec):
        if v > 0:
            return levels[i]
    return None


# ── methods ───────────────────────────────────────────────────────────────────

def m_ours(d, cats, cells, levels, tau=15, lam=math.log(2), weights=None):
    ti = levels.index(tau)
    w = weights or {c: 1.0 for c in cats}
    tot = sum(w[c] for c in cats)
    out = {}
    for cid in cells:
        s = 0.0
        for c in cats:
            v = d["grid_scores"].get(c, {}).get(cid)
            if v:
                s += w[c] * (1 - math.exp(-lam * v[ti]))
        out[cid] = s / tot
    return out


def m_cumulative(d, cats, cells, levels, tau=15):
    ti = levels.index(tau)
    return {cid: sum((d["grid_scores"].get(c, {}).get(cid) or [0] * len(levels))[ti]
                     for c in cats) for cid in cells}


def m_binary15(d, cats, cells, levels, tau=15):
    ti = levels.index(tau)
    return {cid: sum(1 for c in cats
                     if (d["grid_scores"].get(c, {}).get(cid) or [0] * len(levels))[ti] > 0)
                 / len(cats) for cid in cells}


def m_walkscore(d, cats, cells, levels, max_t=15):
    """Fixed weights x linear decay on time to the nearest instance."""
    use = {c: w for c, w in WALKSCORE_WEIGHTS.items() if c in cats}
    tot = sum(use.values())
    out = {}
    for cid in cells:
        s = 0.0
        for c, w in use.items():
            v = d["grid_scores"].get(c, {}).get(cid)
            if not v:
                continue
            t = nearest_level(v, levels)
            if t is not None:
                s += w * max(0.0, 1.0 - t / max_t)
        out[cid] = s / tot
    return out


def m_gravity(d, cats, cells, levels, beta=0.15):
    """Hansen: sum over reachable amenities of exp(-beta * travel time)."""
    out = {}
    for cid in cells:
        s = 0.0
        for c in cats:
            v = d["grid_scores"].get(c, {}).get(cid)
            if not v:
                continue
            for t, n in zip(levels, first_reach_band(v)):
                if n:
                    s += n * math.exp(-beta * t)
        out[cid] = s
    return out


# ── statistics ────────────────────────────────────────────────────────────────

def pear(x, y):
    n = len(x)
    mx, my = sum(x) / n, sum(y) / n
    vx = sum((i - mx) ** 2 for i in x)
    vy = sum((j - my) ** 2 for j in y)
    if vx * vy == 0:
        return float("nan")
    return sum((i - mx) * (j - my) for i, j in zip(x, y)) / math.sqrt(vx * vy)


def ranks(v):
    n = len(v)
    order = sorted(range(n), key=lambda i: v[i])
    r = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and v[order[j + 1]] == v[order[i]]:
            j += 1
        for q in range(i, j + 1):
            r[order[q]] = (i + j) / 2 + 1
        i = j + 1
    return r


def spear(x, y):
    return pear(ranks(x), ranks(y))


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--pkl", type=Path, default=PKL)
    p.add_argument("--profile", default=None,
                   help="preset key to use for 'ours' (default: uniform weights)")
    p.add_argument("--beta", type=float, default=0.15, help="gravity decay. default 0.15")
    p.add_argument("--out", type=Path, default=OUT)
    args = p.parse_args()

    from ward_assign import assign

    d, levels, cats = load(args.pkl)
    wards = assign(d["grid_cells"])
    cells = sorted(wards)
    print(f"input: {len(cats)} categories, {len(levels)} levels, "
          f"{len(cells):,} ward-assigned cells\n")

    methods = {
        "ours": m_ours(d, cats, cells, levels),
        "cumulative": m_cumulative(d, cats, cells, levels),
        "binary15": m_binary15(d, cats, cells, levels),
        "walkscore": m_walkscore(d, cats, cells, levels),
        "gravity": m_gravity(d, cats, cells, levels, args.beta),
    }
    names = list(methods)

    # ward aggregation
    wagg = {m: {} for m in names}
    for m in names:
        acc = {}
        for cid in cells:
            acc.setdefault(wards[cid], []).append(methods[m][cid])
        wagg[m] = {w: sum(v) / len(v) for w, v in acc.items()}
    wkeys = sorted(wagg["ours"])

    print("Pairwise agreement - GRID CELLS (Spearman above / Pearson below diagonal)")
    print(f"{'':<12}" + "".join(f"{n:>12}" for n in names))
    for a in names:
        row = f"{a:<12}"
        for b in names:
            if a == b:
                row += f"{'-':>12}"
            else:
                x = [methods[a][c] for c in cells]
                y = [methods[b][c] for c in cells]
                v = spear(x, y) if names.index(b) > names.index(a) else pear(x, y)
                row += f"{v:>12.3f}"
        print(row)

    print("\nAgreement with 'ours' - WARD level")
    print(f"{'method':<14}{'pearson':>9}{'spearman':>10}{'rank shift med':>16}"
          f"{'p90':>6}{'max':>6}{'top20':>8}")
    base = [wagg["ours"][w] for w in wkeys]
    rb = sorted(range(len(wkeys)), key=lambda i: -base[i])
    pb = {wkeys[i]: r for r, i in enumerate(rb)}
    for m in names[1:]:
        v = [wagg[m][w] for w in wkeys]
        rm = sorted(range(len(wkeys)), key=lambda i: -v[i])
        pm = {wkeys[i]: r for r, i in enumerate(rm)}
        sh = sorted(abs(pb[w] - pm[w]) for w in wkeys)
        top = len({wkeys[i] for i in rb[:20]} & {wkeys[i] for i in rm[:20]})
        print(f"{m:<14}{pear(v, base):>9.3f}{spear(v, base):>10.3f}"
              f"{sh[len(sh)//2]:>16}{sh[int(len(sh)*.9)]:>6}{sh[-1]:>6}{top:>6}/20")

    args.out.mkdir(parents=True, exist_ok=True)
    with open(args.out / "methods_ward.json", "w", encoding="utf-8") as f:
        json.dump({"wards": wkeys,
                   "scores": {m: [wagg[m][w] for w in wkeys] for m in names}},
                  f, indent=2)
    print(f"\nwrote {args.out / 'methods_ward.json'}")


if __name__ == "__main__":
    main()
