from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo.errors import PyMongoError

from src.collector import frontier
from src.config import settings
from src.db import bootstrap
from src.ingest import CommentsRejected, ingest_comments, ingest_project
from src.ingest.user import recompute_user_totals
from src.parsers import parse_devlog_page, parse_project_page

FIXTURES = Path(__file__).parent / "fixtures"
DEVLOG_FIXTURE = FIXTURES / "devlog_28968.html"
PROJECT_FIXTURE = FIXTURES / "project_8100.html"
TEST_DB = "stardance_stats_test"
UTC = timezone.utc
NOW = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)

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


def parse_thread(devlog_id: int = 28968, project_id: int | None = 8100):
    return parse_devlog_page(
        DEVLOG_FIXTURE.read_text(encoding="utf-8"), devlog_id, project_id
    )


def thread_of(comments: list[dict], *, devlog_id: int = 28968, count: int | None = None):
    """A parsed-shaped result carrying exactly the comments given."""
    from src.parsers.common import ParseResult

    result = ParseResult()
    result.data["comments"] = comments
    result.data["devlog"] = {
        "_id": devlog_id,
        "project_id": 8100,
        "comments_count": len(comments) if count is None else count,
        "comments_seen": len(comments),
    }
    return result


def comment(cid: int, username: str, *, body: str = "nice", devlog_id: int = 28968):
    return {
        "_id": cid,
        "devlog_id": devlog_id,
        "project_id": 8100,
        "username": username,
        "posted_at": NOW,
        "position": 0,
        "body": body,
        "body_length": len(body),
        "mentions": [],
    }


async def seed_devlog(db, devlog_id: int = 28968, *, author: str = "The_Craw", comments: int = 5):
    await db.devlogs.insert_one({
        "_id": devlog_id,
        "project_id": 8100,
        "username": author,
        "username_lower": author.lower(),
        "comments": comments,
    })


async def test_first_ingest_writes_the_thread(db):
    await seed_devlog(db)
    summary = await ingest_comments(db, parse_thread(), now=NOW)

    assert summary["comments"] == 5
    assert summary["new"] == 5
    assert summary["retired"] == 0

    first = await db.comments.find_one({"_id": 5110})
    assert first["username"] == "water"
    assert first["username_lower"] == "water"
    assert first["devlog_id"] == 28968
    assert first["project_id"] == 8100
    assert first["body"] == "this is NOT how to devlog brotato"
    assert first["first_seen"] == NOW
    assert first["last_crawled"] == NOW
    assert first["is_self"] is False


async def test_the_devlogs_author_replying_is_marked_as_their_own(db):
    await seed_devlog(db, author="water")
    await ingest_comments(db, parse_thread(), now=NOW)

    assert (await db.comments.find_one({"_id": 5110}))["is_self"] is True
    assert await db.comments.count_documents({"is_self": False}) == 4


async def test_crawling_clears_the_flag_and_stores_the_page_count(db):
    await seed_devlog(db, comments=4)
    await db.devlogs.update_one({"_id": 28968}, {"$set": {"comments_stale": True}})

    await ingest_comments(db, parse_thread(), now=NOW)

    devlog = await db.devlogs.find_one({"_id": 28968})
    assert devlog["comments_stale"] is False
    assert devlog["comments_seen"] == 5
    # The project page's number; the thread's own would re-flag it every crawl.
    assert devlog["comments_crawled_count"] == 4
    assert devlog["comments_crawled_at"] == NOW


async def test_a_vanished_comment_is_retired_not_deleted(db):
    await seed_devlog(db)
    await ingest_comments(db, parse_thread(), now=NOW)

    later = NOW + timedelta(hours=1)
    kept = [c for c in parse_thread().data["comments"] if c["_id"] != 5110]
    summary = await ingest_comments(db, thread_of(kept, count=5), now=later)

    assert summary["retired"] == 1
    gone = await db.comments.find_one({"_id": 5110})
    assert gone["gone"] is True
    assert gone["gone_at"] == later
    assert gone["body"] == "this is NOT how to devlog brotato"


async def test_a_comment_that_comes_back_is_live_again(db):
    await seed_devlog(db)
    both = [comment(1, "zed"), comment(2, "ana")]
    await ingest_comments(db, thread_of(both), now=NOW)

    await ingest_comments(db, thread_of([comment(2, "ana")], count=2), now=NOW)
    assert (await db.comments.find_one({"_id": 1}))["gone"] is True

    await ingest_comments(db, thread_of(both), now=NOW)
    row = await db.comments.find_one({"_id": 1})
    assert "gone" not in row and "gone_at" not in row


