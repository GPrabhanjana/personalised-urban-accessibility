#!/usr/bin/env python3
"""
Batch-render accessibility grids over a FIXED extent, one image per setting.

Every panel in a run uses the same bounding box and the same colour scale, so
the images are directly comparable and differences between panels are caused
only by the setting being varied. This replaces manually screenshotting the web
map, where the extent, zoom and basemap tiles drift between captures.

Extent (pick one)
-----------------
  --from-hotspots FILE --rank N   window found by find_hotspots.py
  --bbox LON0 LAT0 LON1 LAT1      explicit
  --whole-city                    everything assigned to a BBMP ward

Sweep (pick one)
----------------
  --presets A B C     one panel per preset
  --sweep tau         one panel per travel-time threshold, preset held fixed
  --sweep decay       one panel per decay setting
  --sweep weight      one panel per weighting tier, applied to --sweep-group
  --sweep grouping    --sweep-group evaluated as one group, then separately
  --sweep resolution  the same configuration at cell and at ward resolution

Add --ward-view to collapse any run to ward means.

Writes one PNG per panel plus a combined contact sheet.
Requires matplotlib.
"""

import argparse
import copy
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scoring import (Grid, load_presets, score_preset, DECAY, WEIGHT,  # noqa: E402
                     NEGATIVE, AVOID_LAMBDA, DECAY_LABEL, DECAY_ORDER,
                     LEVELS, DEFAULT_PKL, DEFAULT_PRESETS)
import basemap  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = REPO_ROOT / "outputs" / "figures"

# The exact 5-stop ramp the web app interpolates in getColorFromScore()
# (webapp/script.js). Close to viridis but not identical — the fourth stop is
# lighter — so figures rendered here match the ones captured from the app.
WEBAPP_STOPS = [(68, 1, 84), (49, 104, 142), (53, 183, 121),
                (173, 221, 142), (253, 231, 37)]


# ── extent ────────────────────────────────────────────────────────────────────

def window_from_bbox(grid, bbox, pad=0):
    """Smallest row/col window covering bbox, expanded by `pad` cells."""
    lon0, lat0, lon1, lat1 = bbox
    rows, cols = [], []
    for (r, c), cid in grid.index.items():
        x0, y0, x1, y1 = grid.cells[cid]["bounds"]
        if x1 >= lon0 and x0 <= lon1 and y1 >= lat0 and y0 <= lat1:
            rows.append(r); cols.append(c)
    if not rows:
        sys.exit("error: bbox does not intersect the grid")
    return (max(min(rows) - pad, 0), max(min(cols) - pad, 0),
            min(max(rows) + pad, grid.n_rows - 1), min(max(cols) + pad, grid.n_cols - 1))


def window_whole_city(grid, pad=0):
    rows = [grid.cells[c]["row"] for c in grid.ward_of]
    cols = [grid.cells[c]["col"] for c in grid.ward_of]
    return (max(min(rows) - pad, 0), max(min(cols) - pad, 0),
            min(max(rows) + pad, grid.n_rows - 1), min(max(cols) + pad, grid.n_cols - 1))


def extent_of(grid, win):
    """(left, right, bottom, top) in lon/lat."""
    r0, c0, r1, c1 = win
    bs = [grid.cells[grid.index[(r, c)]]["bounds"]
          for r in (r0, r1) for c in (c0, c1) if (r, c) in grid.index]
    return (min(b[0] for b in bs), max(b[2] for b in bs),
            min(b[1] for b in bs), max(b[3] for b in bs))


def mesh_edges(grid, win):
    """
    Cell-edge coordinates in Web Mercator, for pcolormesh.

    The grid was built in UTM and reprojected, so a column's longitude varies
    very slightly with row; we take the median edge per column (and per row) and
    project those. At 250 m cells the residual is far below one pixel.
    """
    import statistics as st
    r0, c0, r1, c1 = win
    xs, ys = [], []
    for c in range(c0, c1 + 1):
        vals = [grid.cells[grid.index[(r, c)]]["bounds"][0]
                for r in range(r0, r1 + 1) if (r, c) in grid.index]
        xs.append(st.median(vals))
    last = [grid.cells[grid.index[(r, c1)]]["bounds"][2]
            for r in range(r0, r1 + 1) if (r, c1) in grid.index]
    xs.append(st.median(last))
    for r in range(r0, r1 + 1):
        vals = [grid.cells[grid.index[(r, c)]]["bounds"][1]
                for c in range(c0, c1 + 1) if (r, c) in grid.index]
        ys.append(st.median(vals))
    top = [grid.cells[grid.index[(r1, c)]]["bounds"][3]
           for c in range(c0, c1 + 1) if (r1, c) in grid.index]
    ys.append(st.median(top))
    return [basemap.merc_x(v) for v in xs], [basemap.merc_y(v) for v in ys]


