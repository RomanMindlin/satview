"""Sources served as map-projected tiles from a STAC catalogue: Sentinel-2 and Landsat.

Each tile's bands are cloud-optimized GeoTIFFs holding 10-30 m pixels for a 110-185 km
square, plus overviews (reduced-resolution copies). Only the part covering the area is
read over HTTP, from the overview just finer than the target grid, so a pass costs tens
of MB instead of several GB. Subclasses only name the catalogue and platforms.
"""

from __future__ import annotations

import logging
import math
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

from satview import net
from satview.areas import AREAS, DEFAULT_AREA, lonlat_bounds, region_of
from satview.cache import cached_file, granule_dir
from satview.config import HighResConfig, parse_utc
from satview.pipeline import reflectance_image
from satview.sources.base import AreaDef, Granule, Image, TimeSpec, utcnow
from satview.sources.polar import Record, choose_passes, geojson_polygons, group_passes, search_window

log = logging.getLogger(__name__)

BANDS = ("red", "green", "blue")  # asset names, the same in both catalogues
WORKERS = 8  # tiles read in parallel; each read is a few HTTP range requests
# Do not probe for sidecar files on open, and retry transient HTTP errors.
GDAL_ENV = {"GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR", "GDAL_HTTP_MAX_RETRY": "4",
            "GDAL_HTTP_RETRY_DELAY": "2", "GDAL_HTTP_MULTIRANGE": "YES",
            "GDAL_HTTP_MERGE_CONSECUTIVE_RANGES": "YES"}


def _iso(t: datetime) -> str:
    return f"{t:%Y-%m-%dT%H:%M:%SZ}"


def search(http, url: str, collection: str, start: datetime, end: datetime,
           bbox: tuple[float, float, float, float]) -> list[dict]:
    """Items of a collection over a lon/lat box observed between start and end, following result pages."""
    body = {"collections": [collection], "bbox": list(bbox), "datetime": f"{_iso(start)}/{_iso(end)}",
            "limit": 100}
    request: dict | None = {"href": f"{url}/search", "method": "POST", "body": body}
    items: list[dict] = []
    while request:
        body = request.get("body")
        r = http.request(request.get("method", "GET"), request["href"], json=body, timeout=net.TIMEOUT)
        r.raise_for_status()
        page = r.json()
        items.extend(page["features"])
        request = next((link for link in page.get("links", []) if link.get("rel") == "next"), None)
        if request and request.get("merge"):  # the link's body only holds what changes
            request = {**request, "body": {**body, **request.get("body", {})}}
    return items


def scaling(asset: dict) -> tuple[float, float]:
    """Scale and offset from stored values to reflectance (STAC raster extension)."""
    band = (asset.get("raster:bands") or [{}])[0]
    return band.get("scale", 1.0), band.get("offset", 0.0)


def read_tile(hrefs: list[str], scales: list[tuple[float, float]],
              bounds: tuple[float, float, float, float], resolution: float) -> bytes:
    """A GeoTIFF with the part of single-band files (all on one grid) inside a lon/lat box,
    read from the coarsest overview still at least as fine as `resolution` metres.

    Values are kept as stored (nodata 0); the scales and offsets go into the file.
    """
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.io import MemoryFile
    from rasterio.transform import Affine
    from rasterio.warp import transform_bounds
    from rasterio.windows import Window, from_bounds

    with rasterio.Env(**GDAL_ENV), ExitStack() as stack:
        datasets = [stack.enter_context(rasterio.open(href)) for href in hrefs]
        src = datasets[0]
        factor = max([1] + [f for f in src.overviews(1) if f * src.res[0] <= resolution])
        box = from_bounds(*transform_bounds("EPSG:4326", src.crs, *bounds, densify_pts=21), src.transform)
        # Whole overview pixels, within the file.
        col0 = max(math.floor(box.col_off / factor) * factor, 0)
        row0 = max(math.floor(box.row_off / factor) * factor, 0)
        col1 = min(math.ceil((box.col_off + box.width) / factor) * factor, src.width)
        row1 = min(math.ceil((box.row_off + box.height) / factor) * factor, src.height)
        window = Window(col0, row0, col1 - col0, row1 - row0)
        rows, cols = math.ceil(window.height / factor), math.ceil(window.width / factor)
        data = np.stack([ds.read(1, window=window, out_shape=(rows, cols), resampling=Resampling.nearest)
                         for ds in datasets])
        transform = src.window_transform(window) @ Affine.scale(window.width / cols, window.height / rows)
        crs, nodata = src.crs, src.nodata or 0

    with MemoryFile() as memory:
        with memory.open(driver="GTiff", width=cols, height=rows, count=len(datasets), dtype=data.dtype,
                         crs=crs, transform=transform, nodata=nodata, compress="deflate") as dst:
            dst.write(data)
            dst.scales = [scale for scale, _ in scales]
            dst.offsets = [offset for _, offset in scales]
        return memory.read()


