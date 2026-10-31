"""Shared logic for polar orbiters: footprint coverage, grouping granules into passes, picking passes.

A pass over Israel can straddle two consecutive granules (VIIRS: 6 min, OLCI: 3 min),
so granules are grouped per satellite and judged together.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import cached_property
from typing import Any, Literal

import numpy as np

from satview.areas import Region
from satview.pipeline import is_daylight
from satview.sources.base import TimeSpec

LATEST_LOOKBACK = timedelta(hours=48)  # enough to always include yesterday's midday passes
AT_TOLERANCE = timedelta(hours=12)


def search_window(when: TimeSpec, lookback: timedelta = LATEST_LOOKBACK,
                  tolerance: timedelta = AT_TOLERANCE) -> tuple[datetime, datetime]:
    if when.mode == "latest":
        return when.now - lookback, when.now
    if when.mode == "at":
        return when.at - tolerance, when.at + tolerance
    return when.start, when.end


def geojson_polygons(footprint: dict | None) -> list[np.ndarray]:
    """Outer rings of a GeoJSON Polygon or MultiPolygon as (n, 2) lon/lat arrays."""
    if not footprint:
        return []
    if footprint["type"] == "Polygon":
        return [np.asarray(footprint["coordinates"][0], dtype=float)]
    if footprint["type"] == "MultiPolygon":
        return [np.asarray(polygon[0], dtype=float) for polygon in footprint["coordinates"]]
    return []


def points_in_polygon(x: np.ndarray, y: np.ndarray, polygon: np.ndarray) -> np.ndarray:
    """Even-odd rule for (n, 2) lon/lat polygon vertices; fine away from the antimeridian."""
    px, py = polygon[:, 0], polygon[:, 1]
    inside = np.zeros(x.shape, dtype=bool)
    j = len(polygon) - 1
    with np.errstate(divide="ignore", invalid="ignore"):
        for i in range(len(polygon)):
            crosses = (py[i] > y) != (py[j] > y)
            x_cross = (px[j] - px[i]) * (y - py[i]) / (py[j] - py[i]) + px[i]
            inside ^= crosses & (x < x_cross)
            j = i
    return inside


def coverage(polygons: list[np.ndarray], bounds: tuple[float, float, float, float], n: int = 24) -> float:
    """Share of an n x n grid of points in the lon/lat box that falls inside any polygon."""
    lon_min, lat_min, lon_max, lat_max = bounds
    x, y = np.meshgrid(np.linspace(lon_min, lon_max, n), np.linspace(lat_min, lat_max, n))
    inside = np.zeros(x.shape, dtype=bool)
    for polygon in polygons:
        inside |= points_in_polygon(x, y, polygon)
    return float(inside.mean())


@dataclass
class Record:
    """One granule/frame as found by a search."""

    platform: str
    start: datetime
    end: datetime
    polygons: list[np.ndarray]
    item: Any


@dataclass
class Pass:
    platform: str
    start: datetime
    end: datetime
    region: Region  # the area coverage and nadir distance are judged against
    polygons: list[np.ndarray] = field(default_factory=list)
    items: list[Any] = field(default_factory=list)

    @cached_property
    def coverage(self) -> float:
        return coverage(self.polygons, self.region.bbox)

    @cached_property
    def nadir_offset(self) -> float:
        """Rough distance (degrees) from the area centre to the footprint centre: smaller is
        closer to nadir, with finer pixels and less distortion."""
        centre = np.mean([p.mean(axis=0) for p in self.polygons], axis=0)
        lon, lat = self.region.center
        dx = (centre[0] - lon) * np.cos(np.radians(lat))
        return float(np.hypot(dx, centre[1] - lat))


def group_passes(records: list[Record], max_gap: timedelta, region: Region) -> list[Pass]:
    """Merge consecutive granules of the same satellite into passes over the region."""
    passes: list[Pass] = []
    for r in sorted(records, key=lambda r: (r.platform, r.start)):
        last = passes[-1] if passes else None
        if last and last.platform == r.platform and r.start - last.end <= max_gap:
            last.end = max(last.end, r.end)
        else:
            last = Pass(r.platform, r.start, r.end, region)
            passes.append(last)
        last.polygons.extend(r.polygons)
        last.items.append(r.item)
    return sorted(passes, key=lambda p: p.start)


def choose_passes(passes: list[Pass], when: TimeSpec, min_coverage: float,
                  pick: Literal["best", "all"]) -> list[Pass]:
    """Daytime passes covering enough of the area: for `latest` the most recent day, for a
    range every day in it; per day the best one (coverage, then closeness to nadir) or all."""
    good = [p for p in passes if p.coverage >= min_coverage and is_daylight(p.start, p.region.center)]
    if when.mode == "range":
        good = [p for p in good if when.start <= p.start <= when.end]
    if not good:
        return []
    if when.mode == "at":
        return [min(good, key=lambda p: abs(p.start - when.at))]

    by_day: dict = defaultdict(list)
    for p in good:
        by_day[p.start.date()].append(p)
    days = [max(by_day)] if when.mode == "latest" else sorted(by_day)
    chosen = []
    for day in days:
        if pick == "best":
            chosen.append(max(by_day[day], key=lambda p: (round(p.coverage, 1), -p.nadir_offset)))
        else:
            chosen.extend(sorted(by_day[day], key=lambda p: p.start))
    return chosen
