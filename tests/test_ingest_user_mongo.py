from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo.errors import PyMongoError

from src.config import settings
from src.db import bootstrap
from src.ingest import (
    UserAnomalyRejected,
    ingest_project,
    ingest_user,
    recompute_all_users,
    recompute_user_totals,
)
from src.parsers import parse_project_page, parse_user_page

FIXTURES = Path(__file__).parent / "fixtures"
TEST_DB = "stardance_stats_test_users"
UTC = timezone.utc
NOW = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)

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


def parse_user(user_id=32, username=None):
    parsed = parse_user_page((FIXTURES / "user_32.html").read_text(encoding="utf-8"), user_id)
    if username:
        parsed.data["user"]["username"] = username
    return parsed


def parse_project():
    return parse_project_page((FIXTURES / "project_8100.html").read_text(encoding="utf-8"), 8100)


async def craw(db, uid=99, now=NOW):
    """The_Craw owns fixture project 8100, so totals are checkable."""
    return await ingest_user(db, parse_user(uid, "The_Craw"), now=now)


async def test_first_ingest_writes_user_and_snapshot(db):
    summary = await ingest_user(db, parse_user(), now=NOW)
    assert summary["first_ingest"] is True
    assert summary["snapshot"] is True

    doc = await db.users.find_one({"_id": 32})
    assert doc["username_lower"] == "fantamomo"
    assert doc["slack_id"] == "U0905G0BRU5"
    assert doc["stats"]["followers"] == 5
    assert await db.user_snapshots.count_documents({"uid": 32}) == 1


async def test_unchanged_reingest_writes_no_snapshot(db):
    await ingest_user(db, parse_user(), now=NOW)
    again = await ingest_user(db, parse_user(), now=NOW + timedelta(hours=1))
    assert again["changed"] == []
    assert again["snapshot"] is False
    assert await db.user_snapshots.count_documents({"uid": 32}) == 1


async def test_rename_is_recorded_and_keyed_on_id(db):
    """The whole reason users are keyed on id: handles change."""
    await ingest_user(db, parse_user(), now=NOW)
    summary = await ingest_user(db, parse_user(username="NewName"), now=NOW + timedelta(days=1))

    assert summary["renamed_from"] == "Fantamomo"
    doc = await db.users.find_one({"_id": 32})
    assert doc["username"] == "NewName"
    assert doc["previous_usernames"] == ["Fantamomo"]
    assert await db.users.count_documents({}) == 1


async def test_ingest_links_id_onto_project_rows(db):
    """Project pages carry no user id, so the user crawl resolves it."""
    await ingest_project(db, parse_project(), now=NOW)

    project = await db.projects.find_one({"_id": 8100})
    assert project.get("owner_id") is None

    summary = await craw(db)
    assert summary["linked"] == {
        "projects_owned": 1, "projects_member": 1, "devlogs": 107, "ships": 2
    }

    project = await db.projects.find_one({"_id": 8100})
    assert project["owner_id"] == 99
    assert project["member_ids"] == [99]
    assert await db.devlogs.count_documents({"user_id": 99}) == 107
    assert await db.ships.count_documents({"user_id": 99}) == 2


async def test_linking_is_case_insensitive_on_handle(db):
    await ingest_project(db, parse_project(), now=NOW)
    summary = await ingest_user(db, parse_user(99, "THE_CRAW"), now=NOW)
    assert summary["linked"]["devlogs"] == 107


async def test_totals_are_computed_from_our_own_rows(db):
    await ingest_project(db, parse_project(), now=NOW)
    summary = await craw(db)

    totals = summary["totals"]
    assert totals["ship_stardust"] == 3042          # 633 + 2409, summed by us
    assert totals["likes_received"] == 271
    assert totals["comments_received"] == 38
    assert totals["reposts_received"] == 9
    assert totals["hours"] == pytest.approx(256.2, abs=0.5)
    assert totals["shipped_hours"] == pytest.approx(165.0, abs=0.01)   # 133 + 32
    assert totals["best_multiplier"] == 19.78
    assert totals["avg_multiplier"] == pytest.approx(19.715, abs=0.001)
    assert totals["stardust_per_paid_hour"] == pytest.approx(3042 / 165.0, abs=0.01)
    assert "stardust_per_hour" not in totals


