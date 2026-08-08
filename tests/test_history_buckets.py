from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.api.services.history import METRICS, HistoryError, parse_metrics, trunc
from src.collector.rollup import TRACKED as GLOBAL_TRACKED
from src.ingest.project import TRACKED as PROJECT_TRACKED
from src.ingest.user import TRACKED as USER_TRACKED

UTC = timezone.utc


def test_week_buckets_start_on_sunday_like_dateTrunc():
    """$dateTrunc weeks start Sunday; a Monday-based truncation is off by one."""
    wednesday = datetime(2026, 8, 5, 17, 30, tzinfo=UTC)
    assert trunc(wednesday, "1w") == datetime(2026, 8, 2, tzinfo=UTC)
    assert trunc(wednesday, "1d") == datetime(2026, 8, 5, tzinfo=UTC)
    assert trunc(wednesday, "1h") == datetime(2026, 8, 5, 17, 0, tzinfo=UTC)


def test_truncation_normalises_to_utc_first():
    late = datetime(2026, 8, 5, 23, 30, tzinfo=timezone(timedelta(hours=-5)))
    assert trunc(late, "1d") == datetime(2026, 8, 6, tzinfo=UTC)


def test_metrics_are_deduplicated_and_order_is_kept():
    assert parse_metrics("project", "likes, devlogs ,likes") == ["likes", "devlogs"]


def test_an_empty_metric_list_is_an_error():
    for bad in ("", " , "):
        try:
            parse_metrics("project", bad)
        except HistoryError:
            continue
        raise AssertionError(f"{bad!r} should not have parsed")


def test_the_served_metrics_are_the_ones_snapshots_carry():
    """The vocabulary comes from the writers, so the two cannot drift apart."""
    assert METRICS["project"].metrics == frozenset(PROJECT_TRACKED)
    assert METRICS["user"].metrics == frozenset(USER_TRACKED)
    assert METRICS["global"].metrics == frozenset(GLOBAL_TRACKED)
