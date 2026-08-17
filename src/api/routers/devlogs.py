from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from motor.motor_asyncio import AsyncIOMotorDatabase

from ...parsers.common import utcnow
from ..deps import db as db_dep
from ..services import stamp
from ..services.history import HistoryError, Interval, bucketed_series, parse_metrics

router = APIRouter()

Sort = Literal[
    "posted_at", "likes", "comments", "views", "reposts", "duration_seconds", "relevance"
]


def _oldest_crawl(rows: list[dict[str, Any]]) -> datetime | None:
    """The stalest row bounds how current the page as a whole is."""
    return min((r["last_crawled"] for r in rows if r.get("last_crawled")), default=None)


async def _attach_context(
    db: AsyncIOMotorDatabase, rows: list[dict[str, Any]]
) -> None:
    """A devlog row holds ids; a card away from its project needs the names too."""
    pids = sorted({r["project_id"] for r in rows if r.get("project_id") is not None})
    handles = sorted({r["username_lower"] for r in rows if r.get("username_lower")})

    projects = {
        doc["_id"]: doc
        async for doc in db.projects.find(
            {"_id": {"$in": pids}},
            {"title": 1, "banner_url": 1, "is_super_star": 1, "is_hardware": 1},
        )
    }
    users = {
        doc["username_lower"]: doc
        async for doc in db.users.find(
            {"username_lower": {"$in": handles}}, {"username_lower": 1, "avatar_url": 1}
        )
    }

    for row in rows:
        project = projects.get(row.get("project_id")) or {}
        row["project_title"] = project.get("title")
        row["project_banner_url"] = project.get("banner_url")
        row["is_super_star"] = bool(project.get("is_super_star"))
        row["is_hardware"] = bool(project.get("is_hardware"))
        row["avatar_url"] = (users.get(row.get("username_lower")) or {}).get("avatar_url")


# Declared above /devlogs/{devlog_id} so the bare path is not read as an id.
@router.get("/devlogs")
async def list_devlogs(
    q: str | None = Query(
        None,
        min_length=2,
        max_length=96,
        description="Words to find in a devlog's text. Word stems match, so "
        '"solder" finds "soldering"; "a phrase" in quotes must appear as written.',
    ),
    sort: Sort = "posted_at",
    order: Literal["desc", "asc"] = "desc",
    days: int | None = Query(
        None, ge=1, le=3650, description="Only devlogs posted within this many days."
    ),
    with_media: bool = Query(False, description="Only devlogs carrying an image or video."),
    project_id: int | None = Query(None, description="One project's devlogs."),
    username: str | None = Query(None, description="One author's devlogs."),
    limit: int = Query(30, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: AsyncIOMotorDatabase = Depends(db_dep),
) -> dict[str, Any]:
    """Every devlog we hold, newest first, searched, or ranked by one of its numbers."""
    if sort == "relevance" and not q:
        raise HTTPException(400, "relevance ranks a search; pass q as well")

    query: dict[str, Any] = {}
    if q:
        query["$text"] = {"$search": q}
    if days is not None:
        query["posted_at"] = {"$gte": utcnow() - timedelta(days=days)}
    if with_media:
        query["media.0"] = {"$exists": True}
    if project_id is not None:
        query["project_id"] = project_id
    if username:
        query["username_lower"] = username.lstrip("@").lower()
    if sort != "relevance":
        # Merged, not assigned: sorting by time already bounds this same field.
        query.setdefault(sort, {})["$ne"] = None

    total = await db.devlogs.count_documents(query)
    if sort == "relevance":
        # A $meta projection adds the score without dropping the rest of the row.
        cursor = db.devlogs.find(query, {"score": {"$meta": "textScore"}}).sort(
            [("score", {"$meta": "textScore"}), ("_id", -1)]
        )
    else:
        # _id breaks ties, so paging cannot show the same row twice or skip one.
        way = -1 if order == "desc" else 1
        cursor = db.devlogs.find(query).sort([(sort, way), ("_id", way)])

    items = await cursor.skip(offset).limit(limit).to_list(length=limit)
    await _attach_context(db, items)

    ranked = sort != "posted_at"
    for index, item in enumerate(items):
        item["rank"] = offset + index + 1 if ranked else None

    return stamp(
        {
            "q": q,
            "sort": sort,
            "order": "desc" if sort == "relevance" else order,
            "days": days,
            "total": total,
            "limit": limit,
            "offset": offset,
            "items": items,
        },
        _oldest_crawl(items),
    )


@router.get("/devlogs/{devlog_id}")
async def get_devlog(
    devlog_id: int, db: AsyncIOMotorDatabase = Depends(db_dep)
) -> dict[str, Any]:
    """One devlog as we last read it; see /devlogs/{id}/history for its series."""
    doc = await db.devlogs.find_one({"_id": devlog_id})
    if not doc:
        raise HTTPException(404, f"devlog {devlog_id} not tracked")
    return stamp(doc, doc.get("last_crawled"))


@router.get("/devlogs/{devlog_id}/comments")
async def get_devlog_comments(
    devlog_id: int,
    include_gone: bool = Query(
        False, description="Comments the thread has stopped rendering."
    ),
    db: AsyncIOMotorDatabase = Depends(db_dep),
) -> dict[str, Any]:
    """One devlog's thread, oldest first, as we last read it."""
    devlog = await db.devlogs.find_one(
        {"_id": devlog_id},
        {"project_id": 1, "comments": 1, "comments_seen": 1, "comments_crawled_at": 1},
    )
    if not devlog:
        raise HTTPException(404, f"devlog {devlog_id} not tracked")

    query: dict[str, Any] = {"devlog_id": devlog_id}
    if not include_gone:
        query["gone"] = {"$ne": True}
    items = await db.comments.find(query).sort([("position", 1)]).to_list(length=500)

    return stamp(
        {
            "devlog_id": devlog_id,
            "project_id": devlog.get("project_id"),
            # What the devlog's counter claims, against what the page rendered.
            "comments_count": devlog.get("comments"),
            "total": len(items),
            "items": items,
        },
        devlog.get("comments_crawled_at"),
    )


@router.get("/devlogs/{devlog_id}/history")
async def get_devlog_history(
    devlog_id: int,
    metrics: str = Query(
        "likes,comments,views",
        description="Comma-separated metric names; see /v1/meta.",
    ),
    interval: Interval = "1d",
    start: datetime | None = None,
    end: datetime | None = None,
    delta: bool = Query(True, description="Include per-bucket change."),
    fill: Literal["none", "locf"] = Query(
        "none", description="locf carries the last observation into empty buckets."
    ),
    db: AsyncIOMotorDatabase = Depends(db_dep),
) -> dict[str, Any]:
    """Bucketed engagement series, reporting the last observation per bucket."""
    devlog = await db.devlogs.find_one(
        {"_id": devlog_id},
        projection={"project_id": 1, "username": 1, "posted_at": 1, "last_crawled": 1},
    )
    if not devlog:
        raise HTTPException(404, f"devlog {devlog_id} not tracked")

    try:
        requested = parse_metrics("devlog", metrics)
        result = await bucketed_series(
            db, "devlog", devlog_id,
            metrics=requested, interval=interval, start=start, end=end,
            delta=delta, fill=fill,
        )
    except HistoryError as exc:
        raise HTTPException(400, str(exc)) from exc

    result["entity"] = {
        "type": "devlog", "id": devlog_id, "project_id": devlog.get("project_id"),
        "author": devlog.get("username"), "posted_at": devlog.get("posted_at"),
    }
    return stamp(result, devlog.get("last_crawled"))
