"""Meteosat-9 SEVIRI over the Indian Ocean (45.5°E): natural color every 15 minutes.

Without an NRT licence only the hourly scans arrive in real time, the rest an hour later.

Each scan is one native `.nat` file covering the full disk (~180 MB).
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from satview.cache import granule_dir
from satview.config import Meteosat9IodcConfig
from satview.pipeline import satpy_image
from satview.sources.base import AreaDef, Granule, Image, TimeSpec, select_times
from satview.sources.constants import SEVIRI_IODC_COLLECTION
from satview.sources.eumetsat import NRT_DELAY, DataStore, sensing_start


class Meteosat9Iodc:
    name = "meteosat9_iodc"
    geostationary = True

    def __init__(self, cfg: Meteosat9IodcConfig):
        self.cfg = cfg
        self._store: DataStore | None = None

    @property
    def store(self) -> DataStore:
        if self._store is None:
            self._store = DataStore(self.cfg.credentials)
        return self._store

    def find(self, when: TimeSpec, area: AreaDef) -> list[Granule]:
        # Only the hourly scans can be downloaded before they are an hour old (see NRT_DELAY).
        latest_window = timedelta(minutes=60) + NRT_DELAY
        start, end = when.window(latest_window if when.mode == "latest" else timedelta(minutes=15))
        products = self.store.search(SEVIRI_IODC_COLLECTION, start, end)
        return [Granule(self.name, str(p), sensing_start(p), payload=p)
                for p in select_times(products, sensing_start, when, timedelta(minutes=15))]

    def download(self, granule: Granule, cache: Path) -> list[Path]:
        product = granule.payload
        entries = [e for e in product.entries if e.endswith(".nat")]
        if not entries:
            raise RuntimeError("product has no .nat file")
        return self.store.download(product, entries, granule_dir(cache, granule.id))

    def load(self, files: list[Path], area: AreaDef, granule: Granule) -> Image:
        return satpy_image("seviri_l1b_native", files, self.cfg.composite, area, "SEVIRI",
                           resampler=self.cfg.resampler, load_kwargs={"upper_right_corner": "NE"})

    def check(self) -> str:
        return self.store.check(SEVIRI_IODC_COLLECTION)
