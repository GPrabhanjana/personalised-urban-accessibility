#!/usr/bin/env python3
"""
Rebuild the grid score pickle using exact network routing.

Replaces the affine-scaling approach (mapgen2.py, factor = level/15) with real
shortest-path routing at every threshold, computed over the OSM pedestrian graph
built by src/01_isochrones/build_walk_graph.py. No Valhalla, no Docker.

What changes versus mapgen2:
  * every threshold is routed, not geometrically shrunk from the 15-minute
    polygon, so the IoU 0.64 interpolation limitation disappears
  * amenities are read from SOURCE geometry, so each counts once. The existing
    catchment files mix real isochrones with source amenity footprints, which
    inflates feature counts
  * large polygon amenities seed multiple origins and are routed as a single
    multi-source query, so their catchment is a true union counted once
  * cell coverage is estimated from a regular lattice of sample points rather
    than polygon intersection, which removes the polygonisation step and its
    shape parameter while keeping the same ">= 50% of the cell" semantics

What deliberately does NOT change: the output schema. Still
cell_id -> [k per level] per category, so the two-stage architecture and the
real-time web view are unaffected.
"""

import argparse
import heapq
import json
import pickle
import sys
import time
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src" / "01_isochrones"))

ATTRS = REPO_ROOT / "src" / "analysis" / "category_attributes.json"
POINTS = REPO_ROOT / "data" / "03_geometry" / "points"
POLY = REPO_ROOT / "data" / "03_geometry" / "poly"
METRO = REPO_ROOT / "data" / "reference" / "metro.geojson"
OUT = REPO_ROOT / "outputs" / "preprocessed_grid_scores_routed.pkl"

GROUP_FIELDS = ["tourism", "attraction", "shop", "leisure", "amenity"]
UTM, WGS84 = "EPSG:32643", "EPSG:4326"
LARGE_PERIMETER_M = 250.0   # above this, seed multiple origins (as gen_iso_poly did)
ORIGIN_SPACING_M = 250.0


# ── amenity origins ───────────────────────────────────────────────────────────

def category_of(base, props):
    for f in GROUP_FIELDS:
        v = props.get(f)
        if v:
            return f"{base}+{v}"
    return None


def base_of(fp):
    b = fp.stem.replace("(points)", "").replace("(poly)", "").strip()
    return b.replace("-", "+")


