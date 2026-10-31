import os
import shutil
from pathlib import Path

import pytest

DATA = Path(__file__).parent / "data"
EXAMPLE_CONFIG = Path(__file__).parent.parent / "config.example.yaml"


@pytest.fixture(autouse=True)
def isolated_environment():
    """Config loading reads .env files into os.environ; undo that after each test."""
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)


@pytest.fixture
def example_config(tmp_path):
    """config.example.yaml copied away from the project's own .env."""
    return Path(shutil.copy(EXAMPLE_CONFIG, tmp_path / "config.yaml"))


@pytest.fixture
def write_config(tmp_path):
    """Write a config.yaml into a temp folder and return its path."""

    def write(text: str = "") -> Path:
        path = tmp_path / "config.yaml"
        path.write_text(text)
        return path

    return write


@pytest.fixture
def no_credentials(monkeypatch):
    for var in ("EUMETSAT_KEY", "EUMETSAT_SECRET", "EARTHDATA_TOKEN", "EARTHDATA_USERNAME", "EARTHDATA_PASSWORD",
                "CDSE_USER", "CDSE_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
