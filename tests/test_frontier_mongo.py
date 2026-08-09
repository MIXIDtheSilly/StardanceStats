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


async def test_a_quiet_stretch_demotes_to_cold(db):
    await apply_sitemap(db, entries(("project", 1, NOW)), now=NOW)
    quiet_for = timedelta(hours=settings.cold_after_unchanged_hours)

    await frontier.record_crawl(db, "project", 1, status="ok", changed=False, now=NOW)
    row = await db.crawl_frontier.find_one({"_id": "project:1"})
    assert row["unchanged_since"] == NOW
    assert row["tier"] == "hot"

    await frontier.record_crawl(
        db, "project", 1, status="ok", changed=False, now=NOW + quiet_for
    )
    row = await db.crawl_frontier.find_one({"_id": "project:1"})
    # The clock still runs from the streak's first crawl, not this one.
    assert row["unchanged_since"] == NOW
    assert row["tier"] == "cold"


async def test_crawling_more_often_does_not_demote_sooner(db):
    """The whole point of timing the streak instead of counting it."""
    await apply_sitemap(db, entries(("project", 1, NOW)), now=NOW)

    for minute in range(0, 120, 10):
        await frontier.record_crawl(
            db, "project", 1, status="ok", changed=False, now=NOW + timedelta(minutes=minute)
        )

    row = await db.crawl_frontier.find_one({"_id": "project:1"})
    assert row["consecutive_unchanged"] == 12
    assert row["tier"] == "hot"


async def test_a_change_resets_the_demotion_counter(db):
    await apply_sitemap(db, entries(("project", 1, NOW)), now=NOW)
    for _ in range(4):
        await frontier.record_crawl(db, "project", 1, status="ok", changed=False, now=NOW)

    await frontier.record_crawl(db, "project", 1, status="ok", changed=True, now=NOW)
    row = await db.crawl_frontier.find_one({"_id": "project:1"})
    assert row["consecutive_unchanged"] == 0
    assert row["unchanged_since"] is None
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

    # And it does not start the demotion clock either way.
    assert fresh["unchanged_since"] is None
    assert stale["unchanged_since"] is None


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
    assert row["next_due"] == NOW + timedelta(hours=settings.tier_frozen_hours)
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


async def test_seeding_a_range_queues_ids_the_sitemap_never_listed(db):
    """Ids are sequential and every one is public, so the range reaches drafts."""
    await frontier.seed_id_range(db, "project", 1, 5, now=NOW)

    rows = await frontier.due(db, kind="project", now=NOW)
    assert sorted(r["ref_id"] for r in rows) == [1, 2, 3, 4, 5]
    assert (await db.crawl_frontier.find_one({"_id": "project:3"}))["url"] == "/projects/3"


async def test_seeding_never_disturbs_a_row_already_being_tracked(db):
    """Re-seeding is routine, so it must not reset a schedule or drop an etag."""
    await apply_sitemap(db, entries(("project", 2, NOW)), now=NOW)
    await frontier.record_crawl(db, "project", 2, status="ok", etag='W/"abc"', now=NOW)
    before = await db.crawl_frontier.find_one({"_id": "project:2"})

    result = await frontier.seed_id_range(db, "project", 1, 3, now=NOW)

    assert result["seeded"] == 2  # 1 and 3; 2 was already there
    assert await db.crawl_frontier.find_one({"_id": "project:2"}) == before


async def test_seeded_rows_survive_the_next_sitemap_sync(db):
    """Absent from the sitemap by definition, so delisting must skip them."""
    await frontier.seed_id_range(db, "project", 1, 2, now=NOW)
    await apply_sitemap(db, entries(("project", 1, NOW)), now=NOW)

    seeded_only = await db.crawl_frontier.find_one({"_id": "project:2"})
    assert seeded_only["tier"] != "frozen"
    assert seeded_only["next_due"] is None
    assert [r["ref_id"] for r in await frontier.due(db, kind="project", now=NOW)] == [1, 2]


async def test_max_ref_id_ignores_slug_keyed_kinds(db):
    await apply_sitemap(
        db, entries(("project", 41154, NOW), ("mission", "hackpad", NOW)), now=NOW
    )
    assert await frontier.max_ref_id(db, "project") == 41154
    assert await frontier.max_ref_id(db, "mission") == 0


async def test_the_scan_covers_the_whole_range_on_its_first_pass(db):
    """Drafts sit below the highest listed id, so the tail alone would miss them."""
    await apply_sitemap(db, entries(("project", 40, NOW)), now=NOW)

    result = await frontier.extend_scan(db, "project", margin=5, now=NOW)

    assert result["seeded"] == 44  # 1..45, less the listed id 40
    assert result["covered_to"] == 45


async def test_a_second_pass_only_seeds_what_grew(db):
    await apply_sitemap(db, entries(("project", 40, NOW)), now=NOW)
    await frontier.extend_scan(db, "project", margin=5, now=NOW)

    # A project above the old ceiling turns up and is ingested.
    await db.projects.insert_one({"_id": 47})
    result = await frontier.extend_scan(db, "project", margin=5, now=NOW)

    assert result["seeded"] == 7  # 46..52
    assert result["covered_to"] == 52


async def test_a_tail_of_dead_ids_cannot_ratchet_the_ceiling(db):
    """Seeded rows must not raise it, or every pass would extend past the last."""
    await apply_sitemap(db, entries(("project", 40, NOW)), now=NOW)
    first = await frontier.extend_scan(db, "project", margin=5, now=NOW)

    for _ in range(3):
        again = await frontier.extend_scan(db, "project", margin=5, now=NOW)
        assert again["seeded"] == 0
        assert again["covered_to"] == first["covered_to"]
