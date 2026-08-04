from __future__ import annotations

from motor.motor_asyncio import AsyncIOMotorDatabase

from ..db import get_db


async def db() -> AsyncIOMotorDatabase:
    return get_db()