def ward_flatten(grid, scores):
    """Give every cell its ward's mean score.

    Ward View in the application colours whole wards; drawing it here as a
    flattened cell raster reuses the same mesh, extent and colour scale as every
    other panel, so a cell-resolution panel and a ward-resolution panel of the
    same configuration are directly comparable. Cells outside every ward keep
    their own value and are masked out of the figure anyway.
    """
    acc = {}
    for cid, w in grid.ward_of.items():
        acc.setdefault(w, []).append(scores[cid])
    mean = {w: sum(v) / len(v) for w, v in acc.items()}
    return {cid: mean[grid.ward_of[cid]] if cid in grid.ward_of else s
            for cid, s in scores.items()}


def raster(grid, scores, win, mask_outside):
    r0, c0, r1, c1 = win
    out = []
    for r in range(r0, r1 + 1):
        row = []
        for c in range(c0, c1 + 1):
            cid = grid.index.get((r, c))
            if cid is None or (mask_outside and cid not in grid.ward_of):
                row.append(float("nan"))
            else:
                row.append(scores[cid])
        out.append(row)
    return out


# ── panel construction ────────────────────────────────────────────────────────

def build_panels(grid, args, presets):
    """-> [(slug, title, cell_id->score)]"""
    if args.sweep is None:
        return [(k, presets[k]["label"], score_preset(grid, presets[k])) for k in presets]

    if not args.preset:
        sys.exit("error: --sweep needs --preset")
    base = presets[args.preset]
    panels = []

    if args.sweep == "tau":
        # grid.levels, not the module-level LEVELS default: the routed dataset
        # carries 3-15 min, the superseded one only 5-15.
        for tau in (args.tau_values or grid.levels):
            p = copy.deepcopy(base)
            for g in p["groups"]:
                g["ti"] = grid.level_index(tau)
            panels.append((f"tau{tau}", f"$\\tau_c$ = {tau} min", score_preset(grid, p)))

    elif args.sweep == "decay":
        # Iterate DECAY_ORDER, not DECAY, and take the title from DECAY_LABEL:
        # a positional --titles list silently mislabels the panels if the dict
        # order and the intended display order disagree.
        for name in DECAY_ORDER:
            p = copy.deepcopy(base)
            for g in p["groups"]:
                if g["negative"]:
                    continue      # avoidance has no "how many nearby" setting
                g["lam"] = DECAY[name]
            panels.append((f"decay_{name}", DECAY_LABEL[name],
                           score_preset(grid, p)))

    elif args.sweep == "weight":
        if not args.sweep_group:
            sys.exit("error: --sweep weight needs --sweep-group "
                     + f"(one of: {[g['label'] for g in base['groups']]})")
        hit = [g for g in base["groups"] if g["label"] == args.sweep_group]
        if not hit:
            sys.exit(f"error: no group {args.sweep_group!r} in preset {args.preset!r}; "
                     f"have {[g['label'] for g in base['groups']]}")
        # The four treatments, in the order the interface shows them, so the
        # contact sheet reads Wanted / Required on the top row and Avoid / Never
        # below when laid out 2x2.
        for tier, title in (("standard", "Wanted"), ("required", "Required"),
                            ("avoided", "Avoid"), ("excluded", "Never")):
            p = copy.deepcopy(base)
            for g in p["groups"]:
                if g["label"] == args.sweep_group:
                    g["w"] = WEIGHT[tier]
                    g["required"] = tier == "required"
                    g["excluded"] = tier == "excluded"
                    g["negative"] = tier in NEGATIVE
                    g["weight_name"] = tier
                    if tier in NEGATIVE:
                        g["lam"] = AVOID_LAMBDA
            panels.append((f"treatment_{tier}",
                           f"{args.sweep_group}: {title}",
                           score_preset(grid, p)))

    elif args.sweep == "resolution":
        s = score_preset(grid, base)
        panels.append(("resolution_cell", "Grid cells", s))
        panels.append(("resolution_ward", "Ward means", ward_flatten(grid, s)))

    elif args.sweep == "grouping":
        # Same categories, evaluated as one substitutable group versus separately.
        target = [g for g in base["groups"] if g["label"] == args.sweep_group]
        if not target:
            sys.exit(f"error: no group {args.sweep_group!r} in preset {args.preset!r}; "
                     f"have {[g['label'] for g in base['groups']]}")
        if len(target[0]["categories"]) < 2:
            sys.exit(f"error: {args.sweep_group!r} has one category; nothing to ungroup")
        panels.append(("grouping_together", f"{args.sweep_group}: grouped",
                       score_preset(grid, base)))
        p = copy.deepcopy(base)
        split = []
        for g in p["groups"]:
            if g["label"] != args.sweep_group:
                split.append(g)
                continue
            for c in g["categories"]:
                one = copy.deepcopy(g)
                one["categories"] = [c]
                one["label"] = c
                split.append(one)
        p["groups"] = split
        panels.append(("grouping_separate", f"{args.sweep_group}: evaluated separately",
                       score_preset(grid, p)))
    return panels