def mosaic(files: list[Path], area: AreaDef) -> np.ndarray:
    """Cached tiles warped onto the area: (bands, rows, cols) reflectance, NaN where no tile
    has data. Where tiles overlap (neighbours, or one tile split over two products) the
    first one wins."""
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.transform import from_bounds
    from rasterio.warp import reproject

    dst_transform = from_bounds(*area.area_extent, area.width, area.height)
    out = np.full((len(BANDS), area.height, area.width), np.nan, np.float32)
    for path in files:
        with rasterio.open(path) as src:
            warped = np.full(out.shape, src.nodata, src.dtypes[0])
            reproject(src.read(), warped, src_transform=src.transform, src_crs=src.crs, src_nodata=src.nodata,
                      dst_transform=dst_transform, dst_crs=area.crs.to_wkt(), dst_nodata=src.nodata,
                      resampling=Resampling.average)
            fill = np.isnan(out[0]) & (warped != src.nodata).all(axis=0)
            for band, (scale, offset) in enumerate(zip(src.scales, src.offsets)):
                out[band][fill] = warped[band][fill] * scale + offset
    return out


class TileSource:
    """Passes of a STAC collection of tiles; subclasses set the class attributes."""

    name: str
    geostationary = False
    catalogue: str  # STAC API root
    collection: str
    instrument: str  # for the picture title
    platforms: dict[str, str]  # STAC `platform` -> tag, e.g. sentinel-2a -> S2A
    platform_names: dict[str, str]  # tag -> name in the title
    lookback: timedelta  # how far back `latest` searches: revisit time plus publishing delay
    tolerance: timedelta  # how far from --at a pass may be

    def __init__(self, cfg: HighResConfig):
        self.cfg = cfg
        self.http = net.session()

    def sign(self, href: str) -> str:
        """URL to read a file from; catalogues that need a token override this."""
        return href

    def _search(self, start: datetime, end: datetime, bbox: tuple[float, float, float, float]) -> list[dict]:
        return search(self.http, self.catalogue, self.collection, start, end, bbox)

    def find(self, when: TimeSpec, area: AreaDef) -> list[Granule]:
        wanted = {platform: tag for platform, tag in self.platforms.items() if tag in self.cfg.platforms}
        region = region_of(area)
        records = []
        for item in self._search(*search_window(when, self.lookback, self.tolerance), region.bbox):
            tag = wanted.get(item["properties"].get("platform", "").lower())
            if tag:
                t = parse_utc(item["properties"]["datetime"])
                records.append(Record(tag, t, t, geojson_polygons(item.get("geometry")), item))
        passes = choose_passes(group_passes(records, timedelta(minutes=2), region), when,
                               self.cfg.min_coverage, self.cfg.pick)
        for p in passes:
            log.info("%s: %s pass at %s covers %.0f%% of the area (%d tiles)", self.name, p.platform,
                     f"{p.start:%Y-%m-%d %H:%M}Z", p.coverage * 100, len(p.items))
        return [Granule(self.name, f"{p.platform}_{p.start:%Y%m%dT%H%M}", p.start, tag=p.platform,
                        payload=(p, area)) for p in passes]

    def download(self, granule: Granule, cache: Path) -> list[Path]:
        p, area = granule.payload
        resolution = area.pixel_size_x
        # The grid's extent (km) and spacing are part of the name: another or changed area needs
        # another window, a finer grid a finer overview.
        extent = "_".join(f"{v / 1000:g}" for v in area.area_extent)
        paths = [granule_dir(cache, granule.id) / f"{item['id']}_{extent}_{resolution:g}m.tif"
                 for item in p.items]
        todo = [(path, item) for path, item in zip(paths, p.items) if not path.exists()]
        if not todo:
            return paths
        log.info("%s %s: reading %d tile(s) for a %g m grid", self.name, granule.label, len(todo), resolution)
        bounds = lonlat_bounds(area)
        # Signed here, not in the threads, so a token is fetched once.
        jobs = [(path, [self.sign(item["assets"][band]["href"]) for band in BANDS],
                 [scaling(item["assets"][band]) for band in BANDS]) for path, item in todo]

        def fetch(job) -> Path:
            path, hrefs, scales = job
            return net.retry(lambda: cached_file(path, lambda out: out.write(
                read_tile(hrefs, scales, bounds, resolution))), f"read {path.stem}")

        with ThreadPoolExecutor(WORKERS) as pool:
            list(pool.map(fetch, jobs))
        return paths

    def load(self, files: list[Path], area: AreaDef, granule: Granule) -> Image:
        title = f"{self.platform_names[granule.tag]} {self.instrument} · true color"
        return reflectance_image(mosaic(files, area), area, granule.time, title)

    def check(self) -> str:
        now = utcnow()
        found = len(self._search(now - self.lookback, now, AREAS[DEFAULT_AREA].bbox))
        return f"{found} {self.collection} tiles over the area in the last {self.lookback.days} days"
