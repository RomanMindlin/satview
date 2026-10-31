"""Turn an Image into a picture: coastlines, borders, cities, label; save PNG/GeoTIFF; animate.

Overlays come from Natural Earth 1:10m shapefiles, downloaded once into the cache.
"""

from __future__ import annotations

import logging
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import shapefile
from PIL import Image as PILImage
from PIL import ImageDraw, ImageFont
from pyproj import Transformer

from satview import net
from satview.areas import lonlat_bounds
from satview.config import OutputConfig, Overlays
from satview.sources.base import AreaDef, Image

log = logging.getLogger(__name__)

NATURAL_EARTH_URL = "https://naciscdn.org/naturalearth/10m/{}.zip"
NATURAL_EARTH_LAYERS = {
    "coastline": "physical/ne_10m_coastline",
    "lakes": "physical/ne_10m_lakes",  # Sea of Galilee, Dead Sea
    "borders": "cultural/ne_10m_admin_0_boundary_lines_land",
    "places": "cultural/ne_10m_populated_places_simple",
}
MIN_CITY_POPULATION = 40_000

BACKGROUND = (0, 0, 0)
COASTLINE_COLOR = (255, 215, 64, 230)
BORDER_COLOR = (255, 255, 255, 170)
CITY_COLOR = (255, 255, 255, 255)
SUPERSAMPLE = 2  # overlays are drawn larger and scaled down, for smooth lines


@dataclass
class OverlayData:
    coastlines: list[np.ndarray] = field(default_factory=list)  # (n, 2) lon/lat polylines
    borders: list[np.ndarray] = field(default_factory=list)
    cities: list[tuple[str, float, float]] = field(default_factory=list)  # name, lon, lat


def _shapefile(data_dir: Path, layer: str) -> Path:
    name = NATURAL_EARTH_LAYERS[layer].rsplit("/", 1)[-1]
    shp = data_dir / name / f"{name}.shp"
    if not shp.exists():
        archive = net.download(net.session(), NATURAL_EARTH_URL.format(NATURAL_EARTH_LAYERS[layer]),
                               data_dir / f"{name}.zip")
        with zipfile.ZipFile(archive) as z:
            for member in z.namelist():
                if Path(member).suffix in (".shp", ".shx", ".dbf", ".prj", ".cpg"):
                    (data_dir / name).mkdir(parents=True, exist_ok=True)
                    (data_dir / name / Path(member).name).write_bytes(z.read(member))
        archive.unlink()
    return shp


def _intersects(bbox, bounds) -> bool:
    return not (bbox[2] < bounds[0] or bbox[0] > bounds[2] or bbox[3] < bounds[1] or bbox[1] > bounds[3])


def _lines(shp: Path, bounds) -> list[np.ndarray]:
    lines = []
    with shapefile.Reader(str(shp)) as reader:
        for shape in reader.iterShapes():
            if not shape.points or not _intersects(shape.bbox, bounds):
                continue
            points = np.asarray(shape.points, dtype=float)
            parts = list(shape.parts) + [len(points)]
            lines.extend(points[a:b] for a, b in zip(parts[:-1], parts[1:]) if b - a > 1)
    return lines


def _cities(shp: Path, bounds) -> list[tuple[str, float, float]]:
    cities = []
    with shapefile.Reader(str(shp)) as reader:
        for rec in reader.iterRecords(fields=["name", "pop_max", "longitude", "latitude"]):
            if rec["pop_max"] >= MIN_CITY_POPULATION and _intersects(
                    (rec["longitude"], rec["latitude"]) * 2, bounds):
                cities.append((rec["name"], rec["longitude"], rec["latitude"]))
    return cities


def load_overlays(data_dir: Path, area: AreaDef, which: Overlays) -> OverlayData:
    lon_min, lat_min, lon_max, lat_max = lonlat_bounds(area)
    bounds = (lon_min - 0.5, lat_min - 0.5, lon_max + 0.5, lat_max + 0.5)
    data = OverlayData()
    if which.coastlines:
        data.coastlines = _lines(_shapefile(data_dir, "coastline"), bounds) + _lines(
            _shapefile(data_dir, "lakes"), bounds)
    if which.borders:
        data.borders = _lines(_shapefile(data_dir, "borders"), bounds)
    if which.cities:
        data.cities = _cities(_shapefile(data_dir, "places"), lonlat_bounds(area))
    return data


def _projector(area: AreaDef, width: int, height: int):
    """lon/lat arrays -> pixel x/y arrays on a picture of the area at the given size."""
    to_xy = Transformer.from_crs("EPSG:4326", area.crs, always_xy=True)
    xmin, ymin, xmax, ymax = area.area_extent

    def project(lons, lats):
        x, y = to_xy.transform(lons, lats)
        return (x - xmin) / (xmax - xmin) * width, (ymax - y) / (ymax - ymin) * height

    return project


def _font(size: int) -> ImageFont.FreeTypeFont:
    # Pillow's bundled font, so pictures look the same on every machine.
    return ImageFont.load_default(size=size)


