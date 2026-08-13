from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo.errors import PyMongoError

from src.collector import frontier
from src.collector.run import drain
from src.config import settings
from src.db import bootstrap
from src.fetcher import FetchResult

TEST_DB = "stardance_stats_test_scrape_all"
UTC = timezone.utc
NOW = datetime.now(UTC)
LATER = NOW + timedelta(days=7)

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
    await database.crawl_frontier.insert_many(
        [
            {"_id": "project:1", "kind": "project", "ref_id": 1, "next_due": LATER,
             "tier": "frozen"},
            {"_id": "project:2", "kind": "project", "ref_id": 2, "next_due": LATER,
             "tier": "cold"},
            {"_id": "user:1", "kind": "user", "ref_id": 1, "next_due": LATER, "tier": "cold"},
            {"_id": "user:2", "kind": "user", "ref_id": 2, "next_due": LATER, "gone": True},
            {"_id": "devlog:1", "kind": "devlog", "ref_id": 1, "next_due": LATER,
             "parent_id": 1},
        ]
    )
    try:
        yield database
    finally:
        await client.drop_database(TEST_DB)
        client.close()


class StubFetcher:
    """Answers 404 to everything and counts what was asked for."""

    endpoints = [object()]
    aggregate_rps = 1.0

    def __init__(self):
        self.paths: list[str] = []

    async def get(self, path, etag=None, last_modified=None):
        self.paths.append(path)
        return FetchResult(
            f"https://example.test{path}", 404, None, None, None, from_cache=False
        )


class BrokenFetcher(StubFetcher):
    """Fails every time, which is what leaves a row unrescheduled."""

    async def get(self, path, etag=None, last_modified=None):
        self.paths.append(path)
        raise RuntimeError("no route to host")


async def due_ids(db) -> set[str]:
    return {row["_id"] for row in await frontier.due(db, limit=100)}


async def test_nothing_is_due_before_the_sweep(db):
    assert await due_ids(db) == set()


async def test_marking_brings_the_whole_frontier_forward(db):
    result = await frontier.mark_all_due(db)
    assert result["marked"] == 4
    assert await due_ids(db) == {"project:1", "project:2", "user:1", "devlog:1"}


async def test_pages_that_are_gone_stay_out_by_default(db):
    await frontier.mark_all_due(db)
    assert "user:2" not in await due_ids(db)


async def test_gone_pages_can_be_asked_for(db):
    await frontier.mark_all_due(db, include_gone=True)
    assert "user:2" in await due_ids(db)
    row = await db.crawl_frontier.find_one({"_id": "user:2"})
    assert "gone" not in row


async def test_one_kind_can_be_marked_alone(db):
    await frontier.mark_all_due(db, kind="user")
    assert await due_ids(db) == {"user:1"}


async def test_the_sweep_reads_every_marked_row_then_stops(db):
    await frontier.mark_all_due(db)
    fetcher = StubFetcher()

    result = await drain(db, fetcher, batch=2)

    assert result["crawled"] == 4
    assert result["stopped"] == "drained"
    assert len(fetcher.paths) == 4


async def test_a_page_budget_holds_the_sweep_back(db):
    await frontier.mark_all_due(db)

    result = await drain(db, StubFetcher(), max_pages=2, batch=2)

    assert result["crawled"] == 2
    assert result["stopped"] == "budget"


async def test_a_batch_that_only_crashes_ends_the_sweep(db):
    """A crash leaves the row due, so without this the sweep would never end."""
    await frontier.mark_all_due(db)
    fetcher = BrokenFetcher()

    result = await drain(db, fetcher, batch=2)

    assert result["stopped"] == "stuck"
    assert len(fetcher.paths) == 2


async def test_one_kind_sweeps_alone(db):
    await frontier.mark_all_due(db)
    fetcher = StubFetcher()

    result = await drain(db, fetcher, kind="user")

    assert result["crawled"] == 1
    assert fetcher.paths == ["/users/1/projects"]


async def test_the_tier_is_left_to_reassert_itself(db):
    """A sweep is one pass, not a permanent change of pace."""
    await frontier.mark_all_due(db)
    rows = {r["_id"]: r for r in await db.crawl_frontier.find({}).to_list(length=10)}
    assert rows["project:1"]["tier"] == "frozen"
    assert rows["project:2"]["tier"] == "cold"
