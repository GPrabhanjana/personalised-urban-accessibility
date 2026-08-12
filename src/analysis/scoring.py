#!/usr/bin/env python3
"""
Accessibility scoring engine — a Python reimplementation of the formula the
web application evaluates in the browser.

Implements, for grid cell g:

    s_g = SUM_i [ w_i * (1 - exp(-lambda_i * k_i)) ] / SUM_i w_i

where i indexes a *group* (one or more amenity categories treated as
substitutable), k_i is the number of overlapping catchments summed across the
group's categories at that group's travel-time threshold, and any group marked
`required` with k_i == 0 forces s_g = 0.

Reads the preprocessed pickle directly, so no routing or geometry work is
needed. Standard library only.
"""

import json
import math
import pickle
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
# The routed dataset is canonical: 50 categories, thresholds 3-15 min. The older
# preprocessed_grid_scores_with_wards.pkl is the superseded interpolated build
# (44 categories, levels 5-15 only) and cannot score the v2 presets at all,
# since those use tau=12. Kept in outputs/ for the published-vs-routed
# comparison in REVISION_NOTES section 6; do not make it the default again.
DEFAULT_PKL = REPO_ROOT / "outputs" / "preprocessed_grid_scores_routed.pkl"
DEFAULT_GRID_GEOJSON = REPO_ROOT / "outputs" / "grids" / "grid2.geojson"
DEFAULT_PRESETS = Path(__file__).resolve().parent / "presets_v2.json"

LEVELS = [5, 7, 9, 11, 13, 15]

# Half-life parameterisation. The keys are the internal names used by every
# stored preset and analysis output; the labels are what the interface and the
# manuscript show, phrased as "how many nearby options do you want".
DECAY = {
    "expansive": math.log(2) / 2,   # half-life k=2
    "balanced": math.log(2),        # half-life k=1
    "focused": 2 * math.log(2),     # half-life k=0.5
}
DECAY_LABEL = {
    "focused": "A few",             # one or two suffices
    "balanced": "A handful",        # each further option is worth about half
    "expansive": "A lot",           # variety keeps adding value
}
# Presentation order, fewest wanted to most. DECAY itself is keyed for lookup and
# its insertion order is not meaningful; anything that renders or lists the
# settings must use this, so labels cannot drift out of step with values.
DECAY_ORDER = ["focused", "balanced", "expansive"]
# Accept the display wording in preset files as well as the internal names.
DECAY_ALIAS = {"few": "focused", "handful": "balanced", "lot": "expansive"}

# Avoidance carries no "how many" setting - the question does not arise when
# you want none of something - so its penalty always accumulates at the middle
# rate, matching the interface.
AVOID_LAMBDA = DECAY["balanced"]

# Weighting tiers.
#
# Positive tiers contribute w * (1 - e^-lam k) to the numerator and w to the
# denominator, so more nearby is better.
#
# Negative tiers only ever subtract. "avoided" deducts w * (1 - e^-lam k) from
# the numerator and contributes nothing to the denominator, so it has no effect
# where k = 0 and grows to a full -w penalty as unwanted instances accumulate.
# Adding an avoided category can therefore never raise a location's score.
# "excluded" is the hard form: any instance zeroes the cell outright.
# Presets name a tier directly; the interface splits the same information into
# a treatment (want / required / avoid / never) and a weight (standard /
# preferred). "preferred_avoided" is the doubled-penalty pairing.
WEIGHT = {
    "standard": 1.0,
    "preferred": 2.0,
    "required": 1.0,            # hard: k == 0 -> score 0
    "avoided": 1.0,             # soft penalty
    "preferred_avoided": 2.0,   # soft penalty, doubled
    "excluded": 1.0,            # hard: k > 0 -> score 0, contributes nothing
}
NEGATIVE = {"avoided", "preferred_avoided", "excluded"}


# ── data loading ──────────────────────────────────────────────────────────────

class Grid:
    """Grid geometry + per-category catchment counts."""

    def __init__(self, pkl_path=DEFAULT_PKL):
        with open(pkl_path, "rb") as f:
            d = pickle.load(f)
        self.cells = d["grid_cells"]                    # cell_id -> bounds/center/row/col
        self.scores = d["grid_scores"]                  # category -> cell_id -> [k5..k15]
        self.levels = d.get("levels", LEVELS)
        # grid_index is empty in some pickles; rebuild it from the cells themselves.
        self.index = {(c["row"], c["col"]): cid for cid, c in self.cells.items()}
        self.cell_size_m = d.get("cell_size_m")
        self.categories = set(self.scores)
        self.n_rows = max(c["row"] for c in self.cells.values()) + 1
        self.n_cols = max(c["col"] for c in self.cells.values()) + 1
        self.ward_of = {}

    def level_index(self, tau):
        if tau not in self.levels:
            sys.exit(f"error: tau={tau} not in available levels {self.levels}")
        return self.levels.index(tau)

    def load_wards(self, geojson_path=DEFAULT_GRID_GEOJSON):
        """
        cell_id -> ward name, by majority area overlap with the BBMP polygons.

        Uses ward_assign rather than the ward_name baked into grid2.geojson so
        that figures and statistics share one definition. The published tagging
        also has a defect: five contiguous cells around 12.9117 N, 77.5505 E are
        untagged despite sitting inside Vasanthpura ward, which renders as a hole
        in any map drawn from it. Falls back to the GeoJSON if the ward polygons
        or geopandas are unavailable.
        """
        try:
            from ward_assign import assign
            self.ward_of = assign(self.cells)
            if self.ward_of:
                return self.ward_of
        except Exception as e:
            print(f"  ward_assign unavailable ({type(e).__name__}); "
                  f"falling back to {Path(geojson_path).name}")
        if not Path(geojson_path).exists():
            return self.ward_of
        with open(geojson_path, encoding="utf-8") as f:
            data = json.load(f)
        for feat in data["features"]:
            p = feat["properties"]
            w = p.get("ward_name")
            if w and w != "Unassigned":
                self.ward_of[int(p["cell_id"])] = w
        return self.ward_of


