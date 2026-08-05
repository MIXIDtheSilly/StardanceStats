from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from motor.motor_asyncio import AsyncIOMotorDatabase

from ..deps import db as db_dep

router = APIRouter()

USER_METRICS = {
    "followers", "following", "devlogs", "projects", "ships", "votes",
    "streak", "achievements_earned", "ship_stardust", "hours", "shipped_hours",
    "paid_hours", "likes_received", "comments_received", "reposts_received",
    "views_received", "best_multiplier", "avg_multiplier",
}

# totals.* is computed from our crawled rows, stats.* comes off the profile.
LEADERBOARD_METRICS = {
    "ship_stardust": "totals.ship_stardust",
    "hours": "totals.hours",
    "shipped_hours": "totals.shipped_hours",
    "paid_hours": "totals.paid_hours",
    "likes_received": "totals.likes_received",
    "comments_received": "totals.comments_received",
    "views_received": "totals.views_received",
    "best_multiplier": "totals.best_multiplier",
    "stardust_per_paid_hour": "totals.stardust_per_paid_hour",
    "estimated_total_stardust": "totals.estimated_total_stardust",
    "followers": "stats.followers",
    "devlogs": "stats.devlogs",
    "ships": "stats.ships",
    "projects": "stats.projects",
}

Interval = Literal["1h", "1d", "1w"]

_TRUNC = {
    "1h": {"unit": "hour", "binSize": 1},
    "1d": {"unit": "day", "binSize": 1},
    "1w": {"unit": "week", "binSize": 1},
}


async def _find_user(db: AsyncIOMotorDatabase, ref: str) -> dict[str, Any]:
    """Look up by id or handle, including previous handles after a rename."""
    if ref.isdigit():
        doc = await db.users.find_one({"_id": int(ref)})
        if doc:
            return doc

    handle = ref.lstrip("@").lower()
    doc = await db.users.find_one({"username_lower": handle})
    if doc:
        return doc

    doc = await db.users.find_one({"previous_usernames": {"$regex": f"^{ref.lstrip('@')}$", "$options": "i"}})
    if doc:
        return doc

    raise HTTPException(404, f"user {ref!r} not tracked")


@router.get("/users/{ref}")
async def get_user(ref: str, db: AsyncIOMotorDatabase = Depends(db_dep)) -> dict[str, Any]:
    return await _find_user(db, ref)


@router.get("/users/{ref}/projects")
async def get_user_projects(
    ref: str, db: AsyncIOMotorDatabase = Depends(db_dep)
) -> dict[str, Any]:
    user = await _find_user(db, ref)
    cursor = db.projects.find(
        {"$or": [{"owner_id": user["_id"]}, {"member_ids": user["_id"]}]}
    ).sort([("stats.stardust_total", -1)])
    items = await cursor.to_list(length=200)
    return {"user_id": user["_id"], "username": user["username"], "total": len(items), "items": items}


@router.get("/users/{ref}/devlogs")
async def get_user_devlogs(
    ref: str,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    sort: Literal["posted_at", "likes", "comments", "duration_seconds"] = "posted_at",
    db: AsyncIOMotorDatabase = Depends(db_dep),
) -> dict[str, Any]:
    user = await _find_user(db, ref)
    query = {"user_id": user["_id"]}
    total = await db.devlogs.count_documents(query)
    cursor = db.devlogs.find(query).sort([(sort, -1)]).skip(offset).limit(limit)
    return {
        "user_id": user["_id"],
        "username": user["username"],
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": await cursor.to_list(length=limit),
    }


@router.get("/users/{ref}/ships")
async def get_user_ships(
    ref: str, db: AsyncIOMotorDatabase = Depends(db_dep)
) -> dict[str, Any]:
    user = await _find_user(db, ref)
    cursor = db.ships.find({"user_id": user["_id"]}).sort([("shipped_at", -1)])
    items = await cursor.to_list(length=500)
    return {
        "user_id": user["_id"],
        "username": user["username"],
        "total": len(items),
        "stardust_total": sum(s.get("payout") or 0 for s in items),
        "items": items,
    }


