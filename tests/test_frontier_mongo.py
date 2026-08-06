from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo.errors import PyMongoError

from src.collector import frontier
from src.collector.sitemap import SitemapEntry, apply_sitemap
from src.config import settings
from src.db import bootstrap

TEST_DB = "stardance_stats_test_frontier"
UTC = timezone.utc
NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def db():
    client = AsyncIOMotorClient(settings.mongo_url, tz_aware=True, serverSelectionTimeoutMS=1500)
    try:
        await client.admin.command("ping")
    except PyMongoError as exc:
        pytest.skip(f"no MongoDB at {settings.mongo_url}: {exc}")

    await client.drop_database(TEST_DB)
    database = client[TEST_DB]
    await bootstrap(database)
    try:
        yield database
    finally:
        await client.drop_database(TEST_DB)
        client.close()


SEGMENT = {"project": "projects", "user": "users", "mission": "missions"}


def entries(*specs) -> list[SitemapEntry]:
    out = []
    for kind, ref_id, lastmod in specs:
        path = f"/{SEGMENT[kind]}/{ref_id}"
        out.append(SitemapEntry(kind, ref_id, path, lastmod))
    return out


async def test_sync_creates_rows_due_immediately(db):
    summary = await apply_sitemap(
        db,
        entries(
            ("project", 8100, NOW - timedelta(days=1)),
            ("user", 11155, NOW - timedelta(days=40)),
        ) + [SitemapEntry("other", None, "/leaderboard", None)],
        now=NOW,
    )
    assert summary == {"seen": 3, "new": 2, "promoted": 0, "skipped": 1, "delisted": 0}

    project = await db.crawl_frontier.find_one({"_id": "project:8100"})
    assert project["tier"] == "hot"
    assert project["url"] == "/projects/8100"
    assert project["next_due"] is None  # never crawled: due now
    assert project["in_sitemap"] is True

    user = await db.crawl_frontier.find_one({"_id": "user:11155"})
    assert user["tier"] == "cold"

    due = await frontier.due(db, now=NOW)
    assert {row["_id"] for row in due} == {"project:8100", "user:11155"}


async def test_advancing_lastmod_makes_a_row_due_again(db):
    await apply_sitemap(db, entries(("project", 1, NOW - timedelta(days=90))), now=NOW)
    await frontier.record_crawl(db, "project", 1, status="ok", changed=False, now=NOW)

    row = await db.crawl_frontier.find_one({"_id": "project:1"})
    assert row["tier"] == "cold"
    assert row["next_due"] > NOW
    assert not await frontier.due(db, now=NOW)

    later = NOW + timedelta(hours=2)
    summary = await apply_sitemap(db, entries(("project", 1, later)), now=later)
    assert summary["promoted"] == 1

    row = await db.crawl_frontier.find_one({"_id": "project:1"})
    assert row["tier"] == "hot"
    assert [r["_id"] for r in await frontier.due(db, now=later)] == ["project:1"]


async def test_unchanged_lastmod_does_not_disturb_the_schedule(db):
    lastmod = NOW - timedelta(days=90)
    await apply_sitemap(db, entries(("project", 1, lastmod)), now=NOW)
    await frontier.record_crawl(db, "project", 1, status="ok", changed=False, now=NOW)
    scheduled = (await db.crawl_frontier.find_one({"_id": "project:1"}))["next_due"]

    summary = await apply_sitemap(db, entries(("project", 1, lastmod)), now=NOW + timedelta(hours=1))
    assert summary["promoted"] == 0
    row = await db.crawl_frontier.find_one({"_id": "project:1"})
    assert row["next_due"] == scheduled


