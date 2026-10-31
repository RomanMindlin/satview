"""The interface every source adapter implements, and the types passed through the pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Literal, Protocol, Sequence, TypeVar

import numpy as np
from pyresample.geometry import AreaDefinition

AreaDef = AreaDefinition
TimeMode = Literal["latest", "at", "range"]
T = TypeVar("T")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(t: datetime) -> datetime:
    """Treat naive datetimes as UTC (Satpy and eumdac return naive UTC times)."""
    return t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t.astimezone(timezone.utc)


@dataclass(frozen=True)
class TimeSpec:
    """Which scenes to fetch: the newest, the one nearest a time, or one per step in a range."""

    mode: TimeMode = "latest"
    at: datetime | None = None
    start: datetime | None = None
    end: datetime | None = None
    step: timedelta = timedelta(minutes=30)
    now: datetime = field(default_factory=utcnow)

    def window(self, before: timedelta, after: timedelta | None = None) -> tuple[datetime, datetime]:
        """Search window around the requested time(s)."""
        after = before if after is None else after
        if self.mode == "latest":
            return self.now - before, self.now
        if self.mode == "at":
            return self.at - before, self.at + after
        return self.start - before, self.end + after

    def slots(self) -> list[datetime]:
        """Target times: one per step for a range, otherwise a single time."""
        if self.mode == "latest":
            return [self.now]
        if self.mode == "at":
            return [self.at]
        slots, t = [], self.start
        while t <= self.end:
            slots.append(t)
            t += self.step
        return slots


def select_times(items: Sequence[T], key: Callable[[T], datetime], when: TimeSpec,
                 tolerance: timedelta) -> list[T]:
    """Pick the newest item for `latest`, else the item nearest each slot (within tolerance)."""
    if not items:
        return []
    if when.mode == "latest":
        return [max(items, key=key)]
    chosen: list[T] = []
    seen: set[int] = set()
    for slot in when.slots():
        best = min(items, key=lambda item: abs(key(item) - slot))
        if abs(key(best) - slot) <= tolerance and id(best) not in seen:
            seen.add(id(best))
            chosen.append(best)
    return sorted(chosen, key=key)


@dataclass
class Granule:
    """One scene a source can deliver: what to download and when it was observed."""

    source: str
    id: str  # unique product id, also used as the cache folder name
    time: datetime  # nominal observation time, UTC
    tag: str = ""  # tells apart products of one source at the same time, e.g. a GIBS layer
    daily: bool = False  # time is only a date (daily composites)
    payload: Any = field(default=None, repr=False, compare=False)

    @property
    def label(self) -> str:
        when = f"{self.time:%Y-%m-%d}" if self.daily else f"{self.time:%Y-%m-%d %H:%M}Z"
        return f"{self.tag} {when}" if self.tag else when


@dataclass
class Image:
    """A picture on the target area, ready to be enhanced and rendered."""

    data: np.ndarray  # (rows, cols, 4) uint8 RGBA; alpha 0 marks no data
    area: AreaDef
    time: datetime
    title: str  # e.g. "Meteosat-12 FCI · true color"
    daily: bool = False

    @property
    def label(self) -> str:
        when = f"{self.time:%Y-%m-%d} (daily)" if self.daily else f"{self.time:%Y-%m-%d %H:%M} UTC"
        return f"{self.title} · {when}"


class Source(Protocol):
    name: str
    geostationary: bool

    def find(self, when: TimeSpec, area: AreaDef) -> list[Granule]: ...

    def download(self, granule: Granule, cache: Path) -> list[Path]: ...

    def load(self, files: list[Path], area: AreaDef, granule: Granule) -> Image: ...  # usually via Satpy

    def check(self) -> str:
        """Verify credentials and product IDs; return a short status or raise."""
        ...