async def test_an_empty_thread_that_still_counts_replies_is_refused(db):
    await seed_devlog(db)
    await ingest_comments(db, parse_thread(), now=NOW)

    with pytest.raises(CommentsRejected, match="rendered empty"):
        await ingest_comments(db, thread_of([], count=5), now=NOW)

    assert await db.comments.count_documents({"gone": {"$ne": True}}) == 5


async def test_a_thread_emptied_of_every_comment_is_believed(db):
    """The counter reading zero corroborates the render, so the rows retire."""
    await db.users.insert_one({"_id": 77, "username": "water", "username_lower": "water"})
    await seed_devlog(db)
    await ingest_comments(db, parse_thread(), now=NOW)

    summary = await ingest_comments(db, thread_of([], count=0), now=NOW)

    assert summary["retired"] == 5
    assert await db.comments.count_documents({"gone": {"$ne": True}}) == 0
    # Their author's totals moved, so the crawl has to say whose to recompute.
    assert summary["linked_users"] == [77]
    assert (await recompute_user_totals(db, 77))["comments_sent"] == 0


async def test_an_unreadable_field_is_refused(db):
    await seed_devlog(db)
    broken = thread_of([comment(1, "zed")])
    broken.missing.add("comment.posted_at")

    with pytest.raises(CommentsRejected, match="unparsed fields"):
        await ingest_comments(db, broken, now=NOW)

    assert await db.comments.count_documents({}) == 0


async def test_known_handles_are_linked_to_their_user(db):
    await db.users.insert_one({"_id": 77, "username": "water", "username_lower": "water"})
    await seed_devlog(db)

    summary = await ingest_comments(db, parse_thread(), now=NOW)

    assert summary["linked_users"] == [77]
    assert (await db.comments.find_one({"_id": 5110}))["user_id"] == 77


async def test_totals_count_what_a_user_sent(db):
    await db.users.insert_one({"_id": 77, "username": "zed", "username_lower": "zed"})
    await seed_devlog(db, 100, author="zed")
    await seed_devlog(db, 200, author="other")

    await ingest_comments(db, thread_of(
        [comment(1, "zed", body="on my own devlog", devlog_id=100)], devlog_id=100
    ), now=NOW)
    await ingest_comments(db, thread_of(
        [comment(2, "zed", body="hello", devlog_id=200),
         comment(3, "someone", body="hi", devlog_id=200)], devlog_id=200
    ), now=NOW)

    totals = await recompute_user_totals(db, 77)

    assert totals["comments_sent"] == 2
    assert totals["comments_to_others"] == 1
    assert totals["comment_threads"] == 2
    assert totals["projects_commented"] == 1
    assert totals["comment_chars"] == len("on my own devlog") + len("hello")
    assert totals["avg_comment_length"] == round(totals["comment_chars"] / 2, 1)
    assert totals["first_comment_at"] == NOW


async def test_retired_comments_leave_the_totals(db):
    await db.users.insert_one({"_id": 77, "username": "zed", "username_lower": "zed"})
    await seed_devlog(db, 100, author="other")

    await ingest_comments(db, thread_of(
        [comment(1, "zed", devlog_id=100), comment(2, "zed", devlog_id=100)],
        devlog_id=100,
    ), now=NOW)
    assert (await recompute_user_totals(db, 77))["comments_sent"] == 2

    await ingest_comments(db, thread_of(
        [comment(1, "zed", devlog_id=100)], devlog_id=100, count=2
    ), now=NOW)
    assert (await recompute_user_totals(db, 77))["comments_sent"] == 1


async def test_project_ingest_flags_the_threads_that_moved(db):
    parsed = parse_project_page(PROJECT_FIXTURE.read_text(encoding="utf-8"), 8100)
    summary = await ingest_project(db, parsed, now=NOW)

    with_comments = await db.devlogs.count_documents({"comments": {"$gt": 0}})
    assert with_comments == 20
    assert summary["threads_flagged"] == with_comments
    assert await db.devlogs.count_documents({"comments_stale": True}) == with_comments
    # A devlog nobody replied to is not worth a page fetch.
    assert await db.devlogs.count_documents(
        {"comments": 0, "comments_stale": True}
    ) == 0


