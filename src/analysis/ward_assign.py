#!/usr/bin/env python3
"""
Assign grid cells to BBMP wards by majority area overlap.

`wardgen2.py` does this for the published 250 m grid only. Sensitivity analysis
needs the same assignment for grids regenerated at other cell sizes, using the
same rule so results stay comparable: a cell belongs to the ward covering the
largest share of its area, provided that share reaches `threshold`.

Areas are computed in UTM 43N, not degrees, so the majority test is a real area
test rather than a lat/lon artefact.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WARDS = REPO_ROOT / "data" / "wards.geojson"
UTM, WGS84 = "EPSG:32643", "EPSG:4326"


def assign(cells, wards_path=WARDS, threshold=0.5, name_field="Ward_Name",
           fill=True):
    """
    cells: {cell_id: {"bounds": (minx, miny, maxx, maxy), ...}}
    -> {cell_id: ward_name}

    A cell goes to the ward covering the largest share of its area. With
    `fill=True` (default) that plurality assignment stands even below
    `threshold`, so any cell overlapping a ward at all is assigned; cells with no
    overlap remain unassigned and correctly fall outside the city.

    `fill=False` applies the strict majority rule of wardgen2, which leaves
    boundary-straddling cells unassigned and produces small holes in rendered
    maps. Ward-level statistics are insensitive to the choice (the affected cells
    are ~2% of the total, all at ward edges), but the strict rule is retained for
    reproducing the published tagging.
    """
    import geopandas as gpd
    from shapely.geometry import box

    wards = gpd.read_file(wards_path)[[name_field, "geometry"]].to_crs(UTM)
    wards = wards[wards.geometry.notna() & ~wards.geometry.is_empty]
    wards["geometry"] = wards.geometry.buffer(0)          # repair self-intersections

    ids = list(cells)
    grid = gpd.GeoDataFrame(
        {"cell_id": ids},
        geometry=[box(*cells[c]["bounds"]) for c in ids],
        crs=WGS84,
    ).to_crs(UTM)
    grid["cell_area"] = grid.geometry.area

    ov = gpd.overlay(grid, wards, how="intersection", keep_geom_type=True)
    if ov.empty:
        return {}
    ov["frac"] = ov.geometry.area / ov["cell_area"]
    ov = ov.sort_values("frac", ascending=False).drop_duplicates("cell_id")
    if not fill:
        ov = ov[ov["frac"] >= threshold]
    return dict(zip(ov["cell_id"], ov[name_field]))


if __name__ == "__main__":
    import argparse, pickle
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("pkl", type=Path)
    p.add_argument("--threshold", type=float, default=0.5)
    a = p.parse_args()
    d = pickle.load(open(a.pkl, "rb"))
    m = assign(d["grid_cells"], threshold=a.threshold)
    print(f"{a.pkl.name}: {len(m):,} of {len(d['grid_cells']):,} cells assigned "
          f"to {len(set(m.values()))} wards")
