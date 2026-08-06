from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

import pytest
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo.errors import PyMongoError

from src.config import settings
from src.db import bootstrap
from src.ingest import ingest_project
from src.parsers import parse_project_page
from src.parsers.project import mission_slug

FIXTURES = Path(__file__).parent / "fixtures"
ON_MISSION = FIXTURES / "project_26067_mission.html"
NO_MISSION = FIXTURES / "project_8100.html"
TEST_DB = "stardance_stats_test_mission"
UTC = timezone.utc
NOW = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def on_mission() -> str:
    return ON_MISSION.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def no_mission() -> str:
    return NO_MISSION.read_text(encoding="utf-8")


def test_slug_comes_off_the_href():
    assert mission_slug("/missions/frictionless") == "frictionless"
    assert mission_slug("https://stardance.hackclub.com/missions/pcb-golf") == "pcb-golf"
    assert mission_slug("/projects/8100") is None
    assert mission_slug(None) is None


def test_a_project_on_a_mission_reads_its_slug_and_name(on_mission):
    parsed = parse_project_page(on_mission, 26067)
    mission = parsed.data["project"]["mission"]

    assert mission["slug"] == "frictionless"
    assert mission["name"] == "Frictionless"
    assert mission["shipped"] is True
    assert parsed.missing == set()
    assert parsed.warnings == []


def test_a_project_with_no_mission_reads_none(no_mission):
    parsed = parse_project_page(no_mission, 8100)
    assert parsed.data["project"]["mission"] is None
    assert parsed.warnings == []


def test_ships_carry_the_mission_they_were_submitted_to(on_mission):
    (ship,) = parse_project_page(on_mission, 26067).data["ships"]
    assert ship["mission"] == "Frictionless"
    assert ship["mission_slug"] == "frictionless"


def test_ships_off_a_mission_carry_none(no_mission):
    for ship in parse_project_page(no_mission, 8100).data["ships"]:
        assert ship["mission"] is None
        assert ship["mission_slug"] is None


def test_guide_progress_is_read_before_the_first_ship(on_mission):
    """The progress bar renders only before the first ship to the mission."""
    html = on_mission.replace(
        '<section class="mission-panel mission-panel--shipped"',
        '<section class="mission-panel"',
    ).replace(
        '<div class="mission-panel__actions">',
        '<progress class="mission-panel__progress-bar" max="7" value="3"></progress>'
        '<div class="mission-panel__actions">',
        1,
    )
    mission = parse_project_page(html, 26067).data["project"]["mission"]
    assert mission["shipped"] is False
    assert mission["sections_done"] == 3
    assert mission["sections_total"] == 7


def test_a_vanished_panel_with_a_shipped_mission_warns(on_mission):
    """No panel and a renamed panel look alike; a ship naming a mission catches it."""
    html = re.sub(
        r'<section class="mission-panel.*?</section>', "", on_mission, count=1, flags=re.DOTALL
    )
    parsed = parse_project_page(html, 26067)

    assert parsed.data["project"]["mission"] is None
    assert any("no panel rendered" in w for w in parsed.warnings)


def test_a_panel_disagreeing_with_the_ships_warns(on_mission):
    html = on_mission.replace(
        '<a class="mission-panel__title-link" href="/missions/frictionless">',
        '<a class="mission-panel__title-link" href="/missions/something-else">',
    )
    parsed = parse_project_page(html, 26067)
    assert parsed.data["project"]["mission"]["slug"] == "something-else"
    assert any("not among shipped missions" in w for w in parsed.warnings)


def test_a_panel_without_a_readable_slug_is_missing_not_silent(on_mission):
    html = on_mission.replace(
        '<a class="mission-panel__title-link" href="/missions/frictionless">',
        '<a class="mission-panel__title-link" href="/elsewhere">',
    )
    parsed = parse_project_page(html, 26067)
    assert "mission_slug" in parsed.missing


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


@pytest.mark.asyncio
async def test_the_mission_is_stored_and_queryable(db, on_mission):
    await ingest_project(db, parse_project_page(on_mission, 26067), now=NOW)

    doc = await db.projects.find_one({"_id": 26067})
    assert doc["mission"]["slug"] == "frictionless"
    assert doc["mission"]["shipped"] is True

    found = await db.projects.find({"mission.slug": "frictionless"}).to_list(10)
    assert [p["_id"] for p in found] == [26067]

    ship = await db.ships.find_one({"project_id": 26067})
    assert ship["mission_slug"] == "frictionless"


@pytest.mark.asyncio
async def test_a_project_off_a_mission_stores_null(db, no_mission):
    await ingest_project(db, parse_project_page(no_mission, 8100), now=NOW)
    doc = await db.projects.find_one({"_id": 8100})
    assert doc["mission"] is None
    assert await db.projects.count_documents({"mission.slug": {"$ne": None}}) == 0


@pytest.mark.asyncio
async def test_a_mission_disappearing_warns_rather_than_passing_quietly(db, on_mission):
    await ingest_project(db, parse_project_page(on_mission, 26067), now=NOW)

    detached = re.sub(
        r'<section class="mission-panel.*?</section>', "", on_mission, count=1, flags=re.DOTALL
    )
    summary = await ingest_project(db, parse_project_page(detached, 26067), now=NOW)

    assert any("no longer rendered on the panel" in w for w in summary["warnings"])
    assert (await db.projects.find_one({"_id": 26067}))["mission"] is None