@router.get("/users/{ref}/history")
async def get_user_history(
    ref: str,
    metrics: str = Query("followers,devlogs,ships", description="Comma-separated; see /v1/meta."),
    interval: Interval = "1d",
    start: datetime | None = None,
    end: datetime | None = None,
    delta: bool = Query(True),
    db: AsyncIOMotorDatabase = Depends(db_dep),
) -> dict[str, Any]:
    """Bucketed time series for one user, reporting the last value per bucket."""
    user = await _find_user(db, ref)

    requested = [m.strip() for m in metrics.split(",") if m.strip()]
    unknown = [m for m in requested if m not in USER_METRICS]
    if unknown:
        raise HTTPException(400, f"unknown metric(s): {unknown}; valid: {sorted(USER_METRICS)}")
    if not requested:
        raise HTTPException(400, "no metrics requested")

    match: dict[str, Any] = {"uid": user["_id"]}
    if start or end:
        window: dict[str, Any] = {}
        if start:
            window["$gte"] = start
        if end:
            window["$lte"] = end
        match["ts"] = window

    group: dict[str, Any] = {"_id": {"$dateTrunc": {"date": "$ts", **_TRUNC[interval]}}}
    for metric in requested:
        group[metric] = {"$last": f"${metric}"}

    rows = await db.user_snapshots.aggregate([
        {"$match": match},
        {"$sort": {"ts": 1}},
        {"$group": group},
        {"$sort": {"_id": 1}},
    ]).to_list(length=100_000)

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
            points.append(point)
            previous = value
        series[metric] = points

    return {
        "entity": {"type": "user", "id": user["_id"], "username": user["username"]},
        "interval": interval,
        "buckets": len(rows),
        "series": series,
    }


@router.get("/leaderboard")
async def leaderboard(
    metric: str = Query("ship_stardust", description="See /v1/leaderboard/metrics."),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    complete_only: bool = Query(
        False, description="Only users whose crawled rows match their profile counts."
    ),
    db: AsyncIOMotorDatabase = Depends(db_dep),
) -> dict[str, Any]:
    """Our own ranking, computed from crawled rows.

    A user whose projects are not all crawled under-ranks, so every row carries
    a complete flag and complete_only filters to the rows that are.
    """
    field = LEADERBOARD_METRICS.get(metric)
    if field is None:
        raise HTTPException(
            400, f"unknown metric {metric!r}; valid: {sorted(LEADERBOARD_METRICS)}"
        )

    query: dict[str, Any] = {field: {"$ne": None}, "hidden": {"$ne": True}}
    if complete_only:
        query["coverage.complete"] = True

    cursor = (
        db.users.find(query, {"username": 1, "avatar_url": 1, "stats": 1, "totals": 1, "coverage": 1})
        .sort([(field, -1)])
        .skip(offset)
        .limit(limit)
    )
    rows = await cursor.to_list(length=limit)

    section, key = field.split(".", 1)
    items = [
        {
            "rank": offset + i + 1,
            "user_id": r["_id"],
            "username": r.get("username"),
            "avatar_url": r.get("avatar_url"),
            "value": (r.get(section) or {}).get(key),
            "complete": (r.get("coverage") or {}).get("complete", False),
        }
        for i, r in enumerate(rows)
    ]
    return {
        "metric": metric,
        "source": "computed from crawled rows",
        "total": await db.users.count_documents(query),
        "limit": limit,
        "offset": offset,
        "items": items,
    }


@router.get("/leaderboard/metrics")
async def leaderboard_metrics() -> dict[str, Any]:
    return {
        "metrics": sorted(LEADERBOARD_METRICS),
        "computed_by_us": sorted(
            k for k, v in LEADERBOARD_METRICS.items() if v.startswith("totals.")
        ),
        "reported_by_profile": sorted(
            k for k, v in LEADERBOARD_METRICS.items() if v.startswith("stats.")
        ),
        "note": (
            "ship_stardust counts ship payouts only. Upstream totals also include "
            "achievements, missions, reviewer and show-and-tell payouts and manual "
            "grants, none of which are public."
        ),
    }
