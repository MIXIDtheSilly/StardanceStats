from __future__ import annotations

import json
import time
from collections import OrderedDict
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase

from ...config import settings

MAX_ENTRIES = 2048

_counts: OrderedDict[str, tuple[float, int]] = OrderedDict()


def _key(collection: str, query: dict[str, Any]) -> str:
    return collection + "|" + json.dumps(query, sort_keys=True, default=str)


def clear() -> None:
    _counts.clear()


async def cached_count(
    db: AsyncIOMotorDatabase, collection: str, query: dict[str, Any]
) -> int:
    """count_documents, held for as long as the response it lands in is cacheable."""
    ttl = settings.api_cache_seconds
    if ttl <= 0:
        return await db[collection].count_documents(query)

    key = _key(collection, query)
    now = time.monotonic()
    hit = _counts.get(key)
    if hit is not None and hit[0] > now:
        _counts.move_to_end(key)
        return hit[1]

    total = await db[collection].count_documents(query)
    _counts[key] = (now + ttl, total)
    _counts.move_to_end(key)
    while len(_counts) > MAX_ENTRIES:
        _counts.popitem(last=False)
    return total


async def total_documents(db: AsyncIOMotorDatabase, collection: str) -> int:
    """A whole collection's size, off the metadata rather than a scan of it."""
    return await db[collection].estimated_document_count()