async def test_unpaid_hours_carry_an_estimate(db):
    """Paid-to-date understates an account mid-cycle, so price the rest at its own rate."""
    await ingest_project(db, parse_project(), now=NOW)
    totals = (await craw(db))["totals"]

    assert totals["unpaid_hours"] == pytest.approx(totals["hours"] - 165.0, abs=0.01)
    assert totals["estimated_pending_stardust"] == round(
        totals["unpaid_hours"] * (3042 / 165.0)
    )
    assert totals["estimated_total_stardust"] > totals["ship_stardust"]


async def test_a_ship_awaiting_payout_counts_as_unpaid_hours(db):
    """Shipped but unearned hours belong in the estimate, not in the rate."""
    await ingest_project(db, parse_project(), now=NOW)
    await db.ships.update_one(
        {"project_id": 8100, "ship_number": 2},
        {"$set": {"payout": None, "multiplier": None, "status": "pending"}},
    )
    totals = await recompute_user_totals(db, (await craw(db))["user_id"])

    assert totals["shipped_hours"] == pytest.approx(165.0, abs=0.01)
    assert totals["paid_hours"] == pytest.approx(133.0, abs=0.01)     # ship 1 only
    assert totals["ship_stardust"] == 2409
    assert totals["stardust_per_paid_hour"] == pytest.approx(2409 / 133.0, abs=0.01)
    assert totals["unpaid_hours"] == pytest.approx(totals["hours"] - 133.0, abs=0.01)


async def test_a_user_who_has_never_been_paid_gets_no_estimate(db):
    totals = (await ingest_user(db, parse_user(), now=NOW))["totals"]

    assert totals["shipped_hours"] == 0
    assert totals["paid_hours"] == 0
    assert totals["estimated_pending_stardust"] is None
    assert totals["estimated_total_stardust"] is None


async def test_totals_are_zero_not_null_for_a_user_with_no_rows(db):
    summary = await ingest_user(db, parse_user(), now=NOW)
    assert summary["totals"]["ship_stardust"] == 0
    assert summary["totals"]["hours"] == 0
    assert summary["totals"]["shipped_hours"] == 0
    assert summary["totals"]["stardust_per_paid_hour"] is None
    assert summary["totals"]["best_multiplier"] is None


async def test_coverage_flags_partial_crawls(db):
    """A user whose projects we have not all crawled must not look complete."""
    await ingest_project(db, parse_project(), now=NOW)
    await craw(db)

    doc = await db.users.find_one({"_id": 99})
    coverage = doc["coverage"]
    # The fixture profile reports 8 projects; we crawled one of them.
    assert coverage["projects_seen"] == 1
    assert coverage["projects_reported"] == 8
    assert coverage["complete"] is False


async def test_coverage_lists_the_projects_we_still_owe(db):
    """The crawler works off this list, so it must name ids, not just a count."""
    await ingest_project(db, parse_project(), now=NOW)
    parsed = parse_user(99, "The_Craw")
    parsed.data["user"]["project_ids"] = [8100, 18181, 19167]
    await ingest_user(db, parsed, now=NOW)

    doc = await db.users.find_one({"_id": 99})
    assert doc["project_ids"] == [8100, 18181, 19167]
    assert doc["coverage"]["projects_missing"] == [18181, 19167]


async def test_a_tab_without_the_list_does_not_erase_it(db):
    """Feed-tab parses yield None; that must not wipe a known project list."""
    parsed = parse_user(99, "The_Craw")
    parsed.data["user"]["project_ids"] = [8100, 18181]
    await ingest_user(db, parsed, now=NOW)

    await ingest_user(db, parse_user(99, "The_Craw"), now=NOW)   # project_ids None

    doc = await db.users.find_one({"_id": 99})
    assert doc["project_ids"] == [8100, 18181]


async def test_coverage_is_complete_once_every_listed_project_is_held(db):
    await ingest_project(db, parse_project(), now=NOW)
    parsed = parse_user(99, "The_Craw")
    parsed.data["user"].update(projects_count=1, project_ids=[8100])
    await ingest_user(db, parsed, now=NOW)

    doc = await db.users.find_one({"_id": 99})
    assert doc["coverage"]["complete"] is True


async def test_deleted_devlogs_do_not_block_completeness(db):
    """devlogs_reported counts rows no page renders, so the gap is not a crawl we owe."""
    await ingest_project(db, parse_project(), now=NOW)
    parsed = parse_user(99, "The_Craw")
    parsed.data["user"].update(projects_count=1, project_ids=[8100], devlogs_count=999)
    await ingest_user(db, parsed, now=NOW)

    coverage = (await db.users.find_one({"_id": 99}))["coverage"]
    assert coverage["devlogs_seen"] < coverage["devlogs_reported"]
    assert coverage["complete"] is True


