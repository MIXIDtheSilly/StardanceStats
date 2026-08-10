from __future__ import annotations

import logging
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase

from ..config import settings
from ..fetcher import Fetcher, FetchError
from ..ingest import CommentsRejected, ingest_comments
from ..parsers import ParseError, parse_devlog_page
from ..parsers.common import utcnow
from . import frontier as frontier_store

log = logging.getLogger(__name__)

DEVLOG_PATH = "/projects/{project_id}/devlogs/{devlog_id}"

# A queued thread is one whose counter moved; the schedule is only a backstop.
THREAD_TIER = "warm"


async def crawl_devlog(
    db: AsyncIOMotorDatabase,
    fetcher: Fetcher,
    devlog_id: int,
    *,
    project_id: int | None = None,
    use_cache: bool = True,
) -> dict[str, Any]:
    """Crawl one devlog's comment thread. Expected outcomes are returned, not raised."""
    devlog = await db.devlogs.find_one({"_id": devlog_id}, {"project_id": 1, "comments": 1})
    project_id = project_id or (devlog or {}).get("project_id")
    if project_id is None:
        # The show route is nested, so without the project there is no url.
        await _record(db, devlog_id, "parse_error", error="no project for devlog")
        return {"devlog_id": devlog_id, "status": "parse_error", "error": "no project"}

    frontier_id = frontier_store.frontier_id("devlog", devlog_id)
    frontier = await db.crawl_frontier.find_one({"_id": frontier_id}) if use_cache else None
    etag = (frontier or {}).get("etag")
    last_modified = (frontier or {}).get("last_modified")

    path = DEVLOG_PATH.format(project_id=project_id, devlog_id=devlog_id)
    try:
        response = await fetcher.get(path, etag=etag, last_modified=last_modified)
    except FetchError as exc:
        await _record(db, devlog_id, "fetch_error", project_id, error=str(exc))
        return {"devlog_id": devlog_id, "status": "fetch_error", "error": str(exc)}

    now = utcnow()

    if response.from_cache:
        await _record(
            db, devlog_id, "not_modified", project_id,
            etag=etag, last_modified=last_modified,
        )
        # The watermark still moves, or the flag comes straight back.
        await db.devlogs.update_one(
            {"_id": devlog_id},
            {"$set": {
                "comments_crawled_at": now,
                "comments_crawled_count": (devlog or {}).get("comments"),
                "comments_stale": False,
            }},
        )
        return {"devlog_id": devlog_id, "status": "not_modified"}

    if response.status in (404, 410):
        await db.devlogs.update_one(
            {"_id": devlog_id}, {"$set": {"gone": True, "comments_stale": False}}
        )
        await _record(db, devlog_id, "gone", project_id)
        return {"devlog_id": devlog_id, "status": "gone"}

    if not response.ok:
        await _record(
            db, devlog_id, "http_error", project_id, error=f"http {response.status}"
        )
        return {"devlog_id": devlog_id, "status": "http_error", "code": response.status}

    try:
        parsed = parse_devlog_page(response.text, devlog_id, project_id)
    except ParseError as exc:
        await _record(db, devlog_id, "parse_error", project_id, error=f"parse: {exc}")
        log.error("parse failure on devlog %s: %s", devlog_id, exc)
        return {"devlog_id": devlog_id, "status": "parse_error", "error": str(exc)}

    try:
        summary = await ingest_comments(db, parsed, now=now)
    except CommentsRejected as exc:
        log.error("thread rejected on devlog %s: %s", devlog_id, exc)
        await _record(db, devlog_id, "anomaly", project_id, error=f"anomaly: {exc}")
        return {"devlog_id": devlog_id, "status": "anomaly", "error": str(exc)}

    await _record(
        db, devlog_id, "ok", project_id,
        changed=bool(summary["new"] or summary["retired"]),
        etag=response.etag, last_modified=response.last_modified,
    )
    return {"devlog_id": devlog_id, "status": "ok", **summary}


async def enqueue_stale_threads(
    db: AsyncIOMotorDatabase, *, limit: int | None = None
) -> dict[str, Any]:
    """Queue the threads whose comment counter has moved. No network of its own."""
    if not settings.crawl_comments:
        return {"queued": 0, "disabled": True}

    limit = limit or settings.comment_queue_limit
    cursor = db.devlogs.find(
        {"comments_stale": True, "gone": {"$ne": True}},
        {"project_id": 1},
    ).limit(limit)
    rows = [row async for row in cursor if row.get("project_id") is not None]

    queued = await frontier_store.enqueue_devlogs(db, rows, tier=THREAD_TIER)
    pending = await db.devlogs.count_documents({"comments_stale": True})
    if queued:
        log.info("queued %d comment thread(s), %d still flagged", queued, pending)
    return {"queued": queued, "pending": pending}


async def _record(
    db: AsyncIOMotorDatabase,
    devlog_id: int,
    status: str,
    project_id: int | None = None,
    **kwargs: Any,
) -> None:
    await frontier_store.record_crawl(
        db, "devlog", devlog_id, status=status, parent_id=project_id, **kwargs
    )
