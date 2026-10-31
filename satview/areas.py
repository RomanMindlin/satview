"""The target areas: Israel and surroundings, or the same picture cut south of Mitzpe Ramon.

The areas are hardcoded; config.yaml `area` (or `--area`) picks one by name.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache

import numpy as np
from pyproj import CRS, Transformer
from pyresample.geometry import AreaDefinition


@dataclass(frozen=True)
class Region:
    description: str
    bbox: tuple[float, float, float, float]  # lon_min, lat_min, lon_max, lat_max
    center: tuple[float, float]  # lon, lat used for the daylight check
    crop_of: str | None = None  # area whose grid this one is cut from, pixel for pixel


AREAS = {
    # Eilat to the Golan, plus the coast and some sea.
    "israel": Region("Israel and surroundings", (33.8, 29.0, 36.2, 33.6), (35.0, 31.5)),
    # The israel picture without the south: the edge is ~14 km below Mitzpe Ramon (30.6°N),
    # just enough to keep the town clear of the label.
    "israel_north": Region("Israel north of Mitzpe Ramon", (33.8, 30.5, 36.2, 33.6), (35.0, 32.0),
                           crop_of="israel"),
}
DEFAULT_AREA = "israel"
# UTM zone 36N: true distances, and a standard EPSG code for GeoTIFF readers.
CRS_CODE = "EPSG:32636"
RESOLUTION_M = 500.0


def _box_outline(xmin: float, ymin: float, xmax: float, ymax: float, n: int = 50):
    """Points along the four sides of a box, so curved edges survive reprojection."""
    t = np.linspace(0.0, 1.0, n)
    xs = np.concatenate([xmin + (xmax - xmin) * t, np.full(n, xmax), xmax - (xmax - xmin) * t, np.full(n, xmin)])
    ys = np.concatenate([np.full(n, ymin), ymin + (ymax - ymin) * t, np.full(n, ymax), ymax - (ymax - ymin) * t])
    return xs, ys


@cache
def get_area(resolution_m: float = RESOLUTION_M, name: str = DEFAULT_AREA) -> AreaDefinition:
    """The grid every source is resampled to: the named area's bbox in UTM 36N at RESOLUTION_M,
    or finer for the high-resolution sources (same extent, so pictures still line up).

    An area cut from another keeps that one's columns and only drops rows, so its picture is
    an exact crop: a box with the same longitudes alone would come out narrower, since UTM
    meridians converge northwards."""
    region = AREAS[name]
    crs = CRS.from_user_input(CRS_CODE)
    to_proj = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    x, y = to_proj.transform(*_box_outline(*region.bbox))
    res = resolution_m
    xmin, ymin = np.floor(x.min() / res) * res, np.floor(y.min() / res) * res
    xmax, ymax = np.ceil(x.max() / res) * res, np.ceil(y.max() / res) * res
    if region.crop_of:
        pxmin, pymin, pxmax, pymax = get_area(res, region.crop_of).area_extent
        xmin, ymin, xmax, ymax = pxmin, max(ymin, pymin), pxmax, min(ymax, pymax)
    width = int(round((xmax - xmin) / res))
    height = int(round((ymax - ymin) / res))
    return AreaDefinition(name, region.description, name, crs, width, height, (xmin, ymin, xmax, ymax))


def region_of(area: AreaDefinition) -> Region:
    """The named area a grid was made for."""
    return AREAS[area.area_id]


def lonlat_bounds(area: AreaDefinition) -> tuple[float, float, float, float]:
    """lon_min, lat_min, lon_max, lat_max of the whole grid (a bit larger than its bbox)."""
    to_ll = Transformer.from_crs(area.crs, "EPSG:4326", always_xy=True)
    lons, lats = to_ll.transform(*_box_outline(*area.area_extent))
    return float(lons.min()), float(lats.min()), float(lons.max()), float(lats.max())
