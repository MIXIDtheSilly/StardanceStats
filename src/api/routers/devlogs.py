from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
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
