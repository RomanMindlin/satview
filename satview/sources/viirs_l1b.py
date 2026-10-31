"""VIIRS L1B from NASA Earthdata (S-NPP, NOAA-20, NOAA-21): 1-3 daytime passes a day.

Searches CMR with earthaccess for M-band radiances (02MOD) and their geolocation
(03MOD), picks the best daytime pass, and downloads with an Earthdata bearer token
(given directly, or created from a username and password).
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from satview import net
from satview.areas import AREAS, DEFAULT_AREA, region_of
from satview.cache import granule_dir
from satview.config import ViirsL1bConfig, parse_utc
from satview.pipeline import satpy_image
from satview.sources.base import AreaDef, Granule, Image, TimeSpec
from satview.sources.constants import EDL_TOKEN_URL, VIIRS_PLATFORM_NAMES, VIIRS_PREFIXES
from satview.sources.polar import Record, choose_passes, group_passes, search_window

log = logging.getLogger(__name__)

GRANULE_KEY = re.compile(r"\.(A\d{7}\.\d{4})\.")  # VJ102MOD.A2026303.1130.021....nc -> A2026303.1130


def _url(granule) -> str:
    links = granule.data_links()
    if not links:
        raise RuntimeError(f"no download link for {granule['umm'].get('GranuleUR')}")
    return links[0]


def _polygons(umm: dict) -> list[np.ndarray]:
    geometry = umm["SpatialExtent"]["HorizontalSpatialDomain"]["Geometry"]
    polygons = [np.array([(p["Longitude"], p["Latitude"]) for p in g["Boundary"]["Points"]])
                for g in geometry.get("GPolygons", [])]
    for r in geometry.get("BoundingRectangles", []):
        w, s, e, n = (r["WestBoundingCoordinate"], r["SouthBoundingCoordinate"],
                      r["EastBoundingCoordinate"], r["NorthBoundingCoordinate"])
        polygons.append(np.array([(w, s), (e, s), (e, n), (w, n)]))
    return polygons


def _times(umm: dict) -> tuple[datetime, datetime]:
    extent = umm["TemporalExtent"]["RangeDateTime"]
    return parse_utc(extent["BeginningDateTime"]), parse_utc(extent["EndingDateTime"])


def _iso(t: datetime) -> str:
    return f"{t:%Y-%m-%dT%H:%M:%SZ}"


class ViirsL1b:
    name = "viirs_l1b"
    geostationary = False

    def __init__(self, cfg: ViirsL1bConfig):
        self.cfg = cfg
        self.http = net.session()
        self._bearer: str | None = None

    def _token(self) -> str:
        if self._bearer is None:
            creds = self.cfg.credentials
            token = os.environ.get(creds.token_env)
            if not token:
                r = self.http.post(EDL_TOKEN_URL, auth=(os.environ[creds.user_env], os.environ[creds.password_env]),
                                   headers={"Accept": "application/json"}, timeout=net.TIMEOUT)
                if r.status_code == 401:
                    raise RuntimeError(f"Earthdata login failed; check {creds.user_env}/{creds.password_env}")
                r.raise_for_status()
                token = r.json()["access_token"]
            self._bearer = token
        return self._bearer

    def _short_names(self, platform: str) -> tuple[str, str]:
        prefix, suffix = VIIRS_PREFIXES[platform], "_NRT" if self.cfg.nrt else ""
        return f"{prefix}02MOD{suffix}", f"{prefix}03MOD{suffix}"

    def _search(self, short_name: str, start: datetime, end: datetime,
                bbox: tuple[float, float, float, float]) -> list:
        import earthaccess

        return net.retry(lambda: earthaccess.search_data(
            short_name=short_name, bounding_box=bbox, temporal=(_iso(start), _iso(end)),
            day_night_flag="day", count=-1), f"CMR search {short_name}")

    def find(self, when: TimeSpec, area: AreaDef) -> list[Granule]:
        start, end = search_window(when)
        region = region_of(area)
        records = []
        for platform in self.cfg.platforms:
            data_name, geo_name = self._short_names(platform)
            geo = {}
            for g in self._search(geo_name, start, end, region.bbox):
                if m := GRANULE_KEY.search(_url(g)):
                    geo[m.group(1)] = g
            for g in self._search(data_name, start, end, region.bbox):
                m = GRANULE_KEY.search(_url(g))
                if not m or m.group(1) not in geo:
                    log.debug("%s: no geolocation for %s", self.name, _url(g))
                    continue
                t0, t1 = _times(g["umm"])
                records.append(Record(platform, t0, t1, _polygons(g["umm"]), (g, geo[m.group(1)])))
        passes = choose_passes(group_passes(records, timedelta(minutes=2), region), when,
                               self.cfg.min_coverage, self.cfg.pick)
        for p in passes:
            log.info("%s: %s pass at %s covers %.0f%% of the area", self.name, p.platform,
                     f"{p.start:%Y-%m-%d %H:%M}Z", p.coverage * 100)
        return [Granule(self.name, f"{p.platform}_{p.start:%Y%m%dT%H%M}", p.start, tag=p.platform, payload=p)
                for p in passes]

    def download(self, granule: Granule, cache: Path) -> list[Path]:
        dest = granule_dir(cache, granule.id)
        files = []
        for pair in granule.payload.items:
            for g in pair:
                url = _url(g)
                path = dest / url.rsplit("/", 1)[-1]
                headers = None if path.exists() else {"Authorization": f"Bearer {self._token()}"}
                files.append(net.download(self.http, url, path, headers))
        return files

    def load(self, files: list[Path], area: AreaDef, granule: Granule) -> Image:
        return satpy_image("viirs_l1b", files, self.cfg.composite, area, "VIIRS",
                           platform=VIIRS_PLATFORM_NAMES[granule.tag], resampler=self.cfg.resampler)

    def check(self) -> str:
        import earthaccess

        names = [n for p in self.cfg.platforms for n in self._short_names(p)]
        missing = [n for n in names if not earthaccess.search_datasets(short_name=n, count=1)]
        if missing:
            raise RuntimeError(f"collections not found in CMR: {', '.join(missing)}")

        token = self._token()
        expires = _token_expiry(token)
        if expires is not None and expires < datetime.now(timezone.utc):
            raise RuntimeError(f"Earthdata token expired on {expires:%Y-%m-%d}")
        # A data URL redirects to storage with a valid token and to the login page without. LANCE
        # (NRT) sends accounts that have not accepted its licence to a page only a browser can use.
        now = datetime.now(timezone.utc)
        recent = self._search(names[0], now - timedelta(days=3), now, AREAS[DEFAULT_AREA].bbox)
        if recent:
            url = _url(recent[0])
            # GET, not HEAD: LANCE answers HEAD with 200 either way. Only the headers are read.
            r = self.http.get(url, headers={"Authorization": f"Bearer {token}"},
                              allow_redirects=False, stream=True, timeout=net.TIMEOUT)
            r.close()
            location = r.headers.get("Location", "")
            if "/profiles/licenses" in location:
                raise RuntimeError(f"LANCE NRT licence not accepted: open {url} in a browser, log in "
                                   "and accept it (once)")
            if r.status_code in (401, 403) or "urs.earthdata.nasa.gov" in location:
                raise RuntimeError("Earthdata token rejected by the data server")
        until = f", token valid until {expires:%Y-%m-%d}" if expires else ""
        return f"{len(names)} collections found{until}"


def _token_expiry(token: str) -> datetime | None:
    """Expiry of an Earthdata Login token (a JWT), or None if it cannot be read."""
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return datetime.fromtimestamp(claims["exp"], timezone.utc)
    except (IndexError, KeyError, ValueError):
        return None
