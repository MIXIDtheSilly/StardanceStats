from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo.errors import PyMongoError

from src.collector.crawl_devlog import crawl_devlog, enqueue_stale_threads
from src.collector.run import crawl_one
from src.config import settings
from src.db import bootstrap
from src.fetcher import FetchResult

FIXTURE = Path(__file__).parent / "fixtures" / "devlog_28968.html"
TEST_DB = "stardance_stats_test"
UTC = timezone.utc
NOW = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)

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


class StubFetcher:
    """Answers one canned response and remembers what was asked for."""

    def __init__(self, status: int = 200, *, body: str | None = None, cached: bool = False):
        self.status = status
        self.body = body
        self.cached = cached
        self.paths: list[str] = []

    async def get(self, path, etag=None, last_modified=None):
        self.paths.append(path)
        return FetchResult(
            f"https://example.test{path}", self.status, self.body,
            "W/\"etag\"", None, from_cache=self.cached,
        )


async def seed(db, *, comments: int = 5, stale: bool = True):
    await db.devlogs.insert_one({
        "_id": 28968,
        "project_id": 8100,
        "username": "The_Craw",
        "username_lower": "the_craw",
        "comments": comments,
        "comments_stale": stale,
    })


async def test_a_thread_is_fetched_from_its_projects_route(db):
    await seed(db)
    fetcher = StubFetcher(body=FIXTURE.read_text(encoding="utf-8"))

    result = await crawl_devlog(db, fetcher, 28968)

    assert fetcher.paths == ["/projects/8100/devlogs/28968"]
    assert result["status"] == "ok"
    assert result["comments"] == 5
    assert await db.comments.count_documents({}) == 5

    row = await db.crawl_frontier.find_one({"_id": "devlog:28968"})
    assert row["last_status"] == "ok"
    assert row["parent_id"] == 8100


async def test_the_project_can_be_given_instead_of_looked_up(db):
    fetcher = StubFetcher(body=FIXTURE.read_text(encoding="utf-8"))
    result = await crawl_devlog(db, fetcher, 28968, project_id=8100)

    assert result["status"] == "ok"
    assert fetcher.paths == ["/projects/8100/devlogs/28968"]


async def test_a_devlog_we_have_never_seen_is_not_guessed_at(db):
    fetcher = StubFetcher(body="")
    result = await crawl_devlog(db, fetcher, 4242)

    assert result["status"] == "parse_error"
    assert fetcher.paths == []


async def test_an_unchanged_page_still_settles_the_flag(db):
    await seed(db, comments=4)
    result = await crawl_devlog(db, StubFetcher(304, cached=True), 28968)

    assert result["status"] == "not_modified"
    devlog = await db.devlogs.find_one({"_id": 28968})
    assert devlog["comments_stale"] is False
    # Or the next project crawl would queue this page straight back.
    assert devlog["comments_crawled_count"] == 4


async def test_a_deleted_devlog_stops_being_queued(db):
    await seed(db)
    result = await crawl_devlog(db, StubFetcher(404), 28968)

    assert result["status"] == "gone"
    devlog = await db.devlogs.find_one({"_id": 28968})
    assert devlog["gone"] is True
    assert devlog["comments_stale"] is False


async def test_a_broken_page_leaves_the_rows_we_hold(db):
    await seed(db)
    await crawl_devlog(db, StubFetcher(body=FIXTURE.read_text(encoding="utf-8")), 28968)

    result = await crawl_devlog(db, StubFetcher(body="<html>redesigned</html>"), 28968)

    assert result["status"] == "parse_error"
    assert await db.comments.count_documents({"gone": {"$ne": True}}) == 5


async def test_the_sweep_queues_flagged_threads_only(db):
    await seed(db)
    await db.devlogs.insert_one(
        {"_id": 1, "project_id": 8100, "comments": 0, "comments_stale": False}
    )

    outcome = await enqueue_stale_threads(db)

    assert outcome == {"queued": 1, "pending": 1}
    assert await db.crawl_frontier.count_documents({"kind": "devlog"}) == 1
    assert (await db.crawl_frontier.find_one({"_id": "devlog:28968"}))["ref_id"] == 28968


async def test_the_sweep_can_be_switched_off(db):
    await seed(db)
    settings.crawl_comments = False
    try:
        assert await enqueue_stale_threads(db) == {"queued": 0, "disabled": True}
    finally:
        settings.crawl_comments = True
    assert await db.crawl_frontier.count_documents({"kind": "devlog"}) == 0


async def test_the_collector_routes_a_devlog_row_to_the_thread_crawl(db):
    await seed(db)
    fetcher = StubFetcher(body=FIXTURE.read_text(encoding="utf-8"))

    row = {"kind": "devlog", "ref_id": 28968, "parent_id": 8100}
    result = await crawl_one(db, fetcher, row)

    assert result["status"] == "ok"
    assert result["devlog_id"] == 28968
