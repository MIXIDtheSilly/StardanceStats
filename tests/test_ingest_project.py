from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.ingest.project import _day_range, build_stats, check_anomalies
from src.parsers import parse_project_page
from src.parsers.common import ParseResult

FIXTURE = Path(__file__).parent / "fixtures" / "project_8100.html"
UTC = timezone.utc


@pytest.fixture(scope="module")
def parsed():
    return parse_project_page(FIXTURE.read_text(encoding="utf-8"), 8100)


@pytest.fixture(scope="module")
def stats(parsed):
    return build_stats(
        parsed.data["project"], parsed.data["devlogs"], parsed.data["ships"]
    )


def test_engagement_is_summed_across_devlogs(stats):
    assert stats["likes"] == 271
    assert stats["comments"] == 38
    assert stats["reposts"] == 9
    assert stats["views"] > 0


def test_ship_derived_stats(stats):
    assert stats["ships"] == 2
    assert stats["stardust_total"] == 3042          # 633 + 2409
    assert stats["latest_multiplier"] == 19.65      # newest ship, not largest
    assert stats["avg_multiplier"] == pytest.approx(19.715, abs=0.001)


def test_hours_prefer_the_pages_own_figure(stats):
    assert stats["total_hours"] == 256.0
    assert stats["summed_hours"] == pytest.approx(256.0, abs=1.0)


def test_stardust_per_hour(stats):
    assert stats["stardust_per_hour"] == pytest.approx(3042 / 256.0, abs=0.01)


def test_hours_fall_back_to_summed_when_header_is_absent(parsed):
    project = dict(parsed.data["project"], total_hours=None)
    fallback = build_stats(project, parsed.data["devlogs"], parsed.data["ships"])
    assert fallback["total_hours"] == pytest.approx(256.0, abs=1.0)


def test_empty_project_does_not_divide_by_zero():
    stats = build_stats({"devlogs_count": 0, "total_hours": 0}, [], [])
    assert stats["stardust_total"] == 0
    assert stats["stardust_per_hour"] is None
    assert stats["latest_multiplier"] is None


def test_first_ingest_is_always_accepted(stats):
    assert check_anomalies(stats, None, ParseResult()) == []


def test_growth_is_accepted(stats):
    previous = dict(stats, devlogs=100, total_hours=250.0, stardust_total=3000)
    assert check_anomalies(stats, previous, ParseResult()) == []


def test_collapsed_counter_is_rejected(stats):
    """A selector that stops matching reads as zero, which must not persist."""
    broken = dict(stats, devlogs=0, total_hours=0.0)
    reasons = check_anomalies(broken, stats, ParseResult())
    assert any("devlogs fell" in r for r in reasons)
    assert any("total_hours fell" in r for r in reasons)


def test_small_dip_is_tolerated(stats):
    """Deleted devlogs and removed likes are ordinary; don't cry wolf."""
    dipped = dict(stats, likes=stats["likes"] - 3, devlogs=stats["devlogs"] - 1)
    assert check_anomalies(dipped, stats, ParseResult()) == []


def test_field_becoming_unreadable_is_rejected(stats):
    vanished = dict(stats, followers=None)
    reasons = check_anomalies(vanished, stats, ParseResult())
    assert any("followers became unreadable" in r for r in reasons)


def test_parse_misses_are_reported(stats):
    result = ParseResult()
    result.missing.add("devlog.likes")
    reasons = check_anomalies(stats, stats, result)
    assert any("unparsed fields" in r for r in reasons)


def test_day_range_is_inclusive_and_utc_midnight():
    start = datetime(2026, 6, 26, 21, 15, tzinfo=UTC)
    end = datetime(2026, 6, 29, 3, 0, tzinfo=UTC)
    days = _day_range(start, end)
    assert days[0] == datetime(2026, 6, 26, tzinfo=UTC)
    assert days[-1] == datetime(2026, 6, 29, tzinfo=UTC)
    assert len(days) == 4
    assert all(d.tzinfo is UTC and d.hour == 0 for d in days)


def test_day_range_single_day():
    day = datetime(2026, 8, 3, 12, tzinfo=UTC)
    assert _day_range(day, day + timedelta(hours=1)) == [datetime(2026, 8, 3, tzinfo=UTC)]
