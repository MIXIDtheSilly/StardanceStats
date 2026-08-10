from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from motor.motor_asyncio import AsyncIOMotorDatabase

from ..deps import db as db_dep
from ..services import stamp
from ..services.history import HistoryError, Interval, bucketed_series, parse_metrics

router = APIRouter()


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
