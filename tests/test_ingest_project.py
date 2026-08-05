from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.ingest.project import (
    _day_range,
    build_stats,
    check_anomalies,
    estimate_unpaid,
    payout_hours,
)
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


def test_shipped_hours_sum_the_ship_cards(stats):
    assert stats["shipped_hours"] == pytest.approx(165.0, abs=0.01)   # 133 + 32
    assert stats["paid_hours"] == pytest.approx(165.0, abs=0.01)      # both paid out


def test_the_rate_is_over_paid_hours_only(stats):
    """Same basis on both sides: payouts over the hours those payouts were for."""
    assert stats["stardust_per_paid_hour"] == pytest.approx(3042 / 165.0, abs=0.01)
    assert "stardust_per_hour" not in stats, "mixed-basis rate is gone for good"


def test_a_ship_in_review_does_not_dilute_the_rate(parsed):
    """It has hours but no payout yet, so it belongs in shipped, not paid."""
    ships = parsed.data["ships"]
    in_review = dict(ships[0], _id=999, payout=None, multiplier=None, hours_at_ship=80.0)
    stats = build_stats(parsed.data["project"], parsed.data["devlogs"], ships + [in_review])

    assert stats["shipped_hours"] == pytest.approx(245.0, abs=0.01)   # 165 + 80
    assert stats["paid_hours"] == pytest.approx(165.0, abs=0.01)
    assert stats["stardust_per_paid_hour"] == pytest.approx(3042 / 165.0, abs=0.01)


def test_unpaid_hours_are_valued_at_the_realised_rate(stats):
    unpaid = stats["summed_hours"] - 165.0
    assert stats["unpaid_hours"] == pytest.approx(unpaid, abs=0.01)

    expected = round(unpaid * (3042 / 165.0))
    assert stats["estimated_pending_stardust"] == expected
    assert stats["estimated_total_stardust"] == 3042 + expected


def test_nothing_paid_out_means_no_rate_to_extrapolate_from():
    stats = build_stats({"devlogs_count": 1, "total_hours": 40.0}, [], [])
    assert stats["unpaid_hours"] == 0.0             # no devlogs parsed, so no hours
    assert stats["estimated_pending_stardust"] is None
    assert stats["estimated_total_stardust"] is None


def test_estimate_never_reports_negative_unpaid_hours():
    """hours_at_ship is frozen upstream, so a later deletion can exceed our sum."""
    estimate = estimate_unpaid(600, logged_hours=30.0, paid_hours=32.0)
    assert estimate["unpaid_hours"] == 0.0
    assert estimate["estimated_pending_stardust"] == 0
    assert estimate["estimated_total_stardust"] == 600


def test_payout_hours_recover_the_capped_basis(parsed):
    """Payout runs on hours capped at 10h per devlog, which the card never
    shows. Ship 1 has one devlog over the cap, so its payout basis sits below
    the 133h it displays; ship 2 has none and lands on its 32h."""
    s1, s2 = sorted(parsed.data["ships"], key=lambda s: s["shipped_at"])

    assert payout_hours(s1) == pytest.approx(121.8, abs=0.1)
    assert payout_hours(s1) < s1["hours_at_ship"]
    assert payout_hours(s2) == pytest.approx(s2["hours_at_ship"], abs=0.3)


def test_payout_hours_is_none_without_both_inputs():
    assert payout_hours({"payout": 633, "multiplier": None}) is None
    assert payout_hours({"payout": None, "multiplier": 19.65}) is None


def test_hours_fall_back_to_summed_when_header_is_absent(parsed):
    project = dict(parsed.data["project"], total_hours=None)
    fallback = build_stats(project, parsed.data["devlogs"], parsed.data["ships"])
    assert fallback["total_hours"] == pytest.approx(256.0, abs=1.0)


def test_empty_project_does_not_divide_by_zero():
    stats = build_stats({"devlogs_count": 0, "total_hours": 0}, [], [])
    assert stats["stardust_total"] == 0
    assert stats["stardust_per_paid_hour"] is None
    assert stats["shipped_hours"] is None
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
