from __future__ import annotations

from datetime import datetime, timedelta

from ..config import settings

TIERS = ("hot", "warm", "cold", "frozen")

PRIORITY = {"hot": 0, "warm": 1, "cold": 2, "frozen": 3}

# Under a dozen pages, and every project's payout estimate reads them.
LEAD = {"mission": -1}

ERROR_STATUSES = frozenset({"fetch_error", "http_error", "parse_error", "anomaly"})


def priority(tier: str, kind: str | None = None) -> int:
    if kind in LEAD:
        return LEAD[kind]
    return PRIORITY.get(tier, PRIORITY["cold"])


def tier_interval(tier: str) -> timedelta:
    hours = {
        "hot": settings.tier_hot_hours,
        "warm": settings.tier_warm_hours,
        "cold": settings.tier_cold_hours,
        "frozen": settings.tier_frozen_hours,
    }.get(tier, settings.tier_cold_hours)
    return timedelta(hours=hours)


def classify(
    *,
    now: datetime,
    sitemap_lastmod: datetime | None,
    consecutive_unchanged: int = 0,
    changed: bool = False,
    frozen: bool = False,
) -> str:
    """Pick a tier. Demotion deliberately outranks the lastmod window."""
    if frozen:
        return "frozen"
    if changed:
        return "hot"
    if consecutive_unchanged >= settings.cold_after_unchanged:
        return "cold"
    if sitemap_lastmod is None:
        return "cold"

    age = now - sitemap_lastmod
    if age <= timedelta(days=settings.tier_hot_days):
        return "hot"
    if age <= timedelta(days=settings.tier_warm_days):
        return "warm"
    return "cold"


def next_due(tier: str, *, last_crawled: datetime) -> datetime:
    return last_crawled + tier_interval(tier)


def error_backoff(error_count: int) -> timedelta:
    hours = min(2.0 ** max(error_count - 1, 0), settings.max_error_backoff_hours)
    return timedelta(hours=hours)
