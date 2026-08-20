from __future__ import annotations

import asyncio
import re
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from motor.motor_asyncio import AsyncIOMotorDatabase

from ...db import LEADERBOARD_FIELDS
from ..deps import db as db_dep
from ..examples import (
    HISTORY,
    LEADERBOARD,
    LEADERBOARD_METRICS as LEADERBOARD_METRICS_EXAMPLE,
    USER,
    USER_COMMENTS,
    USER_DEVLOGS,
    USER_PROJECTS,
    USER_SEARCH,
    USER_SHIPS,
    example,
)
from ..services import HistoryError, Interval, bucketed_series, cached_count, stamp
from ..services.history import METRICS, parse_metrics

router = APIRouter()

USER_METRICS = METRICS["user"].metrics

LEADERBOARD_METRICS = LEADERBOARD_FIELDS

def _oldest_crawl(rows: list[dict[str, Any]]) -> datetime | None:
    return min((r["last_crawled"] for r in rows if r.get("last_crawled")), default=None)


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

    doc = await db.users.find_one({"previous_usernames_lower": handle})
    if doc:
        return doc

    raise HTTPException(404, f"user {ref!r} not tracked")


def _escape(term: str) -> str:
    """Regex-quote a handle so a search for a.b cannot match aXb."""
    return re.escape(term)


# Declared above /users/{ref} so "search" is not read as a handle.
@router.get("/users/search", responses=example(USER_SEARCH), response_model=None)
async def search_users(
    q: str = Query(..., min_length=1, max_length=64, description="Part of a handle."),
    limit: int = Query(10, ge=1, le=50),
    metric: str = Query("ship_stardust", description="Which ranking the rank belongs to."),
    db: AsyncIOMotorDatabase = Depends(db_dep),
) -> dict[str, Any]:
    """Handles that start with, then merely contain, what was typed."""
    field = LEADERBOARD_METRICS.get(metric)
    if field is None:
        raise HTTPException(
            400, f"unknown metric {metric!r}; valid: {sorted(LEADERBOARD_METRICS)}"
        )

    term = _escape(q.strip().lstrip("@").lower())
    if not term:
        raise HTTPException(400, "nothing to search for")

    fields = {"username": 1, "avatar_url": 1, "stats": 1, "totals": 1, "last_crawled": 1}
    seen: list[dict[str, Any]] = []
    ids: set[int] = set()

    # The contains pass cannot use the index, so it only runs on what the prefix pass missed.
    for pattern in (f"^{term}", term):
        if len(seen) >= limit:
            break
        query = {
            "username_lower": {"$regex": pattern},
            "hidden": {"$ne": True},
            "_id": {"$nin": list(ids)},
        }
        cursor = db.users.find(query, fields).sort([("username_lower", 1)]).limit(limit - len(seen))
        for row in await cursor.to_list(length=limit):
            ids.add(row["_id"])
            seen.append(row)

    section, key = field.split(".", 1)
    ranks = await _ranks(db, field, [(row.get(section) or {}).get(key) for row in seen])

    items = [
        {
            "user_id": row["_id"],
            "username": row.get("username"),
            "avatar_url": row.get("avatar_url"),
            "value": (row.get(section) or {}).get(key),
            "rank": ranks[index],
            "stats": row.get("stats") or {},
            "totals": row.get("totals") or {},
            "exact": (row.get("username") or "").lower() == q.strip().lstrip("@").lower(),
        }
        for index, row in enumerate(seen)
    ]
    items.sort(key=lambda item: (not item["exact"], item["username"] or ""))

    return stamp(
        {"query": q, "metric": metric, "total": len(items), "items": items},
        _oldest_crawl(seen),
    )


async def _ranks(
    db: AsyncIOMotorDatabase, field: str, values: list[Any]
) -> list[int | None]:
    """Standing each value would take, from 1; a tie takes the better place."""
    distinct = sorted({v for v in values if isinstance(v, (int, float))})
    counted = await asyncio.gather(*(
        cached_count(db, "users", {field: {"$gt": value}, "hidden": {"$ne": True}})
        for value in distinct
    ))
    ahead = dict(zip(distinct, counted))
    return [
        ahead[value] + 1 if isinstance(value, (int, float)) else None for value in values
    ]


@router.get("/users/{ref}", responses=example(USER), response_model=None)
async def get_user(ref: str, db: AsyncIOMotorDatabase = Depends(db_dep)) -> dict[str, Any]:
    """One maker by id or handle. A previous handle resolves too, after a rename."""
    doc = await _find_user(db, ref)
    return stamp(doc, doc.get("last_crawled"))


@router.get("/users/{ref}/projects", responses=example(USER_PROJECTS), response_model=None)
async def get_user_projects(
    ref: str, db: AsyncIOMotorDatabase = Depends(db_dep)
) -> dict[str, Any]:
    """Projects this user owns or is a member of, richest first."""
    user = await _find_user(db, ref)
    cursor = db.projects.find(
        {"$or": [{"owner_id": user["_id"]}, {"member_ids": user["_id"]}]}
    ).sort([("stats.stardust_total", -1)])
    items = await cursor.to_list(length=200)
    return stamp(
        {
            "user_id": user["_id"],
            "username": user["username"],
            "total": len(items),
            "items": items,
        },
        user.get("last_crawled"),
    )


