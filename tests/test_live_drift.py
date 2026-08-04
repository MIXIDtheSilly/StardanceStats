from __future__ import annotations

import os

import pytest

from src.fetcher import Fetcher
from src.parsers import parse_project_page

REFERENCE_PROJECT = 8100

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not os.getenv("STARDANCE_LIVE_TESTS"),
        reason="set STARDANCE_LIVE_TESTS=1 to hit the live site",
    ),
]


async def test_reference_project_still_parses_cleanly():
    async with Fetcher() as fetcher:
        response = await fetcher.get(f"/projects/{REFERENCE_PROJECT}")

    assert response.ok, f"unexpected status {response.status}"
    parsed = parse_project_page(response.text, REFERENCE_PROJECT)

    assert parsed.missing == set(), f"selectors went stale: {sorted(parsed.missing)}"
    assert parsed.warnings == [], f"parser warnings: {parsed.warnings}"

    project = parsed.data["project"]
    assert project["title"] == "Crawssembly"
    assert project["owner_username"] == "The_Craw"
    assert project["devlogs_count"] and project["devlogs_count"] > 0
    assert project["total_hours"] and project["total_hours"] > 0
    assert project["followers"] is not None

    devlogs = parsed.data["devlogs"]
    assert len(devlogs) > 50
    assert all(d["posted_at"] and d["likes"] is not None for d in devlogs)

    ships = parsed.data["ships"]
    assert len(ships) >= 2
    for ship in ships:
        assert ship["payout"] is not None, "payout selector broke"
        assert ship["multiplier"] is not None, "multiplier selector broke"
        assert ship["hours_at_ship"] is not None


async def test_upstream_still_serves_no_etag():
    """Documents a live assumption: project pages are not conditionally cacheable.

    If this starts failing, upstream added ETags and the crawler can switch to
    near-free revalidation, which is a win worth noticing rather than a break.
    """
    async with Fetcher() as fetcher:
        response = await fetcher.get(f"/projects/{REFERENCE_PROJECT}")
    assert response.etag is None