# ── presets ───────────────────────────────────────────────────────────────────

def _resolve(name, grid, alias):
    """Accept a technical key, a display name, or a declared alias."""
    if name in grid.categories:
        return name
    if name in alias and alias[name] in grid.categories:
        return alias[name]
    close = [c for c in grid.categories if name.lower() in c.lower()]
    sys.exit(f"error: unknown category {name!r}"
             + (f"; did you mean {close[:4]}?" if close else ""))


def load_presets(grid, path=DEFAULT_PRESETS, keys=None):
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    alias = raw.get("_aliases", {})
    presets = {}
    for key, spec in raw.items():
        if key.startswith("_"):
            continue
        if keys and key not in keys:
            continue
        groups = []
        for g in spec["groups"]:
            weight = g.get("weight", "standard").lower()
            decay = g.get("decay", "balanced").lower()
            decay = DECAY_ALIAS.get(decay, decay)
            if weight not in WEIGHT:
                sys.exit(f"error: {key}: bad weight {weight!r}, expected {list(WEIGHT)}")
            if decay not in DECAY:
                sys.exit(f"error: {key}: bad decay {decay!r}, "
                         f"expected {list(DECAY)} or {list(DECAY_ALIAS)}")
            groups.append({
                "label": g.get("label") or ", ".join(g["categories"]),
                "categories": [_resolve(c, grid, alias) for c in g["categories"]],
                "w": WEIGHT[weight],
                "lam": AVOID_LAMBDA if weight in NEGATIVE else DECAY[decay],
                "weight_name": weight,
                "required": weight == "required",
                "excluded": weight == "excluded",
                "negative": weight in NEGATIVE,
                "ti": grid.level_index(int(g.get("tau", 15))),
            })
        presets[key] = {"label": spec.get("label", key), "groups": groups}
    if keys:
        missing = set(keys) - set(presets)
        if missing:
            sys.exit(f"error: preset(s) not found: {', '.join(sorted(missing))}")
    return presets


# ── scoring ───────────────────────────────────────────────────────────────────

def score_preset(grid, preset):
    """cell_id -> score in [0,1]. Every cell in the grid is scored."""
    groups = preset["groups"]
    # Only positive groups set the scale. Negative groups subtract from the
    # numerator without enlarging the denominator, so they cannot raise a score.
    denom = sum(g["w"] for g in groups if not g["negative"])
    if denom == 0:
        # Avoidance is purely subtractive: it removes score that wanted
        # categories put there and never creates any. With nothing wanted there
        # is nothing to subtract from, and calling an empty area "good" because
        # it lacks the avoided thing would reward emptiness.
        return {cid: 0.0 for cid in grid.cells}

    # pull the per-group count lookup once, so the inner loop is dict gets only
    lookups = [[grid.scores.get(c, {}) for c in g["categories"]] for g in groups]

    out = {}
    for cid in grid.cells:
        total = 0.0
        blocked = False
        for g, tables in zip(groups, lookups):
            ti = g["ti"]
            k = 0
            for t in tables:
                v = t.get(cid)
                if v is not None:
                    k += v[ti]
            if g["required"] and k == 0:
                blocked = True
                break
            if g["excluded"]:
                if k > 0:
                    blocked = True
                    break
                continue          # hard filter only, no contribution
            if not k:
                continue          # saturating term is 0 either way
            term = g["w"] * (1.0 - math.exp(-g["lam"] * k))
            total -= term if g["negative"] else -term
        out[cid] = 0.0 if blocked else max(0.0, total / denom)
    return out


def score_all(grid, presets):
    return {key: score_preset(grid, p) for key, p in presets.items()}


# ── cli: quick inspection ─────────────────────────────────────────────────────

def main():
    import argparse
    p = argparse.ArgumentParser(description="Score the grid under each preset.")
    p.add_argument("--presets", type=Path, default=DEFAULT_PRESETS)
    p.add_argument("--only", nargs="*", help="restrict to these preset keys")
    p.add_argument("--pkl", type=Path, default=DEFAULT_PKL)
    args = p.parse_args()

    grid = Grid(args.pkl)
    grid.load_wards()
    presets = load_presets(grid, args.presets, args.only)
    print(f"grid {grid.n_rows}x{grid.n_cols} = {len(grid.cells)} cells, "
          f"{len(grid.categories)} categories, {len(set(grid.ward_of.values()))} wards\n")

    for key, preset in presets.items():
        s = score_preset(grid, preset)
        inside = [v for cid, v in s.items() if cid in grid.ward_of]
        nz = [v for v in inside if v > 0]
        inside.sort()
        print(f"{preset['label']} ({key})")
        print(f"  groups   : {len(preset['groups'])}")
        print(f"  in-BBMP  : n={len(inside)}  mean={sum(inside)/len(inside):.4f}  "
              f"median={inside[len(inside)//2]:.4f}  max={inside[-1]:.4f}")
        print(f"  non-zero : {len(nz)} ({len(nz)/len(inside)*100:.1f}%)\n")


if __name__ == "__main__":
    main()
