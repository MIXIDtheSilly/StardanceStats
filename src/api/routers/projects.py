from __future__ import annotations

import asyncio
import re
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from motor.motor_asyncio import AsyncIOMotorDatabase

from ...db import PROJECT_RANKING_FIELDS
from ..deps import db as db_dep
from ..examples import (
    HISTORY,
    PROJECT,
    PROJECT_COMMENTS,
    PROJECT_DEVLOGS,
    PROJECT_LIST,
    PROJECT_METRICS as PROJECT_METRICS_EXAMPLE,
    PROJECT_SEARCH,
    PROJECT_SHIPS,
    example,
)
from ..services import HistoryError, Interval, bucketed_series, cached_count, stamp
from ..services.history import METRICS, parse_metrics

router = APIRouter()

PROJECT_METRICS = METRICS["project"].metrics

RANKING_METRICS = PROJECT_RANKING_FIELDS

CARD_FIELDS = {
    "title": 1, "description": 1, "banner_url": 1, "demo_url": 1, "repo_url": 1,
    "owner_id": 1, "owner_username": 1, "owner_avatar_url": 1, "members": 1,
    "is_super_star": 1, "is_hardware": 1, "mission": 1, "stats": 1,
    "created_at_estimate": 1, "first_seen": 1, "last_changed": 1, "last_crawled": 1,
}


async def _as_of(db: AsyncIOMotorDatabase, project_id: int) -> datetime | None:
    """The project's own crawl time; its devlogs and ships come off that page."""
    doc = await db.projects.find_one({"_id": project_id}, {"last_crawled": 1})
    return (doc or {}).get("last_crawled")


def _field(metric: str) -> str:
    field = RANKING_METRICS.get(metric)
    if field is None:
        raise HTTPException(
            400, f"unknown metric {metric!r}; valid: {sorted(RANKING_METRICS)}"
        )
    return field


def _card(row: dict[str, Any], field: str, rank: int | None) -> dict[str, Any]:
    section, key = field.split(".", 1)
    return {
        "rank": rank,
        "project_id": row["_id"],
        "title": row.get("title"),
        "description": row.get("description"),
        "banner_url": row.get("banner_url"),
        "demo_url": row.get("demo_url"),
        "repo_url": row.get("repo_url"),
        "owner_id": row.get("owner_id"),
        "owner_username": row.get("owner_username"),
        "owner_avatar_url": row.get("owner_avatar_url"),
        "members": row.get("members") or [],
        "is_super_star": bool(row.get("is_super_star")),
        "is_hardware": bool(row.get("is_hardware")),
        "mission": row.get("mission"),
        "value": (row.get(section) or {}).get(key),
        "stats": row.get("stats") or {},
        "created_at_estimate": row.get("created_at_estimate"),
        "first_seen": row.get("first_seen"),
        "last_changed": row.get("last_changed"),
    }


def _oldest_crawl(rows: list[dict[str, Any]]) -> datetime | None:
    """The stalest row bounds how current the page as a whole is."""
    return min((r["last_crawled"] for r in rows if r.get("last_crawled")), default=None)


# Declared above /projects/{project_id} so "search" is not read as an id.
@router.get("/projects/search", responses=example(PROJECT_SEARCH), response_model=None)
async def search_projects(
    q: str = Query(..., min_length=1, max_length=96, description="Part of a title."),
    limit: int = Query(10, ge=1, le=50),
    metric: str = Query("stardust_total", description="Which ranking the rank belongs to."),
    db: AsyncIOMotorDatabase = Depends(db_dep),
) -> dict[str, Any]:
    """Titles that start with, then merely contain, what was typed."""
    field = _field(metric)
    term = re.escape(q.strip())
    if not term:
        raise HTTPException(400, "nothing to search for")

    seen: list[dict[str, Any]] = []
    ids: set[int] = set()

    # No lowercased title is stored, so both passes scan; the corpus is small enough.
    for pattern in (f"^{term}", term):
        if len(seen) >= limit:
            break
        query = {
            "title": {"$regex": pattern, "$options": "i"},
            "_id": {"$nin": list(ids)},
        }
        cursor = db.projects.find(query, CARD_FIELDS).sort([("title", 1)]).limit(limit - len(seen))
        for row in await cursor.to_list(length=limit):
            ids.add(row["_id"])
            seen.append(row)

    ranks = await _ranks(db, field, seen)

    wanted = q.strip().lower()
    items = [_card(row, field, ranks[index]) for index, row in enumerate(seen)]
    for item, row in zip(items, seen):
        item["exact"] = (row.get("title") or "").lower() == wanted
    items.sort(key=lambda item: (not item["exact"], item["title"] or ""))

    return stamp(
        {"query": q, "metric": metric, "total": len(items), "items": items},
        _oldest_crawl(seen),
    )


