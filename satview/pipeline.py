"""Steps shared by every source: daylight check, load + resample, enhance, render, save, animate."""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

import numpy as np

from satview import render
from satview.areas import region_of
from satview.config import Config, Enhance, OutputConfig
from satview.sources.base import AreaDef, Granule, Image, Source, as_utc

log = logging.getLogger(__name__)

MIN_SUN_ELEVATION = 5.0  # degrees; below this visible channels are too dark to be useful
EXTENSIONS = {"png": "png", "geotiff": "tif", "gif": "gif", "mp4": "mp4"}


@dataclass
class Result:
    granule: Granule
    status: Literal["saved", "exists", "night", "failed"]
    png: Path | None = None


def sun_elevation(t: datetime, lon: float, lat: float) -> float:
    from pyorbital.astronomy import sun_zenith_angle

    return 90.0 - float(sun_zenith_angle(as_utc(t).replace(tzinfo=None), lon, lat))


def is_daylight(t: datetime, center: tuple[float, float]) -> bool:
    return sun_elevation(t, *center) >= MIN_SUN_ELEVATION


def source_dir(out_dir: Path, source: str, tag: str, daily: bool) -> Path:
    """output/<source>, or output/<source>[_<tag>] for daily images: those of different satellites
    share the date-only stamp, so in one folder the newest picture (the one the Telegram sender
    posts) would always be the same satellite's."""
    return out_dir / ("_".join(part for part in (source, tag) if part) if daily else source)


def output_path(out_dir: Path, granule: Granule, ext: str) -> Path:
    """<source dir>/<YYYY-MM-DD>/<source>[_<tag>]_<YYYYMMDDTHHMM>.<ext> (date only for daily)."""
    stamp = f"{granule.time:%Y%m%d}" if granule.daily else f"{granule.time:%Y%m%dT%H%M}"
    stem = "_".join(part for part in (granule.source, granule.tag, stamp) if part)
    folder = source_dir(out_dir, granule.source, granule.tag, granule.daily)
    return folder / f"{granule.time:%Y-%m-%d}" / f"{stem}.{ext}"


def animation_path(out_dir: Path, source: str, tag: str, start: datetime, end: datetime, ext: str,
                   daily: bool = False) -> Path:
    stem = "_".join(part for part in (source, tag, f"{start:%Y%m%dT%H%M}", f"{end:%Y%m%dT%H%M}") if part)
    return source_dir(out_dir, source, tag, daily) / f"{stem}.{ext}"


def to_rgba(data: np.ndarray, mode: str) -> np.ndarray:
    """Turn L/LA/RGB/RGBA (rows, cols, bands) uint8 data into RGBA."""
    if mode in ("L", "LA"):
        data = data[..., [0, 0, 0] + ([1] if mode == "LA" else [])]
    if data.shape[-1] == 3:
        data = np.concatenate([data, np.full(data.shape[:2] + (1,), 255, np.uint8)], axis=-1)
    return np.ascontiguousarray(data, dtype=np.uint8)


def swath_slices(lons, lats, bounds: tuple[float, float, float, float],
                 step: int = 8) -> tuple[slice, slice]:
    """Rows and columns of a swath whose pixels fall inside a lon/lat box (checked every
    `step` pixels, with one step of margin)."""
    lon_min, lat_min, lon_max, lat_max = bounds
    lo, la = np.asarray(lons[::step, ::step]), np.asarray(lats[::step, ::step])
    inside = (lo >= lon_min) & (lo <= lon_max) & (la >= lat_min) & (la <= lat_max)
    if not inside.any():
        raise RuntimeError("the swath does not cover the area")
    rows, cols = np.flatnonzero(inside.any(axis=1)), np.flatnonzero(inside.any(axis=0))
    return (slice(max((rows[0] - 1) * step, 0), (rows[-1] + 2) * step),
            slice(max((cols[0] - 1) * step, 0), (cols[-1] + 2) * step))


