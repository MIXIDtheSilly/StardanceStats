from __future__ import annotations

import logging

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from pymongo import ASCENDING, DESCENDING
from pymongo.errors import CollectionInvalid, OperationFailure

from .config import settings

log = logging.getLogger(__name__)

_client: AsyncIOMotorClient | None = None

# name -> (timeField, metaField)
TIMESERIES: dict[str, tuple[str, str]] = {
    "user_snapshots": ("ts", "uid"),
    "project_snapshots": ("ts", "pid"),
    "devlog_snapshots": ("ts", "did"),
    "shop_snapshots": ("ts", "sid"),
    "global_snapshots": ("ts", "scope"),
}


def get_client() -> AsyncIOMotorClient:
    global _client
    if _client is None:
        _client = AsyncIOMotorClient(settings.mongo_url, tz_aware=True)
    return _client


def get_db() -> AsyncIOMotorDatabase:
    return get_client()[settings.mongo_db]


async def close() -> None:
    global _client
    if _client is not None:
        _client.close()
        _client = None


async def bootstrap(db: AsyncIOMotorDatabase | None = None) -> None:
    """Create time-series collections and indexes. Safe to call repeatedly."""
    # Explicit None check: Motor Database objects raise on bool().
    if db is None:
        db = get_db()
    existing = set(await db.list_collection_names())

    for name, (time_field, meta_field) in TIMESERIES.items():
        if name in existing:
            continue
        try:
            await db.create_collection(
                name,
                timeseries={
                    "timeField": time_field,
                    "metaField": meta_field,
                    "granularity": "hours",
                },
            )
            log.info("created time-series collection %s", name)
        except (CollectionInvalid, OperationFailure) as exc:
            # Lost a race, or no time-series support; a plain collection works.
            log.warning("could not create timeseries %s (%s); using plain", name, exc)

    if "crawl_log" not in existing:
        try:
            await db.create_collection("crawl_log", capped=True, size=64 * 1024 * 1024)
        except (CollectionInvalid, OperationFailure):
            pass

    await db.users.create_index([("username_lower", ASCENDING)], unique=True, sparse=True)
    await db.users.create_index([("stats.followers", DESCENDING)])
    await db.users.create_index([("stats.stardust_earned", DESCENDING)])
    await db.users.create_index([("last_crawled", ASCENDING)])

    await db.projects.create_index([("owner_id", ASCENDING)])
    await db.projects.create_index([("stats.stardust_total", DESCENDING)])
    await db.projects.create_index([("stats.total_hours", DESCENDING)])
    await db.projects.create_index([("ship_status", ASCENDING)])
    await db.projects.create_index([("is_super_star", ASCENDING)])
    await db.projects.create_index([("mission.slug", ASCENDING)], sparse=True)
    await db.projects.create_index([("last_crawled", ASCENDING)])

    await db.devlogs.create_index([("project_id", ASCENDING), ("posted_at", DESCENDING)])
    await db.devlogs.create_index([("user_id", ASCENDING), ("posted_at", DESCENDING)])
    await db.devlogs.create_index([("username_lower", ASCENDING), ("posted_at", DESCENDING)])
    await db.devlogs.create_index([("likes", DESCENDING)])
    # The comment sweep's only query.
    await db.devlogs.create_index([("comments_stale", ASCENDING)], sparse=True)

    await db.comments.create_index([("devlog_id", ASCENDING), ("position", ASCENDING)])
    await db.comments.create_index([("user_id", ASCENDING), ("posted_at", DESCENDING)])
    await db.comments.create_index(
        [("username_lower", ASCENDING), ("posted_at", DESCENDING)]
    )
    await db.comments.create_index([("project_id", ASCENDING), ("posted_at", DESCENDING)])
    await db.comments.create_index([("posted_at", DESCENDING)])

    await db.ships.create_index([("project_id", ASCENDING), ("ship_number", ASCENDING)])
    await db.ships.create_index([("username_lower", ASCENDING)])
    await db.ships.create_index([("shipped_at", DESCENDING)])
    await db.ships.create_index([("mission_slug", ASCENDING)], sparse=True)
    await db.ships.create_index([("payout_path", ASCENDING)], sparse=True)

    await db.shop_items.create_index([("price_min", ASCENDING)])
    await db.shop_items.create_index([("price_spread", DESCENDING)])
    await db.shop_items.create_index([("regions", ASCENDING)])
    await db.shop_items.create_index([("categories", ASCENDING)])
    await db.shop_items.create_index([("on_sale", ASCENDING)], sparse=True)

    await db.missions.create_index([("payout_path", ASCENDING)])
    await db.missions.create_index([("last_crawled", ASCENDING)])

    # The collector's only hot query.
    await db.crawl_frontier.create_index(
        [("kind", ASCENDING), ("priority", ASCENDING), ("next_due", ASCENDING)]
    )
    await db.crawl_frontier.create_index(
        [("priority", ASCENDING), ("next_due", ASCENDING)]
    )
    await db.crawl_frontier.create_index([("tier", ASCENDING)])
    await db.crawl_frontier.create_index([("sitemap_sync", ASCENDING)])

    log.info("bootstrap complete on db=%s", settings.mongo_db)
