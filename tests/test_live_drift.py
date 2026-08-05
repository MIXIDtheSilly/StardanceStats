from __future__ import annotations

import os

import pytest

from src.collector.crawl_user import USER_PATH
from src.fetcher import Fetcher
from src.parsers import parse_project_page, parse_user_page

REFERENCE_PROJECT = 8100
REFERENCE_USER = 32

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

    # Presence-only markup, so pin it against a project known to have one.
    assert project["is_super_star"] is True, "Super Star badge selector broke"
    assert "super_star_badge" in parsed.found

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


async def test_reference_user_still_parses_cleanly():
    """The projects tab is what we crawl: profile header plus the project list."""
    async with Fetcher() as fetcher:
        response = await fetcher.get(USER_PATH.format(id=REFERENCE_USER))

    assert response.ok, f"unexpected status {response.status}"
    parsed = parse_user_page(response.text, REFERENCE_USER)

    assert parsed.missing == set(), f"selectors went stale: {sorted(parsed.missing)}"
    assert parsed.warnings == [], f"parser warnings: {parsed.warnings}"

    user = parsed.data["user"]
    assert user["username"]
    assert user["followers"] is not None
    assert user["following"] is not None
    assert user["devlogs_count"] is not None
    assert user["joined_at"] is not None
    # 0 is a broken streak, None is a broken parser.
    assert user["streak"] is not None, "streak badge wording changed"

    # None here means the tab panel selector broke.
    assert user["project_ids"] is not None, "projects tab panel selector broke"
    assert len(user["project_ids"]) == user["projects_count"]


async def test_handle_lookup_still_yields_a_numeric_id():
    """The og tags are what make /@handle a one-request id lookup."""
    async with Fetcher() as fetcher:
        response = await fetcher.get("/@The_Craw/projects")

    assert response.ok
    user = parse_user_page(response.text).data["user"]
    assert isinstance(user["_id"], int) and user["_id"] > 0
    assert user["username"].lower() == "the_craw"


async def test_upstream_still_serves_no_etag():
    """If this fails, upstream added ETags and revalidation becomes near-free."""
    async with Fetcher() as fetcher:
        response = await fetcher.get(f"/projects/{REFERENCE_PROJECT}")
    assert response.etag is None
