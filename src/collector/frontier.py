from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Sequence

from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo import UpdateOne

from ..parsers.common import utcnow
from .tiering import ERROR_STATUSES, classify, error_backoff, next_due, priority

log = logging.getLogger(__name__)

UNCHANGED_STATUSES = frozenset({"not_modified"})

SEED_CHUNK = 2000

SCAN_STATE_ID = "id_scan"
SCAN_COLLECTION = {"project": "projects", "user": "users"}


def frontier_id(kind: str, ref_id: int | str) -> str:
    return f"{kind}:{ref_id}"


def path_for(kind: str, ref_id: int | str, *, parent_id: int | None = None) -> str:
    if kind == "user":
        # The projects tab adds every project for the same one request.
        return f"/users/{ref_id}/projects"
    if kind == "mission":
        return f"/missions/{ref_id}"
    if kind == "devlog":
        # The show route is nested, so the row carries its project alongside.
        return f"/projects/{parent_id}/devlogs/{ref_id}"
    return f"/projects/{ref_id}"


async def due(
    db: AsyncIOMotorDatabase,
    *,
    kind: str | None = None,
    exclude: Sequence[str] | None = None,
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
    elif exclude:
        # A residual filter on the (priority, next_due) index, so the scan
        # stays in sort order instead of collecting and sorting in memory.
        query["kind"] = {"$nin": list(exclude)}

    cursor = db.crawl_frontier.find(
        query, projection={"kind": 1, "ref_id": 1, "url": 1, "tier": 1, "parent_id": 1}
    ).sort([("priority", 1), ("next_due", 1)]).limit(limit)
    return [row async for row in cursor]


async def max_ref_id(
    db: AsyncIOMotorDatabase, kind: str, *, listed_only: bool = False
) -> int:
    """Highest id we know of for a kind. Missions are slugs, so they have none."""
    query: dict[str, Any] = {"kind": kind, "ref_id": {"$type": "number"}}
    if listed_only:
        # Seeded rows are mostly 404s, so their ids cannot raise the ceiling.
        query["in_sitemap"] = True
    row = await db.crawl_frontier.find_one(
        query, sort=[("ref_id", -1)], projection={"ref_id": 1}
    )
    return int(row["ref_id"]) if row else 0


async def max_ingested_id(db: AsyncIOMotorDatabase, kind: str) -> int:
    """Highest id that turned out to be a real page, listed or not."""
    row = await db[SCAN_COLLECTION[kind]].find_one(
        sort=[("_id", -1)], projection={"_id": 1}
    )
    return int(row["_id"]) if row else 0


async def extend_scan(
    db: AsyncIOMotorDatabase, kind: str, *, margin: int, now: datetime | None = None
) -> dict[str, Any]:
    """Seed whatever id range the last pass has not covered yet."""
    state = await db.crawl_state.find_one({"_id": SCAN_STATE_ID}) or {}
    covered = int(state.get(kind) or 0)

    ceiling = max(
        await max_ref_id(db, kind, listed_only=True),
        await max_ingested_id(db, kind),
    ) + margin

    if ceiling <= covered:
        return {"kind": kind, "covered_to": covered, "seeded": 0}

    result = await seed_id_range(db, kind, covered + 1, ceiling, now=now)
    await db.crawl_state.update_one(
        {"_id": SCAN_STATE_ID}, {"$set": {kind: ceiling}}, upsert=True
    )
    return {**result, "covered_to": ceiling}


async def seed_id_range(
    db: AsyncIOMotorDatabase,
    kind: str,
    start: int,
    end: int,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Add frontier rows across an id range, leaving rows we already have alone."""
    now = now or utcnow()
    seeded = 0

    # Never $set: a row already being tracked keeps its tier, etag and schedule.
    insert = {
        "in_sitemap": False,
        "first_seen": now,
        "last_crawled": None,
        "consecutive_unchanged": 0,
        "unchanged_since": None,
        "error_count": 0,
        # None means due now, so seeded rows crawl on the next pass.
        "next_due": None,
        "tier": "cold",
    }

    ops: list[UpdateOne] = []
    for ref_id in range(start, end + 1):
        ops.append(
            UpdateOne(
                {"_id": frontier_id(kind, ref_id)},
                {"$setOnInsert": {
                    "kind": kind,
                    "ref_id": ref_id,
                    "url": path_for(kind, ref_id),
                    "priority": priority("cold", kind),
                    **insert,
                }},
                upsert=True,
            )
        )
        if len(ops) >= SEED_CHUNK:
            seeded += (await db.crawl_frontier.bulk_write(ops, ordered=False)).upserted_count
            ops = []

    if ops:
        seeded += (await db.crawl_frontier.bulk_write(ops, ordered=False)).upserted_count

    log.info("seeded %d new %s rows over ids %d-%d", seeded, kind, start, end)
    return {"kind": kind, "from": start, "to": end, "seeded": seeded}


async def enqueue_devlogs(
    db: AsyncIOMotorDatabase,
    rows: list[dict[str, Any]],
    *,
    tier: str = "warm",
    now: datetime | None = None,
) -> int:
    """Make devlog threads due now. Rows are {_id, project_id} from `devlogs`."""
    if not rows:
        return 0
    now = now or utcnow()

    wanted = {frontier_id("devlog", r["_id"]): r for r in rows}
    known = set(await db.crawl_frontier.distinct("_id", {"_id": {"$in": list(wanted)}}))

    fresh = [
        {
            "_id": fid,
            "kind": "devlog",
            "ref_id": row["_id"],
            "parent_id": row["project_id"],
            "url": path_for("devlog", row["_id"], parent_id=row["project_id"]),
            "tier": tier,
            "priority": priority(tier, "devlog"),
            "in_sitemap": False,
            "first_seen": now,
            "last_crawled": None,
            "consecutive_unchanged": 0,
            "unchanged_since": None,
            "error_count": 0,
            "next_due": now,
        }
        for fid, row in wanted.items()
        if fid not in known
    ]
    if fresh:
        await db.crawl_frontier.insert_many(fresh, ordered=False)

    # A moved counter is no reason to retry a page that would not load.
    revived = await db.crawl_frontier.update_many(
        {
            "_id": {"$in": sorted(known)},
            "$or": [{"error_count": {"$lte": 0}}, {"next_due": {"$lte": now}}],
        },
        {"$set": {"tier": tier, "priority": priority(tier, "devlog"), "next_due": now}},
    )
    return len(fresh) + revived.modified_count


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
    parent_id: int | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Retire a frontier row after a crawl and schedule its next visit."""
    now = now or utcnow()
    fid = frontier_id(kind, ref_id)
    existing = await db.crawl_frontier.find_one({"_id": fid}) or {}

    unchanged = int(existing.get("consecutive_unchanged") or 0)
    unchanged_since = existing.get("unchanged_since")
    errors = int(existing.get("error_count") or 0)
    parent_id = parent_id if parent_id is not None else existing.get("parent_id")

    doc: dict[str, Any] = {
        "kind": kind,
        "ref_id": ref_id,
        "url": existing.get("url") or path_for(kind, ref_id, parent_id=parent_id),
        "last_crawled": now,
        "last_status": status,
    }
    if parent_id is not None:
        doc["parent_id"] = parent_id

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
                unchanged_since = None
            elif changed:
                unchanged = 0
                unchanged_since = None
                doc["last_changed"] = now
            else:
                unchanged += 1
                # The streak's first crawl starts the clock, not this one.
                unchanged_since = unchanged_since or now

            tier = classify(
                now=now,
                sitemap_lastmod=existing.get("sitemap_lastmod"),
                unchanged_since=unchanged_since,
                changed=bool(changed),
            )
        doc["consecutive_unchanged"] = unchanged
        doc["unchanged_since"] = unchanged_since
        doc["next_due"] = next_due(tier, last_crawled=now, kind=kind)

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
