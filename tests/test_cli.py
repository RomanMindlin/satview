import pytest
from PIL import Image as PILImage

from satview import cli, sources
from satview.sources.base import Granule

from test_pipeline import FakeSource, NOON


class Broken:
    name = "meteosat9_iodc"
    geostationary = True

    def find(self, when, area):
        raise ConnectionError("data store down")


@pytest.fixture
def config_path(write_config, tmp_path):
    return write_config(f"output: {{dir: {tmp_path / 'out'}, overlays: {{coastlines: false, borders: false}}}}\n")


@pytest.fixture
def fake_sources(monkeypatch):
    class Working(FakeSource):
        def find(self, when, area):
            return [Granule("viirs_gibs", "fake", NOON)]

    def build(name, cfg):
        return Working() if name == "viirs_gibs" else Broken()

    monkeypatch.setattr(sources, "build", build)


def test_one_source_failing_still_exits_zero(config_path, fake_sources, tmp_path, monkeypatch):
    monkeypatch.setenv("EUMETSAT_KEY", "k")
    monkeypatch.setenv("EUMETSAT_SECRET", "s")
    code = cli.main(["run", "-c", str(config_path), "--source", "viirs_gibs", "--source", "meteosat9_iodc"])
    assert code == 0
    assert (tmp_path / "out/viirs_gibs/2026-10-29/viirs_gibs_20261029T1000.png").exists()


def test_area_flag_draws_the_other_area_into_its_own_folder(config_path, fake_sources, tmp_path):
    assert cli.main(["run", "-c", str(config_path), "--source", "viirs_gibs", "--area", "israel_north"]) == 0
    png = tmp_path / "out/israel_north/viirs_gibs/2026-10-29/viirs_gibs_20261029T1000.png"
    assert PILImage.open(png).size == (1200, 1757)  # the israel picture is 1200 x 2594
    assert not (tmp_path / "out/viirs_gibs").exists()


def test_all_sources_failing_exits_one(config_path, fake_sources, monkeypatch):
    monkeypatch.setenv("EUMETSAT_KEY", "k")
    monkeypatch.setenv("EUMETSAT_SECRET", "s")
    assert cli.main(["run", "-c", str(config_path), "--source", "meteosat9_iodc"]) == 1


def test_missing_credentials_exits_two(config_path, fake_sources, no_credentials, caplog):
    assert cli.main(["run", "-c", str(config_path), "--source", "meteosat12_fci"]) == 2
    assert "EUMETSAT_KEY is not set" in caplog.text


def test_no_sources_enabled_exits_two(config_path, caplog):
    assert cli.main(["run", "-c", str(config_path)]) == 2
    assert "no sources enabled" in caplog.text


def test_overrides_from_flags(tmp_path):
    args = cli.build_parser().parse_args(
        ["run", "--start", "2026-10-30T06:00Z", "--end", "2026-10-30T14:00Z", "--step", "15",
         "--format", "png", "--format", "mp4", "--set", "output.width_px=800", "--set", "output.label=false",
         "--area", "israel_north"])
    assert cli.overrides(args) == {
        "output.width_px": 800, "output.label": False, "time.mode": "range", "area": "israel_north",
        "time.range.start": "2026-10-30T06:00Z", "time.range.end": "2026-10-30T14:00Z",
        "time.range.step_minutes": 15, "output.formats": ["png", "mp4"]}


@pytest.mark.parametrize("argv", [["-v", "-c", "x.yaml", "run"], ["run", "-v", "-c", "x.yaml"]])
def test_common_options_before_or_after_the_command(argv):
    args = cli.build_parser().parse_args(argv)
    assert (args.verbose, str(args.config), args.set) == (1, "x.yaml", [])


@pytest.mark.parametrize("argv, error", [
    (["run", "--at", "2026-10-30T10:00Z", "--start", "2026-10-30T09:00Z"], "either --at or --start/--end"),
    (["run", "--start", "2026-10-30T09:00Z"], "must be given together"),
    (["run", "--set", "output.width_px"], "KEY=VALUE"),
])
def test_bad_flags(argv, error):
    with pytest.raises(cli.ConfigError, match=error):
        cli.overrides(cli.build_parser().parse_args(argv))


def test_list_sources_offline(config_path, no_credentials, capsys):
    assert cli.main(["list-sources", "-c", str(config_path), "--offline"]) == 0
    out = capsys.readouterr().out
    assert "viirs_gibs       disabled  credentials: none needed" in out
    assert "meteosat12_fci   disabled  credentials: missing EUMETSAT_KEY, EUMETSAT_SECRET" in out
    assert "sentinel2_msi    disabled  credentials: none needed" in out
    assert "landsat_oli      disabled  credentials: none needed" in out


def test_high_resolution_sources_get_a_finer_grid(write_config):
    cfg = cli.config.load(write_config("sources:\n  landsat_oli: {resolution_m: 200}\n"))
    assert cli.source_area(cfg, "meteosat12_fci").pixel_size_x == 500
    assert cli.source_area(cfg, "sentinel2_msi").pixel_size_x == 100
    assert cli.source_area(cfg, "landsat_oli").pixel_size_x == 200
    assert {cli.source_area(cfg, name).area_id for name in ("meteosat12_fci", "landsat_oli")} == {"israel"}


def test_selected_area_applies_to_every_source(write_config):
    cfg = cli.config.load(write_config("area: israel_north\n"))
    for name in ("meteosat12_fci", "sentinel2_msi"):
        assert cli.source_area(cfg, name).area_id == "israel_north"


def test_list_sources_flags_enabled_source_without_credentials(write_config, no_credentials, capsys):
    path = write_config("sources:\n  sentinel3_olci: {enabled: true}\n")
    assert cli.main(["list-sources", "-c", str(path), "--offline"]) == 1


def test_clean(config_path, capsys):
    assert cli.main(["clean", "-c", str(config_path), "--dry-run"]) == 0
    assert "would remove 0 file(s)" in capsys.readouterr().out
