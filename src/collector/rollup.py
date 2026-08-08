from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase

from ..parsers.common import utcnow

log = logging.getLogger(__name__)

SCOPE = "platform"

_COUNTS = ("users", "projects", "devlogs", "ships", "users_known", "projects_known")

_PROJECT_SUMS = {
    "hours": "$stats.total_hours",
    "shipped_hours": "$stats.shipped_hours",
    "paid_hours": "$stats.paid_hours",
    "stardust_paid": "$stats.stardust_total",
    "likes": "$stats.likes",
    "comments": "$stats.comments",
    "reposts": "$stats.reposts",
    "views": "$stats.views",
    "followers": "$stats.followers",
}

# Every field a point carries, and so every metric the history API will serve.
TRACKED = _COUNTS + tuple(_PROJECT_SUMS)


async def rollup_global(
    db: AsyncIOMotorDatabase, *, now: datetime | None = None
) -> dict[str, Any]:
    """Sum the rows we hold into one `global_snapshots` point."""
    now = now or utcnow()

    group: dict[str, Any] = {"_id": None}
    for name, field in _PROJECT_SUMS.items():
        group[name] = {"$sum": {"$ifNull": [field, 0]}}

    rows = await db.projects.aggregate(
        [{"$match": {"gone": {"$ne": True}}}, {"$group": group}]
    ).to_list(1)
    sums = rows[0] if rows else {}
    sums.pop("_id", None)

    doc: dict[str, Any] = {
        "ts": now,
        "scope": SCOPE,
        "users": await db.users.count_documents({"gone": {"$ne": True}}),
        "projects": await db.projects.count_documents({"gone": {"$ne": True}}),
        "devlogs": await db.devlogs.count_documents({}),
        "ships": await db.ships.count_documents({}),
        "users_known": await db.crawl_frontier.count_documents(
            {"kind": "user", "in_sitemap": {"$ne": False}}
        ),
        "projects_known": await db.crawl_frontier.count_documents(
            {"kind": "project", "in_sitemap": {"$ne": False}}
        ),
    }
    for name, value in sums.items():
        doc[name] = round(value, 2) if isinstance(value, float) else value

    await db.global_snapshots.insert_one(dict(doc))
    log.info(
        "rollup: %d/%d projects, %d/%d users, %d devlogs, %s stardust",
        doc["projects"], doc["projects_known"],
        doc["users"], doc["users_known"],
        doc["devlogs"], doc.get("stardust_paid"),
    )
    doc.pop("_id", None)
    return doc
