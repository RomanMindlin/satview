"""Sentinel-2 / Landsat tile sources, offline: synthetic GeoTIFFs and canned STAC responses."""

from datetime import datetime, timedelta, timezone

import numpy as np
import pytest
import rasterio
from pyproj import Transformer
from rasterio.enums import Resampling
from rasterio.transform import from_origin

from satview import pipeline
from satview.areas import AREAS, get_area, lonlat_bounds
from satview.config import LandsatOliConfig, Sentinel2MsiConfig
from satview.sources import stac
from satview.sources.base import Granule, TimeSpec
from satview.sources.constants import PLANETARY_COMPUTER_TOKEN_URL
from satview.sources.landsat_oli import LandsatOli
from satview.sources.sentinel2_msi import Sentinel2Msi

SCALE, OFFSET = 1e-4, -0.1  # Sentinel-2 L2A
T = datetime(2026, 10, 26, 8, 30, tzinfo=timezone.utc)


def square(crs, lon, lat, half_m):
    """left, top of a square of half-size half_m metres around a lon/lat, in crs."""
    x, y = Transformer.from_crs("EPSG:4326", crs, always_xy=True).transform(lon, lat)
    return x - half_m, y + half_m


def write_band(path, crs, left, top, res, size, value):
    """A single-band uint16 file with internal overviews, like a cloud-optimized GeoTIFF."""
    with rasterio.open(path, "w", driver="GTiff", width=size, height=size, count=1, dtype="uint16", crs=crs,
                       transform=from_origin(left, top, res, res), nodata=0, tiled=True,
                       blockxsize=256, blockysize=256) as dst:
        dst.write(np.full((1, size, size), value, np.uint16))
        dst.build_overviews([2, 4], Resampling.average)
    return str(path)


def write_tile(path, crs, lon, lat, values, half_m=30_000, res=100):
    """A cached tile as read_tile writes it: three bands, scales and offsets."""
    left, top = square(crs, lon, lat, half_m)
    size = int(2 * half_m / res)
    with rasterio.open(path, "w", driver="GTiff", width=size, height=size, count=3, dtype="uint16", crs=crs,
                       transform=from_origin(left, top, res, res), nodata=0) as dst:
        dst.write(np.array(values, np.uint16)[:, None, None] * np.ones((3, size, size), np.uint16))
        dst.scales = [SCALE] * 3
        dst.offsets = [OFFSET] * 3
    return path


def test_read_tile_crops_to_the_area_from_an_overview(tmp_path):
    area = get_area()
    bounds = lonlat_bounds(area)
    # 60 km at 50 m, sticking out west of the area into the sea
    left, top = square("EPSG:32636", bounds[0], 31.0, 30_000)
    hrefs = [write_band(tmp_path / f"{b}.tif", "EPSG:32636", left, top, 50, 1200, v)
             for b, v in zip(stac.BANDS, (2000, 3000, 4000))]
    data = stac.read_tile(hrefs, [(SCALE, OFFSET)] * 3, bounds, resolution=150)
    (tmp_path / "tile.tif").write_bytes(data)
    with rasterio.open(tmp_path / "tile.tif") as tile:
        assert tile.res == (100, 100)  # overview 2: 100 m, the coarsest one still <= 150 m
        assert tile.count == 3 and tile.nodata == 0
        assert tile.scales == (SCALE,) * 3 and tile.offsets == (OFFSET,) * 3
        assert tile.read().reshape(3, -1)[:, 0].tolist() == [2000, 3000, 4000]
        # only the eastern part, inside the area, was read
        assert 25_000 <= tile.bounds.right - tile.bounds.left <= 35_000
        assert tile.bounds.top - tile.bounds.bottom == pytest.approx(60_000)


def test_mosaic_reprojects_and_first_tile_wins(tmp_path):
    area = get_area()
    west = write_tile(tmp_path / "west.tif", "EPSG:32636", 35.0, 31.0, (2000, 3000, 4000))
    east = write_tile(tmp_path / "east.tif", "EPSG:32637", 35.4, 31.0, (6000, 6000, 6000))  # next UTM zone
    rgb = stac.mosaic([west, east], area)
    assert rgb.shape == (3, area.height, area.width)

    def at(lon, lat):
        cols, rows = area.get_array_indices_from_lonlat(lon, lat)
        return rgb[:, int(rows), int(cols)]

    assert at(34.85, 31.0) == pytest.approx([0.1, 0.2, 0.3])
    assert at(35.2, 31.0) == pytest.approx([0.1, 0.2, 0.3])  # overlap: first tile
    assert at(35.6, 31.0) == pytest.approx([0.5, 0.5, 0.5])
    assert np.isnan(at(34.0, 30.0)).all()


