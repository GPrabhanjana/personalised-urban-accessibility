# Data licensing

`LICENSE` (MIT) covers the source code only. The data in this repository is
derived from third-party sources and carries their terms.

## OpenStreetMap derivatives — ODbL

Everything under `outputs/` is derived from OpenStreetMap: the catchment counts
and nearest-amenity distances were computed from an OpenStreetMap pedestrian
network and OpenStreetMap amenity data, extracted July 2025.

These are produced works under the Open Database License (ODbL) v1.0.

- © OpenStreetMap contributors
- <https://www.openstreetmap.org/copyright>
- <https://opendatacommons.org/licenses/odbl/1-0/>

If you redistribute this data or anything derived from it, the attribution and
share-alike terms of the ODbL apply.

## Ward boundaries

`data/wards.geojson` contains BBMP ward boundaries obtained from the Urban Data
Portal and remains subject to that source's terms. It is included because every
analysis script needs it to aggregate cells to wards.

## Basemap tiles

`src/analysis/basemap.py` fetches tiles from the OpenStreetMap tile servers at
render time. No tiles are stored in this repository. Anyone running the figure
scripts is bound by the OpenStreetMap Tile Usage Policy:
<https://operations.osmfoundation.org/policies/tiles/>
