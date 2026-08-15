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

TEST_DB = "stardance_stats_test_projects"
NOW = datetime.now(timezone.utc)

pytestmark = pytest.mark.asyncio

# id, title, stardust, hours, likes
PROJECTS = [
    (1, "Crawssembly", 3000, 250.0, 200),
    (2, "Crawler", 2000, 40.0, 90),
    (3, "Nightcrawler", 1000, 120.0, 10),
    (4, "Something else", 500, 300.0, 400),
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
    await database.projects.insert_many(
        [
            {
                "_id": pid,
                "title": title,
                "owner_username": f"owner{pid}",
                "owner_id": pid * 10,
                "stats": {
                    "stardust_total": stardust,
                    "total_hours": hours,
                    "likes": likes,
                },
                "last_crawled": NOW,
            }
            for pid, title, stardust, hours, likes in PROJECTS
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


async def board(client, **params):
    response = await client.get("/v1/projects", params=params)
    assert response.status_code == 200, response.text
    return response.json()


async def search(client, q, **params):
    response = await client.get("/v1/projects/search", params={"q": q, **params})
    assert response.status_code == 200, response.text
    return response.json()


async def test_the_ranking_runs_highest_first(client):
    titles = [item["title"] for item in (await board(client))["items"]]
    assert titles == ["Crawssembly", "Crawler", "Nightcrawler", "Something else"]


async def test_another_metric_reorders_it(client):
    titles = [item["title"] for item in (await board(client, metric="total_hours"))["items"]]
    assert titles[0] == "Something else"


async def test_ranks_carry_on_past_the_offset(client):
    page = await board(client, limit=2, offset=2)
    assert [item["rank"] for item in page["items"]] == [3, 4]
    assert page["total"] == 4


async def test_a_card_carries_the_numbers_it_shows(client):
    item = (await board(client))["items"][0]
    assert item["value"] == 3000
    assert item["stats"]["total_hours"] == 250.0
    assert item["owner_username"] == "owner1"


async def test_projects_without_the_metric_stay_out(db, client):
    await db.projects.insert_one({"_id": 9, "title": "Blank", "stats": {}})
    assert (await board(client))["total"] == 4


async def test_only_super_stars_when_asked(db, client):
    await db.projects.update_one({"_id": 2}, {"$set": {"is_super_star": True}})
    page = await board(client, super_star=True)
    assert [item["title"] for item in page["items"]] == ["Crawler"]


async def test_only_hardware_when_asked(db, client):
    await db.projects.update_one({"_id": 3}, {"$set": {"is_hardware": True}})
    assert [i["title"] for i in (await board(client, hardware=True))["items"]] == ["Nightcrawler"]


async def test_a_mission_narrows_the_ranking(db, client):
    await db.projects.update_one({"_id": 4}, {"$set": {"mission": {"slug": "frictionless"}}})
    page = await board(client, mission="frictionless")
    assert [item["title"] for item in page["items"]] == ["Something else"]


async def test_an_unknown_metric_is_refused(client):
    assert (await client.get("/v1/projects", params={"metric": "nonsense"})).status_code == 400


async def test_a_partial_title_finds_the_longer_ones(client):
    titles = [item["title"] for item in (await search(client, "crawl"))["items"]]
    assert "Crawler" in titles
    assert "Nightcrawler" in titles


async def test_prefixes_come_before_mere_containers(client):
    titles = [item["title"] for item in (await search(client, "crawl"))["items"]]
    assert titles.index("Crawler") < titles.index("Nightcrawler")


async def test_an_exact_title_leads(client):
    items = (await search(client, "cRaWlEr"))["items"]
    assert items[0]["title"] == "Crawler"
    assert items[0]["exact"] is True


async def test_regex_characters_are_matched_literally(client):
    assert (await search(client, "crawl."))["total"] == 0


async def test_results_carry_their_standing_in_the_ranking(client):
    item = (await search(client, "Nightcrawler"))["items"][0]
    assert item["rank"] == 3
    assert item["value"] == 1000


async def test_the_rank_follows_the_metric_asked_for(client):
    item = (await search(client, "Nightcrawler", metric="total_hours"))["items"][0]
    assert item["rank"] == 3
    assert (await search(client, "Something", metric="total_hours"))["items"][0]["rank"] == 1


async def test_a_tie_is_settled_by_id(db, client):
    """Crawssembly already holds 3000, so these two take the places behind it, not its own."""
    await db.projects.insert_many(
        [
            {"_id": 5, "title": "Tie one", "stats": {"stardust_total": 3000}},
            {"_id": 6, "title": "Tie two", "stats": {"stardust_total": 3000}},
        ]
    )
    ranks = {i["title"]: i["rank"] for i in (await search(client, "Tie "))["items"]}
    assert ranks == {"Tie one": 2, "Tie two": 3}


async def test_a_searched_rank_is_the_one_the_board_shows(db, client):
    """The two endpoints count the same ranking, so a tie must not read differently in each."""
    await db.projects.insert_many(
        [
            {"_id": 5, "title": "Tie one", "stats": {"stardust_total": 3000}},
            {"_id": 6, "title": "Tie two", "stats": {"stardust_total": 3000}},
        ]
    )
    listed = {i["title"]: i["rank"] for i in (await board(client))["items"]}
    hits = {i["title"]: i["rank"] for i in (await search(client, "Tie "))["items"]}
    assert {title: listed[title] for title in hits} == hits


async def test_a_project_the_ranking_does_not_hold_has_no_rank(db, client):
    await db.projects.insert_one({"_id": 7, "title": "Unrated", "stats": {}})
    item = (await search(client, "Unrated"))["items"][0]
    assert item["rank"] is None
    assert item["value"] is None


async def test_the_limit_is_honoured(client):
    assert len((await search(client, "craw", limit=2))["items"]) == 2


async def test_search_is_not_read_as_a_project_id(client):
    """The route sits above /projects/{project_id}, so this must not 404 or 422."""
    response = await client.get("/v1/projects/search", params={"q": "nothing-at-all"})
    assert response.status_code == 200
    assert response.json()["items"] == []


async def devlogs(client, project_id=1, **params):
    response = await client.get(f"/v1/projects/{project_id}/devlogs", params=params)
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture
async def posts(db):
    """Ten devlogs on project 1, newest last, with engagement running the other way."""
    await db.devlogs.insert_many(
        [
            {
                "_id": 100 + n,
                "project_id": 1,
                "posted_at": datetime(2026, 8, 1 + n, tzinfo=timezone.utc),
                "likes": n,
                "views": 100 - n,
                "duration_seconds": 60 * n,
            }
            for n in range(10)
        ]
    )
    return 10


async def test_devlogs_come_back_a_page_at_a_time(client, posts):
    page = await devlogs(client, limit=4)
    assert page["total"] == 10
    assert page["limit"] == 4 and page["offset"] == 0
    assert [d["_id"] for d in page["items"]] == [109, 108, 107, 106]


async def test_the_next_page_carries_on_where_it_left_off(client, posts):
    page = await devlogs(client, limit=4, offset=4)
    assert [d["_id"] for d in page["items"]] == [105, 104, 103, 102]


async def test_the_last_page_is_short_rather_than_padded(client, posts):
    page = await devlogs(client, limit=4, offset=8)
    assert len(page["items"]) == 2


async def test_paging_past_the_end_is_empty_not_an_error(client, posts):
    assert (await devlogs(client, limit=4, offset=40))["items"] == []


async def test_devlogs_can_be_sorted_by_views(client, posts):
    page = await devlogs(client, sort="views", limit=3)
    assert [d["views"] for d in page["items"]] == [100, 99, 98]


async def test_devlogs_can_be_sorted_by_time_logged(client, posts):
    page = await devlogs(client, sort="duration_seconds", limit=2)
    assert [d["duration_seconds"] for d in page["items"]] == [540, 480]


async def test_an_unknown_sort_is_refused(client, posts):
    response = await client.get("/v1/projects/1/devlogs", params={"sort": "nonsense"})
    assert response.status_code == 422


async def test_tied_rows_do_not_repeat_or_vanish_across_pages(db, client, posts):
    """Every row shares a like count, so only the tiebreak keeps paging honest."""
    await db.devlogs.update_many({"project_id": 1}, {"$set": {"likes": 5}})
    seen = []
    for offset in (0, 4, 8):
        seen += [d["_id"] for d in (await devlogs(client, sort="likes", limit=4, offset=offset))["items"]]
    assert sorted(seen) == [100 + n for n in range(10)]


async def test_the_metric_list_is_not_read_as_a_project_id(client):
    response = await client.get("/v1/projects/metrics")
    assert response.status_code == 200
    assert "stardust_total" in response.json()["metrics"]
