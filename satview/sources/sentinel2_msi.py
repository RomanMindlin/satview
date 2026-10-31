"""Sentinel-2 MSI (2A, 2B, 2C) from Earth Search on AWS: 10 m true color, no login.

A pass covering most of the area comes every 2-5 days at about 08:30 UTC (the passes in
between only clip its edges), and is published about 5 hours later.
"""

from __future__ import annotations

from datetime import timedelta

from satview.sources.constants import EARTH_SEARCH_URL, S2_COLLECTION
from satview.sources.stac import TileSource


class Sentinel2Msi(TileSource):
    name = "sentinel2_msi"
    catalogue = EARTH_SEARCH_URL
    collection = S2_COLLECTION
    instrument = "MSI"
    platforms = {"sentinel-2a": "S2A", "sentinel-2b": "S2B", "sentinel-2c": "S2C"}
    platform_names = {"S2A": "Sentinel-2A", "S2B": "Sentinel-2B", "S2C": "Sentinel-2C"}
    lookback = timedelta(days=10)
    tolerance = timedelta(days=3)