def test_reflectance_image_matches_satpy_true_color():
    rgb = np.array([[[0.05, 0.3, 0.8, np.nan]]] * 3, dtype=np.float32)  # sea, desert, cloud, no data
    image = pipeline.reflectance_image(rgb, get_area(), T, "Sentinel-2A MSI · true color")
    red, alpha = image.data[0, :, 0], image.data[0, :, 3]
    assert alpha.tolist() == [255, 255, 255, 0]
    assert red[0] < red[1] < red[2]
    assert red[2] - red[1] > 40  # cloud stands out from bright desert
    assert image.label == "Sentinel-2A MSI · true color · 2026-10-26 08:30 UTC"


class FakeResponse:
    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self.data


class FakeHttp:
    def __init__(self, *pages):
        self.pages, self.calls = list(pages), []

    def request(self, method, url, json=None, timeout=None):
        self.calls.append((method, url, json))
        return FakeResponse(self.pages.pop(0))

    def get(self, url, timeout=None):
        return self.request("GET", url)


def test_search_follows_next_links():
    http = FakeHttp(
        {"features": [{"id": "a"}], "links": [{"rel": "next", "href": "https://stac/search", "method": "POST",
                                               "body": {"next": "p2"}, "merge": True}]},
        {"features": [{"id": "b"}], "links": [{"rel": "next", "href": "https://stac/search?token=p3"}]},
        {"features": [{"id": "c"}], "links": [{"rel": "self", "href": "https://stac/search"}]},
    )
    bbox = AREAS["israel"].bbox
    items = stac.search(http, "https://stac", "s2", T - timedelta(days=1), T, bbox)
    assert [i["id"] for i in items] == ["a", "b", "c"]
    first, second, third = http.calls
    assert first[2] == {"collections": ["s2"], "bbox": list(bbox), "limit": 100,
                        "datetime": "2026-10-25T08:30:00Z/2026-10-26T08:30:00Z"}
    assert second[2] == {**first[2], "next": "p2"}
    assert third == ("GET", "https://stac/search?token=p3", None)


def item(id, platform, t, lon_min, lon_max, href="https://data/x"):
    box = [[lon_min, 28.0], [lon_max, 28.0], [lon_max, 35.0], [lon_min, 35.0], [lon_min, 28.0]]
    return {"id": id, "properties": {"platform": platform, "datetime": f"{t:%Y-%m-%dT%H:%M:%S.%fZ}"},
            "geometry": {"type": "Polygon", "coordinates": [box]},
            "assets": {band: {"href": f"{href}/{band}.tif",
                              "raster:bands": [{"scale": SCALE, "offset": OFFSET, "nodata": 0}]}
                       for band in stac.BANDS}}


ITEMS = [
    item("west", "sentinel-2a", T, 33.0, 35.1),  # a full pass in two tiles
    item("east", "sentinel-2a", T + timedelta(seconds=8), 35.0, 37.0),
    item("sliver", "sentinel-2c", T + timedelta(days=2), 36.0, 37.0),  # newer but covers too little
    item("other", "sentinel-2b", T + timedelta(days=3), 33.0, 37.0),  # platform not configured
]


@pytest.fixture
def canned_search(monkeypatch):
    calls = []

    def search(http, url, collection, start, end, bbox):
        calls.append((url, collection, start, end, bbox))
        return ITEMS

    monkeypatch.setattr(stac, "search", search)
    return calls


