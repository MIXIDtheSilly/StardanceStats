from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo.errors import PyMongoError

from src.config import settings
from src.db import bootstrap
from src.ingest.mission import ingest_mission, load_missions
from src.parsers import ParseError
from src.parsers.mission import parse_mission_page

FIXTURES = Path(__file__).parent / "fixtures"
TEST_DB = "stardance_stats_test_mission_page"
UTC = timezone.utc
NOW = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)


def load(slug: str) -> str:
    return (FIXTURES / f"mission_{slug}.html").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def slack_bot() -> str:
    return load("slack_bot")


@pytest.fixture(scope="module")
def frictionless() -> str:
    return load("frictionless")


@pytest.fixture(scope="module")
def hackpad() -> str:
    return load("hackpad")


@pytest.fixture(scope="module")
def web_os_2() -> str:
    return load("web_os_2")


def test_a_fixed_payout_mission_reads_its_amount(slack_bot):
    parsed = parse_mission_page(slack_bot, "slack-bot")
    mission = parsed.data["mission"]

    assert mission["payout_path"] == "static_prize"
    assert mission["rated"] is False
    assert mission["fixed_stardust"] == 30
    assert mission["stardust_per_hour"] is None
    assert parsed.missing == set()
    assert parsed.warnings == []


def test_a_rated_mission_has_no_fixed_amount(frictionless):
    parsed = parse_mission_page(frictionless, "frictionless")
    mission = parsed.data["mission"]

    assert mission["payout_path"] == "voting"
    assert mission["rated"] is True
    assert mission["fixed_stardust"] is None
    assert parsed.warnings == []


def test_a_flat_rate_mission_reads_its_hourly_rate(hackpad):
    """Neither rated nor fixed: the tile states a stardust-per-hour rate."""
    parsed = parse_mission_page(hackpad, "hackpad")
    mission = parsed.data["mission"]

    assert mission["payout_path"] == "flat_rate"
    assert mission["stardust_per_hour"] == 5.0
    assert mission["fixed_stardust"] is None
    assert parsed.warnings == []


def test_metadata_comes_off_the_hero(slack_bot):
    mission = parse_mission_page(slack_bot, "slack-bot").data["mission"]

    assert mission["mission_id"] == 1
    assert mission["name"] == "Make a Slack Bot"
    assert mission["difficulty"] == "beginner"
    assert mission["is_hardware"] is False
    assert mission["guide_sections"] == 8
    assert mission["estimated_label"] == "~1 hr 30 min"
    assert mission["estimated_minutes"] == 90
    assert mission["description"].startswith("Make a slack bot")


def test_hardware_and_grouped_prizes(hackpad):
    mission = parse_mission_page(hackpad, "hackpad").data["mission"]

    assert mission["is_hardware"] is True
    assert [(p["title"], p["stage"]) for p in mission["prizes"]] == [
        ("Hackpad Kit!", "After design"),
        ("Gets stardust", "After building"),
    ]


def test_prerequisites_come_off_the_locked_panel(web_os_2):
    """A guest meets no prerequisite, so the panel always lists them."""
    mission = parse_mission_page(web_os_2, "web-os-2").data["mission"]

    assert mission["prerequisites"] == ["web-os-1"]
    assert [p["title"] for p in mission["prizes"]] == ["WebOS Stickersheet!"]


def test_the_gallery_names_projects_and_their_approval(frictionless):
    mission = parse_mission_page(frictionless, "frictionless").data["mission"]

    assert len(mission["gallery"]) == 12
    assert mission["gallery_truncated"] is True
    assert all(isinstance(e["project_id"], int) for e in mission["gallery"])
    assert any(e["approved"] for e in mission["gallery"])


def test_the_tile_and_the_modal_are_read_against_each_other(slack_bot):
    html = slack_bot.replace("<strong>30 stardust</strong>", "<strong>45 stardust</strong>")
    parsed = parse_mission_page(html, "slack-bot")

    assert any("fixed payout disagrees" in w for w in parsed.warnings)
    assert parsed.data["mission"]["fixed_stardust"] == 30


