from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query
from motor.motor_asyncio import AsyncIOMotorDatabase

from ...collector.frontier import queue_depth
from ...parsers.common import utcnow
from ..deps import db as db_dep
from ..examples import HEALTH, META, example
from ..services import cached_count, total_documents
from ..services.history import MAX_BUCKETS, METRICS

router = APIRouter()


@router.get("/health", responses=example(HEALTH), response_model=None)
async def health(
    deep: bool = Query(
        False,
        description="Add the queue depth and error rate. Both read the whole "
        "frontier, so they are off by default: this endpoint is polled.",
    ),
    db: AsyncIOMotorDatabase = Depends(db_dep),
) -> dict[str, Any]:
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

    sitemap = await db.crawl_state.find_one({"_id": "sitemap"}) or {}

    out: dict[str, Any] = {
        "status": "degraded" if stale else "ok",
        "mongo": mongo_ok,
        "last_crawl": last_crawl,
        "stale": stale,
        "sitemap": {
            "last_checked": sitemap.get("last_checked"),
            "last_synced": sitemap.get("last_synced"),
            "counts": sitemap.get("counts"),
        },
        "now": now,
    }

    if deep:
        out["errors_last_hour"] = await db.crawl_log.count_documents(
            {"ts": {"$gte": now - timedelta(hours=1)}, "status": {"$ne": "ok"}}
        )
        out["queue"] = await queue_depth(db, now=now)

    return out


@router.get("/meta", responses=example(META), response_model=None)
async def meta(db: AsyncIOMotorDatabase = Depends(db_dep)) -> dict[str, Any]:
    """Corpus size and coverage."""
    # A whole collection is counted off its metadata; only a filter has to look.
    whole = (
        "projects", "devlogs", "ships", "users", "shop_snapshots",
        "project_snapshots", "devlog_snapshots", "user_snapshots", "global_snapshots",
    )
    # All three count frontier rows, so tracked is the denominator for both.
    filtered = {
        "comments": ("comments", {"gone": {"$ne": True}}),
        "shop_items": ("shop_items", {"gone": {"$ne": True}}),
        "users_complete": ("users", {"coverage.complete": True}),
        "users_partial": ("users", {"coverage.complete": False}),
        "threads_read": ("devlogs", {"comments_crawled_at": {"$ne": None}}),
        "threads_pending": ("devlogs", {"comments_stale": True}),
        "projects_listed": ("crawl_frontier", {"kind": "project", "in_sitemap": True}),
        "projects_tracked": ("crawl_frontier", {"kind": "project"}),
        "projects_crawled": (
            "crawl_frontier", {"kind": "project", "last_crawled": {"$ne": None}}
        ),
        "users_listed": ("crawl_frontier", {"kind": "user", "in_sitemap": True}),
        "users_tracked": ("crawl_frontier", {"kind": "user"}),
        "users_crawled": (
            "crawl_frontier", {"kind": "user", "last_crawled": {"$ne": None}}
        ),
    }
    sizes, counted = await asyncio.gather(
        asyncio.gather(*(total_documents(db, name) for name in whole)),
        asyncio.gather(
            *(cached_count(db, name, query) for name, query in filtered.values())
        ),
    )
    size = dict(zip(whole, sizes))
    count = dict(zip(filtered, counted))

    return {
        "counts": {
            "projects": size["projects"],
            "devlogs": size["devlogs"],
            "ships": size["ships"],
            "comments": count["comments"],
            "users": size["users"],
            "shop_items": count["shop_items"],
            "shop_snapshots": size["shop_snapshots"],
            "project_snapshots": size["project_snapshots"],
            "devlog_snapshots": size["devlog_snapshots"],
            "user_snapshots": size["user_snapshots"],
            "global_snapshots": size["global_snapshots"],
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
            "users_complete": count["users_complete"],
            "users_partial": count["users_partial"],
            # listed is what the sitemap indexes; tracked adds what we found ourselves.
            "projects_listed": count["projects_listed"],
            "projects_tracked": count["projects_tracked"],
            "projects_crawled": count["projects_crawled"],
            "users_listed": count["users_listed"],
            "users_tracked": count["users_tracked"],
            "users_crawled": count["users_crawled"],
            "threads_read": count["threads_read"],
            "threads_pending": count["threads_pending"],
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
