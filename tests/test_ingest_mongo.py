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

    for field in ("devlogs", "total_hours", "ships", "stardust_total"):
        values = [row[field] for row in synthetic]
        assert values == sorted(values), f"{field} is not monotonic"

    # Backfill stops before today; today belongs to the observed snapshot.
    assert all(row["ts"].date() < now.date() for row in synthetic)
    observed = await db.project_snapshots.find_one({"pid": 8100, "synthetic": None})
    assert observed is not None
    assert observed["devlogs"] == 107
    assert observed["stardust_total"] == 3042

    # Both ships pre-date today, so payouts are whole by the handoff.
    last = synthetic[-1]
    assert last["ships"] == 2
    assert last["stardust_total"] == 3042
    posted_today = sum(
        1 for d in parsed.data["devlogs"] if d["posted_at"].date() == now.date()
    )
    assert last["devlogs"] == 107 - posted_today

    assert synthetic[0]["devlogs"] >= 1
    assert synthetic[0]["stardust_total"] == 0


async def test_ship_payouts_appear_on_their_ship_dates(db, parsed):
    now = datetime(2026, 8, 3, 20, 0, tzinfo=UTC)
    await ingest_project(db, parsed, now=now)

    # Ship #1 paid 2409 on 2026-06-26; ship #2 paid 633 on 2026-07-02.
    before = await db.project_snapshots.find_one(
        {"pid": 8100, "ts": datetime(2026, 6, 25, tzinfo=UTC)}
    )
    after_first = await db.project_snapshots.find_one(
        {"pid": 8100, "ts": datetime(2026, 6, 27, tzinfo=UTC)}
    )
    after_second = await db.project_snapshots.find_one(
        {"pid": 8100, "ts": datetime(2026, 7, 3, tzinfo=UTC)}
    )
    assert before["stardust_total"] == 0
    assert after_first["stardust_total"] == 2409
    assert after_second["stardust_total"] == 3042


async def test_unchanged_reingest_skips_the_snapshot(db, parsed):
    first = datetime(2026, 8, 3, 20, 0, tzinfo=UTC)
    await ingest_project(db, parsed, now=first)
    count = await db.project_snapshots.count_documents({"pid": 8100})

    # An hour later with identical data: nothing changed, heartbeat not due.
    again = await ingest_project(db, parsed, now=first + timedelta(hours=1))
    assert again["first_ingest"] is False
    assert again["changed"] == []
    assert again["snapshot"] is False
    assert await db.project_snapshots.count_documents({"pid": 8100}) == count


async def test_heartbeat_snapshot_after_a_quiet_day(db, parsed):
    first = datetime(2026, 8, 3, 20, 0, tzinfo=UTC)
    await ingest_project(db, parsed, now=first)
    count = await db.project_snapshots.count_documents({"pid": 8100})

    later = await ingest_project(db, parsed, now=first + timedelta(hours=25))
    assert later["snapshot"] is True
    assert await db.project_snapshots.count_documents({"pid": 8100}) == count + 1


async def test_change_is_detected_and_snapshotted(db, parsed):
    first = datetime(2026, 8, 3, 20, 0, tzinfo=UTC)
    await ingest_project(db, parsed, now=first)

    parsed.data["devlogs"][0]["likes"] += 5
    second = await ingest_project(db, parsed, now=first + timedelta(hours=1))
    parsed.data["devlogs"][0]["likes"] -= 5  # restore for other tests

    assert "likes" in second["changed"]
    assert second["snapshot"] is True

    project = await db.projects.find_one({"_id": 8100})
    assert project["stats"]["likes"] == 276


async def test_collapsed_page_is_rejected_and_leaves_state_intact(db, parsed):
    """The failure this guard exists for: a redesign turning stats into zeros."""
    first = datetime(2026, 8, 3, 20, 0, tzinfo=UTC)
    await ingest_project(db, parsed, now=first)

    broken = parse_project_page(FIXTURE.read_text(encoding="utf-8"), 8100)
    broken.data["project"]["devlogs_count"] = 0
    broken.data["project"]["total_hours"] = 0
    broken.data["devlogs"] = []
    broken.data["ships"] = []

    with pytest.raises(AnomalyRejected):
        await ingest_project(db, broken, now=first + timedelta(hours=2))

    project = await db.projects.find_one({"_id": 8100})
    assert project["stats"]["devlogs"] == 107
    assert project["stats"]["stardust_total"] == 3042

    logged = await db.crawl_log.find_one({"ref_id": 8100, "status": "anomaly"})
    assert logged is not None


async def test_super_star_is_persisted(db, parsed):
    await ingest_project(db, parsed, now=datetime(2026, 8, 3, 20, 0, tzinfo=UTC))

    project = await db.projects.find_one({"_id": 8100})
    assert project["is_super_star"] is True
    assert project["super_star_at"] == datetime(2026, 6, 16, 12, 22, 56, tzinfo=UTC)
    assert project["super_star_by"] == "cskartikey"


async def test_award_details_survive_the_card_ageing_off_the_timeline(db, parsed):
    """The badge outlives the event card, so the date must not be blanked."""
    first = datetime(2026, 8, 3, 20, 0, tzinfo=UTC)
    await ingest_project(db, parsed, now=first)

    later = parse_project_page(FIXTURE.read_text(encoding="utf-8"), 8100)
    later.data["project"].update(super_star_at=None, super_star_by=None)
    await ingest_project(db, later, now=first + timedelta(hours=6))

    project = await db.projects.find_one({"_id": 8100})
    assert project["is_super_star"] is True
    assert project["super_star_at"] == datetime(2026, 6, 16, 12, 22, 56, tzinfo=UTC)
    assert project["super_star_by"] == "cskartikey"


async def test_losing_the_badge_warns_rather_than_flipping_quietly(db, parsed):
    """An admin un-marking and a renamed class are indistinguishable here."""
    first = datetime(2026, 8, 3, 20, 0, tzinfo=UTC)
    await ingest_project(db, parsed, now=first)

    gone = parse_project_page(FIXTURE.read_text(encoding="utf-8"), 8100)
    gone.data["project"]["is_super_star"] = False
    summary = await ingest_project(db, gone, now=first + timedelta(hours=6))

    assert any("Super Star" in w for w in summary["warnings"])
    assert (await db.projects.find_one({"_id": 8100}))["is_super_star"] is False

    logged = await db.crawl_log.find_one({"ref_id": 8100, "status": "ok"}, sort=[("ts", -1)])
    assert any("Super Star" in w for w in logged["warnings"])
