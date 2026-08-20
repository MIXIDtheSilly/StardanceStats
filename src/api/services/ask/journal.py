from __future__ import annotations

import asyncio
import logging
from typing import Any

from .... import db as database

log = logging.getLogger(__name__)

COLLECTION = "ask_log"
WRITE_TIMEOUT = 3.0


async def record(entry: dict[str, Any]) -> None:
    try:
        await asyncio.wait_for(
            database.get_db()[COLLECTION].insert_one(entry), WRITE_TIMEOUT
        )
    except asyncio.TimeoutError:
        log.warning("ask log write timed out after %ss", WRITE_TIMEOUT)
    except Exception as exc:
        log.warning("ask log write failed: %r", exc)
