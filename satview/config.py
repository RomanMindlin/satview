"""Load and validate config.yaml. Secrets never live in it, only the names of env vars holding them."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, ValidationError, field_validator, model_validator

from satview.areas import AREAS, DEFAULT_AREA
from satview.sources.base import TimeSpec

MAX_RANGE_SLOTS = 1000
ROOT_AREA = "israel"  # its pictures go straight into output/, other areas' into output/<area>/


class ConfigError(Exception):
    """A problem with config.yaml, CLI overrides or credentials, reported without a traceback."""


def parse_utc(value: Any) -> Any:
    """Accept `2026-10-30T10:00Z`, `2026-10-30 10:00`, `2026-10-30` or a datetime; naive means UTC."""
    if isinstance(value, str):
        text = value.strip()
        if text[-1:] in ("Z", "z"):
            text = text[:-1] + "+00:00"
        try:
            value = datetime.fromisoformat(text)
        except ValueError:
            raise ValueError(f"invalid time {value!r}, expected e.g. 2026-10-30T10:00Z") from None
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    return value


UtcDatetime = Annotated[datetime, BeforeValidator(parse_utc)]
Resampler = Literal["nearest", "bilinear"]
Format = Literal["png", "geotiff", "gif", "mp4"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Enhance(Strict):
    """Per-source tweak applied after Satpy's default enhancement, so satellites look alike."""

    gamma: float = Field(1.0, gt=0)  # > 1 brightens dark areas
    contrast: float = Field(1.0, gt=0)
    brightness: float = Field(1.0, gt=0)


class Credentials(Strict):
    """Names of the environment variables holding a source's secrets."""

    def env_vars(self) -> list[str]:
        return list(self.model_dump().values())

    def missing(self) -> list[str]:
        return [var for var in self.env_vars() if not os.environ.get(var)]


class EumetsatCredentials(Credentials):
    key_env: str = "EUMETSAT_KEY"
    secret_env: str = "EUMETSAT_SECRET"


class EarthdataCredentials(Credentials):
    """An Earthdata Login token, or a username and password to create one."""

    token_env: str = "EARTHDATA_TOKEN"
    user_env: str = "EARTHDATA_USERNAME"
    password_env: str = "EARTHDATA_PASSWORD"

    def missing(self) -> list[str]:
        if os.environ.get(self.token_env) or (os.environ.get(self.user_env) and os.environ.get(self.password_env)):
            return []
        return [f"{self.token_env} (or {self.user_env} and {self.password_env})"]


class CdseCredentials(Credentials):
    user_env: str = "CDSE_USER"
    password_env: str = "CDSE_PASSWORD"


class SourceConfig(Strict):
    enabled: bool = False
    enhance: Enhance = Field(default_factory=Enhance)

    @property
    def _credentials(self) -> Credentials | None:
        return getattr(self, "credentials", None)

    def env_vars(self) -> list[str]:
        return self._credentials.env_vars() if self._credentials else []

    def missing_env_vars(self) -> list[str]:
        return self._credentials.missing() if self._credentials else []


class Meteosat12FciConfig(SourceConfig):
    composite: Literal["true_color", "natural_color", "vis_06"] = "true_color"
    resolution: Literal["1km", "500m"] = "1km"
    resampler: Resampler = "bilinear"  # nearest leaves 1 km pixels blocky on the 500 m grid
    credentials: EumetsatCredentials = Field(default_factory=EumetsatCredentials)

    @model_validator(mode="after")
    def _hrfi_is_red_only(self):
        if self.resolution == "500m" and self.composite != "vis_06":
            raise ValueError("resolution 500m (HRFI) only has the red channel; use composite: vis_06")
        return self


class Meteosat9IodcConfig(SourceConfig):
    composite: Literal["natural_color", "VIS006"] = "natural_color"  # SEVIRI has no blue channel
    resampler: Resampler = "bilinear"
    credentials: EumetsatCredentials = Field(default_factory=EumetsatCredentials)


class ViirsGibsConfig(SourceConfig):
    layers: list[str] = Field(default_factory=lambda: ["VIIRS_NOAA20_CorrectedReflectance_TrueColor",
                                                       "VIIRS_SNPP_CorrectedReflectance_TrueColor"],
                              min_length=1)


