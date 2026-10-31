import os
import time

import pytest

from satview import net
from satview.cache import cached_file, clean


def test_cached_file_writes_once(tmp_path):
    calls = []

    def fetch(out):
        calls.append(1)
        out.write(b"data")

    dest = tmp_path / "a" / "file.nc"
    assert cached_file(dest, fetch).read_bytes() == b"data"
    cached_file(dest, fetch)
    assert len(calls) == 1


def test_failed_download_leaves_nothing(tmp_path):
    def fetch(out):
        out.write(b"half")
        raise ConnectionError("dropped")

    dest = tmp_path / "file.nc"
    with pytest.raises(ConnectionError):
        cached_file(dest, fetch)
    assert list(tmp_path.iterdir()) == []


def age(path, days):
    old = time.time() - days * 86400
    os.utime(path, (old, old))


class FakeResponse:
    is_redirect = False

    def __init__(self, content_type, body):
        self.headers, self.body = {"Content-Type": content_type}, body
        self.url = "https://urs.earthdata.nasa.gov/oauth/authorize?client_id=x"

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size):
        yield self.body


class FakeSession:
    def __init__(self, response):
        self.response = response

    def get(self, url, **kwargs):
        return self.response


def test_download_rejects_a_login_page(tmp_path, monkeypatch):
    monkeypatch.setattr(net, "retry", lambda fn, what: fn())
    page = FakeSession(FakeResponse("text/html; charset=utf-8", b"<!DOCTYPE html>"))
    with pytest.raises(RuntimeError, match="urs.earthdata.nasa.gov sent a web page instead of the file"):
        net.download(page, "https://data/x.nc", tmp_path / "x.nc")
    assert list(tmp_path.iterdir()) == []  # nothing cached, so the next run tries again
    data = FakeSession(FakeResponse("application/x-netcdf", b"CDF"))
    assert net.download(data, "https://data/x.nc", tmp_path / "x.nc").read_bytes() == b"CDF"


def test_clean_removes_old_raw_files_only(tmp_path):
    old = tmp_path / "meteosat12_fci" / "product1" / "chunk.nc"
    new = tmp_path / "meteosat12_fci" / "product2" / "chunk.nc"
    static = tmp_path / "naturalearth" / "ne_10m_coastline" / "ne_10m_coastline.shp"
    for path in (old, new, static):
        path.parent.mkdir(parents=True)
        path.write_bytes(b"x" * 10)
    age(old, 3)
    age(static, 30)

    assert clean(tmp_path, keep_days=2, dry_run=True) == (1, 10)
    assert old.exists()

    assert clean(tmp_path, keep_days=2) == (1, 10)
    assert not old.exists() and not old.parent.exists()
    assert new.exists() and static.exists()


def test_clean_missing_cache(tmp_path):
    assert clean(tmp_path / "none", 2) == (0, 0)