def load_amenities():
    """
    -> {category: [ [ (lon,lat), ... ] ]}  one origin list per amenity.

    Source files are separate per-theme OSM exports, so one physical feature can
    appear in several of them (a bakery in both food+drink and shops+food). Each
    osm_id is therefore assigned to exactly ONE category - the largest candidate,
    which keeps specialist duplicates folded into the main category rather than
    counted twice.
    """
    from shapely.geometry import shape
    from shapely.ops import transform
    import pyproj

    to_utm = pyproj.Transformer.from_crs(WGS84, UTM, always_xy=True).transform
    out = {}

    # pass 1: gather every candidate (osm_id -> categories) to resolve duplicates
    cand = []
    tally = {}
    for fp in sorted(POINTS.glob("*.geojson")) + sorted(POLY.glob("*.geojson")):
        base = base_of(fp)
        for ft in json.load(open(fp, encoding="utf-8"))["features"]:
            props = ft.get("properties") or {}
            c = category_of(base, props)
            if not c:
                continue
            oid = props.get("full_id") or props.get("osm_id")
            cand.append((oid, c))
            tally[c] = tally.get(c, 0) + 1
    assign = {}
    for oid, c in cand:
        if oid is None:
            continue
        cur = assign.get(oid)
        if cur is None or (tally[c], c) > (tally[cur], cur):
            assign[oid] = c
    dropped = sum(1 for oid, c in cand if oid is not None and assign.get(oid) != c)
    print(f"  deduplicated {dropped} cross-file duplicate features by osm_id")

    def wanted(props, c):
        oid = props.get("full_id") or props.get("osm_id")
        return oid is None or assign.get(oid) == c

    for fp in sorted(POINTS.glob("*.geojson")):
        base = base_of(fp)
        for ft in json.load(open(fp, encoding="utf-8"))["features"]:
            g = ft.get("geometry") or {}
            if g.get("type") != "Point":
                continue
            props = ft.get("properties") or {}
            c = category_of(base, props)
            if c and wanted(props, c):
                out.setdefault(c, []).append([tuple(g["coordinates"][:2])])

    for fp in sorted(POLY.glob("*.geojson")):
        base = base_of(fp)
        for ft in json.load(open(fp, encoding="utf-8"))["features"]:
            g = ft.get("geometry") or {}
            if g.get("type") not in ("Polygon", "MultiPolygon"):
                continue
            props = ft.get("properties") or {}
            c = category_of(base, props)
            if not c or not wanted(props, c):
                continue
            geom = shape(g)
            if geom.is_empty:
                continue
            per = transform(to_utm, geom).length
            if per <= LARGE_PERIMETER_M:
                cen = geom.centroid
                origins = [(cen.x, cen.y)]
            else:
                # walk the boundary at fixed spacing, mirroring gen_iso_poly
                bnd = geom.exterior if geom.geom_type == "Polygon" else geom.geoms[0].exterior
                bu = transform(to_utm, bnd)
                n = max(2, int(bu.length // ORIGIN_SPACING_M))
                origins = [(bnd.interpolate(i / n, normalized=True).x,
                            bnd.interpolate(i / n, normalized=True).y) for i in range(n)]
            out.setdefault(c, []).append(origins)

    if METRO.exists():
        for ft in json.load(open(METRO, encoding="utf-8"))["features"]:
            g = ft.get("geometry") or {}
            if g.get("type") == "Point":
                out.setdefault("metro station", []).append([tuple(g["coordinates"][:2])])
    return out


# ── grid generation ───────────────────────────────────────────────────────────

def make_grid(bounds, cell_size_m):
    """Regular grid over `bounds` (lon/lat), squared in UTM. Mirrors mapgen2."""
    import pyproj
    to_utm = pyproj.Transformer.from_crs(WGS84, UTM, always_xy=True)
    to_wgs = pyproj.Transformer.from_crs(UTM, WGS84, always_xy=True)
    ub = to_utm.transform_bounds(*bounds)
    cols = int(np.ceil((ub[2] - ub[0]) / cell_size_m))
    rows = int(np.ceil((ub[3] - ub[1]) / cell_size_m))
    cells, cid = {}, 0
    for r in range(rows):
        for c in range(cols):
            x0 = ub[0] + c * cell_size_m
            y0 = ub[1] + r * cell_size_m
            wx0, wy0 = to_wgs.transform(x0, y0)
            wx1, wy1 = to_wgs.transform(x0 + cell_size_m, y0 + cell_size_m)
            cells[cid] = {"bounds": (wx0, wy0, wx1, wy1),
                          "center": ((wx0 + wx1) / 2, (wy0 + wy1) / 2),
                          "row": r, "col": c}
            cid += 1
    return cells


# ── grid + sample lattice ─────────────────────────────────────────────────────

def build_samples(cells, per_side):
    """Regular per_side x per_side lattice inside each cell -> (lon, lat, cell_ix)."""
    ids = sorted(cells)
    frac = (np.arange(per_side) + 0.5) / per_side
    lons, lats, owner = [], [], []
    for ix, cid in enumerate(ids):
        x0, y0, x1, y1 = cells[cid]["bounds"]
        gx = x0 + frac * (x1 - x0)
        gy = y0 + frac * (y1 - y0)
        xx, yy = np.meshgrid(gx, gy)
        lons.append(xx.ravel()); lats.append(yy.ravel())
        owner.append(np.full(per_side * per_side, ix))
    return (np.concatenate(lons), np.concatenate(lats),
            np.concatenate(owner).astype(np.int32), ids)


# ── bounded multi-source Dijkstra ─────────────────────────────────────────────

def bounded_dijkstra(indptr, indices, data, sources, limit):
    """Only touches nodes within `limit`, so cost is independent of graph size."""
    dist, heap = {}, []
    for s in sources:
        if s not in dist or 0.0 < dist[s]:
            dist[s] = 0.0
            heapq.heappush(heap, (0.0, s))
    while heap:
        d, u = heapq.heappop(heap)
        if d > dist.get(u, np.inf):
            continue
        for e in range(indptr[u], indptr[u + 1]):
            v = indices[e]
            nd = d + data[e]
            if nd <= limit and nd < dist.get(v, np.inf):
                dist[v] = nd
                heapq.heappush(heap, (nd, v))
    return dist


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--samples", type=int, default=7,
                   help="lattice per side. default 7 (49 points, 36 m spacing)")
    p.add_argument("--coverage", type=float, default=0.5, help="fraction covered. default 0.5")
    p.add_argument("--cell-size", type=float, default=None,
                   help="regenerate the grid at this cell size in metres "
                        "(default: reuse the published 250 m grid)")
    p.add_argument("--out", type=Path, default=OUT)
    p.add_argument("--limit-categories", type=int, default=0, help="debug: first N categories")
    p.add_argument("--curate", type=Path,
                   default=REPO_ROOT / "src" / "03_maintain" / "category_rules.reconciled.json",
                   help="category rules to apply to TRUE amenity counts; 'none' to skip")
    args = p.parse_args()

    from build_walk_graph import load as load_graph
    from scipy.spatial import cKDTree

    attrs = json.load(open(ATTRS, encoding="utf-8"))
    speed = attrs["walking_speed_m_per_min"]
    levels = attrs["tau_grid"]
    budgets = np.array([speed * t for t in levels], dtype=float)
    print(f"speed {speed} m/min | levels {levels} | budgets {budgets.astype(int).tolist()} m")

    ref = pickle.load(open(REPO_ROOT / "outputs" /
                           "preprocessed_grid_scores_with_wards.pkl", "rb"))
    ref_cells = ref["grid_cells"]
    if args.cell_size and args.cell_size != ref.get("cell_size_m", 250):
        bs = [c["bounds"] for c in ref_cells.values()]
        bounds = (min(b[0] for b in bs), min(b[1] for b in bs),
                  max(b[2] for b in bs), max(b[3] for b in bs))
        cells = make_grid(bounds, args.cell_size)
        print(f"grid: regenerated at {args.cell_size} m -> {len(cells):,} cells "
              f"(reference was {len(ref_cells):,} at {ref.get('cell_size_m',250)} m)")
    else:
        cells = ref_cells
        print(f"grid: {len(cells):,} cells at {ref.get('cell_size_m',250)} m")

    g, lon, lat = load_graph()
    indptr, indices, data = g.indptr, g.indices, g.data
    print(f"graph: {len(lon):,} nodes")

    slon, slat, sowner, cell_ids = build_samples(cells, args.samples)
    n_per = args.samples ** 2
    need = args.coverage * n_per
    print(f"samples: {len(slon):,} ({n_per} per cell), need >= {need:g} covered")

    tree = cKDTree(np.c_[lon, lat])
    t0 = time.time()
    snap_deg, snap_node = tree.query(np.c_[slon, slat])
    # approximate degree->metre at this latitude for the snap offset
    mlat = 111132.0
    mlon = 111320.0 * np.cos(np.radians(float(np.mean(slat))))
    snap_off = snap_deg * np.sqrt((mlat ** 2 + mlon ** 2) / 2.0)
    print(f"snapped samples in {time.time()-t0:.1f}s "
          f"(median offset {np.median(snap_off):.0f} m)")

    order = np.argsort(snap_node, kind="stable")
    node_sorted = snap_node[order]
    starts = np.searchsorted(node_sorted, np.arange(len(lon)), "left")
    ends = np.searchsorted(node_sorted, np.arange(len(lon)), "right")

    amen = load_amenities()
    if args.limit_categories:
        amen = dict(list(amen.items())[:args.limit_categories])

    # Curation runs on TRUE amenity counts, not catchment-feature counts.
    if str(args.curate).lower() != "none" and Path(args.curate).exists():
        rules = json.load(open(args.curate, encoding="utf-8"))
        mn = rules.get("min_amenity_count", 0)
        fk, fd = set(rules.get("force_keep", [])), set(rules.get("force_drop", []))
        before = len(amen)
        for tgt, srcs in rules.get("merges", {}).items():
            for sc in srcs:
                if sc in amen:
                    amen.setdefault(tgt, []).extend(amen.pop(sc))
        amen = {c: v for c, v in amen.items()
                if c not in fd and (len(v) >= mn or c in fk)}
        print(f"curation ({Path(args.curate).name}): min={mn}, force_keep={len(fk)}, "
              f"force_drop={len(fd)}, merges={len(rules.get('merges', {}))}"
              f"  ->  {before} categories reduced to {len(amen)}")

    total = sum(len(v) for v in amen.values())
    print(f"amenities: {total:,} across {len(amen)} categories\n")

    tree_g = cKDTree(np.c_[lon, lat])
    grid_scores = {}
    max_budget = float(budgets[-1])
    done = 0
    t0 = time.time()

    for cat, items in sorted(amen.items()):
        counts = {}
        for origins in items:
            arr = np.array(origins, dtype=float)
            _, src_nodes = tree_g.query(arr)
            src_nodes = np.unique(np.atleast_1d(src_nodes))
            dist = bounded_dijkstra(indptr, indices, data, src_nodes, max_budget)
            if not dist:
                done += 1
                continue
            per_cell = {}
            for u, d in dist.items():
                a, b = starts[u], ends[u]
                if a == b:
                    continue
                for si in order[a:b]:
                    eff = d + snap_off[si]
                    if eff > max_budget:
                        continue
                    row = per_cell.get(sowner[si])
                    if row is None:
                        row = per_cell[sowner[si]] = [0] * len(budgets)
                    for li in range(len(budgets)):
                        if eff <= budgets[li]:
                            row[li] += 1
            for cix, row in per_cell.items():
                tgt = counts.get(cix)
                if tgt is None:
                    tgt = counts[cix] = [0] * len(budgets)
                for li in range(len(budgets)):
                    if row[li] >= need:
                        tgt[li] += 1
            done += 1
            if done % 2000 == 0:
                el = time.time() - t0
                print(f"  {done:,}/{total:,} amenities  {el:.0f}s  "
                      f"eta {el/done*(total-done):.0f}s")
        grid_scores[cat] = {cell_ids[cix]: v for cix, v in counts.items()
                            if any(v)}

    out = {
        "grid_cells": cells,
        "grid_index": {(c["row"], c["col"]): cid for cid, c in cells.items()},
        "grid_scores": grid_scores,
        "cell_size_m": args.cell_size or ref.get("cell_size_m", 250),
        "coverage_threshold": args.coverage,
        "levels": levels,
        "max_level": levels[-1],
        "metadata": {
            "method": "exact_network_routing",
            "walking_speed_m_per_min": speed,
            "budgets_m": budgets.tolist(),
            "samples_per_cell": n_per,
            "coverage_rule": f">= {args.coverage:.0%} of {n_per} sample points reachable",
            "amenities": {k: len(v) for k, v in amen.items()},
            "total_amenities": total,
            "graph_nodes": int(len(lon)),
            "sources": ["rahul2021acceptable", "banerjee2018pedestrian"],
            "supersedes": "affine-scaled catchments from mapgen2.py",
        },
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "wb") as f:
        pickle.dump(out, f)
    print(f"\nwrote {args.out} in {time.time()-t0:.0f}s")
    print(f"  categories {len(grid_scores)}  total amenities {total:,}")


if __name__ == "__main__":
    main()
