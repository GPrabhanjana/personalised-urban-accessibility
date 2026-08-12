# Personalised Urban Accessibility

Code and derived data for an interactive framework for personalised urban
accessibility assessment, applied to all 198 wards of Bruhat Bengaluru
Mahanagara Palike.

Public walkability tools are easy to use but return one score to every resident
of a location. Accessibility measures in the research literature can express
what a particular resident needs, but are run as offline GIS workflows. This
framework does both: a user states what they want nearby, what they need, what
they would rather avoid and how far they will walk for each, and the score
updates immediately.

This repository supports a manuscript submitted to *Discover Cities*. It
contains the scoring engine, the profile construction, the comparisons reported
in the paper, and the precomputed data those need. The manuscript and the web
application are not included.

## The measure

Each selected group of amenity categories takes four settings:

| Control | Options | Effect |
|---|---|---|
| How it's treated | Wanted · Required · Avoid · Never | Adds to the score, must be reachable, subtracts, or disqualifies |
| How much it matters | Standard · Emphasised | Relative weight, or size of the penalty |
| How many nearby | A few · A handful · A lot | What a second and third instance add once you have one |
| How far you'll walk | 3–15 min | Per-category walking-time threshold |

The score is a weighted sum of saturating opportunity counts, minus a saturating
penalty for anything avoided, normalised by the wanted weights and clamped at
zero. `Required` and `Never` are hard constraints and are mirror images of one
another. Avoided groups stay out of the denominator, so naming something to
avoid can lower a score but never raise it.

Catchments come from bounded shortest-path search over an OpenStreetMap
pedestrian network, precomputed as a count per category per threshold. That is
what leaves only arithmetic at query time.

`src/analysis/scoring.py` is the single source of truth for weights, saturation
rates and treatments.

## Reproducing the reported results

```sh
pip install -r requirements.txt
cd src/analysis

# profile specification (Table 5) — every threshold and rate derived by rule
python build_profiles.py --report

# non-personalised baselines (Table 6)
python benchmark_methods.py

# competing aggregations given identical preferences (Table 8)
python compare_mcda.py --presets presets_v3.json

# Walk Score-style reconstruction (Table 7); --mapping A|B|C|D for the sensitivity
python compare_walkscore.py --mapping C

# WalkTHERE comparison and the personalisation effect
python compare_walkthere.py --presets presets_v3.json

# figures
python render_maps.py --whole-city --presets-file presets_demo.json \
    --preset demo --sweep resolution --sheet-cols 2 --autoscale
```

All of these run from the data in `outputs/`; nothing needs re-routing.

## Layout

| Path | Contents |
|---|---|
| `src/analysis/` | Scoring engine, profile construction, comparisons, figure rendering |
| `src/01_isochrones/`, `src/02_grid/`, `src/03_maintain/` | Upstream pipeline: pedestrian graph, routed grid, category curation |
| `outputs/` | Precomputed catchment counts and nearest-amenity distances |
| `data/` | BBMP ward boundaries |

### Configuration

- `category_attributes.json` — trip-purpose distances, substitutability and
  disamenity buffers, each with its source. Thresholds and saturation rates are
  derived from this rather than set per profile.
- `evidence_profiles_v3.json` — the four profiles, each naming only its groups
  and how they are treated.
- `presets_v3.json` — generated from the two above by `build_profiles.py`.
- `presets_demo.json` — an illustrative configuration used only for the
  component-effect figures. Not evidence-derived, and deliberately separate.

## What is not here

The upstream pipeline is included so the method is inspectable, but it cannot be
re-run from this repository: the routing graph and the raw OpenStreetMap
extracts are large and are not carried. `outputs/` holds their product, which is
what every analysis script reads.

## Data

Amenities from OpenStreetMap as of July 2025: 20,790 features across 50
categories, each assigned to exactly one category. Ward boundaries from the
Urban Data Portal. Walking speed 80 m·min⁻¹.

Coverage is uneven, and the manuscript reports this as its principal constraint.
Open data is markedly more complete for formal and institutional amenities than
for the informal retail lower-income households depend on, and only parks and
schools are mapped completely enough to carry a `Required` designation.

## Licence

Code is MIT (`LICENSE`). The data is derived from OpenStreetMap and is subject to
the ODbL, and the ward boundaries to their own source's terms; see
[`DATA-LICENSE.md`](DATA-LICENSE.md).