def test_find_groups_tiles_into_passes(canned_search):
    cfg = Sentinel2MsiConfig(platforms=["S2A", "S2C"])
    area = get_area(cfg.resolution_m)
    now = T + timedelta(days=4)
    [granule] = Sentinel2Msi(cfg).find(TimeSpec(now=now), area)
    assert (granule.id, granule.tag, granule.time) == ("S2A_20261026T0830", "S2A", T)
    p, payload_area = granule.payload
    assert [i["id"] for i in p.items] == ["west", "east"] and p.coverage == 1.0
    assert payload_area is area
    [(url, collection, start, end, bbox)] = canned_search
    assert (collection, start, end, bbox) == ("sentinel-2-c1-l2a", now - timedelta(days=10), now,
                                              AREAS["israel"].bbox)


def test_download_reads_each_tile_once_with_signed_urls(canned_search, tmp_path, monkeypatch):
    reads = []

    def read_tile(hrefs, scales, bounds, resolution):
        reads.append((hrefs, scales, resolution))
        return b"tif"

    monkeypatch.setattr(stac, "read_tile", read_tile)
    monkeypatch.setattr(LandsatOli, "token", lambda self: "sig=1")
    source = LandsatOli(LandsatOliConfig())
    p = Sentinel2Msi(Sentinel2MsiConfig(platforms=["S2A"])).find(TimeSpec(now=T), get_area())[0].payload[0]
    granule = Granule("landsat_oli", "L9_20261026T0830", T, tag="L9", payload=(p, get_area(200)))

    paths = source.download(granule, tmp_path)
    assert [path.name for path in paths] == ["west_574.2_3208.2_811.8_3722.4_200m.tif",
                                             "east_574.2_3208.2_811.8_3722.4_200m.tif"]
    assert all(path.read_bytes() == b"tif" for path in paths)
    hrefs, scales, resolution = reads[0]
    assert hrefs == [f"https://data/x/{band}.tif?sig=1" for band in stac.BANDS]
    assert scales == [(SCALE, OFFSET)] * 3 and resolution == 200
    assert source.download(granule, tmp_path) == paths
    assert len(reads) == 2  # the second download came from the cache


def test_each_area_searches_and_caches_its_own_tiles(canned_search, tmp_path, monkeypatch):
    monkeypatch.setattr(stac, "read_tile", lambda hrefs, scales, bounds, resolution: b"tif")
    area = get_area(name="israel_north")
    [granule] = Sentinel2Msi(Sentinel2MsiConfig(platforms=["S2A"])).find(TimeSpec(now=T), area)
    assert canned_search[0][4] == AREAS["israel_north"].bbox
    paths = Sentinel2Msi(Sentinel2MsiConfig()).download(granule, tmp_path)
    assert [path.name for path in paths] == ["west_574_3374_812_3722.5_500m.tif",
                                             "east_574_3374_812_3722.5_500m.tif"]


def test_landsat_token_is_reused_until_it_expires():
    source = LandsatOli(LandsatOliConfig())
    expiry = datetime.now(timezone.utc) + timedelta(hours=1)
    source.http = FakeHttp({"token": "sig=1", "msft:expiry": f"{expiry:%Y-%m-%dT%H:%M:%SZ}"},
                           {"token": "sig=2", "msft:expiry": f"{expiry:%Y-%m-%dT%H:%M:%SZ}"})
    assert source.sign("https://x/b.tif") == "https://x/b.tif?sig=1"
    assert source.token() == "sig=1"
    assert source.http.calls == [("GET", f"{PLANETARY_COMPUTER_TOKEN_URL}/landsat-c2-l2", None)]
    source._token = ("sig=1", datetime.now(timezone.utc))  # about to expire
    assert source.token() == "sig=2"


def test_load_titles_the_picture(tmp_path):
    area = get_area()
    tile = write_tile(tmp_path / "t.tif", "EPSG:32636", 35.0, 31.0, (2000, 3000, 4000))
    granule = Granule("sentinel2_msi", "S2C_x", T, tag="S2C")
    image = Sentinel2Msi(Sentinel2MsiConfig()).load([tile], area, granule)
    assert image.data.shape == (area.height, area.width, 4)
    assert 0 < (image.data[..., 3] > 0).mean() < 0.1
    assert image.title == "Sentinel-2C MSI · true color"


def test_finer_grid_has_the_same_extent():
    coarse, fine = get_area(), get_area(100)
    assert fine.pixel_size_x == 100
    assert fine.area_extent == pytest.approx(coarse.area_extent, abs=500)
    assert fine.width == pytest.approx(coarse.width * 5, abs=5)