class PolarConfig(SourceConfig):
    min_coverage: float = Field(0.6, ge=0, le=1)  # share of the area a pass must cover
    pick: Literal["best", "all"] = "best"  # best daytime pass per day, or every one


class ViirsL1bConfig(PolarConfig):
    platforms: list[Literal["noaa20", "noaa21", "snpp"]] = Field(
        default_factory=lambda: ["noaa20", "noaa21", "snpp"], min_length=1)
    composite: Literal["true_color", "true_color_lowres", "natural_color"] = "true_color"
    nrt: bool = False  # LANCE near-real-time files: ~3 h latency instead of ~6-12 h
    resampler: Resampler = "nearest"
    credentials: EarthdataCredentials = Field(default_factory=EarthdataCredentials)


class Sentinel3OlciConfig(PolarConfig):
    platforms: list[Literal["S3A", "S3B"]] = Field(default_factory=lambda: ["S3A", "S3B"], min_length=1)
    composite: Literal["true_color", "true_color_land", "true_color_desert"] = "true_color"
    resampler: Resampler = "nearest"
    credentials: CdseCredentials = Field(default_factory=CdseCredentials)


class HighResConfig(PolarConfig):
    """Sentinel-2 and Landsat: drawn on a finer grid than the shared 500 m one."""

    # Grid spacing in metres. Below 50 m the whole area no longer fits in memory comfortably.
    resolution_m: float = Field(100, ge=50, le=500)


class Sentinel2MsiConfig(HighResConfig):
    platforms: list[Literal["S2A", "S2B", "S2C"]] = Field(default_factory=lambda: ["S2A", "S2B", "S2C"],
                                                          min_length=1)


class LandsatOliConfig(HighResConfig):
    platforms: list[Literal["L8", "L9"]] = Field(default_factory=lambda: ["L8", "L9"], min_length=1)


class SourcesConfig(Strict):
    meteosat12_fci: Meteosat12FciConfig = Field(default_factory=Meteosat12FciConfig)
    meteosat9_iodc: Meteosat9IodcConfig = Field(default_factory=Meteosat9IodcConfig)
    viirs_gibs: ViirsGibsConfig = Field(default_factory=ViirsGibsConfig)
    viirs_l1b: ViirsL1bConfig = Field(default_factory=ViirsL1bConfig)
    sentinel3_olci: Sentinel3OlciConfig = Field(default_factory=Sentinel3OlciConfig)
    sentinel2_msi: Sentinel2MsiConfig = Field(default_factory=Sentinel2MsiConfig)
    landsat_oli: LandsatOliConfig = Field(default_factory=LandsatOliConfig)


SOURCE_NAMES = tuple(SourcesConfig.model_fields)


class TimeRange(Strict):
    start: UtcDatetime | None = None
    end: UtcDatetime | None = None
    step_minutes: int = Field(30, ge=1)


class TimeConfig(Strict):
    mode: Literal["latest", "at", "range"] = "latest"
    at: UtcDatetime | None = None
    range: TimeRange = Field(default_factory=TimeRange)

    @model_validator(mode="after")
    def _check_times(self):
        now = datetime.now(timezone.utc)
        if self.mode == "at":
            if self.at is None:
                raise ValueError("mode 'at' needs time.at, e.g. 2026-10-30T10:00Z")
            if self.at > now:
                raise ValueError(f"time.at {self.at:%Y-%m-%dT%H:%MZ} is in the future")
        elif self.mode == "range":
            start, end, step = self.range.start, self.range.end, self.range.step_minutes
            if start is None or end is None:
                raise ValueError("mode 'range' needs time.range.start and time.range.end")
            if start >= end:
                raise ValueError("time.range.start must be before time.range.end")
            if start > now:
                raise ValueError(f"time.range.start {start:%Y-%m-%dT%H:%MZ} is in the future")
            if (end - start) / timedelta(minutes=step) > MAX_RANGE_SLOTS:
                raise ValueError(f"time range has more than {MAX_RANGE_SLOTS} steps; "
                                 "shorten it or raise step_minutes")
        return self

    def spec(self) -> TimeSpec:
        return TimeSpec(mode=self.mode, at=self.at, start=self.range.start, end=self.range.end,
                        step=timedelta(minutes=self.range.step_minutes))


class Overlays(Strict):
    coastlines: bool = True
    borders: bool = True
    cities: bool = False

    @property
    def any(self) -> bool:
        return self.coastlines or self.borders or self.cities


