"""Offline pipeline tests on a recorded GIBS sample, plus a rendering regression test.

Regenerate the reference picture after an intended visual change with
    SATVIEW_UPDATE_REFERENCE=1 pytest tests/test_pipeline.py
"""

import os
from datetime import datetime, timezone

import numpy as np
import pytest
from PIL import Image as PILImage

from satview import config, pipeline, render
from satview.areas import AREAS, get_area
from satview.config import Enhance, ViirsGibsConfig
from satview.sources.base import Granule, Image
from satview.sources.viirs_gibs import ViirsGibs, request_bounds

from conftest import DATA

SAMPLE = DATA / "gibs_noaa20_20261029.png"
REFERENCE = DATA / "regression_gibs.png"
LAYER = "VIIRS_NOAA20_CorrectedReflectance_TrueColor"
DAY = datetime(2026, 10, 29, tzinfo=timezone.utc)
NOON = datetime(2026, 10, 29, 10, 0, tzinfo=timezone.utc)
MIDNIGHT = datetime(2026, 10, 29, 22, 0, tzinfo=timezone.utc)


def gibs_granule(area):
    return Granule("viirs_gibs", f"{LAYER}_2026-10-29", DAY, tag="noaa20", daily=True,
                   payload=(LAYER, request_bounds(area)))


@pytest.fixture(scope="module")
def gibs_image():
    area = get_area()
    return ViirsGibs(ViirsGibsConfig()).load([SAMPLE], area, gibs_granule(area))


# A few hand-made lines and a city stand in for Natural Earth, so the test runs offline.
SYNTHETIC_OVERLAYS = render.OverlayData(
    coastlines=[np.array([(34.2, 31.2), (34.5, 31.6), (34.75, 32.1), (34.95, 32.8), (35.1, 33.1)])],
    borders=[np.array([(34.9, 29.5), (34.25, 31.3)]), np.array([(35.0, 29.5), (35.55, 32.7)])],
    cities=[("Jerusalem", 35.21, 31.77), ("Haifa", 34.99, 32.79)],
)


def test_gibs_sample_loads_onto_the_area(gibs_image):
    area = get_area()
    assert gibs_image.data.shape == (area.height, area.width, 4)
    assert (gibs_image.data[..., 3] > 0).mean() > 0.99
    assert gibs_image.label == "NOAA-20 VIIRS · true color (GIBS) · 2026-10-29 (daily)"


def test_render_size_and_regression(gibs_image, tmp_path):
    out = config.OutputConfig(overlays=config.Overlays(cities=True))
    picture = render.render(gibs_image, out, SYNTHETIC_OVERLAYS)
    assert picture.width == 1200
    assert picture.height == round(1200 * gibs_image.data.shape[0] / gibs_image.data.shape[1])

    if os.environ.get("SATVIEW_UPDATE_REFERENCE") or not REFERENCE.exists():
        picture.save(REFERENCE)
        pytest.skip(f"wrote new reference {REFERENCE}")
    reference = np.asarray(PILImage.open(REFERENCE).convert("RGB"), dtype=np.int16)
    actual = np.asarray(picture, dtype=np.int16)
    assert actual.shape == reference.shape
    diff = np.abs(actual - reference)
    # tolerate small differences from library versions (resampling, font hinting)
    assert diff.mean() < 1.0
    assert (diff.max(axis=-1) > 40).mean() < 0.005


def test_render_without_overlays_or_label_keeps_native_size(gibs_image):
    out = config.OutputConfig(width_px=None, label=False)
    picture = render.render(gibs_image, out, None)
    assert picture.size == (gibs_image.data.shape[1], gibs_image.data.shape[0])
    expected = gibs_image.data[..., :3].astype(int) * (gibs_image.data[..., 3:] > 0)
    assert np.abs(np.asarray(picture, dtype=int) - expected).max() <= 1


def test_satpy_load_and_resample(tmp_path):
    """Exercise Scene load -> resample -> enhance -> finalize with Satpy's generic image reader."""
    import rasterio
    from rasterio.transform import from_bounds

    area = get_area()
    rgba = np.asarray(PILImage.open(SAMPLE).convert("RGBA"))
    bounds = request_bounds(area)
    tif = tmp_path / "20261029_1000_gibs.tif"
    rows, cols = rgba.shape[:2]
    with rasterio.open(tif, "w", driver="GTiff", width=cols, height=rows, count=3, dtype="uint8",
                       crs="EPSG:4326", transform=from_bounds(*bounds, cols, rows)) as dst:
        dst.write(np.moveaxis(rgba[..., :3], -1, 0))

    image = pipeline.satpy_image("generic_image", [tif], "image", area, "TEST", platform="Sample")
    assert image.data.shape == (area.height, area.width, 4)
    assert image.time == NOON
    assert image.title == "Sample TEST · image"
    assert (image.data[..., 3] > 0).mean() > 0.99
    # same picture as our own lon/lat resampler, up to Satpy's default contrast stretch
    ours = pipeline.resample_latlon(rgba, bounds, area, "nearest")[..., 0].astype(float)
    assert np.corrcoef(ours.ravel(), image.data[..., 0].astype(float).ravel())[0, 1] > 0.95


