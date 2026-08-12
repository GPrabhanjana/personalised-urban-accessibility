#!/usr/bin/env python3
"""
Build a routable pedestrian graph from the local OpenStreetMap extract.

Reads valhalla_south_india/custom_files/karnataka.osm.pbf through GDAL's OSM
driver, keeps walkable ways, and writes a sparse graph to outputs/routing/.

This replaces the Valhalla/Docker dependency for catchment generation. Routing
is still fully path-dependent: edges follow street geometry and traversal cost
is network length, so reachability is shortest-path over the walk network, not
a radius. The only thing not reproduced is Valhalla's contour polygonisation,
which is a rendering of reachability rather than reachability itself.
"""

import argparse
import sys
import time
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]
PBF = REPO_ROOT / "valhalla_south_india" / "custom_files" / "karnataka.osm.pbf"
OUT = REPO_ROOT / "outputs" / "routing"

# Ways a pedestrian may use. Mirrors Valhalla's pedestrian costing: motorways and
# their links are excluded, everything else walkable is kept.
WALKABLE = {
    "residential", "service", "footway", "tertiary", "secondary", "primary",
    "living_street", "path", "steps", "unclassified", "track", "pedestrian",
    "road", "primary_link", "secondary_link", "tertiary_link", "trunk",
    "trunk_link", "corridor", "crossing",
}
EXCLUDED = {"motorway", "motorway_link", "construction", "proposed", "raceway"}

# BBMP bounding box, from the analysis grid extent
BBOX = (77.45284, 12.83092, 77.79005, 13.14815)

R = 6371008.8


def haversine(lon1, lat1, lon2, lat2):
    p = np.pi / 180.0
    a = (np.sin((lat2 - lat1) * p / 2) ** 2
         + np.cos(lat1 * p) * np.cos(lat2 * p) * np.sin((lon2 - lon1) * p / 2) ** 2)
    return 2 * R * np.arcsin(np.sqrt(a))


def build(pbf=PBF, bbox=BBOX, precision=7):
    import pyogrio
    from shapely import get_coordinates

    t0 = time.time()
    print(f"reading {pbf.name} (lines, bbox-filtered)...")
    gdf = pyogrio.read_dataframe(pbf, layer="lines", bbox=bbox, columns=["highway"])
    print(f"  {len(gdf):,} ways in {time.time() - t0:.1f}s")

    keep = gdf["highway"].isin(WALKABLE) & ~gdf["highway"].isin(EXCLUDED)
    gdf = gdf.loc[keep & gdf.geometry.notna()]
    print(f"  {len(gdf):,} walkable ways")

    # Explode every way into consecutive coordinate pairs. Shared coordinates
    # become shared nodes, which is what makes the result a connected network
    # rather than a pile of disjoint lines.
    src_list, dst_list = [], []
    coords_all, offsets = [], []
    for geom in gdf.geometry.values:
        c = get_coordinates(geom)
        if len(c) < 2:
            continue
        offsets.append(len(coords_all))
        coords_all.append(c)
    if not coords_all:
        sys.exit("error: no walkable geometry found")

    stacked = np.vstack(coords_all)
    keys = np.round(stacked, precision)
    uniq, inv = np.unique(keys, axis=0, return_inverse=True)
    print(f"  {len(stacked):,} vertices -> {len(uniq):,} unique nodes")

    pos = 0
    for c in coords_all:
        n = len(c)
        idx = inv[pos:pos + n]
        src_list.append(idx[:-1])
        dst_list.append(idx[1:])
        pos += n
    src = np.concatenate(src_list)
    dst = np.concatenate(dst_list)

    lon, lat = uniq[:, 0], uniq[:, 1]
    w = haversine(lon[src], lat[src], lon[dst], lat[dst])
    ok = (w > 0) & (src != dst)
    src, dst, w = src[ok], dst[ok], w[ok]
    print(f"  {len(src):,} directed edge pairs, total length {w.sum() / 1000:,.0f} km")

    OUT.mkdir(parents=True, exist_ok=True)
    fp = OUT / "walk_graph.npz"
    np.savez_compressed(fp, src=src, dst=dst, w=w, lon=lon, lat=lat)
    print(f"\nwrote {fp}  ({fp.stat().st_size / 1e6:.1f} MB) in {time.time() - t0:.1f}s")
    return fp


def load(fp=OUT / "walk_graph.npz"):
    """-> (scipy CSR graph, lon, lat). Undirected: walking is symmetric."""
    from scipy.sparse import coo_matrix
    d = np.load(fp)
    src, dst, w, lon, lat = d["src"], d["dst"], d["w"], d["lon"], d["lat"]
    n = len(lon)
    g = coo_matrix((np.concatenate([w, w]),
                    (np.concatenate([src, dst]), np.concatenate([dst, src]))),
                   shape=(n, n)).tocsr()
    return g, lon, lat


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--pbf", type=Path, default=PBF)
    args = p.parse_args()
    if not args.pbf.exists():
        sys.exit(f"error: extract not found: {args.pbf}")
    build(args.pbf)


if __name__ == "__main__":
    main()
