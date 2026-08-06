from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase

from ..parsers.common import utcnow
from .tiering import ERROR_STATUSES, classify, error_backoff, next_due, priority

log = logging.getLogger(__name__)

UNCHANGED_STATUSES = frozenset({"not_modified"})


def frontier_id(kind: str, ref_id: int | str) -> str:
    return f"{kind}:{ref_id}"


def path_for(kind: str, ref_id: int | str) -> str:
    if kind == "user":
        # The projects tab adds every project for the same one request.
        return f"/users/{ref_id}/projects"
    if kind == "mission":
        return f"/missions/{ref_id}"
    return f"/projects/{ref_id}"


async def due(
    db: AsyncIOMotorDatabase,
    *,
    kind: str | None = None,
    limit: int = 50,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Rows whose next_due has passed, hottest first then oldest."""
    now = now or utcnow()
    query: dict[str, Any] = {
        "$or": [{"next_due": {"$lte": now}}, {"next_due": None}],
        "gone": {"$ne": True},
    }
    if kind:
        query["kind"] = kind

    cursor = db.crawl_frontier.find(
        query, projection={"kind": 1, "ref_id": 1, "url": 1, "tier": 1}
    ).sort([("priority", 1), ("next_due", 1)]).limit(limit)
    return [row async for row in cursor]


async def record_crawl(
    db: AsyncIOMotorDatabase,
    kind: str,
    ref_id: int | str,
    *,
    status: str,
    changed: bool | None = False,
    etag: str | None = None,
    last_modified: str | None = None,
    error: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Retire a frontier row after a crawl and schedule its next visit."""
    now = now or utcnow()
    fid = frontier_id(kind, ref_id)
    existing = await db.crawl_frontier.find_one({"_id": fid}) or {}

    unchanged = int(existing.get("consecutive_unchanged") or 0)
    errors = int(existing.get("error_count") or 0)

    doc: dict[str, Any] = {
        "kind": kind,
        "ref_id": ref_id,
        "url": existing.get("url") or path_for(kind, ref_id),
        "last_crawled": now,
        "last_status": status,
    }

    if status in ERROR_STATUSES:
        # A broken fetch says nothing about how fast the page moves.
        errors += 1
        tier = existing.get("tier") or "cold"
        doc["error_count"] = errors
        doc["last_error"] = error or status
        doc["next_due"] = now + error_backoff(errors)
    else:
        doc["error_count"] = 0
        doc["last_error"] = None
        if status == "gone":
            doc["gone"] = True
            tier = "frozen"
        else:
            if changed is None:
                unchanged = 0
            elif changed:
                unchanged = 0
                doc["last_changed"] = now
            else:
                unchanged += 1

            tier = classify(
                now=now,
                sitemap_lastmod=existing.get("sitemap_lastmod"),
                consecutive_unchanged=unchanged,
                changed=bool(changed),
            )
        doc["consecutive_unchanged"] = unchanged
        doc["next_due"] = next_due(tier, last_crawled=now)

    doc["tier"] = tier
    doc["priority"] = priority(tier, kind)
    if etag:
        doc["etag"] = etag
    if last_modified:
        doc["last_modified"] = last_modified

    await db.crawl_frontier.update_one(
        {"_id": fid}, {"$set": doc, "$setOnInsert": {"first_seen": now}}, upsert=True
    )
    return doc


async def queue_depth(
    db: AsyncIOMotorDatabase, *, now: datetime | None = None
) -> dict[str, Any]:
    """Frontier size, how much of it is due, and the tier split."""
    now = now or utcnow()
    pipeline = [
        {"$group": {
            "_id": {"kind": "$kind", "tier": {"$ifNull": ["$tier", "unassigned"]}},
            "n": {"$sum": 1},
            "due": {"$sum": {
                "$cond": [
                    {"$or": [
                        {"$eq": [{"$ifNull": ["$next_due", None]}, None]},
                        {"$lte": ["$next_due", now]},
                    ]},
                    1, 0,
                ]
            }},
        }},
    ]

    by_kind: dict[str, dict[str, Any]] = {}
    total = due_total = 0
    async for row in db.crawl_frontier.aggregate(pipeline):
        kind = row["_id"]["kind"] or "unknown"
        bucket = by_kind.setdefault(kind, {"total": 0, "due": 0, "tiers": {}})
        bucket["total"] += row["n"]
        bucket["due"] += row["due"]
        bucket["tiers"][row["_id"]["tier"]] = row["n"]
        total += row["n"]
        due_total += row["due"]

    never = await db.crawl_frontier.count_documents({"last_crawled": None})
    return {"total": total, "due": due_total, "never_crawled": never, "by_kind": by_kind}