@router.get("/users/{ref}/devlogs", responses=example(USER_DEVLOGS), response_model=None)
async def get_user_devlogs(
    ref: str,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    sort: Literal[
        "posted_at", "likes", "comments", "views", "duration_seconds"
    ] = "posted_at",
    db: AsyncIOMotorDatabase = Depends(db_dep),
) -> dict[str, Any]:
    """Devlogs this user has posted, across every project they work on."""
    user = await _find_user(db, ref)
    query = {"user_id": user["_id"]}
    total = await cached_count(db, "devlogs", query)
    # _id breaks ties, so paging cannot show the same row twice or skip one.
    cursor = db.devlogs.find(query).sort([(sort, -1), ("_id", -1)]).skip(offset).limit(limit)
    return stamp(
        {
            "user_id": user["_id"],
            "username": user["username"],
            "total": total,
            "limit": limit,
            "offset": offset,
            "items": await cursor.to_list(length=limit),
        },
        user.get("last_crawled"),
    )


@router.get("/users/{ref}/comments", responses=example(USER_COMMENTS), response_model=None)
async def get_user_comments(
    ref: str,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    sort: Literal["posted_at", "body_length"] = "posted_at",
    include_self: bool = Query(True, description="Replies under their own devlogs."),
    db: AsyncIOMotorDatabase = Depends(db_dep),
) -> dict[str, Any]:
    """Comments this user has written, from the threads we have read."""
    user = await _find_user(db, ref)
    query: dict[str, Any] = {"user_id": user["_id"], "gone": {"$ne": True}}
    if not include_self:
        query["is_self"] = False

    total = await cached_count(db, "comments", query)
    cursor = db.comments.find(query).sort([(sort, -1)]).skip(offset).limit(limit)
    items = await cursor.to_list(length=limit)
    totals = user.get("totals") or {}
    return stamp(
        {
            "user_id": user["_id"],
            "username": user["username"],
            "total": total,
            "limit": limit,
            "offset": offset,
            # Counted off the devlog counters, so it leads what we have read.
            "comments_received": totals.get("comments_received"),
            "items": items,
        },
        # Each row is as old as its thread's crawl; the oldest bounds the page.
        _oldest_crawl(items),
    )


@router.get("/users/{ref}/ships", responses=example(USER_SHIPS), response_model=None)
async def get_user_ships(
    ref: str, db: AsyncIOMotorDatabase = Depends(db_dep)
) -> dict[str, Any]:
    """Every rated ship this user has shipped, newest first."""
    user = await _find_user(db, ref)
    cursor = db.ships.find({"user_id": user["_id"]}).sort([("shipped_at", -1)])
    items = await cursor.to_list(length=500)
    return stamp(
        {
            "user_id": user["_id"],
            "username": user["username"],
            "total": len(items),
            "stardust_total": sum(s.get("payout") or 0 for s in items),
            "items": items,
        },
        user.get("last_crawled"),
    )


@router.get("/users/{ref}/history", responses=example(HISTORY), response_model=None)
async def get_user_history(
    ref: str,
    metrics: str = Query("followers,devlogs,ships", description="Comma-separated; see /v1/meta."),
    interval: Interval = "1d",
    start: datetime | None = None,
    end: datetime | None = None,
    delta: bool = Query(True),
    fill: Literal["none", "locf"] = Query(
        "none", description="locf carries the last observation into empty buckets."
    ),
    db: AsyncIOMotorDatabase = Depends(db_dep),
) -> dict[str, Any]:
    """Bucketed time series for one user, reporting the last value per bucket."""
    user = await _find_user(db, ref)

    try:
        requested = parse_metrics("user", metrics)
        result = await bucketed_series(
            db, "user", user["_id"],
            metrics=requested, interval=interval, start=start, end=end,
            delta=delta, fill=fill,
        )
    except HistoryError as exc:
        raise HTTPException(400, str(exc)) from exc

    result["entity"] = {
        "type": "user", "id": user["_id"], "username": user.get("username")
    }
    return stamp(result, user.get("last_crawled"))


@router.get("/leaderboard", responses=example(LEADERBOARD), response_model=None)
async def leaderboard(
    metric: str = Query("ship_stardust", description="See /v1/leaderboard/metrics."),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    complete_only: bool = Query(
        False, description="Only users whose crawled rows match their profile counts."
    ),
    db: AsyncIOMotorDatabase = Depends(db_dep),
) -> dict[str, Any]:
    """Our own ranking, computed from crawled rows."""
    field = LEADERBOARD_METRICS.get(metric)
    if field is None:
        raise HTTPException(
            400, f"unknown metric {metric!r}; valid: {sorted(LEADERBOARD_METRICS)}"
        )

    query: dict[str, Any] = {field: {"$ne": None}, "hidden": {"$ne": True}}
    if complete_only:
        query["coverage.complete"] = True

    cursor = (
        db.users.find(
            query,
            {"username": 1, "avatar_url": 1, "stats": 1, "totals": 1,
             "coverage": 1, "last_crawled": 1},
        )
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
    as_of = min((r["last_crawled"] for r in rows if r.get("last_crawled")), default=None)
    return stamp(
        {
            "metric": metric,
            "source": "computed from crawled rows",
            "total": await cached_count(db, "users", query),
            "limit": limit,
            "offset": offset,
            "items": items,
        },
        as_of,
    )


@router.get(
    "/leaderboard/metrics",
    responses=example(LEADERBOARD_METRICS_EXAMPLE),
    response_model=None,
)
async def leaderboard_metrics() -> dict[str, Any]:
    """The metrics /leaderboard will rank by, split by who counted them."""
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
