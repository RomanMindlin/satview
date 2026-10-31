"""EUMETSAT Data Store access shared by the Meteosat adapters, via eumdac."""

from __future__ import annotations

import logging
import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from satview import net
from satview.cache import cached_file
from satview.config import EumetsatCredentials
from satview.sources.base import as_utc, utcnow

log = logging.getLogger(__name__)

# Without an NRT licence the Data Store refuses (403, "NRTLicense required") every scan under
# an hour old except the hourly ones: in the EUMETSAT Data Policy hourly SEVIRI/FCI Level 1
# data are "Core", free in real time, and the rest is free from 1 hour latency.
NRT_DELAY = timedelta(minutes=61)  # the hour counts from the scan start; plus clock slack


def sensing_start(product) -> datetime:
    return as_utc(product.sensing_start)


def _naive_utc(t: datetime) -> datetime:
    # eumdac sends datetime.isoformat(); the API wants plain UTC without an offset.
    return t.astimezone(timezone.utc).replace(tzinfo=None, microsecond=0)


def downloadable(product, now: datetime) -> bool:
    """Whether an account without an NRT licence may download the product yet."""
    t = sensing_start(product)
    return t.minute == 0 or t <= now - NRT_DELAY


class DataStore:
    """Token, time-sorted search, and per-file downloads from a product."""

    def __init__(self, credentials: EumetsatCredentials):
        import eumdac

        key, secret = os.environ[credentials.key_env], os.environ[credentials.secret_env]
        self._token = eumdac.AccessToken((key, secret))
        self._store = eumdac.DataStore(self._token)

    def search(self, collection_id: str, start: datetime, end: datetime) -> list:
        """Products in the window that can be downloaded now, oldest first."""
        collection = self._store.get_collection(collection_id)
        products = net.retry(
            lambda: list(collection.search(dtstart=_naive_utc(start), dtend=_naive_utc(end))),
            f"search {collection_id}")
        now = utcnow()
        allowed = [p for p in products if downloadable(p, now)]
        if len(allowed) < len(products):
            log.debug("%s: skipping %d scan(s) under an hour old (need an NRT licence)",
                      collection_id, len(products) - len(allowed))
        return sorted(allowed, key=sensing_start)

    def download(self, product, entries: list[str], dest_dir: Path) -> list[Path]:
        """Download only the named files of a product (not the whole zip)."""
        paths = []
        for entry in entries:
            dest = dest_dir / Path(entry).name

            def fetch(out, entry=entry):
                with product.open(entry=entry) as src:
                    shutil.copyfileobj(src, out, 1 << 20)

            if not dest.exists():
                log.debug("downloading %s", entry)
            paths.append(net.retry(lambda: cached_file(dest, fetch), f"download {dest.name}"))
        return paths

    def check(self, collection_id: str) -> str:
        str(self._token.access_token)  # fails here on bad credentials
        return f"{collection_id}: {self._store.get_collection(collection_id).title}"
