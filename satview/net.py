"""HTTP session with retries, and a retry helper for client libraries that have none."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Callable, TypeVar
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from satview import __version__
from satview.cache import cached_file

log = logging.getLogger(__name__)
T = TypeVar("T")

TIMEOUT = (15, 120)  # connect, read seconds


def session() -> requests.Session:
    s = requests.Session()
    retry = Retry(total=4, backoff_factor=2, status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods=("GET", "POST"), raise_on_status=False)
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.headers["User-Agent"] = f"satview/{__version__}"
    return s


def retry(fn: Callable[[], T], what: str, attempts: int = 3, backoff: float = 5.0) -> T:
    """Call fn, retrying with exponential backoff on any exception."""
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as e:
            if attempt == attempts:
                raise
            delay = backoff * 2 ** (attempt - 1)
            log.warning("%s failed (%s), retrying in %.0fs", what, e, delay)
            time.sleep(delay)
    raise AssertionError("unreachable")


def download(s: requests.Session, url: str, dest: Path, headers: dict | None = None,
             keep_auth_on_redirect: bool = False) -> Path:
    """Stream url into dest unless it is already cached.

    requests drops the Authorization header when a redirect changes host; some
    services (CDSE) need it kept, others (S3 behind LAADS) reject it, hence the flag.
    """

    def fetch(out):
        target = url
        for _ in range(5):
            r = s.get(target, headers=headers, stream=True, timeout=TIMEOUT,
                      allow_redirects=not keep_auth_on_redirect)
            if keep_auth_on_redirect and r.is_redirect:
                target = r.headers["Location"]
                r.close()
                continue
            r.raise_for_status()
            if r.headers.get("Content-Type", "").startswith("text/html"):
                # A login or licence page, reached through redirects that dropped the Authorization header.
                raise RuntimeError(f"{urlsplit(r.url).netloc} sent a web page instead of the file "
                                   "(a login or licence page; satview list-sources may say more)")
            for chunk in r.iter_content(chunk_size=1 << 20):
                out.write(chunk)
            return
        raise requests.TooManyRedirects(url)

    return retry(lambda: cached_file(dest, fetch), f"download {dest.name}")