async def test_a_read_thread_is_not_flagged_again(db):
    parsed = parse_project_page(PROJECT_FIXTURE.read_text(encoding="utf-8"), 8100)
    await ingest_project(db, parsed, now=NOW)
    await ingest_comments(db, parse_thread(), now=NOW)

    await ingest_project(db, parsed, now=NOW + timedelta(hours=1))

    devlog = await db.devlogs.find_one({"_id": 28968})
    assert devlog["comments_stale"] is False
    # Its neighbours were never read, so they stay queued.
    assert await db.devlogs.count_documents({"comments_stale": True}) == 19


async def test_a_devlog_with_no_comments_is_never_worth_a_fetch(db):
    parsed = parse_project_page(PROJECT_FIXTURE.read_text(encoding="utf-8"), 8100)
    await ingest_project(db, parsed, now=NOW)

    # Twice, because the second pass compares against a watermark of its own.
    await ingest_project(db, parsed, now=NOW + timedelta(hours=1))

    assert await db.devlogs.count_documents({"comments": 0}) == 87
    assert await db.devlogs.count_documents(
        {"comments": 0, "comments_stale": True}
    ) == 0


async def test_a_thread_losing_every_reply_is_queued_to_retire_its_rows(db):
    parsed = parse_project_page(PROJECT_FIXTURE.read_text(encoding="utf-8"), 8100)
    await ingest_project(db, parsed, now=NOW)
    await ingest_comments(db, parse_thread(), now=NOW)
    assert (await db.devlogs.find_one({"_id": 28968}))["comments_stale"] is False

    for devlog in parsed.data["devlogs"]:
        if devlog["_id"] == 28968:
            devlog["comments"] = 0

    await ingest_project(db, parsed, now=NOW + timedelta(hours=1))
    assert (await db.devlogs.find_one({"_id": 28968}))["comments_stale"] is True

    # And once read, it settles: an unread thread must not queue forever.
    await ingest_comments(db, thread_of([], count=0), now=NOW + timedelta(hours=2))
    await ingest_project(db, parsed, now=NOW + timedelta(hours=3))
    assert (await db.devlogs.find_one({"_id": 28968}))["comments_stale"] is False


async def test_a_new_reply_flags_the_thread_again(db):
    parsed = parse_project_page(PROJECT_FIXTURE.read_text(encoding="utf-8"), 8100)
    await ingest_project(db, parsed, now=NOW)
    await ingest_comments(db, parse_thread(), now=NOW)

    await db.devlogs.update_one({"_id": 28968}, {"$set": {"comments": 6}})
    for devlog in parsed.data["devlogs"]:
        if devlog["_id"] == 28968:
            devlog["comments"] = 6

    await ingest_project(db, parsed, now=NOW + timedelta(hours=1))
    assert (await db.devlogs.find_one({"_id": 28968}))["comments_stale"] is True


async def test_enqueued_threads_carry_their_nested_url(db):
    queued = await frontier.enqueue_devlogs(
        db, [{"_id": 28968, "project_id": 8100}], now=NOW
    )
    assert queued == 1

    row = await db.crawl_frontier.find_one({"_id": "devlog:28968"})
    assert row["kind"] == "devlog"
    assert row["parent_id"] == 8100
    assert row["url"] == "/projects/8100/devlogs/28968"
    assert row["next_due"] == NOW

    due = await frontier.due(db, kind="devlog", now=NOW)
    assert [(r["ref_id"], r["parent_id"]) for r in due] == [(28968, 8100)]


async def test_enqueueing_leaves_a_backing_off_thread_alone(db):
    await frontier.enqueue_devlogs(db, [{"_id": 1, "project_id": 2}], now=NOW)
    await frontier.record_crawl(
        db, "devlog", 1, status="fetch_error", parent_id=2, now=NOW
    )
    backoff = (await db.crawl_frontier.find_one({"_id": "devlog:1"}))["next_due"]
    assert backoff > NOW

    queued = await frontier.enqueue_devlogs(db, [{"_id": 1, "project_id": 2}], now=NOW)
    assert queued == 0
    assert (await db.crawl_frontier.find_one({"_id": "devlog:1"}))["next_due"] == backoff


async def test_a_crawled_thread_waits_for_its_counter_not_the_clock(db):
    await frontier.enqueue_devlogs(db, [{"_id": 1, "project_id": 2}], now=NOW)
    await frontier.record_crawl(
        db, "devlog", 1, status="ok", changed=True, parent_id=2, now=NOW
    )

    row = await db.crawl_frontier.find_one({"_id": "devlog:1"})
    expected = NOW + timedelta(hours=settings.comment_recheck_hours)
    assert row["next_due"] == expected
    # A changed project page would be back in half an hour; a thread is not.
    assert row["next_due"] > NOW + timedelta(hours=settings.tier_hot_hours)
