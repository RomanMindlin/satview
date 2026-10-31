"""VIIRS true color from NASA GIBS: one ready-made image per satellite per day, no login.

The daily image over Israel comes from the midday pass (about 10-12 UTC). Before that
pass is processed, today's image is empty here, so `latest` falls back to yesterday.
"""

from __future__ import annotations

import io
import logging
import math
import re
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import numpy as np
from PIL import Image as PILImage

from satview import net
from satview.areas import lonlat_bounds
from satview.cache import cached_file, granule_dir
from satview.config import ViirsGibsConfig
from satview.pipeline import resample_latlon
from satview.sources.base import AreaDef, Granule, Image, TimeSpec
from satview.sources.constants import GIBS_WMS_URL, VIIRS_PLATFORM_NAMES

log = logging.getLogger(__name__)

DEG_PER_PX = 0.0025  # ~250 m, about the layers' native resolution
COMPLETE = 0.95  # share of the area with data for a day's image to count as complete
USABLE = 0.5  # below this the image is rejected


def request_bounds(area: AreaDef) -> tuple[float, float, float, float]:
    """lon/lat box to request: the area's bounds, padded and rounded so it is stable."""
    lon_min, lat_min, lon_max, lat_max = lonlat_bounds(area)
    return (math.floor(lon_min * 100 - 2) / 100, math.floor(lat_min * 100 - 2) / 100,
            math.ceil(lon_max * 100 + 2) / 100, math.ceil(lat_max * 100 + 2) / 100)


def layer_tag(layer: str) -> str:
    """VIIRS_NOAA20_CorrectedReflectance_TrueColor -> noaa20"""
    m = re.match(r"VIIRS_([A-Za-z0-9]+)_", layer)
    return m.group(1).lower() if m else layer.lower()


def coverage(rgba: np.ndarray) -> float:
    """Share of pixels with data; GIBS marks missing data transparent (or black)."""
    has_data = (rgba[..., 3] > 0) & (rgba[..., :3].max(axis=-1) > 0)
    return float(has_data.mean())


def _read_rgba(data: bytes | Path) -> np.ndarray:
    source = io.BytesIO(data) if isinstance(data, bytes) else data
    with PILImage.open(source) as im:
        return np.asarray(im.convert("RGBA"))


class ViirsGibs:
    name = "viirs_gibs"
    geostationary = False

    def __init__(self, cfg: ViirsGibsConfig):
        self.cfg = cfg
        self.http = net.session()

    def _get_map(self, layer: str, day: date, bounds, width: int) -> bytes:
        lon_min, lat_min, lon_max, lat_max = bounds
        height = round(width * (lat_max - lat_min) / (lon_max - lon_min))
        params = {
            "SERVICE": "WMS", "REQUEST": "GetMap", "VERSION": "1.3.0", "LAYERS": layer, "STYLES": "",
            "CRS": "EPSG:4326", "BBOX": f"{lat_min},{lon_min},{lat_max},{lon_max}",  # WMS 1.3: lat first
            "WIDTH": width, "HEIGHT": height, "FORMAT": "image/png", "TRANSPARENT": "TRUE",
            "TIME": day.isoformat(),
        }
        r = self.http.get(GIBS_WMS_URL, params=params, timeout=net.TIMEOUT)
        r.raise_for_status()
        if not r.headers.get("Content-Type", "").startswith("image/"):
            raise RuntimeError(f"GIBS returned an error for {layer} on {day}: {r.text[:300]}")
        return r.content

    def _days(self, when: TimeSpec, layer: str, bounds) -> list[date]:
        if when.mode == "at":
            return [when.at.date()]
        if when.mode == "range":
            first, last = when.start.date(), when.end.date()
            return [first + timedelta(days=i) for i in range((last - first).days + 1)]
        today = when.now.date()
        share = coverage(_read_rgba(self._get_map(layer, today, bounds, width=48)))
        if share >= COMPLETE:
            return [today]
        log.info("%s %s: today's image covers %.0f%% of the area so far, using yesterday",
                 self.name, layer_tag(layer), share * 100)
        return [today - timedelta(days=1)]

    def find(self, when: TimeSpec, area: AreaDef) -> list[Granule]:
        bounds = request_bounds(area)
        return [Granule(self.name, f"{layer}_{day.isoformat()}",
                        datetime.combine(day, time(), tzinfo=timezone.utc),
                        tag=layer_tag(layer), daily=True, payload=(layer, bounds))
                for layer in self.cfg.layers for day in self._days(when, layer, bounds)]

    def download(self, granule: Granule, cache: Path) -> list[Path]:
        layer, bounds = granule.payload
        day = granule.time.date()
        width = math.ceil((bounds[2] - bounds[0]) / DEG_PER_PX)
        # The box is part of the name: each area's crop is cached on its own, and a changed
        # area is fetched again.
        box = "_".join(f"{b:g}" for b in bounds)
        dest = granule_dir(cache, granule.id) / f"{granule.id}_{box}.png"
        if dest.exists() and coverage(_read_rgba(dest)) < COMPLETE:
            dest.unlink()  # cached while the day's passes were still arriving; fetch again
        path = net.retry(lambda: cached_file(dest, lambda out: out.write(self._get_map(layer, day, bounds, width))),
                         f"GIBS {granule.id}")
        share = coverage(_read_rgba(path))
        if share < USABLE:
            path.unlink()
            raise RuntimeError(f"no imagery yet ({share:.0%} of the area has data)")
        if share < COMPLETE:
            log.warning("%s %s: image covers only %.0f%% of the area", self.name, granule.label, share * 100)
        return [path]

    def load(self, files: list[Path], area: AreaDef, granule: Granule) -> Image:
        layer, bounds = granule.payload
        rgba = _read_rgba(files[0]).copy()
        rgba[rgba[..., :3].max(axis=-1) == 0, 3] = 0  # black is no data too
        data = resample_latlon(rgba, bounds, area, "bilinear")
        platform = VIIRS_PLATFORM_NAMES.get(granule.tag, granule.tag)
        product = "true color" if layer.endswith("TrueColor") else layer.rsplit("_", 1)[-1]
        return Image(data, area, granule.time, f"{platform} VIIRS · {product} (GIBS)", daily=True)

    def check(self) -> str:
        yesterday = datetime.now(timezone.utc).date() - timedelta(days=1)
        bounds = (34.0, 31.0, 35.0, 32.0)
        for layer in self.cfg.layers:
            self._get_map(layer, yesterday, bounds, width=16)
        return f"{len(self.cfg.layers)} layer(s) available"