async def test_delisted_rows_are_frozen_not_deleted(db):
    """Drafting or a ban delists a page, and deleting the row would lose its history."""
    await apply_sitemap(
        db, entries(("project", 1, NOW), ("project", 2, NOW)), now=NOW
    )

    later = NOW + timedelta(hours=1)
    summary = await apply_sitemap(db, entries(("project", 1, NOW)), now=later)
    assert summary["delisted"] == 1

    gone = await db.crawl_frontier.find_one({"_id": "project:2"})
    assert gone is not None
    assert gone["in_sitemap"] is False
    assert gone["tier"] == "frozen"
    assert gone["delisted_at"] == later

    # And a second sync does not re-count it.
    assert (await apply_sitemap(db, entries(("project", 1, NOW)), now=later))["delisted"] == 0


async def test_relisting_revives_a_frozen_row(db):
    await apply_sitemap(db, entries(("project", 1, NOW)), now=NOW)
    await apply_sitemap(db, [], now=NOW + timedelta(hours=1))
    assert (await db.crawl_frontier.find_one({"_id": "project:1"}))["in_sitemap"] is False

    back = NOW + timedelta(days=2)
    summary = await apply_sitemap(db, entries(("project", 1, back)), now=back)
    assert summary["promoted"] == 1
    row = await db.crawl_frontier.find_one({"_id": "project:1"})
    assert row["in_sitemap"] is True
    assert row["next_due"] == back


async def test_repeated_unchanged_crawls_demote_to_cold(db):
    await apply_sitemap(db, entries(("project", 1, NOW)), now=NOW)

    for i in range(settings.cold_after_unchanged):
        await frontier.record_crawl(
            db, "project", 1, status="ok", changed=False, now=NOW + timedelta(hours=i)
        )

    row = await db.crawl_frontier.find_one({"_id": "project:1"})
    assert row["consecutive_unchanged"] == settings.cold_after_unchanged
    assert row["tier"] == "cold"


async def test_a_change_resets_the_demotion_counter(db):
    await apply_sitemap(db, entries(("project", 1, NOW)), now=NOW)
    for _ in range(4):
        await frontier.record_crawl(db, "project", 1, status="ok", changed=False, now=NOW)

    await frontier.record_crawl(db, "project", 1, status="ok", changed=True, now=NOW)
    row = await db.crawl_frontier.find_one({"_id": "project:1"})
    assert row["consecutive_unchanged"] == 0
    assert row["tier"] == "hot"
    assert row["last_changed"] == NOW


async def test_first_ingest_tiers_from_the_sitemap_not_from_change(db):
    """Nothing to compare against must not read as volatile, or 30k pages end up hot."""
    await apply_sitemap(
        db, entries(("project", 1, NOW), ("project", 2, NOW - timedelta(days=90))), now=NOW
    )
    await frontier.record_crawl(db, "project", 1, status="ok", changed=None, now=NOW)
    await frontier.record_crawl(db, "project", 2, status="ok", changed=None, now=NOW)

    fresh = await db.crawl_frontier.find_one({"_id": "project:1"})
    stale = await db.crawl_frontier.find_one({"_id": "project:2"})
    assert fresh["tier"] == "hot"     # upstream touched it yesterday
    assert stale["tier"] == "cold"    # untouched for three months

    # And it does not count against the demotion budget either way.
    assert fresh["consecutive_unchanged"] == 0
    assert stale["consecutive_unchanged"] == 0


async def test_not_modified_counts_as_unchanged(db):
    await apply_sitemap(db, entries(("project", 1, NOW)), now=NOW)
    await frontier.record_crawl(db, "project", 1, status="not_modified", now=NOW)
    row = await db.crawl_frontier.find_one({"_id": "project:1"})
    assert row["consecutive_unchanged"] == 1


async def test_errors_back_off_without_changing_the_tier(db):
    """A failed fetch says nothing about how fast the page moves."""
    await apply_sitemap(db, entries(("project", 1, NOW)), now=NOW)
    await frontier.record_crawl(db, "project", 1, status="ok", changed=True, now=NOW)

    first = await frontier.record_crawl(
        db, "project", 1, status="fetch_error", error="timeout", now=NOW
    )
    assert first["tier"] == "hot"
    assert first["error_count"] == 1
    assert first["next_due"] == NOW + timedelta(hours=1)

    second = await frontier.record_crawl(db, "project", 1, status="http_error", now=NOW)
    assert second["error_count"] == 2
    assert second["next_due"] == NOW + timedelta(hours=2)

    ok = await frontier.record_crawl(db, "project", 1, status="ok", changed=True, now=NOW)
    assert ok["error_count"] == 0
    assert ok["last_error"] is None


