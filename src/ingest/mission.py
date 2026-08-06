from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase

from ..parsers.common import ParseResult, utcnow
from ..parsers.mission import PAYOUT_FLAT, PAYOUT_STATIC

log = logging.getLogger(__name__)

# What a project's stats need to know about the mission its ships went to.
ECONOMICS = ("payout_path", "fixed_stardust", "stardust_per_hour", "rated")


async def load_missions(db: AsyncIOMotorDatabase) -> dict[str, dict[str, Any]]:
    """Every mission's payout terms, keyed by slug. Under a dozen rows."""
    projection = {key: 1 for key in ECONOMICS}
    return {row["_id"]: row async for row in db.missions.find({}, projection)}


async def ingest_mission(
    db: AsyncIOMotorDatabase, result: ParseResult, *, now: datetime | None = None
) -> dict[str, Any]:
    """Persist one parsed mission page. Returns a summary of what changed."""
    now = now or utcnow()
    mission = dict(result.data["mission"])
    slug = mission["_id"]

    existing = await db.missions.find_one({"_id": slug})
    changed = _changed_economics(existing, mission)

    mission["last_crawled"] = now
    if existing is None:
        mission["first_seen"] = now
    if changed:
        mission["terms_changed_at"] = now

    await db.missions.update_one({"_id": slug}, {"$set": mission}, upsert=True)

    requeued = 0
    if changed and existing is not None:
        # Every attached project priced its unpaid hours off the old terms.
        result.warn(f"payout terms changed: {', '.join(changed)}")
        requeued = await requeue_mission_projects(db, slug, now=now)

    await _log_crawl(db, slug, "ok", now, changed=changed, requeued=requeued)

    return {
        "slug": slug,
        "first_ingest": existing is None,
        "payout_path": mission.get("payout_path"),
        "changed": changed,
        "requeued_projects": requeued,
        "warnings": result.warnings,
    }


def _changed_economics(
    existing: dict[str, Any] | None, mission: dict[str, Any]
) -> list[str]:
    if existing is None:
        return []
    return [key for key in ECONOMICS if existing.get(key) != mission.get(key)]


async def requeue_mission_projects(
    db: AsyncIOMotorDatabase, slug: str, *, now: datetime | None = None
) -> int:
    """Make every project touching a mission due, so its estimate is restated."""
    now = now or utcnow()
    attached = await db.projects.distinct("_id", {"mission.slug": slug})
    shipped = await db.ships.distinct("project_id", {"mission_slug": slug})
    ids = {pid for pid in [*attached, *shipped] if pid is not None}
    if not ids:
        return 0

    result = await db.crawl_frontier.update_many(
        {"_id": {"$in": [f"project:{pid}" for pid in ids]}},
        {"$set": {"next_due": now, "tier": "hot", "priority": 0}},
    )
    log.info("mission %s terms changed; requeued %d project(s)", slug, result.modified_count)
    return result.modified_count


def assign_payout_paths(
    ships: list[dict[str, Any]], missions: dict[str, dict[str, Any]]
) -> None:
    """Stamp each ship with the path its mission pays it through."""
    claimed: set[str] = set()

    for ship in sorted(ships, key=_ship_order):
        slug = ship.get("mission_slug")
        terms = missions.get(slug) if slug else None
        path = "voting"

        # An observed payout settles it; only voting ever writes one.
        if terms is not None and ship.get("payout") is None:
            declared = terms.get("payout_path")
            if declared == PAYOUT_FLAT:
                path = PAYOUT_FLAT
            elif declared == PAYOUT_STATIC and slug not in claimed:
                # Upstream rates every ship after the first (Projects::ShipsController).
                path = PAYOUT_STATIC

        if slug:
            claimed.add(slug)
        ship["payout_path"] = path


def _ship_order(ship: dict[str, Any]) -> tuple[int, float]:
    number = ship.get("ship_number")
    stamp = ship.get("shipped_at")
    return (number if number is not None else 1 << 30, stamp.timestamp() if stamp else 0.0)


def mission_pending(
    ships: list[dict[str, Any]], missions: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """Hours that a mission pays for directly, and what they are worth."""
    fixed_hours = flat_hours = 0.0
    stardust = 0.0

    for ship in ships:
        if ship.get("payout") is not None:
            continue
        hours = ship.get("hours_at_ship") or 0.0
        terms = missions.get(ship.get("mission_slug")) or {}

        if ship.get("payout_path") == PAYOUT_STATIC:
            fixed_hours += hours
            stardust += terms.get("fixed_stardust") or 0
        elif ship.get("payout_path") == PAYOUT_FLAT:
            flat_hours += hours
            stardust += hours * (terms.get("stardust_per_hour") or 0)

    return {
        "fixed_payout_hours": round(fixed_hours, 2),
        "flat_rate_hours": round(flat_hours, 2),
        "hours": round(fixed_hours + flat_hours, 2),
        "stardust": round(stardust),
    }


async def _log_crawl(
    db: AsyncIOMotorDatabase, slug: str, status: str, ts: datetime, **extra: Any
) -> None:
    try:
        await db.crawl_log.insert_one(
            {"ts": ts, "kind": "mission", "ref_id": slug, "status": status, **extra}
        )
    except Exception:  # logging must never break ingest
        log.exception("crawl_log write failed")
