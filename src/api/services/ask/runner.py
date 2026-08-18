from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo.errors import ExecutionTimeout, OperationFailure, PyMongoError

from ....config import settings
from .guard import AskError

log = logging.getLogger(__name__)

# Long bodies would swamp both the table and the response.
MAX_CELL = 1200

_client: AsyncIOMotorClient | None = None


def get_client() -> AsyncIOMotorClient:
    """Its own client, because its credentials are the whole safety story here."""
    global _client
    if _client is None:
        if not settings.ask_mongo_url:
            raise AskError("no read-only database user is configured")
        _client = AsyncIOMotorClient(
            settings.ask_mongo_url, tz_aware=True, serverSelectionTimeoutMS=5000
        )
    return _client


async def close() -> None:
    global _client
    if _client is not None:
        _client.close()
        _client = None


async def run(collection: str, pipeline: list[dict[str, Any]], *, limit: int) -> list[dict]:
    """Run a validated pipeline, and turn a database refusal into a readable one."""
    db = get_client()[settings.mongo_db]
    try:
        cursor = db[collection].aggregate(
            pipeline,
            maxTimeMS=settings.ask_query_timeout_ms,
            allowDiskUse=False,
            batchSize=limit,
        )
        rows = await cursor.to_list(length=limit)
    except ExecutionTimeout as exc:
        raise AskError(
            "the query ran past its time limit; narrow it or group over less"
        ) from exc
    except OperationFailure as exc:
        # The message names what it refused, which is what a repair turn needs.
        raise AskError(f"the database refused the query: {exc.details or exc}") from exc
    except PyMongoError as exc:
        log.warning("ask query failed on %s: %s", collection, exc)
        raise AskError(f"the query could not run: {exc}") from exc

    return [jsonable(row) for row in rows]


def jsonable(value: Any) -> Any:
    """BSON to something the response model can carry, with long strings clipped."""
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [jsonable(item) for item in value]
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str) and len(value) > MAX_CELL:
        return value[:MAX_CELL] + "..."
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)
