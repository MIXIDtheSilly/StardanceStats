from __future__ import annotations

import logging
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase

from ..fetcher import Fetcher, FetchError
from ..ingest import AnomalyRejected, ingest_project
from ..parsers import ParseError, parse_project_page
from ..parsers.common import utcnow

log = logging.getLogger(__name__)


async def crawl_project(
    db: AsyncIOMotorDatabase,
    fetcher: Fetcher,
    project_id: int,
    *,
    use_cache: bool = True,
) -> dict[str, Any]:
    """Crawl one project. Expected outcomes are returned, not raised."""
    frontier_id = f"project:{project_id}"
    frontier = await db.crawl_frontier.find_one({"_id": frontier_id}) if use_cache else None
    etag = (frontier or {}).get("etag")
    last_modified = (frontier or {}).get("last_modified")

    try:
        response = await fetcher.get(
            f"/projects/{project_id}", etag=etag, last_modified=last_modified
        )
    except FetchError as exc:
        await _touch_frontier(db, frontier_id, project_id, error=str(exc))
        return {"project_id": project_id, "status": "fetch_error", "error": str(exc)}

    now = utcnow()

    if response.from_cache:
        await _touch_frontier(db, frontier_id, project_id, etag=etag, last_modified=last_modified)
        await db.projects.update_one({"_id": project_id}, {"$set": {"last_crawled": now}})
        return {"project_id": project_id, "status": "not_modified"}

    if response.status in (404, 410):
        await db.projects.update_one(
            {"_id": project_id}, {"$set": {"gone": True, "last_crawled": now}}
        )
        await _touch_frontier(db, frontier_id, project_id, tier="frozen")
        return {"project_id": project_id, "status": "gone"}

    if not response.ok:
        await _touch_frontier(db, frontier_id, project_id, error=f"http {response.status}")
        return {"project_id": project_id, "status": "http_error", "code": response.status}

    try:
        parsed = parse_project_page(response.text, project_id)
    except ParseError as exc:
        await _touch_frontier(db, frontier_id, project_id, error=f"parse: {exc}")
        log.error("parse failure on project %s: %s", project_id, exc)
        return {"project_id": project_id, "status": "parse_error", "error": str(exc)}

    try:
        summary = await ingest_project(db, parsed, now=now)
    except AnomalyRejected as exc:
        log.error("anomaly on project %s: %s", project_id, exc)
        await _touch_frontier(db, frontier_id, project_id, error=f"anomaly: {exc}")
        return {"project_id": project_id, "status": "anomaly", "error": str(exc)}

    await _touch_frontier(
        db, frontier_id, project_id,
        etag=response.etag, last_modified=response.last_modified,
    )
    return {"project_id": project_id, "status": "ok", **summary}


async def crawl_projects(
    db: AsyncIOMotorDatabase, fetcher: Fetcher, project_ids: list[int], **kwargs: Any
) -> list[dict[str, Any]]:
    """Crawl several projects in sequence (the rate limiter serialises anyway)."""
    return [await crawl_project(db, fetcher, pid, **kwargs) for pid in project_ids]


async def _touch_frontier(
    db: AsyncIOMotorDatabase,
    frontier_id: str,
    project_id: int,
    *,
    etag: str | None = None,
    last_modified: str | None = None,
    error: str | None = None,
    tier: str | None = None,
) -> None:
    now = utcnow()
    update: dict[str, Any] = {
        "kind": "project",
        "ref_id": project_id,
        "url": f"/projects/{project_id}",
        "last_crawled": now,
    }
    if etag:
        update["etag"] = etag
    if last_modified:
        update["last_modified"] = last_modified
    if tier:
        update["tier"] = tier

    ops: dict[str, Any] = {"$set": update}
    if error:
        update["last_error"] = error
        ops["$inc"] = {"error_count": 1}
    else:
        update["last_error"] = None
        ops["$set"]["error_count"] = 0

    await db.crawl_frontier.update_one({"_id": frontier_id}, ops, upsert=True)