def test_swath_slices():
    lats, lons = np.meshgrid(np.linspace(40, 20, 200), np.linspace(25, 45, 100), indexing="ij")
    rows, cols = pipeline.swath_slices(lons, lats, (33.8, 29.0, 36.2, 33.6), step=4)
    inside = (lons >= 33.8) & (lons <= 36.2) & (lats >= 29.0) & (lats <= 33.6)
    r, c = np.flatnonzero(inside.any(axis=1)), np.flatnonzero(inside.any(axis=0))
    assert rows.start <= r[0] and rows.stop > r[-1] and cols.start <= c[0] and cols.stop > c[-1]
    assert rows.stop - rows.start < 0.4 * 200 and cols.stop - cols.start < 0.3 * 100
    with pytest.raises(RuntimeError, match="does not cover"):
        pipeline.swath_slices(lons + 50, lats, (33.8, 29.0, 36.2, 33.6))


def test_to_rgba():
    la = np.array([[[10, 255]]], dtype=np.uint8)
    assert pipeline.to_rgba(la, "LA").tolist() == [[[10, 10, 10, 255]]]
    rgb = np.array([[[1, 2, 3]]], dtype=np.uint8)
    assert pipeline.to_rgba(rgb, "RGB").tolist() == [[[1, 2, 3, 255]]]


def test_enhance(gibs_image):
    assert pipeline.enhance(gibs_image, Enhance()) is gibs_image
    brighter = pipeline.enhance(gibs_image, Enhance(gamma=1.5))
    assert brighter.data[..., :3].mean() > gibs_image.data[..., :3].mean()
    assert np.array_equal(brighter.data[..., 3], gibs_image.data[..., 3])


def test_daylight():
    center = AREAS["israel"].center
    assert pipeline.is_daylight(NOON, center)
    assert not pipeline.is_daylight(MIDNIGHT, center)


class FakeSource:
    """Serves the recorded GIBS sample as a minute-resolution geostationary product."""

    name = "viirs_gibs"
    geostationary = True

    def __init__(self):
        self.downloads = 0

    def download(self, granule, cache):
        self.downloads += 1
        return [SAMPLE]

    def load(self, files, area, granule):
        image = ViirsGibs(ViirsGibsConfig()).load(files, area, gibs_granule(area))
        return Image(image.data, area, granule.time, "Fake SAT · true color")


@pytest.fixture
def cfg(write_config, tmp_path):
    return config.load(write_config(f"output: {{dir: {tmp_path / 'out'}, formats: [png, geotiff], "
                                    "overlays: {coastlines: false, borders: false}}\n"))


def test_process_is_idempotent(cfg):
    source, area = FakeSource(), get_area()
    granule = Granule("viirs_gibs", "fake", NOON)
    result = pipeline.process(source, granule, cfg, area)
    assert result.status == "saved" and result.png.exists()
    assert result.png.with_suffix(".tif").exists()
    assert pipeline.process(source, granule, cfg, area).status == "exists"
    assert source.downloads == 1
    assert pipeline.process(source, granule, cfg, area, force=True).status == "saved"
    assert source.downloads == 2


def test_geotiff_is_georeferenced(cfg):
    import rasterio

    area = get_area()
    result = pipeline.process(FakeSource(), Granule("viirs_gibs", "fake", NOON), cfg, area)
    with rasterio.open(result.png.with_suffix(".tif")) as tif:
        assert tif.crs.to_epsg() == 32636
        assert tif.count == 4
        assert tuple(tif.bounds) == pytest.approx(area.area_extent)


def test_process_skips_night(cfg):
    source = FakeSource()
    assert pipeline.process(source, Granule("viirs_gibs", "fake", MIDNIGHT), cfg, get_area()).status == "night"
    assert source.downloads == 0


@pytest.mark.parametrize("fmt", ["gif", "mp4"])
def test_animate(cfg, fmt):
    cfg.output.formats = ["png", fmt]
    source, area = FakeSource(), get_area()
    results = [pipeline.process(source, Granule("viirs_gibs", f"f{h}", NOON.replace(hour=h)), cfg, area)
               for h in (8, 9, 10)]
    [path] = pipeline.animate("viirs_gibs", results, cfg.output)
    assert path.name == f"viirs_gibs_20261029T0800_20261029T1000.{fmt}"
    assert path.stat().st_size > 10_000
    if fmt == "gif":
        assert PILImage.open(path).n_frames == 3
