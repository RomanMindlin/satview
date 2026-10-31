"""Meteosat-12 FCI at 0°: true color every 10 minutes from the EUMETSAT Data Store.

Without an NRT licence only the hourly scans arrive in real time, the rest an hour later.

A full-disk product is 40 BODY chunk files (south to north) plus a TRAIL file, about
1 GB in all. Only the few chunks whose rows cover the area are downloaded.
"""

from __future__ import annotations

import logging
import re
from datetime import timedelta
from functools import cache
from pathlib import Path

import numpy as np
from pyproj import Transformer

from satview.areas import lonlat_bounds
from satview.cache import granule_dir
from satview.config import Meteosat12FciConfig
from satview.pipeline import satpy_image
from satview.sources.base import AreaDef, Granule, Image, TimeSpec, select_times
from satview.sources.constants import FCI_FDHSI_COLLECTION, FCI_HRFI_COLLECTION, FCI_PLATFORM
from satview.sources.eumetsat import NRT_DELAY, DataStore, sensing_start

log = logging.getLogger(__name__)

FCI_CHUNKS = 40
# The full-disk grid spans ±this in geostationary metres (Satpy areas.yaml, mtg_fci_fdss_*).
FCI_HALF_EXTENT_M = 5567999.998550739
FCI_GEOS = "+proj=geos +lon_0=0 +h=35786400 +ellps=WGS84 +sweep=y"
BODY_CHUNK = re.compile(r"-BODY-.*_(\d{4})\.nc$")


@cache
def fci_chunks(bounds: tuple[float, float, float, float], margin: int = 1) -> tuple[int, ...]:
    """BODY chunk numbers (1 = southernmost) whose rows cover a lon/lat box.

    Chunks split the disk into 40 near-equal row bands; real heights vary by a few
    rows, so `margin` extra chunks are taken on each side.
    """
    lon_min, lat_min, lon_max, lat_max = bounds
    lons, lats = np.meshgrid(np.linspace(lon_min, lon_max, 20), np.linspace(lat_min, lat_max, 20))
    _, y = Transformer.from_crs("EPSG:4326", FCI_GEOS, always_xy=True).transform(lons, lats)
    if not np.all(np.isfinite(y)):
        raise ValueError(f"area {bounds} is not fully visible from 0°")
    fraction = (y + FCI_HALF_EXTENT_M) / (2 * FCI_HALF_EXTENT_M)  # 0 = south edge of the disk
    first = int(np.floor(fraction.min() * FCI_CHUNKS)) + 1 - margin
    last = int(np.floor(fraction.max() * FCI_CHUNKS)) + 1 + margin
    return tuple(range(max(first, 1), min(last, FCI_CHUNKS) + 1))


def body_entries(entries: list[str], chunks: tuple[int, ...]) -> list[str]:
    """Product files that are BODY chunks with the given numbers (TRAIL is not needed by Satpy)."""
    return [e for e in entries if (m := BODY_CHUNK.search(e)) and int(m.group(1)) in chunks]


class Meteosat12Fci:
    name = "meteosat12_fci"
    geostationary = True

    def __init__(self, cfg: Meteosat12FciConfig):
        self.cfg = cfg
        self._store: DataStore | None = None

    @property
    def store(self) -> DataStore:
        if self._store is None:
            self._store = DataStore(self.cfg.credentials)
        return self._store

    @property
    def collection(self) -> str:
        return FCI_FDHSI_COLLECTION if self.cfg.resolution == "1km" else FCI_HRFI_COLLECTION

    def find(self, when: TimeSpec, area: AreaDef) -> list[Granule]:
        # Products reach the Data Store 10-15 minutes after the scan ends, and only the hourly
        # ones can be downloaded before they are an hour old, so look back past that hour.
        latest_window = timedelta(minutes=40) + NRT_DELAY
        start, end = when.window(latest_window if when.mode == "latest" else timedelta(minutes=10))
        products = [p for p in self.store.search(self.collection, start, end) if f"{FCI_PLATFORM}+" in str(p)]
        chunks = fci_chunks(lonlat_bounds(area))
        return [Granule(self.name, str(p), sensing_start(p), payload=(p, chunks))
                for p in select_times(products, sensing_start, when, timedelta(minutes=10))]

    def download(self, granule: Granule, cache: Path) -> list[Path]:
        product, chunks = granule.payload
        entries = body_entries(list(product.entries), chunks)
        if not entries:
            raise RuntimeError(f"product has no BODY chunks {chunks}")
        log.info("%s %s: chunks %d-%d (%d of %d files)", self.name, granule.label, chunks[0], chunks[-1],
                 len(entries), FCI_CHUNKS)
        return self.store.download(product, entries, granule_dir(cache, granule.id))

    def load(self, files: list[Path], area: AreaDef, granule: Granule) -> Image:
        import hdf5plugin  # noqa: F401  registers the HDF5 compression filters FCI files use

        return satpy_image("fci_l1c_nc", files, self.cfg.composite, area, "FCI",
                           resampler=self.cfg.resampler, load_kwargs={"upper_right_corner": "NE"})

    def check(self) -> str:
        return self.store.check(self.collection)
