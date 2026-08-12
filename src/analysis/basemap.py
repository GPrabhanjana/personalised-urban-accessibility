#!/usr/bin/env python3
"""
Web-Mercator basemap tiles for figure rendering.

Fetches and stitches slippy-map tiles so accessibility grids can be drawn over
a recognisable street map. Tiles are cached on disk, so a given extent is
downloaded once and every later re-render is offline and instant.

Note on projection: tiles are Web Mercator (EPSG:3857) while the grid is stored
in WGS84 lon/lat. Everything is converted to Mercator before plotting, so the
overlay lines up exactly rather than being stretched to fit.

Attribution is required when publishing: basemap figures must credit
"(C) OpenStreetMap contributors" (and CARTO, for the carto styles).
"""

import math
import time
from io import BytesIO
from pathlib import Path

R = 6378137.0  # Web Mercator sphere radius

PROVIDERS = {
    "osm": ("https://tile.openstreetmap.org/{z}/{x}/{y}.png",
            "(C) OpenStreetMap contributors"),
    "carto-light": ("https://a.basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png",
                    "(C) OpenStreetMap contributors (C) CARTO"),
    "carto-dark": ("https://a.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png",
                   "(C) OpenStreetMap contributors (C) CARTO"),
}

USER_AGENT = "OSFN-research-figure-render/1.0 (academic use; contact repo owner)"
CACHE = Path(__file__).resolve().parents[2] / "outputs" / ".tilecache"


# ── projection ────────────────────────────────────────────────────────────────

def merc(lon, lat):
    lat = max(min(lat, 85.05112878), -85.05112878)
    return R * math.radians(lon), R * math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))


def merc_x(lon):
    return R * math.radians(lon)


def merc_y(lat):
    lat = max(min(lat, 85.05112878), -85.05112878)
    return R * math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))


def _tile_xy(lon, lat, z):
    n = 2 ** z
    x = int((lon + 180.0) / 360.0 * n)
    lat_r = math.radians(max(min(lat, 85.05112878), -85.05112878))
    y = int((1.0 - math.asinh(math.tan(lat_r)) / math.pi) / 2.0 * n)
    return min(max(x, 0), n - 1), min(max(y, 0), n - 1)


def _tile_lon(x, z):
    return x / 2 ** z * 360.0 - 180.0


def _tile_lat(y, z):
    return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / 2 ** z))))


def pick_zoom(bbox, target_px=1400, max_zoom=19):
    """Smallest zoom whose rendering of bbox is at least target_px wide."""
    lon0, _, lon1, _ = bbox
    frac = abs(lon1 - lon0) / 360.0
    for z in range(1, max_zoom + 1):
        if frac * 256 * 2 ** z >= target_px:
            return z
    return max_zoom


# ── fetching ──────────────────────────────────────────────────────────────────

def _get_tile(provider, z, x, y, session, delay=0.1):
    cache_fp = CACHE / provider / str(z) / str(x) / f"{y}.png"
    if cache_fp.exists():
        return cache_fp.read_bytes()
    url = PROVIDERS[provider][0].format(z=z, x=x, y=y)
    resp = session.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
    resp.raise_for_status()
    cache_fp.parent.mkdir(parents=True, exist_ok=True)
    cache_fp.write_bytes(resp.content)
    time.sleep(delay)  # be polite to the tile server on cache misses
    return resp.content


def fetch(bbox, provider="carto-light", target_px=1400, max_tiles=192):
    """
    bbox = (lon0, lat0, lon1, lat1)
    -> (PIL.Image, (left, right, bottom, top) in Web Mercator metres)

    Returns (None, None) if tiles cannot be fetched, so callers can degrade
    gracefully to a plain background rather than failing the whole render.
    """
    try:
        import requests
        from PIL import Image
    except ImportError as e:
        print(f"  basemap unavailable ({e}); rendering without one")
        return None, None

    if provider not in PROVIDERS:
        raise ValueError(f"unknown provider {provider!r}; have {list(PROVIDERS)}")

    lon0, lat0, lon1, lat1 = bbox
    z = pick_zoom(bbox, target_px)
    while z > 1:
        x0, y0 = _tile_xy(lon0, lat1, z)   # top-left
        x1, y1 = _tile_xy(lon1, lat0, z)   # bottom-right
        if (x1 - x0 + 1) * (y1 - y0 + 1) <= max_tiles:
            break
        z -= 1

    n_tiles = (x1 - x0 + 1) * (y1 - y0 + 1)
    img = Image.new("RGB", ((x1 - x0 + 1) * 256, (y1 - y0 + 1) * 256), "#ffffff")
    session = requests.Session()
    misses = 0
    try:
        for tx in range(x0, x1 + 1):
            for ty in range(y0, y1 + 1):
                fp = CACHE / provider / str(z) / str(tx) / f"{ty}.png"
                if not fp.exists():
                    misses += 1
                data = _get_tile(provider, z, tx, ty, session)
                img.paste(Image.open(BytesIO(data)).convert("RGB"),
                          ((tx - x0) * 256, (ty - y0) * 256))
    except Exception as e:
        print(f"  basemap fetch failed ({type(e).__name__}: {e}); rendering without one")
        return None, None

    extent = (merc_x(_tile_lon(x0, z)), merc_x(_tile_lon(x1 + 1, z)),
              merc_y(_tile_lat(y1 + 1, z)), merc_y(_tile_lat(y0, z)))
    print(f"  basemap: {provider} z{z}, {n_tiles} tiles ({misses} fetched, "
          f"{n_tiles - misses} cached)")
    return img, extent


def attribution(provider):
    return PROVIDERS.get(provider, (None, ""))[1]
