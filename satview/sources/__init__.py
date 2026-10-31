"""Source adapters, one module per satellite, imported only when that source is used."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from satview.config import Config
    from satview.sources.base import Source

ADAPTERS = {
    "meteosat12_fci": "meteosat12_fci:Meteosat12Fci",
    "meteosat9_iodc": "meteosat9_iodc:Meteosat9Iodc",
    "viirs_gibs": "viirs_gibs:ViirsGibs",
    "viirs_l1b": "viirs_l1b:ViirsL1b",
    "sentinel3_olci": "sentinel3_olci:Sentinel3Olci",
    "sentinel2_msi": "sentinel2_msi:Sentinel2Msi",
    "landsat_oli": "landsat_oli:LandsatOli",
}


def build(name: str, cfg: Config) -> Source:
    module, cls = ADAPTERS[name].split(":")
    return getattr(import_module(f"{__name__}.{module}"), cls)(cfg.source(name))
