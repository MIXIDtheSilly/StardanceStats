from __future__ import annotations

import logging
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase

from ..fetcher import Fetcher, FetchError
from ..ingest.mission import ingest_mission
from ..parsers import ParseError
from ..parsers.mission import parse_mission_page
from ..parsers.common import utcnow
from . import frontier as frontier_store

log = logging.getLogger(__name__)


async def crawl_mission(
    db: AsyncIOMotorDatabase,
    fetcher: Fetcher,
    slug: str,
    *,
    use_cache: bool = True,
) -> dict[str, Any]:
    """Crawl one mission. Expected outcomes are returned, not raised."""
    frontier_id = frontier_store.frontier_id("mission", slug)
    frontier = await db.crawl_frontier.find_one({"_id": frontier_id}) if use_cache else None
    etag = (frontier or {}).get("etag")
    last_modified = (frontier or {}).get("last_modified")

    try:
        response = await fetcher.get(
            f"/missions/{slug}", etag=etag, last_modified=last_modified
        )
    except FetchError as exc:
        await _record(db, slug, "fetch_error", error=str(exc))
        return {"slug": slug, "status": "fetch_error", "error": str(exc)}

    now = utcnow()

    if response.from_cache:
        await _record(db, slug, "not_modified", etag=etag, last_modified=last_modified)
        await db.missions.update_one({"_id": slug}, {"$set": {"last_crawled": now}})
        return {"slug": slug, "status": "not_modified"}

    if response.status in (404, 410):
        await db.missions.update_one(
            {"_id": slug}, {"$set": {"gone": True, "last_crawled": now}}
        )
        await _record(db, slug, "gone")
        return {"slug": slug, "status": "gone"}

    if not response.ok:
        await _record(db, slug, "http_error", error=f"http {response.status}")
        return {"slug": slug, "status": "http_error", "code": response.status}

    try:
        parsed = parse_mission_page(response.text, slug)
    except ParseError as exc:
        await _record(db, slug, "parse_error", error=f"parse: {exc}")
        log.error("parse failure on mission %s: %s", slug, exc)
        return {"slug": slug, "status": "parse_error", "error": str(exc)}

    if parsed.missing:
        # Payout terms drive every attached project's estimate, so a half-read
        # page is worse than the one we already have.
        await _record(db, slug, "incomplete", error=f"unparsed: {sorted(parsed.missing)}")
        log.error("mission %s unparsed fields: %s", slug, sorted(parsed.missing))
        return {"slug": slug, "status": "incomplete", "missing": sorted(parsed.missing)}

    summary = await ingest_mission(db, parsed, now=now)

    await _record(
        db, slug, "ok",
        changed=None if summary["first_ingest"] else bool(summary["changed"]),
        etag=response.etag, last_modified=response.last_modified,
    )
    return {"slug": slug, "status": "ok", **summary}


async def _record(
    db: AsyncIOMotorDatabase, slug: str, status: str, **kwargs: Any
) -> None:
    await frontier_store.record_crawl(db, "mission", slug, status=status, **kwargs)
