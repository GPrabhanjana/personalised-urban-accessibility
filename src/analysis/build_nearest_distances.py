#!/usr/bin/env python3
"""
Per-cell network distances to the nearest r amenities of each category.

Walk Score, as formalised by Dai et al. (arXiv:2212.05192), scores on DISTANCE
to the nearest instances - and for categories where variety matters ("depth of
choice") on a weighted combination of the distances to the top-r nearest. Our
framework scores on COUNTS within a threshold. Comparing the two fairly requires
both to be derived from the SAME routed network, so this produces the distance
side of that pair from the identical graph, amenity set and grid.

Range is 2400 m by default: Walk Score awards zero beyond a 30-minute walk, so
truncating at our usual 1200 m would systematically penalise it in a comparison
we are running ourselves.

Cell distance is defined consistently with our coverage rule: the MEDIAN over the
cell's sample lattice of (network distance to the sample point's nearest graph
node + snap offset). A cell is "reached" at the distance where at least half of
it can reach the amenity, which is the same >= 50% semantics used for k.

Output: {category: {cell_id: [d1 <= d2 <= ... <= dr]}} in metres.
"""

import argparse
import json
import pickle
import sys
import time
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src" / "01_isochrones"))
sys.path.insert(0, str(REPO_ROOT / "src" / "02_grid"))

OUT = REPO_ROOT / "outputs" / "nearest_distances.pkl"
D_INF = 2400.0   # Walk Score: no points beyond a 30-minute walk


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--max-distance", type=float, default=D_INF,
                   help="routing cutoff in metres. default 2400 (Walk Score D_inf)")
    p.add_argument("--top-r", type=int, default=10,
                   help="how many nearest instances to keep. default 10 "
                        "(Dai et al. use r=10 for restaurants)")
    p.add_argument("--samples", type=int, default=7)
    p.add_argument("--coverage", type=float, default=0.5)
    p.add_argument("--out", type=Path, default=OUT)
    args = p.parse_args()

    from build_walk_graph import load as load_graph
    from build_grid_routed import (load_amenities, build_samples, bounded_dijkstra)
    from scipy.spatial import cKDTree

    ref = pickle.load(open(REPO_ROOT / "outputs" /
                           "preprocessed_grid_scores_with_wards.pkl", "rb"))
    cells = ref["grid_cells"]
    g, lon, lat = load_graph()
    indptr, indices, data = g.indptr, g.indices, g.data
    print(f"grid {len(cells):,} cells | graph {len(lon):,} nodes | "
          f"cutoff {args.max_distance:.0f} m | top-r {args.top_r}")

    slon, slat, sowner, cell_ids = build_samples(cells, args.samples)
    n_per = args.samples ** 2
    need = int(np.ceil(args.coverage * n_per))     # need this many to have a median
    tree = cKDTree(np.c_[lon, lat])
    snap_deg, snap_node = tree.query(np.c_[slon, slat])
    mlat, mlon = 111132.0, 111320.0 * np.cos(np.radians(float(np.mean(slat))))
    snap_off = snap_deg * np.sqrt((mlat ** 2 + mlon ** 2) / 2.0)
    print(f"samples {len(slon):,} ({n_per}/cell), need >= {need} reachable for a median")

    order = np.argsort(snap_node, kind="stable")
    ns = snap_node[order]
    starts = np.searchsorted(ns, np.arange(len(lon)), "left")
    ends = np.searchsorted(ns, np.arange(len(lon)), "right")

    amen = load_amenities()
    rules = json.load(open(REPO_ROOT / "src" / "03_maintain" /
                           "category_rules.reconciled.json", encoding="utf-8"))
    mn = rules.get("min_amenity_count", 0)
    fk, fd = set(rules.get("force_keep", [])), set(rules.get("force_drop", []))
    amen = {c: v for c, v in amen.items()
            if c not in fd and (len(v) >= mn or c in fk)}
    total = sum(len(v) for v in amen.values())
    print(f"amenities {total:,} across {len(amen)} categories\n")

    tree_g = cKDTree(np.c_[lon, lat])
    out = {}
    done, t0 = 0, time.time()

    for cat, items in sorted(amen.items()):
        per_cell = {}          # cell_ix -> list of distances (kept short)
        for origins in items:
            arr = np.array(origins, dtype=float)
            _, src = tree_g.query(arr)
            src = np.unique(np.atleast_1d(src))
            dist = bounded_dijkstra(indptr, indices, data, src, args.max_distance)
            done += 1
            if not dist:
                continue
            # gather reachable sample distances per cell
            acc = {}
            for u, d in dist.items():
                a, b = starts[u], ends[u]
                if a == b:
                    continue
                for si in order[a:b]:
                    eff = d + snap_off[si]
                    if eff <= args.max_distance:
                        acc.setdefault(sowner[si], []).append(eff)
            for cix, vals in acc.items():
                if len(vals) < need:
                    continue                     # under half the cell -> not reached
                vals.sort()
                med = vals[need - 1]             # distance at which >=50% is reached
                lst = per_cell.setdefault(cix, [])
                lst.append(med)
                if len(lst) > args.top_r * 3:    # trim occasionally
                    lst.sort(); del lst[args.top_r:]
            if done % 2000 == 0:
                el = time.time() - t0
                print(f"  {done:,}/{total:,}  {el:.0f}s  eta {el/done*(total-done):.0f}s")
        out[cat] = {cell_ids[cix]: sorted(v)[:args.top_r] for cix, v in per_cell.items()}

    doc = {"distances": out,
           "cell_ids": cell_ids,
           "metadata": {
               "max_distance_m": args.max_distance,
               "top_r": args.top_r,
               "samples_per_cell": n_per,
               "coverage_rule": f"median over sample lattice at >= {args.coverage:.0%}",
               "amenities": {k: len(v) for k, v in amen.items()},
               "total_amenities": total,
               "purpose": "distance side of the Walk Score comparison "
                          "(Dai et al. arXiv:2212.05192)",
           }}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "wb") as f:
        pickle.dump(doc, f)
    n = sum(len(v) for v in out.values())
    print(f"\nwrote {args.out} in {time.time()-t0:.0f}s")
    print(f"  {len(out)} categories, {n:,} (category, cell) distance lists")


if __name__ == "__main__":
    main()