async def _ranks(
    db: AsyncIOMotorDatabase, field: str, rows: list[dict[str, Any]]
) -> list[int | None]:
    """The place each row holds, counted the way /projects pages the same ranking."""
    section, key = field.split(".", 1)
    values = [(row.get(section) or {}).get(key) for row in rows]
    ranked = [
        (row, value)
        for row, value in zip(rows, values)
        if isinstance(value, (int, float))
    ]
    distinct = sorted({value for _, value in ranked})

    # A tie is settled by id, so the rows tied ahead of this one hold a place too.
    counted = await asyncio.gather(
        *(cached_count(db, "projects", {field: {"$gt": value}}) for value in distinct),
        *(
            cached_count(db, "projects", {field: value, "_id": {"$lt": row["_id"]}})
            for row, value in ranked
        ),
    )
    ahead = dict(zip(distinct, counted))
    tied = dict(zip((row["_id"] for row, _ in ranked), counted[len(distinct):]))

    return [
        ahead[value] + tied[row["_id"]] + 1
        if isinstance(value, (int, float))
        else None
        for row, value in zip(rows, values)
    ]


@router.get("/projects", responses=example(PROJECT_LIST), response_model=None)
async def list_projects(
    metric: str = Query("stardust_total", description="See /v1/projects/metrics."),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    super_star: bool = Query(False, description="Only projects the team has marked."),
    hardware: bool = Query(False, description="Only projects that build something physical."),
    mission: str | None = Query(None, description="Slug of a mission to filter by."),
    db: AsyncIOMotorDatabase = Depends(db_dep),
) -> dict[str, Any]:
    """Projects ranked by one of their stats, highest first."""
    field = _field(metric)

    query: dict[str, Any] = {field: {"$ne": None}}
    if super_star:
        query["is_super_star"] = True
    if hardware:
        query["is_hardware"] = True
    if mission:
        query["mission.slug"] = mission

    # Id breaks a tie, without which two projects on the same value could swap pages between calls.
    cursor = (
        db.projects.find(query, CARD_FIELDS)
        .sort([(field, -1), ("_id", 1)])
        .skip(offset)
        .limit(limit)
    )
    rows = await cursor.to_list(length=limit)

    return stamp(
        {
            "metric": metric,
            "source": "computed from crawled project pages",
            "total": await cached_count(db, "projects", query),
            "limit": limit,
            "offset": offset,
            "items": [
                _card(row, field, offset + index + 1) for index, row in enumerate(rows)
            ],
        },
        _oldest_crawl(rows),
    )


@router.get(
    "/projects/metrics",
    responses=example(PROJECT_METRICS_EXAMPLE),
    response_model=None,
)
async def project_metrics() -> dict[str, Any]:
    """The metrics /projects will rank by, and the subset /history will chart."""
    return {
        "metrics": sorted(RANKING_METRICS),
        "chartable": sorted(PROJECT_METRICS),
        "note": (
            "stardust_total counts rated ship payouts plus what a mission pays "
            "directly. Nothing a project earned off its own page is visible to us."
        ),
    }


@router.get("/projects/{project_id}", responses=example(PROJECT), response_model=None)
async def get_project(
    project_id: int, db: AsyncIOMotorDatabase = Depends(db_dep)
) -> dict[str, Any]:
    """One project as we last read it, with every field we hold for it."""
    doc = await db.projects.find_one({"_id": project_id})
    if not doc:
        raise HTTPException(404, f"project {project_id} not tracked")
    return stamp(doc, doc.get("last_crawled"))


@router.get(
    "/projects/{project_id}/devlogs",
    responses=example(PROJECT_DEVLOGS),
    response_model=None,
)
async def get_project_devlogs(
    project_id: int,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    sort: Literal[
        "posted_at", "likes", "comments", "views", "duration_seconds"
    ] = "posted_at",
    db: AsyncIOMotorDatabase = Depends(db_dep),
) -> dict[str, Any]:
    """This project's devlogs, newest first unless another sort is asked for."""
    total = await cached_count(db, "devlogs", {"project_id": project_id})
    cursor = (
        db.devlogs.find({"project_id": project_id})
        # _id breaks ties, so paging cannot show the same row twice or skip one.
        .sort([(sort, -1), ("_id", -1)])
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


@router.get(
    "/projects/{project_id}/comments",
    responses=example(PROJECT_COMMENTS),
    response_model=None,
)
async def get_project_comments(
    project_id: int,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncIOMotorDatabase = Depends(db_dep),
) -> dict[str, Any]:
    """Every comment we have read across this project's devlogs, newest first."""
    query = {"project_id": project_id, "gone": {"$ne": True}}
    total = await cached_count(db, "comments", query)
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


@router.get(
    "/projects/{project_id}/ships",
    responses=example(PROJECT_SHIPS),
    response_model=None,
)
async def get_project_ships(
    project_id: int, db: AsyncIOMotorDatabase = Depends(db_dep)
) -> dict[str, Any]:
    """Every rated ship on this project, oldest first, and what they paid."""
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


@router.get(
    "/projects/{project_id}/history",
    responses=example(HISTORY),
    response_model=None,
)
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
