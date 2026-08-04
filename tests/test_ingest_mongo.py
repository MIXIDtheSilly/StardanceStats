from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo.errors import PyMongoError

from src.config import settings
from src.db import bootstrap
from src.ingest import AnomalyRejected, ingest_project
from src.parsers import parse_project_page

FIXTURE = Path(__file__).parent / "fixtures" / "project_8100.html"
TEST_DB = "stardance_stats_test"
UTC = timezone.utc

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


@pytest.fixture(scope="module")
def parsed():
    return parse_project_page(FIXTURE.read_text(encoding="utf-8"), 8100)


async def test_first_ingest_writes_everything(db, parsed):
    now = datetime(2026, 8, 3, 20, 0, tzinfo=UTC)
    summary = await ingest_project(db, parsed, now=now)

    assert summary["first_ingest"] is True
    assert summary["devlogs"] == 107
    assert summary["ships"] == 2
    assert summary["snapshot"] is True

    project = await db.projects.find_one({"_id": 8100})
    assert project["title"] == "Crawssembly"
    assert project["stats"]["stardust_total"] == 3042
    assert project["stats"]["likes"] == 271
    assert project["first_seen"] == now

    assert await db.devlogs.count_documents({"project_id": 8100}) == 107
    assert await db.ships.count_documents({"project_id": 8100}) == 2

    devlog = await db.devlogs.find_one({"_id": 33892})
    assert devlog["username_lower"] == "the_craw"
    assert devlog["duration_seconds"] == 5289


async def test_backfill_reconstructs_history(db, parsed):
    now = datetime(2026, 8, 3, 20, 0, tzinfo=UTC)
    summary = await ingest_project(db, parsed, now=now)
    assert summary["backfilled"] > 0

    synthetic = await db.project_snapshots.find(
        {"pid": 8100, "synthetic": True}
    ).sort("ts", 1).to_list(length=10_000)
    assert len(synthetic) == summary["backfilled"]

    # Cumulative counters must never decrease across the reconstruction.
    for field in ("devlogs", "total_hours", "ships", "stardust_total"):
        values = [row[field] for row in synthetic]
        assert values == sorted(values), f"{field} is not monotonic"

    # Backfill stops before today; today belongs 