async def test_a_gone_page_freezes_and_leaves_the_queue(db):
    await apply_sitemap(db, entries(("project", 1, NOW)), now=NOW)
    await frontier.record_crawl(db, "project", 1, status="gone", now=NOW)

    row = await db.crawl_frontier.find_one({"_id": "project:1"})
    assert row["tier"] == "frozen"
    assert row["gone"] is True
    assert row["next_due"] == NOW + timedelta(days=30)
    assert await frontier.due(db, now=NOW + timedelta(days=90)) == []


async def test_due_filters_by_kind_and_orders_by_next_due(db):
    await apply_sitemap(
        db, entries(("project", 1, NOW), ("project", 2, NOW), ("user", 9, NOW)), now=NOW
    )
    await frontier.record_crawl(db, "project", 1, status="ok", changed=True, now=NOW)
    await frontier.record_crawl(
        db, "project", 2, status="ok", changed=True, now=NOW - timedelta(days=1)
    )

    later = NOW + timedelta(days=2)
    ordered = [row["_id"] for row in await frontier.due(db, kind="project", now=later)]
    assert ordered == ["project:2", "project:1"]

    assert [row["_id"] for row in await frontier.due(db, kind="user", now=later)] == ["user:9"]


async def test_hot_rows_are_served_before_cold_ones(db):
    """30k rows come due at once, so the walk order is the order data arrives in."""
    await apply_sitemap(
        db,
        entries(
            ("project", 1, NOW - timedelta(days=90)),   # cold
            ("project", 2, NOW - timedelta(days=10)),   # warm
            ("project", 3, NOW),                        # hot
        ),
        now=NOW,
    )
    ordered = [row["ref_id"] for row in await frontier.due(db, now=NOW)]
    assert ordered == [3, 2, 1]


async def test_queue_depth_reports_tiers_and_what_is_due(db):
    await apply_sitemap(
        db,
        entries(
            ("project", 1, NOW),
            ("project", 2, NOW - timedelta(days=90)),
            ("user", 9, NOW),
        ),
        now=NOW,
    )
    await frontier.record_crawl(db, "project", 1, status="ok", changed=True, now=NOW)

    depth = await frontier.queue_depth(db, now=NOW)
    assert depth["total"] == 3
    assert depth["due"] == 2  # the two never crawled
    assert depth["never_crawled"] == 2
    assert depth["by_kind"]["project"]["tiers"] == {"hot": 1, "cold": 1}
    assert depth["by_kind"]["user"]["total"] == 1


async def test_a_mission_row_is_keyed_by_slug(db):
    await apply_sitemap(db, entries(("mission", "slack-bot", NOW)), now=NOW)

    row = await db.crawl_frontier.find_one({"_id": "mission:slack-bot"})
    assert row["ref_id"] == "slack-bot"
    assert row["url"] == "/missions/slack-bot"


async def test_missions_are_served_before_anything_else(db):
    """Every project's payout estimate reads them, so they lead whatever tier they earn."""
    await apply_sitemap(
        db,
        entries(
            ("project", 1, NOW),                              # hot
            ("mission", "frictionless", NOW - timedelta(days=90)),  # cold
        ),
        now=NOW,
    )
    assert [row["_id"] for row in await frontier.due(db, now=NOW)] == [
        "mission:frictionless", "project:1"
    ]


async def test_a_crawled_mission_keeps_its_lead(db):
    await apply_sitemap(db, entries(("mission", "hackpad", NOW)), now=NOW)
    doc = await frontier.record_crawl(db, "mission", "hackpad", status="ok", now=NOW)

    assert doc["priority"] == -1
    assert doc["url"] == "/missions/hackpad"
