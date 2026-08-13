from __future__ import annotations

import logging
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase

from .. import blacklist
from ..fetcher import Fetcher, FetchError
from ..ingest import (
    UserAnomalyRejected,
    ingest_user,
    missing_project_ids,
    recompute_user_totals,
)
from ..parsers import ParseError, parse_user_page
from ..parsers.common import utcnow
from . import frontier as frontier_store
from .crawl import crawl_project

log = logging.getLogger(__name__)

# The projects tab adds a card per project for the same request cost.
USER_PATH = "/users/{id}/projects"


async def crawl_user(
    db: AsyncIOMotorDatabase,
    fetcher: Fetcher,
    user_id: int,
    *,
    use_cache: bool = True,
    with_projects: bool = True,
    refresh_projects: bool = False,
) -> dict[str, Any]:
    """Crawl one user by numeric id. Expected outcomes are returned, not raised."""
    if blacklist.is_blocked(user_id):
        await blacklist.retire(db, "user", user_id)
        return {"user_id": user_id, "status": "blacklisted"}

    frontier_id = frontier_store.frontier_id("user", user_id)
    frontier = await db.crawl_frontier.find_one({"_id": frontier_id}) if use_cache else None
    etag = (frontier or {}).get("etag")
    last_modified = (frontier or {}).get("last_modified")

    try:
        response = await fetcher.get(
            USER_PATH.format(id=user_id), etag=etag, last_modified=last_modified
        )
    except FetchError as exc:
        await _record(db, user_id, "fetch_error", error=str(exc))
        return {"user_id": user_id, "status": "fetch_error", "error": str(exc)}

    now = utcnow()

    if response.from_cache:
        await _record(db, user_id, "not_modified", etag=etag, last_modified=last_modified)
        await db.users.update_one({"_id": user_id}, {"$set": {"last_crawled": now}})
        return {"user_id": user_id, "status": "not_modified"}

    if response.status in (404, 410):
        await db.users.update_one(
            {"_id": user_id}, {"$set": {"gone": True, "last_crawled": now}}
        )
        await _record(db, user_id, "gone")
        return {"user_id": user_id, "status": "gone"}

    if not response.ok:
        await _record(db, user_id, "http_error", error=f"http {response.status}")
        return {"user_id": user_id, "status": "http_error", "code": response.status}

    try:
        parsed = parse_user_page(response.text, user_id)
    except ParseError as exc:
        await _record(db, user_id, "parse_error", error=f"parse: {exc}")
        log.error("parse failure on user %s: %s", user_id, exc)
        return {"user_id": user_id, "status": "parse_error", "error": str(exc)}

    try:
        summary = await ingest_user(db, parsed, now=now)
    except UserAnomalyRejected as exc:
        log.error("anomaly on user %s: %s", user_id, exc)
        await _record(db, user_id, "anomaly", error=f"anomaly: {exc}")
        return {"user_id": user_id, "status": "anomaly", "error": str(exc)}

    await _record(
        db, user_id, "ok",
        changed=None if summary["first_ingest"] else bool(summary["changed"]),
        etag=response.etag, last_modified=response.last_modified,
    )

    if with_projects:
        summary["projects"] = await crawl_user_projects(
            db, fetcher, user_id, refresh=refresh_projects
        )
        summary["totals"] = await recompute_user_totals(db, user_id)

    return {"status": "ok", **summary}


async def crawl_user_projects(
    db: AsyncIOMotorDatabase,
    fetcher: Fetcher,
    user_id: int,
    *,
    refresh: bool = False,
) -> dict[str, Any]:
    """Crawl the projects a user's profile lists, so their totals are whole."""
    user = await db.users.find_one({"_id": user_id}, {"project_ids": 1})
    claimed = (user or {}).get("project_ids") or []
    if not claimed:
        return {"claimed": 0, "crawled": [], "failed": []}

    todo = claimed if refresh else await missing_project_ids(db, user or {})

    crawled, failed = [], []
    for project_id in todo:
        outcome = await crawl_project(db, fetcher, project_id)
        if outcome.get("status") in ("ok", "not_modified"):
            crawled.append(project_id)
        else:
            failed.append({"project_id": project_id, "status": outcome.get("status")})
            log.warning("project %s for user %s: %s", project_id, user_id, outcome)

    return {"claimed": len(claimed), "crawled": crawled, "failed": failed}


async def crawl_user_by_handle(
    db: AsyncIOMotorDatabase,
    fetcher: Fetcher,
    handle: str,
    *,
    with_projects: bool = True,
    refresh_projects: bool = False,
) -> dict[str, Any]:
    """Crawl by handle, taking the numeric id from the page's og tags."""
    handle = handle.lstrip("@")
    if await blacklist.is_blocked_handle(db, handle):
        return {"handle": handle, "status": "blacklisted"}

    try:
        response = await fetcher.get(f"/@{handle}/projects")
    except FetchError as exc:
        return {"handle": handle, "status": "fetch_error", "error": str(exc)}

    if response.status in (404, 410):
        return {"handle": handle, "status": "gone"}
    if not response.ok:
        return {"handle": handle, "status": "http_error", "code": response.status}

    try:
        parsed = parse_user_page(response.text)
    except ParseError as exc:
        log.error("parse failure on @%s: %s", handle, exc)
        return {"handle": handle, "status": "parse_error", "error": str(exc)}

    # Blocking the id alone would never teach us the handle their projects carry.
    user_id = parsed.data["user"]["_id"]
    if blacklist.is_blocked(user_id):
        await blacklist.remember(db, parsed.data["user"].get("username") or handle)
        await blacklist.retire(db, "user", user_id)
        return {"handle": handle, "user_id": user_id, "status": "blacklisted"}

    try:
        summary = await ingest_user(db, parsed, now=utcnow())
    except UserAnomalyRejected as exc:
        return {"handle": handle, "status": "anomaly", "error": str(exc)}

    if with_projects:
        summary["projects"] = await crawl_user_projects(
            db, fetcher, summary["user_id"], refresh=refresh_projects
        )
        summary["totals"] = await recompute_user_totals(db, summary["user_id"])

    return {"status": "ok", **summary}


async def resolve_project_owners(
    db: AsyncIOMotorDatabase, fetcher: Fetcher, limit: int = 100
) -> dict[str, Any]:
    """Crawl the owners of crawled projects that still have no numeric id."""
    handles = await db.projects.distinct("owner_username", {"owner_id": None})
    handles = [h for h in handles if h][:limit]

    resolved, failed = [], []
    for handle in handles:
        outcome = await crawl_user_by_handle(db, fetcher, handle)
        (resolved if outcome.get("status") == "ok" else failed).append(handle)

    return {"pending": len(handles), "resolved": resolved, "failed": failed}


async def _record(
    db: AsyncIOMotorDatabase, user_id: int, status: str, **kwargs: Any
) -> None:
    await frontier_store.record_crawl(db, "user", user_id, status=status, **kwargs)
