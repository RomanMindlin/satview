from datetime import datetime, timedelta, timezone

import pytest

from satview import config, sources
from satview.config import SOURCE_NAMES, ConfigError

def test_example_config_is_valid(example_config):
    cfg = config.load(example_config)
    assert cfg.enabled_sources() == ["meteosat12_fci", "meteosat9_iodc", "viirs_gibs"]
    assert cfg.sources.meteosat12_fci.composite == "true_color"
    assert cfg.time.mode == "latest"
    assert cfg.output.formats == ["png"]
    # relative paths are resolved against the config's folder
    assert cfg.output.dir == example_config.parent.resolve() / "output"


def test_every_config_source_has_an_adapter():
    assert set(SOURCE_NAMES) == set(sources.ADAPTERS)


def test_empty_config_uses_defaults(write_config):
    cfg = config.load(write_config(""))
    assert cfg.enabled_sources() == []
    assert cfg.output.width_px == 1200


def test_missing_file_is_a_clear_error(tmp_path):
    with pytest.raises(ConfigError, match="config file not found"):
        config.load(tmp_path / "nope.yaml")


def test_unknown_source(write_config):
    with pytest.raises(ConfigError, match=r"sources\.goes16: unknown source \(available: meteosat12_fci"):
        config.load(write_config("sources:\n  goes16: {enabled: true}\n"))


def test_unknown_key_in_a_source(write_config):
    with pytest.raises(ConfigError, match=r"sources\.viirs_gibs\.layer: Extra inputs"):
        config.load(write_config("sources:\n  viirs_gibs: {layer: x}\n"))


def test_bad_composite(write_config):
    with pytest.raises(ConfigError, match=r"sources\.meteosat9_iodc\.composite"):
        config.load(write_config("sources:\n  meteosat9_iodc: {composite: true_color}\n"))


def test_high_resolution_grid(write_config):
    cfg = config.load(write_config("sources:\n  landsat_oli: {resolution_m: 60}\n"))
    assert cfg.sources.landsat_oli.resolution_m == 60
    assert cfg.sources.sentinel2_msi.resolution_m == 100
    with pytest.raises(ConfigError, match=r"sources\.sentinel2_msi\.resolution_m"):
        config.load(write_config("sources:\n  sentinel2_msi: {resolution_m: 10}\n"))
    with pytest.raises(ConfigError, match=r"sources\.sentinel2_msi\.platforms"):
        config.load(write_config("sources:\n  sentinel2_msi: {platforms: [S3A]}\n"))


def test_area(write_config, tmp_path):
    cfg = config.load(write_config(""))
    assert cfg.area == "israel" and cfg.output.dir == tmp_path / "output"
    cfg = config.load(write_config("area: israel_north\n"))
    assert cfg.output.dir == tmp_path / "output" / "israel_north"  # other areas get their own folder
    with pytest.raises(ConfigError, match=r"area: unknown area 'gaza' \(available: israel, israel_north\)"):
        config.load(write_config("area: gaza\n"))


def test_hrfi_is_red_only(write_config):
    with pytest.raises(ConfigError, match="only has the red channel"):
        config.load(write_config("sources:\n  meteosat12_fci: {resolution: 500m}\n"))
    cfg = config.load(write_config("sources:\n  meteosat12_fci: {resolution: 500m, composite: vis_06}\n"))
    assert cfg.sources.meteosat12_fci.resolution == "500m"


@pytest.mark.parametrize("text", ["2026-10-30T10:00Z", "2026-10-30T10:00:00Z", "2026-10-30 10:00",
                                  "2026-10-30T12:00+02:00"])
def test_time_formats(write_config, text):
    cfg = config.load(write_config(f"time: {{mode: at, at: '{text}'}}\n"))
    assert cfg.time.at == datetime(2026, 10, 30, 10, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("text, error", [
    ("time: {mode: at}", "mode 'at' needs time.at"),
    ("time: {mode: at, at: 'yesterday'}", "invalid time 'yesterday'"),
    ("time: {mode: at, at: '2999-01-01T00:00Z'}", "is in the future"),
    ("time: {mode: range, range: {start: '2026-10-30T10:00Z'}}", "needs time.range.start and time.range.end"),
    ("time: {mode: range, range: {start: '2026-10-30T10:00Z', end: '2026-10-30T09:00Z'}}", "must be before"),
    ("time: {mode: range, range: {start: '2025-01-01T00:00Z', end: '2026-01-01T00:00Z', step_minutes: 10}}",
     "more than 1000 steps"),
    ("time: {mode: sometimes}", "time.mode"),
])
def test_bad_times(write_config, text, error):
    with pytest.raises(ConfigError, match=error):
        config.load(write_config(text + "\n"))


def test_overrides_and_source_selection(write_config):
    path = write_config("sources:\n  viirs_gibs: {enabled: true}\n")
    cfg = config.load(path, {"output.width_px": 800, "time.mode": "at", "time.at": "2026-10-30T10:00Z"},
                      sources=["meteosat9_iodc"])
    assert cfg.output.width_px == 800
    assert cfg.time.at.hour == 10
    assert cfg.enabled_sources() == ["meteosat9_iodc"]


def test_unknown_source_on_the_command_line(write_config):
    with pytest.raises(ConfigError, match="unknown source 'goes16'"):
        config.load(write_config(""), sources=["goes16"])


def test_missing_credentials(write_config, no_credentials, monkeypatch):
    cfg = config.load(write_config(""))
    cfg.check_credentials(["viirs_gibs"])  # needs none
    monkeypatch.setenv("EUMETSAT_KEY", "k")
    with pytest.raises(ConfigError) as err:
        cfg.check_credentials(["meteosat12_fci", "sentinel3_olci"])
    message = str(err.value)
    assert "meteosat12_fci: environment variable EUMETSAT_SECRET is not set" in message
    assert "EUMETSAT_KEY" not in message
    assert "sentinel3_olci: environment variable CDSE_USER is not set" in message


def test_earthdata_token_or_login(write_config, no_credentials, monkeypatch):
    cfg = config.load(write_config(""))
    with pytest.raises(ConfigError, match=r"EARTHDATA_TOKEN \(or EARTHDATA_USERNAME and EARTHDATA_PASSWORD\)"):
        cfg.check_credentials(["viirs_l1b"])
    monkeypatch.setenv("EARTHDATA_USERNAME", "u")
    with pytest.raises(ConfigError):
        cfg.check_credentials(["viirs_l1b"])
    monkeypatch.setenv("EARTHDATA_PASSWORD", "p")
    cfg.check_credentials(["viirs_l1b"])


def test_dotenv_next_to_config(write_config, no_credentials, tmp_path):
    (tmp_path / ".env").write_text("EARTHDATA_TOKEN=abc\n")
    cfg = config.load(write_config(""))
    cfg.check_credentials(["viirs_l1b"])


def test_animation_formats_imply_png(write_config):
    cfg = config.load(write_config("output: {formats: [gif, geotiff]}\n"))
    assert cfg.output.scene_formats == ["png", "geotiff"]
    assert cfg.output.animation_formats == ["gif"]


def test_time_spec_from_config(write_config):
    cfg = config.load(write_config(
        "time: {mode: range, range: {start: '2026-10-30T06:00Z', end: '2026-10-30T07:00Z', step_minutes: 20}}\n"))
    spec = cfg.time.spec()
    assert spec.mode == "range"
    assert spec.step == timedelta(minutes=20)
    assert len(spec.slots()) == 4
