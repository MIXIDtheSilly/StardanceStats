from __future__ import annotations

from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends
from motor.motor_asyncio import AsyncIOMotorDatabase

from ...parsers.common import utcnow
from ..deps import db as db_dep

router = APIRouter()


@router.get("/health")
async def health(db: AsyncIOMotorDatabase = Depends(db_dep)) -> dict[str, Any]:
    """Liveness plus collector freshness: is the data actually moving?"""
    now = utcnow()
    try:
        await db.command("ping")
        mongo_ok = True
    except Exception as exc:  # surfaced, not raised: health must always answer
        return {"status": "degraded", "mongo": False, "error": str(exc)}

    last = await db.projects.find_one(
        {"last_crawled": {"$exists": True}},
        sort=[("last_crawled", -1)],
        projection={"last_crawled": 1},
    )
    last_crawl = last.get("last_crawled") if last else None
    stale = last_crawl is None or (now - last_crawl) > timedelta(hours=24)

    errors = await db.crawl_log.count_documents(
        {"ts": {"$gte": now - timedelta(hours=1)}, "status": {"$ne": "ok"}}
    )

    return {
        "status": "degraded" if stale else "ok",
        "mongo": mongo_ok,
        "last_crawl": last_crawl,
        "stale": stale,
        "errors_last_hour": errors,
        "now": now,
    }


@router.get("/meta")
async def meta(db: AsyncIOMotorDatabase = Depends(db_dep)) -> dict[str, Any]:
    """Corpus size and coverage."""
    return {
        "counts": {
            "projects": await db.projects.count_documents({}),
            "devlogs": await db.devlogs.count_documents({}),
            "ships": await db.ships.count_documents({}),
            "users": await db.users.count_documents({}),
            "project_snapshots": await db.project_snapshots.count_documents({}),
        },
        "data_source": "public pages of stardance.hackclub.com",
        "caveats": [
            "Rejected ships, deleted devlogs and unverified profiles are not publicly visible.",
            "Points marked synthetic are reconstructed from timestamps, not observed.",
        ],
    }
