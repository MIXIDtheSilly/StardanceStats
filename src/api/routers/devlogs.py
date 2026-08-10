from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from motor.motor_asyncio import AsyncIOMotorDatabase

from ..deps import db as db_dep
from ..services import stamp

router = APIRouter()


@router.get("/devlogs/{devlog_id}")
async def get_devlog(
    devlog_id: int, db: AsyncIOMotorDatabase = Depends(db_dep)
) -> dict[str, Any]:
    """One devlog. History per devlog is not kept; see /v1/meta."""
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
