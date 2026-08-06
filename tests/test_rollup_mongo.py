from __future__ import annotations

from datetime import datetime, timezone

import pytest
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo.errors import PyMongoError

from src.collector.rollup import SCOPE, rollup_global
from src.collector.sitemap import SitemapEntry, apply_sitemap
from src.config import settings
from src.db import bootstrap

TEST_DB = "stardance_stats_test_rollup"
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


async def test_totals_sum_the_corpus_and_carry_the_frontier_size(db):
    await db.projects.insert_many([
        {"_id": 1, "stats": {"total_hours": 10.5, "stardust_total": 100, "likes": 4}},
        {"_id": 2, "stats": {"total_hours": 2.25, "stardust_total": 50, "likes": 1}},
    ])
    await db.users.insert_one({"_id": 7})
    await db.devlogs.insert_many([{"_id": 1}, {"_id": 2}, {"_id": 3}])
    await apply_sitemap(
        db,
        [
            SitemapEntry("project", 1, "/projects/1", NOW),
            SitemapEntry("project", 2, "/projects/2", NOW),
            SitemapEntry("project", 3, "/projects/3", NOW),
            SitemapEntry("user", 7, "/users/7", NOW),
        ],
        now=NOW,
    )

    doc = await rollup_global(db, now=NOW)

    assert doc["hours"] == 12.75
    assert doc["stardust_paid"] == 150
    assert doc["likes"] == 5
    assert doc["devlogs"] == 3
    # The gap is the point: an early snapshot is a lower bound.
    assert doc["projects"] == 2
    assert doc["projects_known"] == 3

    stored = await db.global_snapshots.find_one({"scope": SCOPE})
    assert stored["ts"] == NOW
    assert stored["stardust_paid"] == 150


async def test_gone_rows_are_excluded(db):
    await db.projects.insert_many([
        {"_id": 1, "stats": {"total_hours": 5.0, "stardust_total": 10}},
        {"_id": 2, "gone": True, "stats": {"total_hours": 99.0, "stardust_total": 999}},
    ])
    doc = await rollup_global(db, now=NOW)
    assert doc["projects"] == 1
    assert doc["hours"] == 5.0


async def test_missing_stats_do_not_break_the_sum(db):
    """A project crawled before its stats block existed must not null the total."""
    await db.projects.insert_many([
        {"_id": 1, "stats": {"total_hours": 5.0}},
        {"_id": 2},
        {"_id": 3, "stats": {"total_hours": None}},
    ])
    doc = await rollup_global(db, now=NOW)
    assert doc["hours"] == 5.0
    assert doc["stardust_paid"] == 0