def satpy_image(reader: str, files: list[Path], composite: str, area: AreaDef, instrument: str, *,
                platform: str | None = None, resampler: str = "nearest",
                load_kwargs: dict | None = None) -> Image:
    """Load a composite with Satpy, resample it to the area and apply Satpy's default enhancement.

    Satpy handles calibration, solar-zenith and Rayleigh corrections, and FCI's green-band
    simulation as part of the composite.
    """
    import dask
    from pyresample.geometry import SwathDefinition
    from satpy import Scene

    from satview.areas import lonlat_bounds

    # The HDF5 library bundled with the netCDF4 wheels is not thread-safe: dask's default
    # threaded scheduler fails with "NetCDF: HDF error" or crashes. The area is small, so
    # computing in one thread costs little. Smaller chunks lower peak memory (and time).
    with dask.config.set({"scheduler": "single-threaded", "array.chunk-size": "32MiB"}):
        scn = Scene(reader=reader, filenames=[str(f) for f in files])
        # Channels first, composite later: polar swaths are cut down to the area before the
        # Rayleigh correction runs. Satpy only does this itself for gridded (geostationary)
        # data, and whole OLCI/VIIRS swaths need several GB of memory.
        scn.load([composite], generate=False, **(load_kwargs or {}))
        swath = next((ds.attrs["area"] for ds in scn.values()
                      if isinstance(ds.attrs.get("area"), SwathDefinition)), None)
        if swath is not None:
            scn = scn.slice(swath_slices(swath.lons, swath.lats, lonlat_bounds(area)))
        scn.generate_possible_composites(unload=True)
        if composite not in scn:
            raise RuntimeError(f"Satpy could not build {composite!r} from {len(files)} file(s); "
                               f"missing: {scn.missing_datasets}")
        dataset = scn.resample(area, resampler=resampler)[composite]
        rgba = enhanced_rgba(dataset)
    platform = platform or dataset.attrs.get("platform_name", "")
    title = f"{platform} {instrument} · {composite.replace('_', ' ')}".strip()
    return Image(rgba, area, as_utc(dataset.attrs["start_time"]), title)


def enhanced_rgba(dataset) -> np.ndarray:
    """Satpy's default enhancement for the dataset's name and standard_name, as RGBA."""
    from satpy.enhancements.enhancer import get_enhanced_image

    data, mode = get_enhanced_image(dataset).finalize(fill_value=None)
    return to_rgba(np.asarray(data.transpose("y", "x", "bands").values), mode)


def reflectance_image(rgb: np.ndarray, area: AreaDef, time: datetime, title: str) -> Image:
    """Image from (3, rows, cols) red, green, blue reflectances (0-1, NaN = no data), enhanced
    like Satpy's true_color composites so it matches the swath sources.

    Its log stretch keeps bright desert (~0.3) well below cloud (~0.8); ESA's ready-made
    Sentinel-2 true color image saturates both."""
    import dask
    import dask.array as da
    import xarray as xr

    # Block by block: the enhancement makes several float64 copies, ~1 GB for a 100 m grid at once.
    with dask.config.set(scheduler="single-threaded"):
        dataset = xr.DataArray(da.from_array(rgb, chunks=(3, 1024, 1024)) * 100,  # Satpy uses %
                               dims=("bands", "y", "x"), coords={"bands": ["R", "G", "B"]},
                               attrs={"name": "true_color", "standard_name": "true_color"})
        return Image(enhanced_rgba(dataset), area, time, title)


