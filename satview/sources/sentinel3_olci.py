"""Sentinel-3 OLCI full resolution (300 m) from the Copernicus Data Space: ~1 morning pass a day.

A frame is ~700 MB, but true color needs only three bands plus geometry, so files are
fetched one by one from the product (~100 MB per frame).
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from satview import net
from satview.areas import AREAS, DEFAULT_AREA, region_of
from satview.cache import granule_dir
from satview.config import Sentinel3OlciConfig, parse_utc
from satview.pipeline import satpy_image
from satview.sources.base import AreaDef, Granule, Image, TimeSpec
from satview.sources.constants import (CDSE_CATALOGUE_URL, CDSE_DOWNLOAD_URL, CDSE_TOKEN_URL,
                                       OLCI_PRODUCT_TYPE)
from satview.sources.polar import Record, choose_passes, geojson_polygons, group_passes, search_window

log = logging.getLogger(__name__)

BANDS = ("Oa03", "Oa06", "Oa08")  # blue, green, red for every true_color variant
# lat/lon, sun/view angles for the Rayleigh correction, and solar flux for reflectances
SUPPORT_FILES = ("geo_coordinates.nc", "tie_geometries.nc", "instrument_data.nc")
# Frames come as NR (hours), ST (days) and NT (weeks) processing; later ones are better.
TIMELINESS = re.compile(r"_(NR|ST|NT)_\d{3}\.SEN3$")
TIMELINESS_RANK = {"NR": 0, "ST": 1, "NT": 2}
PLATFORM_NAMES = {"S3A": "Sentinel-3A", "S3B": "Sentinel-3B"}


def _iso(t: datetime) -> str:
    return f"{t:%Y-%m-%dT%H:%M:%S.000Z}"


def _rank(name: str) -> int:
    m = TIMELINESS.search(name)
    return TIMELINESS_RANK[m.group(1)] if m else -1


class Sentinel3Olci:
    name = "sentinel3_olci"
    geostationary = False

    def __init__(self, cfg: Sentinel3OlciConfig):
        self.cfg = cfg
        self.http = net.session()

    def _search(self, start: datetime, end: datetime, bbox: tuple[float, float, float, float]) -> list[dict]:
        lon_min, lat_min, lon_max, lat_max = bbox
        polygon = (f"POLYGON(({lon_min} {lat_min},{lon_max} {lat_min},{lon_max} {lat_max},"
                   f"{lon_min} {lat_max},{lon_min} {lat_min}))")
        query = (f"Collection/Name eq 'SENTINEL-3'"
                 f" and Attributes/OData.CSC.StringAttribute/any(att:att/Name eq 'productType'"
                 f" and att/OData.CSC.StringAttribute/Value eq '{OLCI_PRODUCT_TYPE}')"
                 f" and OData.CSC.Intersects(area=geography'SRID=4326;{polygon}')"
                 f" and ContentDate/Start gt {_iso(start)} and ContentDate/Start lt {_iso(end)}")
        r = self.http.get(CDSE_CATALOGUE_URL, timeout=net.TIMEOUT,
                          params={"$filter": query, "$orderby": "ContentDate/Start desc", "$top": 1000})
        r.raise_for_status()
        return r.json()["value"]

    def _token(self) -> str:
        creds = self.cfg.credentials
        r = self.http.post(CDSE_TOKEN_URL, timeout=net.TIMEOUT, data={
            "client_id": "cdse-public", "grant_type": "password",
            "username": os.environ[creds.user_env], "password": os.environ[creds.password_env]})
        if r.status_code in (400, 401):
            raise RuntimeError(f"Copernicus Data Space login failed; check {creds.user_env}/{creds.password_env}")
        r.raise_for_status()
        return r.json()["access_token"]

    def find(self, when: TimeSpec, area: AreaDef) -> list[Granule]:
        start, end = search_window(when)
        region = region_of(area)
        frames: dict[tuple, dict] = {}
        for product in self._search(start, end, region.bbox):
            name, platform = product["Name"], product["Name"][:3]
            if platform not in self.cfg.platforms or not product.get("Online", True):
                continue
            key = (platform, product["ContentDate"]["Start"][:19])
            if key not in frames or _rank(name) > _rank(frames[key]["Name"]):
                frames[key] = product
        records = [Record(p["Name"][:3], parse_utc(p["ContentDate"]["Start"]), parse_utc(p["ContentDate"]["End"]),
                          geojson_polygons(p.get("GeoFootprint")), p) for p in frames.values()]
        passes = choose_passes(group_passes(records, timedelta(minutes=1), region), when,
                               self.cfg.min_coverage, self.cfg.pick)
        for p in passes:
            log.info("%s: %s pass at %s covers %.0f%% of the area", self.name, p.platform,
                     f"{p.start:%Y-%m-%d %H:%M}Z", p.coverage * 100)
        return [Granule(self.name, f"{p.platform}_{p.start:%Y%m%dT%H%M}", p.start, tag=p.platform, payload=p)
                for p in passes]

    def download(self, granule: Granule, cache: Path) -> list[Path]:
        dest = granule_dir(cache, granule.id)
        wanted = [(frame, f) for frame in granule.payload.items
                  for f in [f"{b}_radiance.nc" for b in BANDS] + list(SUPPORT_FILES)]
        headers = None
        files = []
        for frame, filename in wanted:
            # Satpy's reader expects the files inside their <product>.SEN3 folder.
            path = dest / frame["Name"] / filename
            if not path.exists() and headers is None:
                headers = {"Authorization": f"Bearer {self._token()}"}
            url = f"{CDSE_DOWNLOAD_URL}({frame['Id']})/Nodes({frame['Name']})/Nodes({filename})/$value"
            files.append(net.download(self.http, url, path, headers, keep_auth_on_redirect=True))
        return files

    def load(self, files: list[Path], area: AreaDef, granule: Granule) -> Image:
        return satpy_image("olci_l1b", files, self.cfg.composite, area, "OLCI",
                           platform=PLATFORM_NAMES.get(granule.tag, granule.tag), resampler=self.cfg.resampler)

    def check(self) -> str:
        self._token()
        now = datetime.now(timezone.utc)
        found = len(self._search(now - timedelta(days=3), now, AREAS[DEFAULT_AREA].bbox))
        return f"login OK, {found} {OLCI_PRODUCT_TYPE} frames over the area in the last 3 days"