class OutputConfig(Strict):
    dir: Path = Path("output")
    cache_dir: Path = Path("cache")
    formats: list[Format] = Field(default_factory=lambda: ["png"], min_length=1)
    width_px: int | None = Field(1200, ge=100, le=8000)  # null keeps the area's native size
    overlays: Overlays = Field(default_factory=Overlays)
    label: bool = True  # satellite name + UTC timestamp
    keep_raw_days: float = Field(2, ge=0)
    animation_fps: float = Field(4, gt=0, le=60)

    @property
    def scene_formats(self) -> list[str]:
        """Per-scene files; animations are built from the PNGs, so they imply png."""
        formats = [f for f in ("png", "geotiff") if f in self.formats]
        if self.animation_formats and "png" not in formats:
            formats.insert(0, "png")
        return formats

    @property
    def animation_formats(self) -> list[str]:
        return [f for f in ("gif", "mp4") if f in self.formats]


class Config(Strict):
    area: str = DEFAULT_AREA
    sources: SourcesConfig = Field(default_factory=SourcesConfig)
    time: TimeConfig = Field(default_factory=TimeConfig)
    output: OutputConfig = Field(default_factory=OutputConfig)

    @field_validator("area")
    @classmethod
    def _known_area(cls, name: str) -> str:
        if name not in AREAS:
            raise ValueError(f"unknown area {name!r} (available: {', '.join(AREAS)})")
        return name

    def source(self, name: str) -> SourceConfig:
        return getattr(self.sources, name)

    def enabled_sources(self) -> list[str]:
        return [name for name in SOURCE_NAMES if self.source(name).enabled]

    def check_credentials(self, names: list[str]) -> None:
        missing = [f"  {name}: environment variable {var} is not set"
                   for name in names for var in self.source(name).missing_env_vars()]
        if missing:
            raise ConfigError("missing credentials (set them in the environment or a .env file):\n"
                              + "\n".join(missing))


def _set_dotted(raw: dict, dotted: str, value: Any) -> None:
    node = raw
    *parents, leaf = dotted.split(".")
    for key in parents:
        child = node.get(key)
        if not isinstance(child, dict):
            child = node[key] = {}
        node = child
    node[leaf] = value


def _format_errors(err: ValidationError) -> str:
    lines = []
    for e in err.errors():
        loc = ".".join(str(part) for part in e["loc"])
        if e["type"] == "extra_forbidden" and len(e["loc"]) == 2 and e["loc"][0] == "sources":
            msg = f"unknown source (available: {', '.join(SOURCE_NAMES)})"
        else:
            msg = e["msg"].removeprefix("Value error, ")
        lines.append(f"  {loc}: {msg}" if loc else f"  {msg}")
    return "\n".join(lines)


def load(path: Path, overrides: dict[str, Any] | None = None, sources: list[str] | None = None) -> Config:
    """Read config.yaml, apply dotted-key overrides, and optionally run only the named sources.

    Relative output paths are resolved against the config file's folder, so cron runs
    write to the same place as manual ones. Pictures of areas other than the default one
    go into a subfolder named after the area. A .env file next to the config is loaded
    into the environment (existing variables win).
    """
    path = Path(path)
    if not path.is_file():
        raise ConfigError(f"config file not found: {path} (copy config.example.yaml to config.yaml)")
    load_dotenv(path.parent / ".env", override=False)
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"{path}: invalid YAML: {e}") from None
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: expected a mapping at the top level")

    for key, value in (overrides or {}).items():
        _set_dotted(raw, key, value)
    if sources is not None:
        unknown = [s for s in sources if s not in SOURCE_NAMES]
        if unknown:
            raise ConfigError(f"unknown source {', '.join(map(repr, unknown))} "
                              f"(available: {', '.join(SOURCE_NAMES)})")
        for name in SOURCE_NAMES:
            _set_dotted(raw, f"sources.{name}.enabled", name in sources)

    try:
        cfg = Config.model_validate(raw)
    except ValidationError as e:
        raise ConfigError(f"{path}: invalid configuration:\n{_format_errors(e)}") from None

    base = path.parent.resolve()
    cfg.output.dir = base / cfg.output.dir
    if cfg.area != ROOT_AREA:  # israel's pictures stay where they always were
        cfg.output.dir /= cfg.area
    cfg.output.cache_dir = base / cfg.output.cache_dir
    return cfg