def resample_latlon(rgba: np.ndarray, bounds: tuple[float, float, float, float], area: AreaDef,
                    method: str = "bilinear") -> np.ndarray:
    """Resample an RGBA image on a regular lon/lat grid (e.g. a WMS response) to the area."""
    from scipy.ndimage import map_coordinates

    lon_min, lat_min, lon_max, lat_max = bounds
    rows, cols = rgba.shape[:2]
    lons, lats = area.get_lonlats()
    col = (lons - lon_min) / (lon_max - lon_min) * cols - 0.5
    row = (lat_max - lats) / (lat_max - lat_min) * rows - 0.5
    order = 1 if method == "bilinear" else 0
    bands = [map_coordinates(rgba[..., b].astype(np.float32), [row, col], order=order, mode="constant", cval=0)
             for b in range(4)]
    return np.clip(np.rint(np.stack(bands, axis=-1)), 0, 255).astype(np.uint8)


def enhance(image: Image, tweak: Enhance) -> Image:
    """Optional per-source gamma/contrast/brightness on top of Satpy's enhancement."""
    if (tweak.gamma, tweak.contrast, tweak.brightness) == (1.0, 1.0, 1.0):
        return image
    rgb = image.data[..., :3].astype(np.float32) / 255.0
    rgb = rgb ** (1.0 / tweak.gamma)
    rgb = ((rgb - 0.5) * tweak.contrast + 0.5) * tweak.brightness
    data = image.data.copy()
    data[..., :3] = np.clip(np.rint(rgb * 255.0), 0, 255).astype(np.uint8)
    return Image(data, image.area, image.time, image.title, image.daily)


def process(source: Source, granule: Granule, cfg: Config, area: AreaDef,
            overlays: render.OverlayData | None = None, force: bool = False) -> Result:
    """Turn one granule into output files, skipping work that is already done or pointless."""
    out = cfg.output
    paths = {fmt: output_path(out.dir, granule, EXTENSIONS[fmt]) for fmt in out.scene_formats}
    if not force and all(p.exists() for p in paths.values()):
        log.info("%s %s: already rendered, skipping", source.name, granule.label)
        return Result(granule, "exists", paths.get("png"))
    if not granule.daily:
        elevation = sun_elevation(granule.time, *region_of(area).center)
        if elevation < MIN_SUN_ELEVATION:
            log.info("%s %s: skipped, sun elevation is %.1f° (night)",
                     source.name, granule.label, elevation)
            return Result(granule, "night")

    started = time.monotonic()
    files = source.download(granule, out.cache_dir / source.name)
    image = enhance(source.load(files, area, granule), cfg.source(source.name).enhance)
    if "png" in paths:
        render.save_png(render.render(image, out, overlays), paths["png"])
    if "geotiff" in paths:
        render.save_geotiff(image, paths["geotiff"])
    log.info("%s %s: saved %s (%.0fs)", source.name, granule.label,
             ", ".join(str(p) for p in paths.values()), time.monotonic() - started)
    return Result(granule, "saved", paths.get("png"))


def animate(source_name: str, results: list[Result], out: OutputConfig, force: bool = False) -> list[Path]:
    """Build a GIF/MP4 per tag from the range's PNGs, including ones rendered by earlier runs.

    An animation newer than all its frames is left alone.
    """
    groups: dict[str, list[Result]] = defaultdict(list)
    for r in results:
        if r.png is not None and r.png.exists():
            groups[r.granule.tag].append(r)
    written = []
    for tag, frames in groups.items():
        if len(frames) < 2:
            log.info("%s: only %d frame(s) for %s, no animation", source_name, len(frames), tag or "range")
            continue
        frames.sort(key=lambda r: r.granule.time)
        newest_frame = max(r.png.stat().st_mtime for r in frames)
        for fmt in out.animation_formats:
            path = animation_path(out.dir, source_name, tag, frames[0].granule.time,
                                  frames[-1].granule.time, EXTENSIONS[fmt], frames[0].granule.daily)
            if not force and path.exists() and path.stat().st_mtime >= newest_frame:
                log.info("%s: %s is up to date", source_name, path.name)
                written.append(path)
                continue
            render.animate([r.png for r in frames], path, out.animation_fps)
            log.info("%s: animation with %d frames saved to %s", source_name, len(frames), path)
            written.append(path)
    return written
