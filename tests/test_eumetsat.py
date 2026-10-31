"""EUMETSAT Data Store: which scans an account without an NRT licence can download."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from satview.sources import eumetsat
from satview.sources.eumetsat import DataStore, downloadable

NOW = datetime(2026, 10, 31, 9, 14, tzinfo=timezone.utc)


def scan(hour, minute):
    # eumdac gives naive UTC times, a few seconds after the nominal slot
    return SimpleNamespace(sensing_start=datetime(2026, 10, 31, hour, minute, 8))


def test_only_hourly_scans_before_an_hour():
    assert downloadable(scan(9, 0), NOW)
    assert not downloadable(scan(8, 50), NOW)
    assert not downloadable(scan(8, 15), NOW)
    assert downloadable(scan(8, 10), NOW)


def test_search_drops_scans_needing_nrt_licence(monkeypatch):
    products = [scan(9, 0), scan(8, 50), scan(8, 10), scan(8, 0)]
    collection = SimpleNamespace(search=lambda **kw: products)
    store = DataStore.__new__(DataStore)  # skip the token
    store._store = SimpleNamespace(get_collection=lambda cid: collection)
    monkeypatch.setattr(eumetsat, "utcnow", lambda: NOW)
    assert store.search("c", NOW - timedelta(hours=2), NOW) == [products[3], products[2], products[0]]