def test_an_icon_disagreeing_with_its_label_warns(slack_bot):
    html = slack_bot.replace(
        "mission-home__rating-icon mission-home__rating-icon--excluded",
        "mission-home__rating-icon",
    )
    parsed = parse_mission_page(html, "slack-bot")

    assert parsed.data["mission"]["rated"] is True
    assert any("but the tile says" in w for w in parsed.warnings)


def test_a_fixed_mission_with_no_readable_amount_is_missing(slack_bot):
    html = slack_bot.replace("30 stardust", "some stardust")
    parsed = parse_mission_page(html, "slack-bot")

    assert "fixed_stardust" in parsed.missing
    assert parsed.data["mission"]["payout_path"] == "static_prize"


def test_losing_both_payout_signals_warns_rather_than_reading_as_rated(slack_bot):
    """A flat-rate mission renders no rating tile, so absence alone proves nothing."""
    html = slack_bot.replace("mission-home__rating-icon", "mission-home__rating-thing")
    parsed = parse_mission_page(html, "slack-bot")

    assert parsed.data["mission"]["payout_path"] is None
    assert "payout_path" in parsed.missing
    assert any("payout markup changed" in w for w in parsed.warnings)


def test_a_page_that_is_not_a_mission_raises(frictionless):
    with pytest.raises(ParseError):
        parse_mission_page("<html><body>nope</body></html>", "frictionless")


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
async def test_a_mission_is_stored_and_its_terms_are_loadable(db, slack_bot):
    summary = await ingest_mission(db, parse_mission_page(slack_bot, "slack-bot"), now=NOW)

    assert summary["first_ingest"] is True
    assert summary["payout_path"] == "static_prize"

    doc = await db.missions.find_one({"_id": "slack-bot"})
    assert doc["fixed_stardust"] == 30
    assert doc["name"] == "Make a Slack Bot"

    terms = await load_missions(db)
    assert terms["slack-bot"]["payout_path"] == "static_prize"
    assert terms["slack-bot"]["fixed_stardust"] == 30


@pytest.mark.asyncio
async def test_terms_changing_requeues_every_attached_project(db, slack_bot, frictionless):
    await ingest_mission(db, parse_mission_page(slack_bot, "slack-bot"), now=NOW)
    await db.projects.insert_one({"_id": 7330, "mission": {"slug": "slack-bot"}})
    await db.ships.insert_one({"_id": 1, "project_id": 555, "mission_slug": "slack-bot"})
    await db.crawl_frontier.insert_many([
        {"_id": "project:7330", "kind": "project", "ref_id": 7330, "tier": "cold", "priority": 2},
        {"_id": "project:555", "kind": "project", "ref_id": 555, "tier": "cold", "priority": 2},
    ])

    # The same page, re-served as a rated mission.
    rerated = frictionless.replace(
        "Frictionless", "Make a Slack Bot"
    )
    summary = await ingest_mission(db, parse_mission_page(rerated, "slack-bot"), now=NOW)

    assert summary["changed"] == ["payout_path", "fixed_stardust", "rated"]
    assert summary["requeued_projects"] == 2
    assert any("payout terms changed" in w for w in summary["warnings"])

    row = await db.crawl_frontier.find_one({"_id": "project:7330"})
    assert row["tier"] == "hot"
    assert row["next_due"] == NOW


@pytest.mark.asyncio
async def test_an_unchanged_recrawl_requeues_nothing(db, slack_bot):
    await ingest_mission(db, parse_mission_page(slack_bot, "slack-bot"), now=NOW)
    summary = await ingest_mission(db, parse_mission_page(slack_bot, "slack-bot"), now=NOW)

    assert summary["changed"] == []
    assert summary["requeued_projects"] == 0
