import pytest

from satview.areas import AREAS, get_area, lonlat_bounds
from satview.sources.meteosat12_fci import body_entries, fci_chunks

PREFIX = "W_XX-EUMETSAT-Darmstadt,IMG+SAT,MTI1+FCI-1C-RRAD-FDHSI-FD--CHK-"
SUFFIX = "_C_EUMT_20261030095241_IDPFI_OPE_20261030095007_20261030095923_N__O_0060_"


def entry(kind, n):
    return f"{PREFIX}{kind}---NC4E{SUFFIX}{n:04d}.nc"


def test_israel_is_in_chunks_31_and_32():
    assert fci_chunks(AREAS["israel"].bbox, margin=0) == (31, 32)


def test_margin_adds_a_chunk_each_side():
    assert fci_chunks(lonlat_bounds(get_area())) == (30, 31, 32, 33)


def test_southern_and_northern_edges_are_clipped():
    assert fci_chunks((0.0, -78.0, 1.0, -77.5), margin=2)[0] == 1
    assert fci_chunks((0.0, 77.5, 1.0, 78.0), margin=2)[-1] == 40


def test_area_off_the_disk():
    with pytest.raises(ValueError, match="not fully visible"):
        fci_chunks((100.0, 30.0, 101.0, 31.0))


def test_body_entries_selects_chunks_only():
    entries = [entry("BODY-", n) for n in range(1, 41)] + [entry("TRAIL-", 41), "EOPMetadata.xml"]
    selected = body_entries(entries, (30, 31, 32, 33))
    assert [s[-7:-3] for s in selected] == ["0030", "0031", "0032", "0033"]
    assert all("-BODY-" in s for s in selected)
