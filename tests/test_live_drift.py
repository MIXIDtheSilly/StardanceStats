from __future__ import annotations

import os

import pytest

from src.collector.crawl_user import USER_PATH
from src.collector.sitemap import parse_sitemap
from src.config import settings
from src.fetcher import Fetcher
from src.parsers import parse_project_page, parse_user_page
from src.parsers.mission import (
    PAYOUT_FLAT,
    PAYOUT_STATIC,
    PAYOUT_VOTING,
    parse_mission_page,
)

REFERENCE_PROJECT = 8100
REFERENCE_USER = 32
# Attached to the "frictionless" mission, and shipped to it.
REFERENCE_MISSION_PROJECT = 26067
PAYOUT_PATHS = (PAYOUT_VOTING, PAYOUT_STATIC, PAYOUT_FLAT)

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


async def test_sitemap_is_still_a_complete_flat_index():
    """A split into a sitemap index would halve the frontier silently rather than fail."""
    async with Fetcher() as fetcher:
        response = await fetcher.get(
            settings.sitemap_path,
            max_bytes=settings.sitemap_max_bytes,
            accept="application/xml,text/xml",
        )

    assert response.ok, f"unexpected status {response.status}"
    assert "<sitemapindex" not in response.text, "sitemap split into an index"

    entries = list(parse_sitemap(response.text))
    kinds = {"project": 0, "user": 0, "mission": 0, "other": 0}
    for entry in entries:
        kinds[entry.kind] += 1

    assert kinds["project"] > 1000, f"only {kinds['project']} projects listed"
    assert kinds["user"] > 10000, f"only {kinds['user']} users listed"
    assert kinds["mission"] > 0, "missions dropped out of the sitemap"

    dated = [e for e in entries if e.ref_id is not None and e.lastmod is not None]
    # lastmod is the whole tiering signal; losing it would not fail a parse.
    assert len(dated) > (kinds["project"] + kinds["user"]) * 0.99, "lastmod went missing"


async def test_sitemap_still_revalidates():
    """The sitemap serves an ETag; losing it makes the hourly poll cost 5 MB."""
    async with Fetcher() as fetcher:
        first = await fetcher.get(
            settings.sitemap_path, max_bytes=settings.sitemap_max_bytes
        )
        assert first.etag, "sitemap stopped serving an ETag"

        second = await fetcher.get(
            settings.sitemap_path, etag=first.etag, max_bytes=settings.sitemap_max_bytes
        )
    assert second.status == 304, f"conditional request returned {second.status}"


async def test_mission_panel_still_names_its_mission():
    """Presence-only markup, so pin it against a project known to be on one."""
    async with Fetcher() as fetcher:
        response = await fetcher.get(f"/projects/{REFERENCE_MISSION_PROJECT}")

    assert response.ok, f"unexpected status {response.status}"
    parsed = parse_project_page(response.text, REFERENCE_MISSION_PROJECT)
    assert parsed.missing == set(), f"selectors went stale: {sorted(parsed.missing)}"
    assert parsed.warnings == [], f"parser warnings: {parsed.warnings}"

    mission = parsed.data["project"]["mission"]
    assert mission is not None, "mission panel selector broke"
    assert mission["slug"] == "frictionless"
    assert mission["name"]
    assert mission["shipped"] is True

    assert any(s["mission_slug"] == "frictionless" for s in parsed.data["ships"]), (
        "ship card mission link selector broke"
    )


async def test_every_mission_still_declares_a_payout_path():
    """Every attached project is priced off these, and there are few enough to pin all."""
    async with Fetcher() as fetcher:
        response = await fetcher.get(
            settings.sitemap_path,
            max_bytes=settings.sitemap_max_bytes,
            accept="application/xml,text/xml",
        )
        assert response.ok, f"unexpected status {response.status}"
        slugs = [e.ref_id for e in parse_sitemap(response.text) if e.kind == "mission"]
        assert slugs, "no missions in the sitemap"

        paths = {}
        for slug in slugs:
            page = await fetcher.get(f"/missions/{slug}")
            assert page.ok, f"mission {slug}: unexpected status {page.status}"
            parsed = parse_mission_page(page.text, slug)

            assert parsed.missing == set(), f"{slug}: stale selectors {sorted(parsed.missing)}"
            assert parsed.warnings == [], f"{slug}: parser warnings {parsed.warnings}"

            mission = parsed.data["mission"]
            assert mission["payout_path"] in PAYOUT_PATHS, f"{slug}: {mission['payout_path']}"
            assert mission["name"] and mission["description"]
            paths[slug] = mission["payout_path"]

            if mission["payout_path"] == PAYOUT_STATIC:
                assert mission["fixed_stardust"], f"{slug}: fixed payout unreadable"
            if mission["payout_path"] == PAYOUT_FLAT:
                assert mission["stardust_per_hour"], f"{slug}: hourly rate unreadable"

    # Both paths have to stay represented, or a one-sided change reads as fine.
    assert PAYOUT_STATIC in paths.values(), "no fixed-payout mission left to pin"
    assert PAYOUT_VOTING in paths.values(), "no rated mission left to pin"


async def test_the_reference_missions_terms_have_not_moved():
    async with Fetcher() as fetcher:
        rated = parse_mission_page(
            (await fetcher.get("/missions/frictionless")).text, "frictionless"
        ).data["mission"]
        fixed = parse_mission_page(
            (await fetcher.get("/missions/slack-bot")).text, "slack-bot"
        ).data["mission"]

    assert rated["payout_path"] == PAYOUT_VOTING and rated["rated"] is True
    assert fixed["payout_path"] == PAYOUT_STATIC and fixed["rated"] is False
    assert fixed["fixed_stardust"] == 30
