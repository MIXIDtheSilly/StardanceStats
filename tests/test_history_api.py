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

TEST_DB = "stardance_stats_test_api"
UTC = timezone.utc
# locf stops at the real clock, so the fixtures hang off today rather than a fixed date.
TODAY = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
NOW = TODAY + timedelta(hours=12)
DAY1 = TODAY - timedelta(days=5)
PID = 8100

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


@pytest.fixture
async def client(db):
    app.dependency_overrides[db_dep] = lambda: db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http
    app.dependency_overrides.clear()


async def seed(db, points, *, last_crawled=NOW):
    """A project plus a snapshot per (day offset, likes) pair."""
    await db.projects.insert_one(
        {"_id": PID, "title": "Crawssembly", "owner_username": "The_Craw",
         "last_crawled": last_crawled}
    )
    docs = []
    for offset, likes, *rest in points:
        doc = {"ts": DAY1 + timedelta(days=offset), "pid": PID, "likes": likes,
               "devlogs": 10 + offset}
        if rest and rest[0]:
            doc["synthetic"] = True
        docs.append(doc)
    if docs:
        await db.project_snapshots.insert_many(docs)


async def history(client, **params):
    params.setdefault("metrics", "likes")
    response = await client.get(f"/v1/projects/{PID}/history", params=params)
    assert response.status_code == 200, response.text
    return response.json()


async def test_a_bucket_reports_its_last_observation(db, client):
    await db.projects.insert_one({"_id": PID, "title": "Crawssembly", "last_crawled": NOW})
    await db.project_snapshots.insert_many([
        {"ts": DAY1 + timedelta(hours=2), "pid": PID, "likes": 10},
        {"ts": DAY1 + timedelta(hours=20), "pid": PID, "likes": 12},
        {"ts": DAY1 + timedelta(days=1), "pid": PID, "likes": 15},
    ])

    points = (await history(client, interval="1d"))["series"]["likes"]
    assert [p["v"] for p in points] == [12, 15]


async def test_deltas_are_computed_per_request_not_stored(db, client):
    await seed(db, [(0, 10), (1, 14), (2, 14)])

    points = (await history(client, interval="1d"))["series"]["likes"]
    assert "d" not in points[0]                    # nothing earlier to subtract
    assert [p["d"] for p in points[1:]] == [4, 0]

    stored = await db.project_snapshots.find_one({"pid": PID})
    assert "d" not in stored


async def test_delta_can_be_switched_off(db, client):
    await seed(db, [(0, 10), (1, 14)])
    points = (await history(client, interval="1d", delta="false"))["series"]["likes"]
    assert all("d" not in p for p in points)


async def test_gaps_stay_gaps_unless_fill_is_asked_for(db, client):
    await seed(db, [(0, 10), (4, 20)])

    plain = await history(client, interval="1d")
    assert [p["ts"][:10] for p in plain["series"]["likes"]] == [
        DAY1.date().isoformat(), (DAY1 + timedelta(days=4)).date().isoformat()
    ]
    assert plain["buckets"] == 2


async def test_locf_carries_the_last_observation_across_empty_buckets(db, client):
    """A chart needs a value per bucket; a snapshot is only written on change."""
    await seed(db, [(0, 10), (4, 20)])

    points = (await history(client, interval="1d", fill="locf", end=NOW.isoformat()))[
        "series"
    ]["likes"]

    assert [p["v"] for p in points] == [10, 10, 10, 10, 20, 20]
    assert [p.get("filled", False) for p in points] == [
        False, True, True, True, False, True
    ]
    # Carried points did not change, and the observation still reports its rise.
    assert [p["d"] for p in points[1:]] == [0, 0, 0, 10, 0]


async def test_a_window_opening_on_a_flat_stretch_is_not_empty(db, client):
    """Without a seed, a range whose observations all predate it renders blank."""
    await seed(db, [(0, 10)])
    window = {"start": (DAY1 + timedelta(days=2)).isoformat(), "end": NOW.isoformat()}

    assert (await history(client, interval="1d", **window))["series"]["likes"] == []

    filled = (await history(client, interval="1d", fill="locf", **window))["series"]["likes"]
    assert [p["v"] for p in filled] == [10, 10, 10, 10]      # DAY1+2 through today
    assert all(p["filled"] for p in filled)


async def test_fill_stops_at_now_rather_than_inventing_a_future(db, client):
    await seed(db, [(0, 10)])
    far = (NOW + timedelta(days=90)).isoformat()

    points = (await history(client, interval="1d", fill="locf", end=far))["series"]["likes"]
    assert points[-1]["ts"][:10] == NOW.date().isoformat()


