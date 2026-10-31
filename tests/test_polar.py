from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from satview.areas import AREAS
from satview.sources.base import TimeSpec
from satview.sources.polar import (Pass, Record, choose_passes, coverage, geojson_polygons, group_passes,
                                   points_in_polygon, search_window)

REGION = AREAS["israel"]
BBOX = REGION.bbox


def box(lon_min, lat_min, lon_max, lat_max):
    return np.array([(lon_min, lat_min), (lon_max, lat_min), (lon_max, lat_max), (lon_min, lat_max)])


def t(day, hour, minute=0):
    return datetime(2026, 10, day, hour, minute, tzinfo=timezone.utc)


def test_points_in_polygon():
    triangle = np.array([(0, 0), (10, 0), (0, 10)])
    x, y = np.array([1, 6, -1, 4.9]), np.array([1, 6, 1, 4.9])
    assert points_in_polygon(x, y, triangle).tolist() == [True, False, False, True]


def test_coverage():
    assert coverage([box(30, 25, 40, 40)], BBOX) == 1.0
    assert coverage([box(50, 25, 60, 40)], BBOX) == 0.0
    lon_mid = (BBOX[0] + BBOX[2]) / 2
    assert coverage([box(30, 25, lon_mid, 40)], BBOX) == pytest.approx(0.5, abs=0.05)
    # two halves together cover everything
    assert coverage([box(30, 25, lon_mid + 0.01, 40), box(lon_mid - 0.01, 25, 40, 40)], BBOX) == 1.0


def test_group_passes_merges_consecutive_granules_per_platform():
    records = [
        Record("noaa20", t(30, 10, 30), t(30, 10, 36), [box(30, 20, 40, 31)], "a"),
        Record("noaa20", t(30, 10, 36), t(30, 10, 42), [box(30, 31, 40, 40)], "b"),
        Record("noaa21", t(30, 10, 36), t(30, 10, 42), [box(30, 25, 40, 40)], "c"),
        Record("noaa20", t(30, 12, 12), t(30, 12, 18), [box(30, 25, 40, 40)], "d"),
    ]
    passes = group_passes(records, timedelta(minutes=2), REGION)
    assert [(p.platform, p.items) for p in passes] == [("noaa20", ["a", "b"]), ("noaa21", ["c"]),
                                                       ("noaa20", ["d"])]
    assert passes[0].coverage == 1.0


def make_pass(start, area, platform="noaa20"):
    return Pass(platform, start, start + timedelta(minutes=6), REGION, [area], [start])


FULL_NADIR = box(30, 25, 40, 40)  # centred near the area
FULL_EDGE = box(34, 25, 48, 40)  # covers the area, centred far east
HALF = box(30, 25, 35, 40)
PASSES = [
    make_pass(t(29, 10), FULL_EDGE),
    make_pass(t(29, 11), FULL_NADIR, "noaa21"),
    make_pass(t(30, 9), HALF),
    make_pass(t(30, 11), FULL_EDGE),
    make_pass(t(30, 23), FULL_NADIR),  # night
]


def test_latest_best_is_the_best_pass_of_the_newest_day():
    assert [p.start for p in choose_passes(PASSES, TimeSpec(), 0.6, "best")] == [t(30, 11)]


def test_best_prefers_nadir_when_coverage_ties():
    spec = TimeSpec(mode="range", start=t(29, 0), end=t(29, 23))
    assert [p.platform for p in choose_passes(PASSES, spec, 0.6, "best")] == ["noaa21"]


def test_range_all_keeps_every_good_daytime_pass():
    spec = TimeSpec(mode="range", start=t(29, 0), end=t(30, 23, 59))
    assert [p.start for p in choose_passes(PASSES, spec, 0.6, "all")] == [t(29, 10), t(29, 11), t(30, 11)]
    assert len(choose_passes(PASSES, spec, 0.4, "all")) == 4


def test_at_picks_nearest_good_pass():
    assert [p.start for p in choose_passes(PASSES, TimeSpec(mode="at", at=t(30, 8)), 0.6, "best")] == [t(30, 11)]


def test_nothing_good():
    assert choose_passes(PASSES[2:3], TimeSpec(), 0.6, "best") == []


def test_search_window():
    now = t(30, 12)
    assert search_window(TimeSpec(now=now)) == (now - timedelta(hours=48), now)
    assert search_window(TimeSpec(now=now), lookback=timedelta(days=10))[0] == t(20, 12)
    days = timedelta(days=3)
    assert search_window(TimeSpec(mode="at", at=now), tolerance=days) == (now - days, now + days)


def test_geojson_polygons():
    ring = [[34, 30], [35, 30], [35, 31], [34, 30]]
    assert [p.shape for p in geojson_polygons({"type": "Polygon", "coordinates": [ring]})] == [(4, 2)]
    multi = {"type": "MultiPolygon", "coordinates": [[ring], [ring[:3] + [ring[0]]]]}
    assert len(geojson_polygons(multi)) == 2
    assert geojson_polygons(None) == [] and geojson_polygons({"type": "Point", "coordinates": [34, 30]}) == []
