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
from src.ingest.project import check_anomalies
from src.parsers import parse_project_page

FIXTURE = Path(__file__).parent / "fixtures" / "project_8100.html"
TEST_DB = "stardance_stats_test_owner"
UTC = timezone.utc
NOW = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)

# As upstream renders it for a project whose owner is no longer visible.
EMPTY_BYLINE = '<p class="project-show__authors">\n                    By\n                  </p>'


@pytest.fixture(scope="module")
def html() -> str:
    return FIXTURE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def ownerless(html: str) -> str:
    """The real page with the author link stripped, byline intact."""
    out = re.sub(
        r'<p class="project-show__authors">.*?</p>', EMPTY_BYLINE, html, count=1, flags=re.DOTALL
    )
    assert out != html and 'class="project-show__author"' not in out
    return out


@pytest.fixture(scope="module")
def bylineless(html: str) -> str:
    """The byline element gone entirely: a genuine selector break."""
    out = re.sub(r'<p class="project-show__authors">.*?</p>', "", html, count=1, flags=re.DOTALL)
    assert out != html
    return out


def test_a_normal_project_still_reads_its_owner(html):
    parsed = parse_project_page(html, 8100)
    assert parsed.data["project"]["owner_username"] == "The_Craw"
    assert "owner_username" in parsed.found
    assert "owner_username" not in parsed.missing


def test_an_empty_byline_counts_as_read_not_missing(ownerless):
    parsed = parse_project_page(ownerless, 8100)
    assert parsed.data["project"]["owner_username"] is None
    assert parsed.data["project"]["members"] == []
    # Read, and empty. This is the whole fix.
    assert "owner_username" in parsed.found
    assert "owner_username" not in parsed.missing
    assert any("no author" in w for w in parsed.warnings)


def test_a_vanished_byline_is_still_a_break(bylineless):
    """A renamed byline class must not read as "this project has no owner"."""
    parsed = parse_project_page(bylineless, 8100)
    assert "owner_username" in parsed.missing
    assert "owner_username" not in parsed.found


def test_the_anomaly_guard_accepts_empty_and_rejects_vanished(ownerless, bylineless):
    stats = {"devlogs": 10, "total_hours": 5.0, "stardust_total": 100, "ships": 1}

    empty = parse_project_page(ownerless, 8100)
    assert check_anomalies(stats, stats, empty) == []

    broken = parse_project_page(bylineless, 8100)
    reasons = check_anomalies(stats, stats, broken)
    assert reasons and "owner_username" in reasons[0]


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
async def test_an_ownerless_project_can_be_crawled_twice(db, ownerless):
    """The regression: the second ingest used to reject and freeze the stats."""
    first = await ingest_project(db, parse_project_page(ownerless, 8100), now=NOW)
    assert first["first_ingest"] is True

    second = await ingest_project(db, parse_project_page(ownerless, 8100), now=NOW)
    assert second["first_ingest"] is False

    doc = await db.projects.find_one({"_id": 8100})
    assert doc["owner_username"] is None
    assert doc["last_crawled"] == NOW


@pytest.mark.asyncio
async def test_an_owner_disappearing_warns_rather_than_freezing(db, html, ownerless):
    """Loud, because it unlinks a user's totals, but upstream's state and not our failure."""
    await ingest_project(db, parse_project_page(html, 8100), now=NOW)
    assert (await db.projects.find_one({"_id": 8100}))["owner_username"] == "The_Craw"

    result = parse_project_page(ownerless, 8100)
    summary = await ingest_project(db, result, now=NOW)

    assert summary["first_ingest"] is False
    assert any("The_Craw" in w for w in summary["warnings"])
    assert (await db.projects.find_one({"_id": 8100}))["owner_username"] is None
