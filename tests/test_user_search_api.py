from __future__ import annotations

from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo.errors import PyMongoError

from src.api.deps import db as db_dep
from src.api.main import app
from src.config import settings
from src.db import bootstrap

TEST_DB = "stardance_stats_test_search"
NOW = datetime.now(timezone.utc)

pytestmark = pytest.mark.asyncio

HANDLES = [
    (1, "mixidmb"),
    (2, "mixid_bot"),
    (3, "notmixid"),
    (4, "MIXID"),
    (5, "someone_else"),
]


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
    await database.users.insert_many(
        [
            {
                "_id": uid,
                "username": name,
                "username_lower": name.lower(),
                "avatar_url": f"https://example.test/{uid}",
                "stats": {"followers": uid},
                "totals": {"ship_stardust": uid * 100},
                "last_crawled": NOW,
            }
            for uid, name in HANDLES
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


async def search(client, q, **params):
    response = await client.get("/v1/users/search", params={"q": q, **params})
    assert response.status_code == 200, response.text
    return response.json()


async def test_a_partial_handle_finds_the_longer_ones(client):
    names = [item["username"] for item in (await search(client, "mixid"))["items"]]
    assert "mixidmb" in names
    assert "mixid_bot" in names


async def test_prefixes_come_before_mere_containers(client):
    names = [item["username"] for item in (await search(client, "mixid"))["items"]]
    assert names.index("mixidmb") < names.index("notmixid")


async def test_an_exact_handle_leads(client):
    items = (await search(client, "MiXiD"))["items"]
    assert items[0]["username"] == "MIXID"
    assert items[0]["exact"] is True


async def test_the_at_sign_is_ignored(client):
    assert (await search(client, "@mixidmb"))["total"] == 1


async def test_results_carry_the_numbers_a_ranking_shows(client):
    item = (await search(client, "mixidmb"))["items"][0]
    assert item["totals"]["ship_stardust"] == 100
    assert item["stats"]["followers"] == 1
    assert item["avatar_url"].endswith("/1")


async def test_regex_characters_are_matched_literally(client):
    assert (await search(client, "mixid."))["total"] == 0


async def test_hidden_people_stay_out(db, client):
    await db.users.insert_one(
        {"_id": 9, "username": "mixid_hidden", "username_lower": "mixid_hidden", "hidden": True}
    )
    names = [item["username"] for item in (await search(client, "mixid"))["items"]]
    assert "mixid_hidden" not in names


async def test_the_limit_is_honoured(client):
    assert len((await search(client, "mixid", limit=2))["items"]) == 2


async def test_results_carry_their_standing_in_the_ranking(client):
    """ship_stardust runs 100..500, so mixidmb on 100 is last of the five."""
    item = (await search(client, "mixidmb"))["items"][0]
    assert item["rank"] == 5
    assert item["value"] == 100


async def test_the_rank_follows_the_metric_asked_for(client):
    item = (await search(client, "mixidmb", metric="followers"))["items"][0]
    assert item["rank"] == 5
    assert (await search(client, "someone_else", metric="followers"))["items"][0]["rank"] == 1


async def test_a_tie_takes_the_better_place(db, client):
    await db.users.insert_many(
        [
            {"_id": 10, "username": "mixid_tie_a", "username_lower": "mixid_tie_a",
             "totals": {"ship_stardust": 500}},
            {"_id": 11, "username": "mixid_tie_b", "username_lower": "mixid_tie_b",
             "totals": {"ship_stardust": 500}},
        ]
    )
    ranks = {i["username"]: i["rank"] for i in (await search(client, "mixid_tie"))["items"]}
    assert ranks == {"mixid_tie_a": 1, "mixid_tie_b": 1}


async def test_somebody_the_ranking_does_not_hold_has_no_rank(db, client):
    await db.users.insert_one(
        {"_id": 12, "username": "mixid_new", "username_lower": "mixid_new", "totals": {}}
    )
    item = next(
        i for i in (await search(client, "mixid_new"))["items"] if i["username"] == "mixid_new"
    )
    assert item["rank"] is None
    assert item["value"] is None


async def test_hidden_people_do_not_pad_the_rank(db, client):
    await db.users.insert_one(
        {"_id": 13, "username": "ghost", "username_lower": "ghost", "hidden": True,
         "totals": {"ship_stardust": 9999}}
    )
    assert (await search(client, "someone_else"))["items"][0]["rank"] == 1


async def test_an_unknown_metric_is_refused(client):
    response = await client.get("/v1/users/search", params={"q": "mixid", "metric": "nonsense"})
    assert response.status_code == 400


async def test_a_handle_is_not_read_as_the_word_search(client):
    """The route sits above /users/{ref}, so this must not 404 as an unknown user."""
    response = await client.get("/v1/users/search", params={"q": "nobody-at-all"})
    assert response.status_code == 200
    assert response.json()["items"] == []
