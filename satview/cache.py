"""Download cache: atomic writes, one folder per product, and cleanup by age."""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import BinaryIO, Callable

log = logging.getLogger(__name__)

# Folders under the cache that hold static data rather than raw downloads.
KEEP_DIRS = ("naturalearth",)


def granule_dir(cache: Path, granule_id: str) -> Path:
    """Folder for one product's files; ids can contain characters awkward in paths."""
    return cache / re.sub(r"[^A-Za-z0-9._+-]", "_", granule_id)


def cached_file(dest: Path, fetch: Callable[[BinaryIO], None]) -> Path:
    """Return dest, writing it with fetch first if missing. A partial download never lands at dest."""
    if dest.exists():
        log.debug("cached: %s", dest)
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    try:
        with open(part, "wb") as out:
            fetch(out)
        part.replace(dest)
    finally:
        part.unlink(missing_ok=True)
    return dest


def clean(cache: Path, keep_days: float, dry_run: bool = False) -> tuple[int, int]:
    """Delete raw files older than keep_days (by modification time). Returns (files, bytes)."""
    if not cache.is_dir():
        return 0, 0
    cutoff = time.time() - keep_days * 86400
    files = size = 0
    for path in sorted(cache.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if path.relative_to(cache).parts[0] in KEEP_DIRS:
            continue
        if path.is_file() and path.stat().st_mtime < cutoff:
            files += 1
            size += path.stat().st_size
            log.debug("remove %s", path)
            if not dry_run:
                path.unlink()
        elif path.is_dir() and not dry_run and not any(path.iterdir()):
            path.rmdir()
    return files, size