def _draw_overlays(picture: PILImage.Image, area: AreaDef, overlays: OverlayData,
                   which: Overlays) -> PILImage.Image:
    width, height = picture.size
    s = SUPERSAMPLE
    layer = PILImage.new("RGBA", (width * s, height * s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    project = _projector(area, width * s, height * s)
    line_width = max(1, round(width / 800 * s))

    for enabled, lines, color in ((which.borders, overlays.borders, BORDER_COLOR),
                                  (which.coastlines, overlays.coastlines, COASTLINE_COLOR)):
        if not enabled:
            continue
        for line in lines:
            x, y = project(line[:, 0], line[:, 1])
            draw.line(list(zip(x.tolist(), y.tolist())), fill=color, width=line_width, joint="curve")

    if which.cities and overlays.cities:
        font = _font(max(10, round(width / 75)) * s)
        radius = max(2, round(width / 400)) * s
        for name, lon, lat in overlays.cities:
            x, y = project(np.array([lon]), np.array([lat]))
            x, y = float(x[0]), float(y[0])
            draw.ellipse([x - radius, y - radius, x + radius, y + radius], fill=CITY_COLOR,
                         outline=(0, 0, 0, 255), width=s)
            draw.text((x + 2 * radius, y), name, font=font, fill=CITY_COLOR, anchor="lm",
                      stroke_width=s, stroke_fill=(0, 0, 0, 255))

    layer = layer.resize((width, height), PILImage.Resampling.LANCZOS)
    return PILImage.alpha_composite(picture.convert("RGBA"), layer).convert("RGB")


def _draw_label(picture: PILImage.Image, text: str) -> None:
    width, height = picture.size
    font = _font(max(12, round(width / 50)))
    pad = round(font.size * 0.5)
    draw = ImageDraw.Draw(picture, "RGBA")
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    x, y = pad, height - pad - (bottom - top) - 2 * pad
    draw.rectangle([0, y, x + (right - left) + 2 * pad, height], fill=(0, 0, 0, 150))
    draw.text((x + pad - left, y + pad - top), text, font=font, fill=(255, 255, 255))


def render(image: Image, out: OutputConfig, overlays: OverlayData | None = None) -> PILImage.Image:
    rgba = PILImage.fromarray(image.data, "RGBA")
    picture = PILImage.new("RGB", rgba.size, BACKGROUND)
    picture.paste(rgba, mask=rgba.getchannel("A"))
    if out.width_px and out.width_px != picture.width:
        height = round(picture.height * out.width_px / picture.width)
        picture = picture.resize((out.width_px, height), PILImage.Resampling.LANCZOS)
    if overlays is not None and out.overlays.any:
        picture = _draw_overlays(picture, image.area, overlays, out.overlays)
    if out.label:
        _draw_label(picture, image.label)
    return picture


def _part(path: Path) -> Path:
    """Temporary sibling for atomic writes; keeps the extension so writers can infer the format."""
    path.parent.mkdir(parents=True, exist_ok=True)
    return path.with_name(f"{path.stem}.part{path.suffix}")


def save_png(picture: PILImage.Image, path: Path) -> None:
    part = _part(path)
    picture.save(part, format="PNG")
    part.replace(path)


def save_geotiff(image: Image, path: Path) -> None:
    """Georeferenced RGBA at the area's native resolution, without overlays or label."""
    import rasterio
    from rasterio.enums import ColorInterp
    from rasterio.transform import from_bounds

    rows, cols = image.data.shape[:2]
    part = _part(path)
    with rasterio.open(part, "w", driver="GTiff", width=cols, height=rows, count=4, dtype="uint8",
                       crs=image.area.crs.to_wkt(), transform=from_bounds(*image.area.area_extent, cols, rows),
                       photometric="RGB", compress="deflate") as dst:
        dst.write(np.moveaxis(image.data, -1, 0))
        dst.colorinterp = [ColorInterp.red, ColorInterp.green, ColorInterp.blue, ColorInterp.alpha]
        dst.update_tags(TIFFTAG_DATETIME=f"{image.time:%Y:%m:%d %H:%M:%S}", TIFFTAG_IMAGEDESCRIPTION=image.label)
    part.replace(path)


def animate(frames: list[Path], path: Path, fps: float) -> None:
    import imageio.v2 as imageio

    images = [np.asarray(PILImage.open(f).convert("RGB")) for f in frames]
    part = _part(path)
    if path.suffix == ".gif":
        imageio.mimsave(part, images, format="GIF", duration=1000 / fps, loop=0)
    else:
        # H.264 with yuv420p needs even dimensions.
        rows, cols = (images[0].shape[0] // 2) * 2, (images[0].shape[1] // 2) * 2
        imageio.mimsave(part, [im[:rows, :cols] for im in images], format="FFMPEG", fps=fps,
                        codec="libx264", pixelformat="yuv420p", macro_block_size=2, quality=8,
                        ffmpeg_log_level="error")
    part.replace(path)
