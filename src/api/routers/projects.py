from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from motor.motor_asyncio import AsyncIOMotorDatabase

from ..deps import db as db_dep
from ..services import HistoryError, Interval, bucketed_series, stamp
from ..services.history import METRICS, parse_metrics

router = APIRouter()

PROJECT_METRICS = METRICS["project"].metrics


async def _as_of(db: AsyncIOMotorDatabase, project_id: int) -> datetime | None:
    """The project's own crawl time; its devlogs and ships come off that page."""
    doc = await db.projects.find_one({"_id": project_id}, {"last_crawled": 1})
    return (doc or {}).get("last_crawled")


@router.get("/projects/{project_id}")
async def get_project(
    project_id: int, db: AsyncIOMotorDatabase = Depends(db_dep)
) -> dict[str, Any]:
    doc = await db.projects.find_one({"_id": project_id})
    if not doc:
        raise HTTPException(404, f"project {project_id} not tracked")
    return stamp(doc, doc.get("last_crawled"))


@router.get("/projects/{project_id}/devlogs")
async def get_project_devlogs(
    project_id: int,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    sort: Literal["posted_at", "likes", "comments", "duration_seconds"] = "posted_at",
    db: AsyncIOMotorDatabase = Depends(db_dep),
) -> dict[str, Any]:
    total = await db.devlogs.count_documents({"project_id": project_id})
    cursor = (
        db.devlogs.find({"project_id": project_id})
        .sort([(sort, -1)])
        .skip(offset)
        .limit(limit)
    )
    return stamp(
        {
            "project_id": project_id,
            "total": total,
            "limit": limit,
            "offset": offset,
            "items": await cursor.to_list(length=limit),
        },
        await _as_of(db, project_id),
    )


@router.get("/projects/{project_id}/comments")
async def get_project_comments(
    project_id: int,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncIOMotorDatabase = Depends(db_dep),
) -> dict[str, Any]:
    """Every comment we have read across this project's devlogs, newest first."""
    query = {"project_id": project_id, "gone": {"$ne": True}}
    total = await db.comments.count_documents(query)
    cursor = (
        db.comments.find(query).sort([("posted_at", -1)]).skip(offset).limit(limit)
    )

    top = await db.comments.aggregate([
        {"$match": query},
        {"$group": {
            "_id": "$username_lower",
            "comments": {"$sum": 1},
            "username": {"$last": "$username"},
            "threads": {"$addToSet": "$devlog_id"},
        }},
        {"$project": {
            "_id": 0, "username": 1, "comments": 1,
            "threads": {"$size": "$threads"},
        }},
        {"$sort": {"comments": -1}},
        {"$limit": 20},
    ]).to_list(length=20)

    # The counters say how many exist; our rows say how many we have read.
    project = await db.projects.find_one({"_id": project_id}, {"stats.comments": 1})
    return stamp(
        {
            "project_id": project_id,
            "comments_count": ((project or {}).get("stats") or {}).get("comments"),
            "total": total,
            "limit": limit,
            "offset": offset,
            "commenters": len(await db.comments.distinct("username_lower", query)),
            "top_commenters": top,
            "items": await cursor.to_list(length=limit),
        },
        await _threads_as_of(db, project_id),
    )


async def _threads_as_of(
    db: AsyncIOMotorDatabase, project_id: int
) -> datetime | None:
    """Threads are crawled one page each, so the stalest one bounds the set."""
    oldest = await db.devlogs.find_one(
        {"project_id": project_id, "comments_crawled_at": {"$ne": None}},
        {"comments_crawled_at": 1},
        sort=[("comments_crawled_at", 1)],
    )
    return (oldest or {}).get("comments_crawled_at")


@router.get("/projects/{project_id}/ships")
async def get_project_ships(
    project_id: int, db: AsyncIOMotorDatabase = Depends(db_dep)
) -> dict[str, Any]:
    cursor = db.ships.find({"project_id": project_id}).sort([("ship_number", 1)])
    items = await cursor.to_list(length=200)
    return stamp(
        {
            "project_id": project_id,
            "total": len(items),
            "stardust_total": sum(s.get("payout") or 0 for s in items),
            "items": items,
        },
        await _as_of(db, project_id),
    )


@router.get("/projects/{project_id}/history")
async def get_project_history(
    project_id: int,
    metrics: str = Query(
        "devlogs,total_hours,likes,stardust_total",
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
    """Bucketed time series, reporting the last observation per bucket."""
    project = await db.projects.find_one(
        {"_id": project_id}, projection={"title": 1, "owner_username": 1, "last_crawled": 1}
    )
    if not project:
        raise HTTPException(404, f"project {project_id} not tracked")

    try:
        requested = parse_metrics("project", metrics)
        result = await bucketed_series(
            db, "project", project_id,
            metrics=requested, interval=interval, start=start, end=end,
            delta=delta, fill=fill,
        )
    except HistoryError as exc:
        raise HTTPException(400, str(exc)) from exc

    result["entity"] = {
        "type": "project", "id": project_id, "title": project.get("title"),
        "owner": project.get("owner_username"),
    }
    return stamp(result, project.get("last_crawled"))
