from __future__ import annotations

import logging
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase

from .. import blacklist
from ..fetcher import Fetcher, FetchError
from ..ingest import AnomalyRejected, ingest_project
from ..parsers import ParseError, parse_project_page
from ..parsers.common import utcnow
from . import frontier as frontier_store

log = logging.getLogger(__name__)


async def crawl_project(
    db: AsyncIOMotorDatabase,
    fetcher: Fetcher,
    project_id: int,
    *,
    use_cache: bool = True,
    defer_user_totals: bool = False,
) -> dict[str, Any]:
    """Crawl one project. Expected outcomes are returned, not raised."""
    frontier_id = frontier_store.frontier_id("project", project_id)
    frontier = await db.crawl_frontier.find_one({"_id": frontier_id}) if use_cache else None
    etag = (frontier or {}).get("etag")
    last_modified = (frontier or {}).get("last_modified")

    try:
        response = await fetcher.get(
            f"/projects/{project_id}", etag=etag, last_modified=last_modified
        )
    except FetchError as exc:
        await _record(db, project_id, "fetch_error", error=str(exc))
        return {"project_id": project_id, "status": "fetch_error", "error": str(exc)}

    now = utcnow()

    if response.from_cache:
        await _record(db, project_id, "not_modified", etag=etag, last_modified=last_modified)
        await db.projects.update_one({"_id": project_id}, {"$set": {"last_crawled": now}})
        return {"project_id": project_id, "status": "not_modified"}

    if response.status in (404, 410):
        await db.projects.update_one(
            {"_id": project_id}, {"$set": {"gone": True, "last_crawled": now}}
        )
        await _record(db, project_id, "gone")
        return {"project_id": project_id, "status": "gone"}

    if not response.ok:
        await _record(db, project_id, "http_error", error=f"http {response.status}")
        return {"project_id": project_id, "status": "http_error", "code": response.status}

    try:
        parsed = parse_project_page(response.text, project_id)
    except ParseError as exc:
        await _record(db, project_id, "parse_error", error=f"parse: {exc}")
        log.error("parse failure on project %s: %s", project_id, exc)
        return {"project_id": project_id, "status": "parse_error", "error": str(exc)}

    # The sitemap lists projects without their owner, so this is the first look.
    owner = (parsed.data.get("project") or {}).get("owner_username")
    if await blacklist.is_blocked_handle(db, owner):
        await blacklist.purge_projects(db, [project_id])
        log.info("project %s belongs to blacklisted %s, dropped", project_id, owner)
        return {"project_id": project_id, "status": "blacklisted"}

    try:
        summary = await ingest_project(db, parsed, now=now)
    except AnomalyRejected as exc:
        log.error("anomaly on project %s: %s", project_id, exc)
        await _record(db, project_id, "anomaly", error=f"anomaly: {exc}")
        return {"project_id": project_id, "status": "anomaly", "error": str(exc)}

    # A project's rows feed its members' ranking totals.
    if not defer_user_totals:
        from ..ingest import recompute_user_totals

        for user_id in summary.get("linked_users", []):
            await recompute_user_totals(db, user_id)

    await _record(
        db, project_id, "ok",
        changed=None if summary["first_ingest"] else bool(summary["changed"]),
        etag=response.etag, last_modified=response.last_modified,
    )
    return {"project_id": project_id, "status": "ok", **summary}


async def crawl_projects(
    db: AsyncIOMotorDatabase, fetcher: Fetcher, project_ids: list[int], **kwargs: Any
) -> list[dict[str, Any]]:
    """Crawl several projects in sequence (the rate limiter serialises anyway)."""
    return [await crawl_project(db, fetcher, pid, **kwargs) for pid in project_ids]


async def _record(
    db: AsyncIOMotorDatabase, project_id: int, status: str, **kwargs: Any
) -> None:
    await frontier_store.record_crawl(db, "project", project_id, status=status, **kwargs)
