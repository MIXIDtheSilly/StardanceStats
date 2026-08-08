from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from motor.motor_asyncio import AsyncIOMotorDatabase

from ...collector.rollup import SCOPE
from ..deps import db as db_dep
from ..services import HistoryError, Interval, bucketed_series, latest_snapshot, stamp
from ..services.history import METRICS, parse_metrics

router = APIRouter()

GLOBAL_METRICS = METRICS["global"].metrics


@router.get("/global")
async def get_global(db: AsyncIOMotorDatabase = Depends(db_dep)) -> dict[str, Any]:
    """The most recent platform rollup: what we hold, summed."""
    latest = await latest_snapshot(db, "global", SCOPE)
    if not latest:
        raise HTTPException(404, "no rollup written yet")

    ts = latest.pop("ts", None)
    latest.pop("_id", None)
    latest.pop("scope", None)
    return stamp({"scope": SCOPE, "totals": latest}, ts)


@router.get("/global/history")
async def get_global_history(
    metrics: str = Query(
        "users,projects,devlogs,hours,stardust_paid",
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
    """Platform-wide history. Counts what we have crawled, not what exists."""
    try:
        requested = parse_metrics("global", metrics)
        result = await bucketed_series(
            db, "global", SCOPE,
            metrics=requested, interval=interval, start=start, end=end,
            delta=delta, fill=fill,
        )
    except HistoryError as exc:
        raise HTTPException(400, str(exc)) from exc

    latest = await latest_snapshot(db, "global", SCOPE)
    result["entity"] = {"type": "global", "scope": SCOPE}
    result["note"] = (
        "Rollups sum the rows we hold. Early points track crawl coverage as "
        "much as platform growth; compare against users_known / projects_known."
    )
    return stamp(result, (latest or {}).get("ts"))
