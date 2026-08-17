from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo.errors import PyMongoError

from src.api.deps import db as db_dep
from src.api.main import app
from src.config import settings
from src.db import bootstrap

TEST_DB = "stardance_stats_test_devlogs"
NOW = datetime.now(timezone.utc)

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

    await database.projects.insert_many(
        [
            {"_id": 1, "title": "Crawssembly", "is_super_star": True, "last_crawled": NOW},
            {"_id": 2, "title": "Nightcrawler", "last_crawled": NOW},
        ]
    )
    await database.users.insert_one(
        {"_id": 10, "username": "Ada", "username_lower": "ada", "avatar_url": "a.png"}
    )
    # Ten devlogs, newest last, with likes running the same way and views the other.
    await database.devlogs.insert_many(
        [
            {
                "_id": 100 + n,
                "project_id": 1 if n % 2 else 2,
                "username": "Ada" if n % 2 else "Bo",
                "username_lower": "ada" if n % 2 else "bo",
                "posted_at": NOW - timedelta(days=9 - n),
                "likes": n,
                "comments": n % 3,
                "views": 100 - n,
                "duration_seconds": 60 * n,
                "media": [{"kind": "image", "url": f"{n}.png"}] if n < 3 else [],
                "body": f"devlog {n} about soldering the {'motor' if n % 2 else 'sensor'}",
                "last_crawled": NOW,
            }
            for n in range(10)
        ]
    )

    try:
        yield database
    finally:
        await client.drop_database(TEST_DB)
        client.close()


@pytest.fixture
async def client(db):
    app.dependency_overrides[db_dep] = lambda: db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http
    app.dependency_overrides.clear()


async def feed(client, **params):
    response = await client.get("/v1/devlogs", params=params)
    assert response.status_code == 200, response.text
    return response.json()


async def test_the_feed_runs_newest_first(client):
    assert [d["_id"] for d in (await feed(client, limit=3))["items"]] == [109, 108, 107]


async def test_a_ranking_reorders_it(client):
    page = await feed(client, sort="views", limit=3)
    assert [d["views"] for d in page["items"]] == [100, 99, 98]


async def test_ranks_carry_on_past_the_offset(client):
    page = await feed(client, sort="likes", limit=2, offset=4)
    assert [d["rank"] for d in page["items"]] == [5, 6]
    assert page["total"] == 10


async def test_the_newest_first_feed_ranks_nothing(client):
    assert {d["rank"] for d in (await feed(client))["items"]} == {None}


async def test_a_window_leaves_the_older_ones_out(client):
    page = await feed(client, days=3)
    assert [d["_id"] for d in page["items"]] == [109, 108, 107]
    assert page["total"] == 3


async def test_only_devlogs_with_media_when_asked(client):
    page = await feed(client, with_media=True)
    assert sorted(d["_id"] for d in page["items"]) == [100, 101, 102]


async def test_one_project_narrows_the_feed(client):
    assert {d["project_id"] for d in (await feed(client, project_id=2))["items"]} == {2}


async def test_one_author_narrows_the_feed(client):
    page = await feed(client, username="@ADA")
    assert {d["username"] for d in page["items"]} == {"Ada"}


async def test_rows_without_the_ranked_number_stay_out(db, client):
    await db.devlogs.insert_one({"_id": 200, "project_id": 1, "posted_at": NOW, "likes": None})
    assert (await feed(client, sort="likes"))["total"] == 10


async def test_a_card_carries_the_project_and_author_it_belongs_to(client):
    items = (await feed(client))["items"]
    ada = next(d for d in items if d["username"] == "Ada")
    assert ada["project_title"] == "Crawssembly"
    assert ada["is_super_star"] is True
    assert ada["avatar_url"] == "a.png"

    # Bo is not a user we hold, so the card carries the project and no face.
    bo = next(d for d in items if d["username"] == "Bo")
    assert bo["project_title"] == "Nightcrawler"
    assert bo["is_super_star"] is False
    assert bo["avatar_url"] is None


async def test_tied_rows_do_not_repeat_or_vanish_across_pages(db, client):
    """Every row shares a like count, so only the tiebreak keeps paging honest."""
    await db.devlogs.update_many({}, {"$set": {"likes": 5}})
    seen = []
    for offset in (0, 4, 8):
        seen += [d["_id"] for d in (await feed(client, sort="likes", limit=4, offset=offset))["items"]]
    assert sorted(seen) == [100 + n for n in range(10)]


async def test_a_search_finds_the_devlogs_that_say_it(client):
    page = await feed(client, q="motor")
    assert {d["_id"] for d in page["items"]} == {101, 103, 105, 107, 109}
    assert page["total"] == 5


async def test_a_search_reaches_the_stem_of_a_word(client):
    """The index stems, so the shorter form finds the longer one; half a word does not."""
    assert (await feed(client, q="soldering"))["total"] == 10
    assert (await feed(client, q="solder"))["total"] == 10
    assert (await feed(client, q="sold"))["total"] == 0


async def test_a_phrase_in_quotes_must_appear_as_written(client):
    assert (await feed(client, q='"soldering the sensor"'))["total"] == 5


async def test_a_search_narrows_with_the_other_filters(client):
    page = await feed(client, q="soldering", days=3)
    assert [d["_id"] for d in page["items"]] == [109, 108, 107]


async def test_relevance_ranks_a_search(client):
    page = await feed(client, q="motor", sort="relevance")
    assert [d["rank"] for d in page["items"]] == [1, 2, 3, 4, 5]


async def test_relevance_without_a_search_is_refused(client):
    assert (await client.get("/v1/devlogs", params={"sort": "relevance"})).status_code == 400


async def test_a_single_letter_is_refused(client):
    assert (await client.get("/v1/devlogs", params={"q": "a"})).status_code == 422


async def test_the_order_can_be_turned_around(client):
    page = await feed(client, order="asc", limit=3)
    assert [d["_id"] for d in page["items"]] == [100, 101, 102]


async def test_turning_a_ranking_around_reads_the_other_end(client):
    page = await feed(client, sort="likes", order="asc", limit=2)
    assert [d["likes"] for d in page["items"]] == [0, 1]


async def test_devlogs_can_be_ranked_by_reposts(db, client):
    await db.devlogs.update_one({"_id": 104}, {"$set": {"reposts": 9}})
    assert (await feed(client, sort="reposts", limit=1))["items"][0]["_id"] == 104


async def test_an_unknown_sort_is_refused(client):
    assert (await client.get("/v1/devlogs", params={"sort": "nonsense"})).status_code == 422


async def test_the_feed_is_not_read_as_a_devlog_id(client):
    """The route sits above /devlogs/{devlog_id}, so this must not 404 or 422."""
    response = await client.get("/v1/devlogs")
    assert response.status_code == 200
    assert response.json()["items"]
