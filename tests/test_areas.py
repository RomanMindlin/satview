from datetime import datetime, timezone

import numpy as np
import pytest
from PIL import Image as PILImage

from satview.areas import AREAS, get_area, lonlat_bounds, region_of
from satview.config import ViirsGibsConfig
from satview.sources.base import TimeSpec
from satview.sources.viirs_gibs import ViirsGibs

MITZPE_RAMON = (34.80, 30.61)


@pytest.mark.parametrize("name", AREAS)
def test_grid_covers_the_area(name):
    area = get_area(name=name)
    lon_min, lat_min, lon_max, lat_max = lonlat_bounds(area)
    bbox = AREAS[name].bbox
    assert lon_min <= bbox[0] and lat_min <= bbox[1] and lon_max >= bbox[2] and lat_max >= bbox[3]
    assert region_of(area) is AREAS[name] and region_of(get_area(100, name)) is AREAS[name]


@pytest.mark.parametrize("resolution", [500, 100])
def test_israel_north_is_the_israel_grid_cut_south_of_mitzpe_ramon(resolution):
    north, israel = get_area(resolution, "israel_north"), get_area(resolution)
    # same columns and top edge, fewer rows: the picture is the top of the israel one
    assert north.width == israel.width and north.height < israel.height
    (xmin, ymin, xmax, ymax), (pxmin, pymin, pxmax, pymax) = north.area_extent, israel.area_extent
    assert (xmin, xmax, ymax) == (pxmin, pxmax, pymax) and ymin > pymin
    assert np.allclose(north.get_lonlats()[0], israel.get_lonlats()[0][:north.height])
    _, row = north.get_array_indices_from_lonlat(*MITZPE_RAMON)
    assert 10_000 < (north.height - row) * resolution < 20_000  # 10-20 km above the bottom edge


def test_gibs_crops_are_cached_per_extent(tmp_path, monkeypatch):
    def get_map(self, layer, day, bounds, width):
        PILImage.new("RGB", (8, 8), (90, 90, 90)).save(tmp_path / "map.png")
        return (tmp_path / "map.png").read_bytes()

    monkeypatch.setattr(ViirsGibs, "_get_map", get_map)
    when = TimeSpec(mode="at", at=datetime(2026, 10, 29, 10, tzinfo=timezone.utc))
    source = ViirsGibs(ViirsGibsConfig(layers=["VIIRS_NOAA20_CorrectedReflectance_TrueColor"]))
    [israel] = source.find(when, get_area())
    [north] = source.find(when, get_area(name="israel_north"))
    assert israel.id == north.id  # one product, two crops of it
    [israel_file], [north_file] = source.download(israel, tmp_path), source.download(north, tmp_path)
    assert israel_file.parent == north_file.parent and israel_file != north_file
