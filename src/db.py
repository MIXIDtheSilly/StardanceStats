from __future__ import annotations

import logging

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from pymongo import ASCENDING, DESCENDING, TEXT
from pymongo.errors import CollectionInvalid, OperationFailure

from .config import settings

log = logging.getLogger(__name__)

_client: AsyncIOMotorClient | None = None

# Lives here so the indexes below and the API's rankings cannot drift apart.
LEADERBOARD_FIELDS: dict[str, str] = {
    "ship_stardust": "totals.ship_stardust",
    "hours": "totals.hours",
    "shipped_hours": "totals.shipped_hours",
    "paid_hours": "totals.paid_hours",
    "likes_received": "totals.likes_received",
    "comments_received": "totals.comments_received",
    "comments_sent": "totals.comments_sent",
    "comments_to_others": "totals.comments_to_others",
    "comment_threads": "totals.comment_threads",
    "projects_commented": "totals.projects_commented",
    "views_received": "totals.views_received",
    "best_multiplier": "totals.best_multiplier",
    "stardust_per_paid_hour": "totals.stardust_per_paid_hour",
    "estimated_total_stardust": "totals.estimated_total_stardust",
    "followers": "stats.followers",
    "following": "stats.following",
    "devlogs": "stats.devlogs",
    "ships": "stats.ships",
    "projects": "stats.projects",
}

# The same for the project ranking, read off the stat block the ingest builds.
PROJECT_RANKING_FIELDS: dict[str, str] = {
    "stardust_total": "stats.stardust_total",
    "estimated_total_stardust": "stats.estimated_total_stardust",
    "stardust_per_paid_hour": "stats.stardust_per_paid_hour",
    "latest_multiplier": "stats.latest_multiplier",
    "avg_multiplier": "stats.avg_multiplier",
    "total_hours": "stats.total_hours",
    "shipped_hours": "stats.shipped_hours",
    "paid_hours": "stats.paid_hours",
    "unpaid_hours": "stats.unpaid_hours",
    "devlogs": "stats.devlogs",
    "ships": "stats.ships",
    "likes": "stats.likes",
    "comments": "stats.comments",
    "views": "stats.views",
    "reposts": "stats.reposts",
    "followers": "stats.followers",
}

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


def _order(value):
    """A text index names its direction ("text") where a plain one numbers it."""
    return value if isinstance(value, str) else int(value)


async def _ensure_index(coll, keys: list[tuple[str, int | str]], **opts) -> None:
    """create_index, tolerating an existing index that differs only in options."""
    try:
        await coll.create_index(keys, **opts)
        return
    except OperationFailure as exc:
        # 85/86: an older definition of this same key or name is in the way.
        if exc.code not in (85, 86):
            raise

    name = opts.get("name") or "_".join(f"{field}_{order}" for field, order in keys)
    for spec in await coll.list_indexes().to_list(length=None):
        if spec["name"] == "_id_":
            continue
        same_key = [(f, _order(o)) for f, o in spec["key"].items()] == list(keys)
        if spec["name"] == name or same_key:
            await coll.drop_index(spec["name"])
            log.info("dropped stale index %s.%s", coll.name, spec["name"])
    await coll.create_index(keys, **opts)


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

    await _ensure_index(db.users, [("username_lower", ASCENDING)], unique=True, sparse=True)
    await _ensure_index(db.users, [("last_crawled", ASCENDING)])
    # Every ranking sorts on one of these, and a rank is a count over one of them.
    for field in dict.fromkeys(LEADERBOARD_FIELDS.values()):
        await _ensure_index(db.users, [(field, DESCENDING)], sparse=True)

    await _ensure_index(db.projects, [("owner_id", ASCENDING)])
    for field in dict.fromkeys(PROJECT_RANKING_FIELDS.values()):
        await _ensure_index(db.projects, [(field, DESCENDING)], sparse=True)
    await _ensure_index(db.projects, [("ship_status", ASCENDING)])
    await _ensure_index(db.projects, [("is_super_star", ASCENDING)])
    await _ensure_index(db.projects, [("mission.slug", ASCENDING)], sparse=True)
    await _ensure_index(db.projects, [("last_crawled", ASCENDING)])

    await _ensure_index(db.devlogs, [("project_id", ASCENDING), ("posted_at", DESCENDING)])
    await _ensure_index(db.devlogs, [("user_id", ASCENDING), ("posted_at", DESCENDING)])
    await _ensure_index(
        db.devlogs, [("username_lower", ASCENDING), ("posted_at", DESCENDING)]
    )
    await _ensure_index(db.devlogs, [("likes", DESCENDING)])
    # The feed ranks every devlog we hold on one of these, with no project to narrow it.
    for field in ("posted_at", "comments", "views", "reposts", "duration_seconds"):
        await _ensure_index(db.devlogs, [(field, DESCENDING)], sparse=True)
    # Older rows kept a preview instead of the post, so the search reads both.
    await _ensure_index(db.devlogs, [("body", TEXT), ("body_preview", TEXT)])
    # The comment sweep's only query.
    await _ensure_index(db.devlogs, [("comments_stale", ASCENDING)], sparse=True)

    await _ensure_index(db.comments, [("devlog_id", ASCENDING), ("position", ASCENDING)])
    await _ensure_index(db.comments, [("user_id", ASCENDING), ("posted_at", DESCENDING)])
    await _ensure_index(
        db.comments, [("username_lower", ASCENDING), ("posted_at", DESCENDING)]
    )
    await _ensure_index(
        db.comments, [("project_id", ASCENDING), ("posted_at", DESCENDING)]
    )
    await _ensure_index(db.comments, [("posted_at", DESCENDING)])

    await _ensure_index(db.ships, [("project_id", ASCENDING), ("ship_number", ASCENDING)])
    await _ensure_index(db.ships, [("username_lower", ASCENDING)])
    await _ensure_index(db.ships, [("shipped_at", DESCENDING)])
    await _ensure_index(db.ships, [("mission_slug", ASCENDING)], sparse=True)
    await _ensure_index(db.ships, [("payout_path", ASCENDING)], sparse=True)

    await _ensure_index(db.shop_items, [("price_min", ASCENDING)])
    await _ensure_index(db.shop_items, [("price_spread", DESCENDING)])
    await _ensure_index(db.shop_items, [("regions", ASCENDING)])
    await _ensure_index(db.shop_items, [("categories", ASCENDING)])
    await _ensure_index(db.shop_items, [("on_sale", ASCENDING)], sparse=True)

    await _ensure_index(db.missions, [("payout_path", ASCENDING)])
    await _ensure_index(db.missions, [("last_crawled", ASCENDING)])

    # The collector's only hot query.
    await _ensure_index(
        db.crawl_frontier,
        [("kind", ASCENDING), ("priority", ASCENDING), ("next_due", ASCENDING)],
    )
    await _ensure_index(
        db.crawl_frontier, [("priority", ASCENDING), ("next_due", ASCENDING)]
    )
    await _ensure_index(db.crawl_frontier, [("tier", ASCENDING)])
    await _ensure_index(db.crawl_frontier, [("sitemap_sync", ASCENDING)])
    # health counts the never-crawled rows, which is an equality on null.
    await _ensure_index(db.crawl_frontier, [("last_crawled", ASCENDING)])

    # Capped and always full, so the error-rate count reads all 64 MB without this.
    await _ensure_index(db.crawl_log, [("ts", DESCENDING)])

    log.info("bootstrap complete on db=%s", settings.mongo_db)
