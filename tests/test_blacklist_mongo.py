from __future__ import annotations

from datetime import datetime, timezone

import pytest
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo.errors import PyMongoError

from src import blacklist
from src.collector import frontier
from src.collector.sitemap import SitemapEntry, apply_sitemap
from src.config import settings
from src.db import bootstrap

TEST_DB = "stardance_stats_test_blacklist"
NOW = datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)

BANNED = 285
BYSTANDER = 900

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


@pytest.fixture(autouse=True)
def blacklisted(monkeypatch):
    monkeypatch.setattr(settings, "blacklist_users", str(BANNED))
    blacklist.forget()
    yield
    blacklist.forget()


async def seed(db) -> None:
    """One blacklisted account plus a bystander whose rows must survive."""
    await db.users.insert_many([
        {"_id": BANNED, "username": "Banned", "username_lower": "banned"},
        {"_id": BYSTANDER, "username": "Keeper", "username_lower": "keeper"},
    ])
    await db.projects.insert_many([
        {"_id": 10, "owner_id": BANNED, "owner_username": "Banned", "members": []},
        # Owned by a handle we have not resolved to an id yet.
        {"_id": 11, "owner_id": None, "owner_username": "Banned", "members": []},
        {
            "_id": 12,
            "owner_id": BYSTANDER,
            "owner_username": "Keeper",
            "members": ["Banned"],
            "member_ids": [BANNED],
        },
    ])
    await db.devlogs.insert_many([
        {"_id": 100, "project_id": 10, "user_id": BANNED, "username_lower": "banned"},
        # Theirs, but posted on someone else's project.
        {"_id": 101, "project_id": 12, "user_id": BANNED, "username_lower": "banned"},
        {"_id": 102, "project_id": 12, "user_id": BYSTANDER, "username_lower": "keeper"},
    ])
    await db.comments.insert_many([
        {"_id": 1000, "devlog_id": 100, "project_id": 10, "user_id": BANNED},
        # A bystander's comment on the banned account's thread.
        {"_id": 1001, "devlog_id": 100, "project_id": 10, "user_id": BYSTANDER},
        # A banned comment on the bystander's thread.
        {"_id": 1002, "devlog_id": 102, "project_id": 12, "user_id": BANNED},
        {"_id": 1003, "devlog_id": 102, "project_id": 12, "user_id": BYSTANDER},
    ])
    await db.ships.insert_many([
        {"_id": 500, "project_id": 10, "user_id": BANNED, "username_lower": "banned"},
        {"_id": 501, "project_id": 12, "user_id": BYSTANDER, "username_lower": "keeper"},
    ])
    await db.user_snapshots.insert_many([
        {"ts": NOW, "uid": BANNED}, {"ts": NOW, "uid": BYSTANDER}
    ])
    await db.project_snapshots.insert_many([{"ts": NOW, "pid": 10}, {"ts": NOW, "pid": 12}])
    await db.devlog_snapshots.insert_many([{"ts": NOW, "did": 100}, {"ts": NOW, "did": 102}])
    await db.crawl_frontier.insert_many([
        {"_id": "user:285", "kind": "user", "ref_id": BANNED},
        {"_id": "project:10", "kind": "project", "ref_id": 10},
        {"_id": "devlog:100", "kind": "devlog", "ref_id": 100},
        {"_id": "user:900", "kind": "user", "ref_id": BYSTANDER},
    ])


async def test_purge_removes_every_trace(db):
    await seed(db)

    result = await blacklist.purge(db)

    assert result["users"] == [BANNED]
    assert result["projects"] == [10, 11]
    assert await db.users.find_one({"_id": BANNED}) is None
    assert await db.projects.count_documents({"_id": {"$in": [10, 11]}}) == 0
    assert await db.devlogs.count_documents({"user_id": BANNED}) == 0
    assert await db.comments.count_documents({"user_id": BANNED}) == 0
    assert await db.ships.count_documents({"user_id": BANNED}) == 0
    assert await db.user_snapshots.count_documents({"uid": BANNED}) == 0
    assert await db.project_snapshots.count_documents({"pid": 10}) == 0
    assert await db.devlog_snapshots.count_documents({"did": 100}) == 0


async def test_purge_takes_their_threads_with_them(db):
    await seed(db)

    await blacklist.purge(db)

    # The bystander commented on a thread that is going away.
    assert await db.comments.find_one({"_id": 1001}) is None
    assert await db.devlogs.find_one({"_id": 100}) is None


async def test_purge_leaves_bystanders_alone(db):
    await seed(db)

    result = await blacklist.purge(db)

    assert await db.users.find_one({"_id": BYSTANDER}) is not None
    assert await db.projects.find_one({"_id": 12}) is not None
    assert await db.devlogs.find_one({"_id": 102}) is not None
    assert await db.comments.find_one({"_id": 1003}) is not None
    assert await db.ships.find_one({"_id": 501}) is not None
    assert await db.user_snapshots.count_documents({"uid": BYSTANDER}) == 1
    # Their totals counted rows that just went, so they need recomputing.
    assert BYSTANDER in result["affected_users"]
    assert BANNED not in result["affected_users"]


async def test_purge_pulls_them_out_of_shared_projects(db):
    await seed(db)

    await blacklist.purge(db)

    shared = await db.projects.find_one({"_id": 12})
    assert shared["members"] == []
    assert shared["member_ids"] == []


async def test_purge_clears_the_frontier(db):
    await seed(db)

    await blacklist.purge(db)

    assert await db.crawl_frontier.find_one({"_id": "user:285"}) is None
    assert await db.crawl_frontier.find_one({"_id": "devlog:100"}) is None
    # Kept, because the sitemap would only list the project again.
    assert (await db.crawl_frontier.find_one({"_id": "project:10"}))["gone"] is True
    assert await db.crawl_frontier.find_one({"_id": "user:900"}) is not None


async def test_purge_is_repeatable_on_an_empty_database(db):
    await seed(db)

    await blacklist.purge(db)
    again = await blacklist.purge(db)

    assert again["deleted"] == {}


async def test_handles_outlive_the_purge(db):
    await seed(db)

    await blacklist.purge(db)

    # The profile is gone, so the handle has to come back from crawl_state.
    assert await blacklist.is_blocked_handle(db, "banned") is True
    assert await blacklist.is_blocked_handle(db, "keeper") is False


async def test_remembering_a_handle_blocks_it(db):
    await blacklist.remember(db, "Banned")

    assert await blacklist.is_blocked_handle(db, "banned") is True


async def test_seeding_skips_blacklisted_ids(db):
    await frontier.seed_id_range(db, "user", BANNED - 1, BANNED + 1, now=NOW)

    ids = await db.crawl_frontier.distinct("ref_id", {"kind": "user"})
    assert sorted(ids) == [BANNED - 1, BANNED + 1]


async def test_seeding_only_skips_the_user_lane(db):
    await frontier.seed_id_range(db, "project", BANNED, BANNED, now=NOW)

    assert await db.crawl_frontier.find_one({"_id": f"project:{BANNED}"}) is not None


async def test_sitemap_never_relists_them(db):
    entries = [
        SitemapEntry("user", BANNED, f"/users/{BANNED}", None),
        SitemapEntry("user", BYSTANDER, f"/users/{BYSTANDER}", None),
    ]

    counts = await apply_sitemap(db, entries, now=NOW)

    assert counts["skipped"] == 1
    assert await db.crawl_frontier.find_one({"_id": "user:285"}) is None
    assert await db.crawl_frontier.find_one({"_id": "user:900"}) is not None