# ── drawing ───────────────────────────────────────────────────────────────────

def render(grid, panels, win, args, out_dir):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    from matplotlib.colors import Normalize

    ext_ll = extent_of(grid, win)
    bbox = (ext_ll[0], ext_ll[2], ext_ll[1], ext_ll[3])  # lon0, lat0, lon1, lat1
    xs, ys = mesh_edges(grid, win)
    mx0, mx1, my0, my1 = xs[0], xs[-1], ys[0], ys[-1]

    tiles, tile_extent = (None, None)
    if args.basemap != "none":
        tiles, tile_extent = basemap.fetch(bbox, args.basemap, args.basemap_px)

    rasters = [(slug, title, np.array(raster(grid, s, win, not args.no_mask), dtype=float))
               for slug, title, s in panels]

    if args.autoscale:
        vals = np.concatenate([R[~np.isnan(R)].ravel() for _, _, R in rasters])
        vmin, vmax = (float(vals.min()), float(vals.max())) if vals.size else (0.0, 1.0)
    else:
        vmin, vmax = 0.0, 1.0
    from matplotlib.colors import ListedColormap, LinearSegmentedColormap
    norm = Normalize(vmin=vmin, vmax=vmax)
    solid = (LinearSegmentedColormap.from_list(
                 "webapp", [tuple(v / 255 for v in c) for c in WEBAPP_STOPS])
             if args.cmap == "webapp" else plt.get_cmap(args.cmap))

    # Default is a FLAT alpha, matching the web app's fillOpacity (0.6). The
    # optional ramp keeps low scores translucent, which reads better on a busy
    # basemap but does not match figures captured from the app.
    lut = solid(np.linspace(0, 1, 256))
    lut[:, 3] = (np.linspace(args.alpha_min, args.alpha, 256) if args.alpha_ramp
                 else np.full(256, args.alpha))
    cmap = ListedColormap(lut)
    cmap.set_bad(alpha=0.0)  # no-data lets the basemap show through
    credit = basemap.attribution(args.basemap) if tiles is not None else ""

    def draw(ax, R, title):
        if tiles is not None:
            ax.imshow(tiles, extent=tile_extent, origin="upper",
                      interpolation="bilinear", zorder=0)
        else:
            ax.set_facecolor(args.nodata_color)
        ax.pcolormesh(xs, ys, np.ma.masked_invalid(R), cmap=cmap, norm=norm,
                      shading="flat", zorder=1,
                      edgecolors="none", antialiased=False)
        ax.set_xlim(mx0, mx1); ax.set_ylim(my0, my1)
        ax.set_aspect("equal")
        ax.set_xticks([]); ax.set_yticks([])
        if title and not args.no_titles:
            import textwrap
            ax.set_title("\n".join(textwrap.wrap(title, 22)), fontsize=10)

    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for slug, title, R in rasters:
        fig, ax = plt.subplots(figsize=(args.size, args.size), dpi=args.dpi)
        draw(ax, R, title)
        if credit:
            ax.text(0.99, 0.01, credit, transform=ax.transAxes, ha="right", va="bottom",
                    fontsize=5, color="#333", zorder=3,
                    bbox=dict(fc="white", ec="none", alpha=0.6, pad=1.2))
        fig.tight_layout(pad=0.2)
        fp = out_dir / f"{slug}.png"
        fig.savefig(fp, bbox_inches="tight", pad_inches=0.02)
        plt.close(fig)
        written.append(fp)

    n = len(rasters)
    ncol = min(n, args.sheet_cols)
    nrow = (n + ncol - 1) // ncol
    fig, axes = plt.subplots(nrow, ncol, figsize=(args.size * ncol, args.size * nrow),
                             dpi=args.dpi, squeeze=False)
    if nrow > 1:
        # Enough to keep a one-line title clear of the panel above without
        # padding the sheet out; pass short --titles and this holds. Wrapped
        # two-line titles need roughly double.
        fig.subplots_adjust(hspace=0.10)
    sm = plt.cm.ScalarMappable(norm=norm, cmap=solid)
    for i, (slug, title, R) in enumerate(rasters):
        draw(axes[i // ncol][i % ncol], R, title)
    for j in range(n, nrow * ncol):
        axes[j // ncol][j % ncol].axis("off")
    # fraction and pad are shares of the figure height, so on a multi-row sheet
    # a fixed value makes the colourbar and its gap grow with the figure and
    # leaves a slab of white space. Divide by nrow to hold both at a constant
    # physical size regardless of how many rows the sheet has.
    cb = fig.colorbar(sm, ax=axes.ravel().tolist(), orientation="horizontal",
                      fraction=0.05 / nrow, pad=0.045 / nrow, aspect=45,
                      shrink=0.92, label="Score",
                      ticks=[vmin, (vmin + vmax) / 2, vmax],
                      format="%.2f")
    if credit:
        # Anchored just below the colourbar's own axis label rather than at a
        # guessed offset: a negative y with bbox_inches="tight" grows the canvas
        # downwards (which left a slab of white under two-row sheets), while a
        # fixed offset lands on top of the tick labels. Measuring the drawn
        # label puts it in the right place for any number of rows.
        fig.canvas.draw()
        bb = cb.ax.xaxis.label.get_window_extent()
        y = fig.transFigure.inverted().transform((0, bb.y0))[1] - 0.012
        fig.text(0.5, y, credit, fontsize=6, color="#333", ha="center", va="top")
    sheet = out_dir / "_contact_sheet.png"
    fig.savefig(sheet, bbox_inches="tight", pad_inches=0.1)
    plt.close(fig)
    written.append(sheet)
    return written, ext_ll, (vmin, vmax)


# ── cli ───────────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    ex = p.add_mutually_exclusive_group(required=True)
    ex.add_argument("--from-hotspots", type=Path)
    ex.add_argument("--bbox", nargs=4, type=float, metavar=("LON0", "LAT0", "LON1", "LAT1"))
    ex.add_argument("--whole-city", action="store_true")
    p.add_argument("--rank", type=int, default=1, help="which hotspot. default 1")
    p.add_argument("--pad", type=int, default=0,
                   help="extra context cells around the window. default 0, so a "
                        "4x4 hotspot renders as exactly 1km x 1km")

    p.add_argument("--presets", nargs="*", help="one panel per preset")
    p.add_argument("--sweep",
                   choices=["tau", "decay", "weight", "grouping", "resolution"])
    p.add_argument("--preset", help="preset held fixed while sweeping")
    p.add_argument("--sweep-group", help="group label for --sweep weight")
    p.add_argument("--tau-values", nargs="*", type=int, default=None,
                   help="explicit thresholds for --sweep tau. "
                        "default: every level present in the dataset")

    p.add_argument("--presets-file", type=Path, default=DEFAULT_PRESETS)
    p.add_argument("--pkl", type=Path, default=DEFAULT_PKL)
    p.add_argument("--out", type=Path, default=None)
    p.add_argument("--name", default=None, help="subfolder name under outputs/figures")
    p.add_argument("--basemap", default="osm",
                   choices=["carto-light", "carto-dark", "osm", "none"],
                   help="street map behind the grid. default osm (matches the web app)")
    p.add_argument("--basemap-px", type=int, default=1400,
                   help="target basemap width in px; drives tile zoom. default 1400")
    p.add_argument("--alpha", type=float, default=0.6,
                   help="grid opacity. default 0.6, the web app's fillOpacity")
    p.add_argument("--alpha-ramp", action="store_true",
                   help="fade low scores out instead of flat opacity; easier to "
                        "read on a busy basemap but does not match the web app")
    p.add_argument("--alpha-min", type=float, default=0.18,
                   help="opacity at the bottom of the scale when --alpha-ramp")
    p.add_argument("--cmap", default="webapp",
                   help="'webapp' reproduces the app's 5-stop ramp; "
                        "or any matplotlib name. default webapp")
    p.add_argument("--nodata-color", default="#f0f0f0")
    p.add_argument("--dpi", type=int, default=200)
    p.add_argument("--size", type=float, default=3.2, help="panel edge in inches")
    p.add_argument("--sheet-cols", type=int, default=3)
    p.add_argument("--autoscale", action="store_true",
                   help="scale colours to the data instead of a fixed 0-1")
    p.add_argument("--no-mask", action="store_true", help="also draw cells outside BBMP")
    p.add_argument("--no-titles", action="store_true")
    p.add_argument("--ward-view", action="store_true",
                   help="collapse every panel to ward means before drawing")
    p.add_argument("--titles", nargs="*", default=None,
                   help="short panel titles, in panel order, replacing the defaults")
    args = p.parse_args()

    grid = Grid(args.pkl)
    grid.load_wards()

    wanted = args.presets or ([args.preset] if args.preset else None)
    presets = load_presets(grid, args.presets_file, wanted)
    if args.sweep is None and not args.presets:
        sys.exit("error: give --presets, or --sweep with --preset")

    if args.whole_city:
        win, label = window_whole_city(grid, args.pad), "whole_city"
    elif args.bbox:
        win, label = window_from_bbox(grid, args.bbox, args.pad), "bbox"
    else:
        doc = json.load(open(args.from_hotspots, encoding="utf-8"))
        hit = [r for r in doc["results"] if r["rank"] == args.rank]
        if not hit:
            sys.exit(f"error: rank {args.rank} not in {args.from_hotspots}")
        h = hit[0]
        win = (max(h["row0"] - args.pad, 0), max(h["col0"] - args.pad, 0),
               min(h["row0"] + h["m"] - 1 + args.pad, grid.n_rows - 1),
               min(h["col0"] + h["m"] - 1 + args.pad, grid.n_cols - 1))
        label = f"{doc['metric']}_m{doc['window']}_rank{args.rank}"
        print(f"extent: hotspot #{args.rank} — {', '.join(h['wards']) or 'unnamed'}")

    panels = build_panels(grid, args, presets)
    if not panels:
        sys.exit("error: nothing to render")

    if args.ward_view:
        panels = [(slug + "_ward", title, ward_flatten(grid, s))
                  for slug, title, s in panels]

    if args.titles and args.sweep == "decay":
        sys.exit("error: --titles is not accepted for --sweep decay; those panels "
                 "label themselves from DECAY_LABEL so they cannot desync")
    if args.titles:
        if len(args.titles) != len(panels):
            sys.exit(f"error: --titles has {len(args.titles)} entries "
                     f"but there are {len(panels)} panels")
        panels = [(slug, new, s) for (slug, _, s), new in zip(panels, args.titles)]

    name = args.name or (f"{label}_{args.sweep}" if args.sweep else f"{label}_presets")
    out_dir = args.out or (OUT_DIR / name)
    written, ext, (vmin, vmax) = render(grid, panels, win, args, out_dir)

    r0, c0, r1, c1 = win
    print(f"window   : rows {r0}-{r1}, cols {c0}-{c1} "
          f"({c1 - c0 + 1}x{r1 - r0 + 1} cells)")
    print(f"extent   : lon {ext[0]:.5f}..{ext[1]:.5f}  lat {ext[2]:.5f}..{ext[3]:.5f}")
    print(f"colour   : {vmin:.3f}..{vmax:.3f} ({'auto' if args.autoscale else 'fixed'})")
    print(f"panels   : {len(panels)}")
    for f in written:
        print(f"  {f.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
