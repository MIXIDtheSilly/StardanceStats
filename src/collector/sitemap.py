from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Iterable, Iterator, NamedTuple

from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo import UpdateOne

from .. import blacklist
from ..config import settings
from ..fetcher import Fetcher, FetchError
from ..parsers.common import utcnow
from .frontier import frontier_id
from .tiering import classify, next_due, priority

log = logging.getLogger(__name__)

STATE_ID = "sitemap"

_URL_RE = re.compile(r"<url>(.*?)</url>", re.DOTALL)
_LOC_RE = re.compile(r"<loc>\s*(.*?)\s*</loc>", re.DOTALL)
_LASTMOD_RE = re.compile(r"<lastmod>\s*(.*?)\s*</lastmod>", re.DOTALL)
_PATH_RE = re.compile(r"^/(projects|users)/(\d+)/?$")
_MISSION_PATH_RE = re.compile(r"^/missions/([A-Za-z0-9][A-Za-z0-9_-]*)/?$")

CHUNK = 2000


class SitemapEntry(NamedTuple):
    kind: str
    ref_id: int | str | None
    path: str
    lastmod: datetime | None


def parse_sitemap(xml: str) -> Iterator[SitemapEntry]:
    """Yield one entry per <url>."""
    for block in _URL_RE.finditer(xml):
        body = block.group(1)
        loc = _LOC_RE.search(body)
        if not loc:
            continue
        path = _path_of(loc.group(1))
        kind, ref_id = _kind_of(path)
        yield SitemapEntry(kind, ref_id, path, _lastmod(_LASTMOD_RE.search(body)))


def _path_of(url: str) -> str:
    return re.sub(r"^https?://[^/]+", "", url.strip()) or "/"


def _kind_of(path: str) -> tuple[str, int | str | None]:
    m = _PATH_RE.match(path)
    if m:
        return ("project" if m.group(1) == "projects" else "user"), int(m.group(2))
    # Missions are keyed by slug; every other kind is numeric.
    m = _MISSION_PATH_RE.match(path)
    if m:
        return "mission", m.group(1)
    return "other", None


def _lastmod(match: re.Match[str] | None) -> datetime | None:
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1).strip(), "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


async def sync_sitemap(
    db: AsyncIOMotorDatabase,
    fetcher: Fetcher,
    *,
    use_cache: bool = True,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Fetch the sitemap and reconcile the frontier against it."""
    now = now or utcnow()
    state = await db.crawl_state.find_one({"_id": STATE_ID}) if use_cache else None

    try:
        response = await fetcher.get(
            settings.sitemap_path,
            etag=(state or {}).get("etag"),
            last_modified=(state or {}).get("last_modified"),
            max_bytes=settings.sitemap_max_bytes,
            accept="application/xml,text/xml",
        )
    except FetchError as exc:
        log.error("sitemap fetch failed: %s", exc)
        return {"status": "fetch_error", "error": str(exc)}

    if response.from_cache:
        await db.crawl_state.update_one(
            {"_id": STATE_ID}, {"$set": {"last_checked": now}}, upsert=True
        )
        return {"status": "not_modified"}

    if not response.ok:
        return {"status": "http_error", "code": response.status}

    summary = await apply_sitemap(db, parse_sitemap(response.text), now=now)

    await db.crawl_state.update_one(
        {"_id": STATE_ID},
        {"$set": {
            "etag": response.etag,
            "last_modified": response.last_modified,
            "last_checked": now,
            "last_synced": now,
            "counts": summary,
        }},
        upsert=True,
    )
    return {"status": "ok", **summary}


async def apply_sitemap(
    db: AsyncIOMotorDatabase,
    entries: Iterable[SitemapEntry],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Upsert frontier rows from sitemap entries and freeze what was delisted."""
    now = now or utcnow()

    known: dict[str, tuple[datetime | None, str | None]] = {}
    async for row in db.crawl_frontier.find({}, {"sitemap_lastmod": 1, "tier": 1}):
        known[row["_id"]] = (row.get("sitemap_lastmod"), row.get("tier"))

    ops: list[UpdateOne] = []
    counts = {"seen": 0, "new": 0, "promoted": 0, "skipped": 0}

    for entry in entries:
        counts["seen"] += 1
        if entry.ref_id is None:
            counts["skipped"] += 1
            continue
        if entry.kind == "user" and blacklist.is_blocked(entry.ref_id):
            counts["skipped"] += 1
            continue

        fid = frontier_id(entry.kind, entry.ref_id)
        previous_lastmod, previous_tier = known.get(fid, (None, None))
        is_new = fid not in known

        update: dict[str, Any] = {
            "kind": entry.kind,
            "ref_id": entry.ref_id,
            "url": entry.path,
            "in_sitemap": True,
            # Delisting is then one indexed query, not a 30k-term $nin.
            "sitemap_sync": now,
            "priority": priority(previous_tier or "cold", entry.kind),
        }
        if entry.lastmod is not None:
            update["sitemap_lastmod"] = entry.lastmod

        insert: dict[str, Any] = {
            "first_seen": now,
            "last_crawled": None,
            "consecutive_unchanged": 0,
            "unchanged_since": None,
            "error_count": 0,
        }

        if is_new:
            counts["new"] += 1
            insert["next_due"] = None
            update["tier"] = tier = classify(now=now, sitemap_lastmod=entry.lastmod)
            update["priority"] = priority(tier, entry.kind)
        elif _advanced(previous_lastmod, entry.lastmod):
            counts["promoted"] += 1
            update["next_due"] = now
            update["tier"] = "hot"
            update["priority"] = priority("hot", entry.kind)
            update["gone"] = False

        ops.append(UpdateOne({"_id": fid}, {"$set": update, "$setOnInsert": insert}, upsert=True))
        if len(ops) >= CHUNK:
            await db.crawl_frontier.bulk_write(ops, ordered=False)
            ops = []

    if ops:
        await db.crawl_frontier.bulk_write(ops, ordered=False)

    counts["delisted"] = await _freeze_delisted(db, now)
    log.info(
        "sitemap: %(seen)d urls, %(new)d new, %(promoted)d promoted, %(delisted)d delisted",
        counts,
    )
    return counts


def _advanced(previous: datetime | None, current: datetime | None) -> bool:
    if current is None:
        return False
    if previous is None:
        return True
    if previous.tzinfo is None:
        previous = previous.replace(tzinfo=timezone.utc)
    return current > previous


async def _freeze_delisted(db: AsyncIOMotorDatabase, now: datetime) -> int:
    """Drafted projects and banned or deverified users leave the sitemap."""
    result = await db.crawl_frontier.update_many(
        {"sitemap_sync": {"$ne": now}, "in_sitemap": {"$ne": False}},
        {"$set": {
            "in_sitemap": False,
            "delisted_at": now,
            "tier": "frozen",
            "priority": priority("frozen"),
            "next_due": next_due("frozen", last_crawled=now),
        }},
    )
    return result.modified_count
