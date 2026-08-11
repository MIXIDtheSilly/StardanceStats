from __future__ import annotations

from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends
from motor.motor_asyncio import AsyncIOMotorDatabase

from ...collector.frontier import queue_depth
from ...parsers.common import utcnow
from ..deps import db as db_dep
from ..services.history import MAX_BUCKETS, METRICS

router = APIRouter()


@router.get("/health")
async def health(db: AsyncIOMotorDatabase = Depends(db_dep)) -> dict[str, Any]:
    """Liveness plus collector freshness: is the data actually moving?"""
    now = utcnow()
    try:
        await db.command("ping")
        mongo_ok = True
    except Exception as exc:  # surfaced, not raised: health must always answer
        return {"status": "degraded", "mongo": False, "error": str(exc)}

    last = await db.projects.find_one(
        {"last_crawled": {"$exists": True}},
        sort=[("last_crawled", -1)],
        projection={"last_crawled": 1},
    )
    last_crawl = last.get("last_crawled") if last else None
    stale = last_crawl is None or (now - last_crawl) > timedelta(hours=24)

    errors = await db.crawl_log.count_documents(
        {"ts": {"$gte": now - timedelta(hours=1)}, "status": {"$ne": "ok"}}
    )

    sitemap = await db.crawl_state.find_one({"_id": "sitemap"}) or {}

    return {
        "status": "degraded" if stale else "ok",
        "mongo": mongo_ok,
        "last_crawl": last_crawl,
        "stale": stale,
        "errors_last_hour": errors,
        "queue": await queue_depth(db, now=now),
        "sitemap": {
            "last_checked": sitemap.get("last_checked"),
            "last_synced": sitemap.get("last_synced"),
            "counts": sitemap.get("counts"),
        },
        "now": now,
    }


@router.get("/meta")
async def meta(db: AsyncIOMotorDatabase = Depends(db_dep)) -> dict[str, Any]:
    """Corpus size and coverage."""
    # All three count frontier rows, so tracked is the denominator for both.
    listed, tracked, crawled = {}, {}, {}
    for kind in ("project", "user"):
        listed[kind] = await db.crawl_frontier.count_documents(
            {"kind": kind, "in_sitemap": True}
        )
        tracked[kind] = await db.crawl_frontier.count_documents({"kind": kind})
        crawled[kind] = await db.crawl_frontier.count_documents(
            {"kind": kind, "last_crawled": {"$ne": None}}
        )
    return {
        "counts": {
            "projects": await db.projects.count_documents({}),
            "devlogs": await db.devlogs.count_documents({}),
            "ships": await db.ships.count_documents({}),
            "comments": await db.comments.count_documents({"gone": {"$ne": True}}),
            "users": await db.users.count_documents({}),
            "shop_items": await db.shop_items.count_documents({"gone": {"$ne": True}}),
            "shop_snapshots": await db.shop_snapshots.count_documents({}),
            "project_snapshots": await db.project_snapshots.count_documents({}),
            "devlog_snapshots": await db.devlog_snapshots.count_documents({}),
            "user_snapshots": await db.user_snapshots.count_documents({}),
            "global_snapshots": await db.global_snapshots.count_documents({}),
        },
        "metrics": {kind: sorted(source.metrics) for kind, source in METRICS.items()},
        "history": {
            "intervals": ["1h", "1d", "1w"],
            "fill": ["none", "locf"],
            "max_buckets": MAX_BUCKETS,
            "note": (
                "A point is written when a tracked number moves, plus a daily "
                "heartbeat. Deltas are computed per request, never stored."
            ),
        },
        "coverage": {
            "users_complete": await db.users.count_documents({"coverage.complete": True}),
            "users_partial": await db.users.count_documents({"coverage.complete": False}),
            # listed is what the sitemap indexes; tracked adds what we found ourselves.
            "projects_listed": listed["project"],
            "projects_tracked": tracked["project"],
            "projects_crawled": crawled["project"],
            "users_listed": listed["user"],
            "users_tracked": tracked["user"],
            "users_crawled": crawled["user"],
            "threads_read": await db.devlogs.count_documents(
                {"comments_crawled_at": {"$ne": None}}
            ),
            "threads_pending": await db.devlogs.count_documents({"comments_stale": True}),
        },
        "data_source": "public pages of stardance.hackclub.com",
        "caveats": [
            "Rejected ships, deleted devlogs and unverified profiles are not publicly visible.",
            "Points marked synthetic are reconstructed from timestamps, not observed.",
            "Rankings are computed from rows we crawled, not from Stardance's own "
            "leaderboard, which is opt-in and ranks on stale approx_ columns.",
            "ship_stardust counts ship payouts only, and is a lower bound until every "
            "one of a user's projects has been crawled. See coverage.complete.",
            "Devlog history starts when we first crawled the card, not when the "
            "devlog was posted, so its early engagement is not recoverable.",
            "Comments are read one thread at a time, queued when a devlog's counter "
            "moves, so comments_sent trails comments_received until the queue drains. "
            "Threads never render deleted comments or banned authors, and those stay "
            "in the counter, so a thread can hold fewer comments than it counts.",
            "Global history sums the rows we hold, so early growth is partly our own "
            "crawl catching up rather than the platform's.",
            "Shop prices are per region and are read as a signed-out visitor. That is "
            "the list price everyone pays, except on Outpost tickets, where a "
            "per-user discount we cannot see is subtracted at checkout.",
        ],
    }
