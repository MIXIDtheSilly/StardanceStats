from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.collector.tiering import classify, error_backoff, next_due, tier_interval
from src.config import settings

UTC = timezone.utc
NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)


def days_ago(n: int) -> datetime:
    return NOW - timedelta(days=n)


def test_lastmod_window_picks_the_tier():
    assert classify(now=NOW, sitemap_lastmod=days_ago(1)) == "hot"
    assert classify(now=NOW, sitemap_lastmod=days_ago(3)) == "hot"
    assert classify(now=NOW, sitemap_lastmod=days_ago(10)) == "warm"
    assert classify(now=NOW, sitemap_lastmod=days_ago(30)) == "warm"
    assert classify(now=NOW, sitemap_lastmod=days_ago(60)) == "cold"


def test_unchanged_crawls_demote_past_the_lastmod_window():
    """A third of 30k pages sit inside the 30-day window and would want a daily crawl."""
    fresh = days_ago(1)
    assert classify(now=NOW, sitemap_lastmod=fresh, consecutive_unchanged=4) == "hot"
    assert classify(now=NOW, sitemap_lastmod=fresh, consecutive_unchanged=5) == "cold"


def test_a_change_beats_demotion():
    assert classify(
        now=NOW, sitemap_lastmod=days_ago(90), consecutive_unchanged=20, changed=True
    ) == "hot"


def test_frozen_beats_everything():
    assert classify(now=NOW, sitemap_lastmod=NOW, changed=True, frozen=True) == "frozen"


def test_unknown_lastmod_is_cold_not_hot():
    """Defaulting to hot would put every undated page on the hot schedule."""
    assert classify(now=NOW, sitemap_lastmod=None) == "cold"


def test_next_due_follows_the_tier():
    assert next_due("hot", last_crawled=NOW) == NOW + timedelta(hours=settings.tier_hot_hours)
    assert next_due("frozen", last_crawled=NOW) == (
        NOW + timedelta(hours=settings.tier_frozen_hours)
    )


def test_unknown_tier_falls_back_to_cold_not_zero():
    """A tier name we do not recognise must slow us down, never speed us up."""
    assert tier_interval("nonsense") == tier_interval("cold")


def test_error_backoff_grows_and_caps():
    assert error_backoff(1) == timedelta(hours=1)
    assert error_backoff(3) == timedelta(hours=4)
    assert error_backoff(99) == timedelta(hours=settings.max_error_backoff_hours)
