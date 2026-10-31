from datetime import datetime, timedelta, timezone

from satview.sources.base import TimeSpec, select_times


def t(hour, minute=0):
    return datetime(2026, 10, 30, hour, minute, tzinfo=timezone.utc)


# FCI-like products every 10 minutes from 06:00 to 08:50
PRODUCTS = [t(6) + timedelta(minutes=10 * i) for i in range(18)]
TOLERANCE = timedelta(minutes=10)


def select(spec):
    return select_times(PRODUCTS, lambda p: p, spec, TOLERANCE)


def test_latest_picks_newest():
    assert select(TimeSpec()) == [t(8, 50)]


def test_at_picks_nearest():
    assert select(TimeSpec(mode="at", at=t(7, 14))) == [t(7, 10)]
    assert select(TimeSpec(mode="at", at=t(7, 16))) == [t(7, 20)]


def test_at_outside_tolerance_finds_nothing():
    assert select(TimeSpec(mode="at", at=t(12))) == []


def test_range_one_per_step():
    spec = TimeSpec(mode="range", start=t(6), end=t(8), step=timedelta(minutes=30))
    assert select(spec) == [t(6), t(6, 30), t(7), t(7, 30), t(8)]


def test_range_step_shorter_than_cadence_does_not_duplicate():
    spec = TimeSpec(mode="range", start=t(6), end=t(6, 20), step=timedelta(minutes=5))
    assert select(spec) == [t(6), t(6, 10), t(6, 20)]


def test_range_with_gap_skips_missing_slots():
    products = [p for p in PRODUCTS if not t(7) <= p <= t(7, 40)]  # an outage
    spec = TimeSpec(mode="range", start=t(6, 30), end=t(8), step=timedelta(minutes=30))
    # 07:00 takes 06:50; 07:30 has nothing within 10 minutes
    assert select_times(products, lambda p: p, spec, TOLERANCE) == [t(6, 30), t(6, 50), t(8)]


def test_empty():
    assert select_times([], lambda p: p, TimeSpec(), TOLERANCE) == []


def test_windows():
    now = t(12)
    assert TimeSpec(now=now).window(timedelta(hours=1)) == (t(11), t(12))
    assert TimeSpec(mode="at", at=t(10)).window(timedelta(minutes=10)) == (t(9, 50), t(10, 10))
    spec = TimeSpec(mode="range", start=t(6), end=t(8))
    assert spec.window(timedelta(minutes=10), timedelta(0)) == (t(5, 50), t(8))
