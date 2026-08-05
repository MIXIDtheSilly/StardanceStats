from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from motor.motor_asyncio import AsyncIOMotorDatabase

from ..deps import db as db_dep

router = APIRouter()

PROJECT_METRICS = {
    "devlogs", "total_hours", "shipped_hours", "paid_hours", "followers",
    "likes", "comments", "reposts", "views", "ships", "stardust_total",
    "latest_multiplier",
}

Interval = Literal["1h", "1d", "1w"]

_TRUNC = {
    "1h": {"unit": "hour", "binSize": 1},
    "1d": {"unit": "day", "binSize": 1},
    "1w": {"unit": "week", "binSize": 1},
}


@router.get("/projects/{project_id}")
async def get_project(
    project_id: int, db: AsyncIOMotorDatabase = Depends(db_dep)
) -> dict[str, Any]:
    doc = await db.projects.find_one({"_id": project_id})
    if not doc:
        raise HTTPException(404, f"project {project_id} not tracked")
    return doc


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
    return {
        "project_id": project_id,
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": await cursor.to_list(length=limit),
    }


@router.get("/projects/{project_id}/ships")
async def get_project_ships(
    project_id: int, db: AsyncIOMotorDatabase = Depends(db_dep)
) -> dict[str, Any]:
    cursor = db.ships.find({"project_id": project_id}).sort([("ship_number", 1)])
    items = await cursor.to_list(length=200)
    return {
        "project_id": project_id,
        "total": len(items),
        "stardust_total": sum(s.get("payout") or 0 for s in items),
        "items": items,
    }


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
    db: AsyncIOMotorDatabase = Depends(db_dep),
) -> dict[str, Any]:
    """Bucketed time series, reporting the last observation per bucket.

    The synthetic flag marks buckets reconstructed at ingest, not observed.
    """
    requested = [m.strip() for m in metrics.split(",") if m.strip()]
    unknown = [m for m in requested if m not in PROJECT_METRICS]
    if unknown:
        raise HTTPException(400, f"unknown metric(s): {unknown}; valid: {sorted(PROJECT_METRICS)}")
    if not requested:
        raise HTTPException(400, "no metrics requested")

    project = await db.projects.find_one(
        {"_id": project_id}, projection={"title": 1, "owner_username": 1}
    )
    if not project:
        raise HTTPException(404, f"project {project_id} not tracked")

    match: dict[str, Any] = {"pid": project_id}
    if start or end:
        window: dict[str, Any] = {}
        if start:
            window["$gte"] = start
        if end:
            window["$lte"] = end
        match["ts"] = window

    group: dict[str, Any] = {
        "_id": {"$dateTrunc": {"date": "$ts", **_TRUNC[interval]}},
        "synthetic": {"$last": "$synthetic"},
    }
    for metric in requested:
        group[metric] = {"$last": f"${metric}"}

    pipeline = [
        {"$match": match},
        {"$sort": {"ts": 1}},
        {"$group": group},
        {"$sort": {"_id": 1}},
    ]
    rows = await db.project_snapshots.aggregate(pipeline).to_list(length=100_000)

    series: dict[str, list[dict[str, Any]]] = {}
    for metric in requested:
        points: list[dict[str, Any]] = []
        previous: float | int | None = None
        for row in rows:
            value = row.get(metric)
            if value is None:
                continue
            point: dict[str, Any] = {"ts": row["_id"], "v": value}
            if delta and previous is not None:
                point["d"] = round(value - previous, 4)
            if row.get("synthetic"):
                point["synthetic"] = True
            points.append(point)
            previous = value
        series[metric] = points

    return {
        "entity": {"type": "project", "id": project_id, "title": project.get("title")},
        "interval": interval,
        "buckets": len(rows),
        "series": series,
    }
