from datetime import datetime, timezone
from pathlib import Path

from satview.cache import granule_dir
from satview.pipeline import animation_path, output_path
from satview.sources.base import Granule

OUT = Path("/out")
T = datetime(2026, 10, 30, 10, 7, 12, tzinfo=timezone.utc)


def test_scene_path():
    g = Granule("meteosat12_fci", "id", T)
    assert output_path(OUT, g, "png") == OUT / "meteosat12_fci/2026-10-30/meteosat12_fci_20261030T1007.png"


def test_scene_path_with_tag():
    g = Granule("viirs_l1b", "id", T, tag="noaa20")
    assert output_path(OUT, g, "tif") == OUT / "viirs_l1b/2026-10-30/viirs_l1b_noaa20_20261030T1007.tif"


def test_daily_scene_path_has_no_time():
    g = Granule("viirs_gibs", "id", T.replace(hour=0, minute=0), tag="snpp", daily=True)
    assert output_path(OUT, g, "png") == OUT / "viirs_gibs/2026-10-30/viirs_gibs_snpp_20261030.png"


def test_animation_path():
    end = T.replace(hour=14, minute=0)
    assert animation_path(OUT, "meteosat9_iodc", "", T, end, "gif") == (
        OUT / "meteosat9_iodc/meteosat9_iodc_20261030T1007_20261030T1400.gif")


def test_labels():
    assert Granule("s", "id", T, tag="noaa20").label == "noaa20 2026-10-30 10:07Z"
    assert Granule("s", "id", T, daily=True).label == "2026-10-30"


def test_granule_dir_is_path_safe():
    product = "W_XX-EUMETSAT-Darmstadt,IMG+SAT,MTI1+FCI-1C-RRAD-FDHSI-FD--x-x---x_C_EUMT_2026"
    assert granule_dir(Path("/c"), product).name == product.replace(",", "_")
    assert granule_dir(Path("/c"), "a/b c").name == "a_b_c"
