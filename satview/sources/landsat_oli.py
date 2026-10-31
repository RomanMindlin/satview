"""Landsat 8 and 9 OLI from Microsoft Planetary Computer: 30 m true color, no login.

The two satellites alternate, so a pass covering most of the area comes every 8 days at
about 08:10 UTC. Surface reflectance is published 1-2 weeks after the pass, so `latest`
is that old.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from satview import net
from satview.config import parse_utc
from satview.sources.base import utcnow
from satview.sources.constants import LANDSAT_COLLECTION, PLANETARY_COMPUTER_TOKEN_URL, PLANETARY_COMPUTER_URL
from satview.sources.stac import TileSource


class LandsatOli(TileSource):
    name = "landsat_oli"
    catalogue = PLANETARY_COMPUTER_URL
    collection = LANDSAT_COLLECTION
    instrument = "OLI"
    platforms = {"landsat-8": "L8", "landsat-9": "L9"}
    platform_names = {"L8": "Landsat 8", "L9": "Landsat 9"}
    lookback = timedelta(days=30)
    tolerance = timedelta(days=8)

    _token: tuple[str, datetime] | None = None

    def token(self) -> str:
        """Planetary Computer file URLs need a short-lived token, free and anonymous."""
        if self._token is None or self._token[1] < utcnow() + timedelta(minutes=5):
            r = self.http.get(f"{PLANETARY_COMPUTER_TOKEN_URL}/{self.collection}", timeout=net.TIMEOUT)
            r.raise_for_status()
            self._token = r.json()["token"], parse_utc(r.json()["msft:expiry"])
        return self._token[0]

    def sign(self, href: str) -> str:
        return f"{href}?{self.token()}"

    def check(self) -> str:
        self.token()
        return super().check()