async def test_backfilled_points_are_marked_synthetic(db, client):
    await seed(db, [(0, 10, True), (1, 12)])
    points = (await history(client, interval="1d"))["series"]["likes"]
    assert points[0]["synthetic"] is True
    assert "synthetic" not in points[1]


async def test_several_metrics_come_back_in_one_request(db, client):
    await seed(db, [(0, 10), (1, 12)])
    series = (await history(client, metrics="likes,devlogs"))["series"]
    assert sorted(series) == ["devlogs", "likes"]
    assert [p["v"] for p in series["devlogs"]] == [10, 11]


async def test_an_unknown_metric_is_refused_with_the_valid_ones(db, client):
    await seed(db, [(0, 10)])
    response = await client.get(f"/v1/projects/{PID}/history", params={"metrics": "karma"})
    assert response.status_code == 400
    assert "karma" in response.json()["detail"]
    assert "stardust_total" in response.json()["detail"]


async def test_too_many_buckets_is_refused_rather_than_allocated(db, client):
    await seed(db, [(0, 10)])
    response = await client.get(
        f"/v1/projects/{PID}/history",
        params={"metrics": "likes", "interval": "1h", "fill": "locf",
                "start": "2020-01-01T00:00:00Z"},
    )
    assert response.status_code == 400
    assert "buckets" in response.json()["detail"]


async def test_history_of_an_unknown_project_is_a_404(db, client):
    response = await client.get("/v1/projects/999999/history")
    assert response.status_code == 404


async def test_responses_carry_when_the_data_was_observed(db, client):
    """A reader cannot tell a flat line from a dead crawler without this."""
    long_ago = datetime.now(UTC) - timedelta(days=9)
    await seed(db, [(0, 10)], last_crawled=long_ago)

    body = await history(client)
    assert body["data_as_of"].startswith(long_ago.date().isoformat())
    assert body["stale"] is True

    doc = await client.get(f"/v1/projects/{PID}")
    assert doc.json()["stale"] is True


async def test_a_fresh_crawl_does_not_read_as_stale(db, client):
    await seed(db, [(0, 10)], last_crawled=datetime.now(UTC))
    assert (await history(client))["stale"] is False


async def test_an_unchanged_response_costs_a_304(db, client):
    await seed(db, [(0, 10)])
    first = await client.get(f"/v1/projects/{PID}")
    etag = first.headers["etag"]
    assert "max-age" in first.headers["cache-control"]

    again = await client.get(f"/v1/projects/{PID}", headers={"If-None-Match": etag})
    assert again.status_code == 304
    assert again.content == b""


async def test_global_history_is_served_from_the_rollups(db, client):
    await db.global_snapshots.insert_many([
        {"ts": DAY1, "scope": "platform", "users": 100, "projects": 20, "stardust_paid": 500},
        {"ts": DAY1 + timedelta(days=1), "scope": "platform", "users": 140,
         "projects": 25, "stardust_paid": 900},
    ])

    body = (await client.get(
        "/v1/global/history", params={"metrics": "users,stardust_paid"}
    )).json()
    assert [p["v"] for p in body["series"]["users"]] == [100, 140]
    assert body["series"]["stardust_paid"][1]["d"] == 400

    latest = (await client.get("/v1/global")).json()
    assert latest["totals"]["users"] == 140
    assert latest["data_as_of"] is not None


async def test_global_history_is_empty_before_the_first_rollup(db, client):
    assert (await client.get("/v1/global")).status_code == 404
    body = (await client.get("/v1/global/history")).json()
    assert body["buckets"] == 0
    assert body["series"]["users"] == []


async def test_a_devlog_can_be_fetched_on_its_own(db, client):
    await db.devlogs.insert_one(
        {"_id": 55231, "project_id": PID, "likes": 12, "last_crawled": NOW}
    )
    body = (await client.get("/v1/devlogs/55231")).json()
    assert body["likes"] == 12
    assert body["data_as_of"] is not None

    assert (await client.get("/v1/devlogs/1")).status_code == 404


async def test_meta_publishes_the_metric_vocabulary(db, client):
    body = (await client.get("/v1/meta")).json()
    assert "likes" in body["metrics"]["project"]
    assert "ship_stardust" in body["metrics"]["user"]
    assert "stardust_paid" in body["metrics"]["global"]
    assert body["history"]["fill"] == ["none", "locf"]
