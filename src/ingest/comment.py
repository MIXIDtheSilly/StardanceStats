from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo import UpdateOne

from ..parsers.common import ParseResult, utcnow

log = logging.getLogger(__name__)


class CommentsRejected(Exception):
    """A thread page parsed structurally but not well enough to write from."""


def check_comment_anomalies(result: ParseResult, held: int) -> list[str]:
    """Return reasons this thread should not be written. Empty means accept."""
    reasons: list[str] = []
    if result.missing:
        reasons.append(f"unparsed fields: {sorted(result.missing)}")

    # A renamed selector and a deleted thread look alike, but a counter also
    # reading zero corroborates the render.
    if held and not result.data.get("comments"):
        counted = (result.data.get("devlog") or {}).get("comments_count")
        if counted != 0:
            reasons.append(
                f"thread rendered empty but we hold {held} comment(s) "
                f"and it counts {counted}"
            )
    return reasons


async def ingest_comments(
    db: AsyncIOMotorDatabase, result: ParseResult, *, now: datetime | None = None
) -> dict[str, Any]:
    """Persist one parsed thread. Returns a summary of what changed."""
    now = now or utcnow()
    parsed = result.data["devlog"]
    comments = result.data["comments"]
    did = parsed["_id"]

    devlog = await db.devlogs.find_one(
        {"_id": did}, {"project_id": 1, "username_lower": 1, "comments": 1}
    )
    project_id = parsed.get("project_id") or (devlog or {}).get("project_id")
    author = (devlog or {}).get("username_lower")

    held = await db.comments.count_documents({"devlog_id": did, "gone": {"$ne": True}})
    reasons = check_comment_anomalies(result, held)
    if reasons:
        await _log_crawl(db, did, "anomaly", now, reasons=reasons)
        raise CommentsRejected(f"devlog {did}: " + "; ".join(reasons))

    written = await _write_comments(db, comments, project_id, author, now)
    retired, orphaned = await _retire_missing(db, did, comments, now)
    linked = await _link_known_users(db, did, comments)
    # A retired comment moves its author's totals too.
    linked |= orphaned

    # The project page's count, not the thread's own: the thread's would re-flag
    # the recrawl until that page caught up.
    watermark = (devlog or {}).get("comments")
    if watermark is None:
        watermark = parsed.get("comments_count")

    await db.devlogs.update_one(
        {"_id": did},
        {"$set": {
            "comments_seen": len(comments),
            "comments_crawled_count": watermark,
            "comments_crawled_at": now,
            "comments_stale": False,
        }},
    )

    await _log_crawl(
        db, did, "ok", now,
        comments=len(comments), new=written, retired=retired,
        warnings=result.warnings,
    )

    return {
        "devlog_id": did,
        "project_id": project_id,
        "comments": len(comments),
        "new": written,
        "retired": retired,
        "linked_users": sorted(linked),
        "warnings": result.warnings,
    }


async def _write_comments(
    db: AsyncIOMotorDatabase,
    comments: list[dict[str, Any]],
    project_id: int | None,
    author: str | None,
    now: datetime,
) -> int:
    if not comments:
        return 0

    ops = []
    for comment in comments:
        doc = dict(comment)
        handle = (doc.get("username") or "").lower() or None
        doc["project_id"] = doc.get("project_id") or project_id
        doc["username_lower"] = handle
        doc["is_self"] = bool(handle and author and handle == author)
        doc["last_crawled"] = now
        # A row coming back means it was only hidden from us, never gone.
        ops.append(UpdateOne(
            {"_id": doc["_id"]},
            {"$set": doc, "$unset": {"gone": "", "gone_at": ""},
             "$setOnInsert": {"first_seen": now}},
            upsert=True,
        ))

    written = await db.comments.bulk_write(ops, ordered=False)
    return written.upserted_count


async def _retire_missing(
    db: AsyncIOMotorDatabase,
    devlog_id: int,
    comments: list[dict[str, Any]],
    now: datetime,
) -> tuple[int, set[int]]:
    """Flag rows the thread no longer renders. Deleted and banned look alike."""
    rendered = [c["_id"] for c in comments]
    query = {"devlog_id": devlog_id, "_id": {"$nin": rendered}, "gone": {"$ne": True}}

    authors = await db.comments.distinct("user_id", query)
    outcome = await db.comments.update_many(query, {"$set": {"gone": True, "gone_at": now}})
    return outcome.modified_count, {uid for uid in authors if uid is not None}


async def _link_known_users(
    db: AsyncIOMotorDatabase, devlog_id: int, comments: list[dict[str, Any]]
) -> set[int]:
    """Stamp user ids onto this thread's rows for handles we already know."""
    handles = {(c.get("username") or "").lower() for c in comments}
    handles.discard("")
    if not handles:
        return set()

    touched: set[int] = set()
    async for user in db.users.find(
        {"username_lower": {"$in": sorted(handles)}}, {"_id": 1, "username_lower": 1}
    ):
        await db.comments.update_many(
            {"devlog_id": devlog_id, "username_lower": user["username_lower"]},
            {"$set": {"user_id": user["_id"]}},
        )
        touched.add(user["_id"])
    return touched


async def _log_crawl(
    db: AsyncIOMotorDatabase, devlog_id: int, status: str, ts: datetime, **extra: Any
) -> None:
    try:
        await db.crawl_log.insert_one(
            {"ts": ts, "kind": "devlog", "ref_id": devlog_id, "status": status, **extra}
        )
    except Exception:  # logging must never break ingest
        log.exception("crawl_log write failed")