async def test_completeness_needs_the_project_list_not_just_a_count(db):
    """Without the projects tab we cannot know which projects we are missing."""
    await ingest_project(db, parse_project(), now=NOW)
    parsed = parse_user(99, "The_Craw")
    parsed.data["user"].update(projects_count=1)      # project_ids stays None
    await ingest_user(db, parsed, now=NOW)

    coverage = (await db.users.find_one({"_id": 99}))["coverage"]
    assert coverage["projects_listed"] is None
    assert coverage["complete"] is False


async def test_totals_refresh_when_more_projects_arrive(db):
    """Crawling a project changes its owner's totals without recrawling them."""
    await craw(db)
    assert (await db.users.find_one({"_id": 99}))["totals"]["ship_stardust"] == 0

    await ingest_project(db, parse_project(), now=NOW)
    await recompute_user_totals(db, 99)

    assert (await db.users.find_one({"_id": 99}))["totals"]["ship_stardust"] == 3042


async def test_recompute_all_users_covers_everyone(db):
    await ingest_project(db, parse_project(), now=NOW)
    await craw(db)
    await ingest_user(db, parse_user(), now=NOW)

    assert await recompute_all_users(db) == 2
    assert (await db.users.find_one({"_id": 99}))["totals"]["ship_stardust"] == 3042
    assert (await db.users.find_one({"_id": 32}))["totals"]["ship_stardust"] == 0


async def test_no_stardance_leaderboard_fields_are_stored(db):
    """We rank on our own numbers; upstream's opt-in figures are not a source."""
    await ingest_project(db, parse_project(), now=NOW)
    await craw(db)

    doc = await db.users.find_one({"_id": 99})
    stats = doc["stats"]
    assert "stardust_balance" not in stats
    assert "stardust_earned" not in stats
    assert "stardust_source" not in stats


async def test_snapshot_carries_both_profile_stats_and_our_totals(db):
    await ingest_project(db, parse_project(), now=NOW)
    await craw(db)

    snap = await db.user_snapshots.find_one({"uid": 99})
    assert snap["followers"] == 5              # from the profile
    assert snap["ship_stardust"] == 3042       # computed by us
    assert snap["likes_received"] == 271


async def test_a_totals_change_writes_history_without_a_profile_crawl(db):
    """Totals move when a project of theirs is crawled, not when they are."""
    await craw(db)
    assert await db.user_snapshots.count_documents({"uid": 99}) == 1

    await ingest_project(db, parse_project(), now=NOW)
    await recompute_user_totals(db, 99, now=NOW + timedelta(hours=1))

    snaps = await db.user_snapshots.find({"uid": 99}).sort("ts", 1).to_list(10)
    assert [s.get("ship_stardust") for s in snaps] == [0, 3042]


async def test_recomputing_unchanged_totals_writes_nothing(db):
    await ingest_project(db, parse_project(), now=NOW)
    await craw(db)
    before = await db.user_snapshots.count_documents({"uid": 99})

    await recompute_user_totals(db, 99, now=NOW + timedelta(hours=2))

    assert await db.user_snapshots.count_documents({"uid": 99}) == before


async def test_totals_moving_does_not_make_the_profile_page_look_changed(db):
    """`changed` drives tiering, so it stays the page's own signal."""
    await craw(db)
    await ingest_project(db, parse_project(), now=NOW)

    summary = await craw(db, now=NOW + timedelta(hours=3))
    assert summary["changed"] == []
    assert "ship_stardust" in summary["totals_changed"]
    assert summary["snapshot"] is True


async def test_collapsed_profile_is_rejected(db):
    await ingest_user(db, parse_user(), now=NOW)

    broken = parse_user()
    broken.data["user"]["devlogs_count"] = 0
    broken.data["user"]["ships_count"] = 0
    broken.data["user"]["followers"] = None

    with pytest.raises(UserAnomalyRejected):
        await ingest_user(db, broken, now=NOW + timedelta(hours=2))

    doc = await db.users.find_one({"_id": 32})
    assert doc["stats"]["devlogs"] == 10
    assert doc["stats"]["followers"] == 5


async def test_hidden_profile_is_stored_without_snapshot(db):
    hidden = parse_user()
    hidden.data["user"]["hidden"] = True
    summary = await ingest_user(db, hidden, now=NOW)

    assert summary["hidden"] is True
    assert summary["snapshot"] is False
    assert await db.users.count_documents({"_id": 32}) == 1
    assert await db.user_snapshots.count_documents({"uid": 32}) == 0
