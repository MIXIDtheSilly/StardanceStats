from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo import ReplaceOne, UpdateOne

from ..config import settings
from ..parsers.common import ParseResult, utcnow
from .mission import assign_payout_paths, load_missions, mission_payout

log = logging.getLogger(__name__)

# A large fall means a parse failure; a small one is ordinary moderation.
MONOTONIC = ("devlogs", "total_hours", "stardust_total", "ships")

# Re-summed from the rendered cards, so a short render deflates them.
CARD_SUMMED = ("views", "likes", "comments", "reposts")

# Fields whose change is worth a new snapshot.
TRACKED = (
    "devlogs", "total_hours", "shipped_hours", "paid_hours", "followers",
    "likes", "comments", "reposts", "views", "ships", "stardust_total",
    "latest_multiplier",
)

# The same, per devlog card. Duration moves when an author edits a post.
DEVLOG_TRACKED = ("likes", "comments", "reposts", "views", "duration_seconds")


class AnomalyRejected(Exception):
    """A parsed page looked structurally fine but its numbers did not."""


def build_stats(
    project: dict[str, Any],
    devlogs: list[dict[str, Any]],
    ships: list[dict[str, Any]],
    missions: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Roll cards up into the project's stat block."""
    missions = missions or {}
    assign_payout_paths(ships, missions)

    summed_hours = sum(d.get("duration_seconds") or 0 for d in devlogs) / 3600.0
    multipliers = [s["multiplier"] for s in ships if s.get("multiplier") is not None]
    payouts = [s["payout"] for s in ships if s.get("payout") is not None]
    latest = max(ships, key=lambda s: s.get("shipped_at") or _EPOCH, default=None)

    # The page's own figure counts devlogs we cannot see, such as deleted ones.
    total_hours = project.get("total_hours")
    if total_hours is None:
        total_hours = round(summed_hours, 2)

    # A mission's own award is never rendered on a ship card, only inferred.
    mission = mission_payout(ships, missions)
    voted_stardust = sum(payouts) if payouts else 0
    stardust_total = voted_stardust + mission["stardust"]

    # A ship pays out only once its review closes, so these differ.
    shipped_hours = _hours_of(ships)
    paid_hours = _hours_of([s for s in ships if s.get("payout") is not None])

    stats: dict[str, Any] = {
        "devlogs": project.get("devlogs_count") or len(devlogs),
        "total_hours": float(total_hours),
        "summed_hours": round(summed_hours, 2),
        "shipped_hours": shipped_hours,
        "paid_hours": paid_hours,
        "followers": project.get("followers"),
        "likes": _sum(devlogs, "likes"),
        "comments": _sum(devlogs, "comments"),
        "reposts": _sum(devlogs, "reposts"),
        "views": _sum(devlogs, "views"),
        "ships": len(ships),
        "stardust_total": stardust_total,
        "voted_stardust": voted_stardust,
        "mission_stardust": mission["stardust"],
        "latest_multiplier": latest.get("multiplier") if latest else None,
        "avg_multiplier": round(sum(multipliers) / len(multipliers), 3) if multipliers else None,
    }
    # Rated payouts over the hours they were for; a mission award has no rate.
    stats["stardust_per_paid_hour"] = (
        round(voted_stardust / paid_hours, 2) if paid_hours else None
    )

    stats.update(estimate_unpaid(
        voted_stardust, summed_hours, paid_hours,
        mission_hours=mission["hours"],
        mission_stardust=mission["stardust"],
        mission_pending_stardust=mission["pending_stardust"],
    ))
    stats["fixed_payout_hours"] = mission["fixed_payout_hours"]
    stats["flat_rate_hours"] = mission["flat_rate_hours"]
    return stats


def _hours_of(ships: list[dict[str, Any]]) -> float | None:
    hours = [s["hours_at_ship"] for s in ships if s.get("hours_at_ship") is not None]
    return round(sum(hours), 2) if hours else None


def estimate_unpaid(
    stardust: int,
    logged_hours: float,
    paid_hours: float | None,
    *,
    mission_hours: float = 0.0,
    mission_stardust: int = 0,
    mission_pending_stardust: int = 0,
) -> dict[str, Any]:
    """Value every hour that has not been paid for yet. `stardust` is rated only."""
    unpaid = round(max(logged_hours - (paid_hours or 0.0), 0.0), 2)
    # A mission pays its own hours at its own terms, never at the voting rate.
    ratable = round(max(unpaid - mission_hours, 0.0), 2)

    out: dict[str, Any] = {
        "unpaid_hours": unpaid,
        "ratable_unpaid_hours": ratable,
        "mission_pending_hours": round(mission_hours, 2),
        # Awaiting approval only: an approved award is already in the total.
        "mission_pending_stardust": mission_pending_stardust,
        "estimated_pending_stardust": None,
        "estimated_total_stardust": None,
    }

    if paid_hours and stardust:
        out["estimated_pending_stardust"] = (
            round(ratable * (stardust / paid_hours)) + mission_pending_stardust
        )
    elif mission_pending_stardust:
        out["estimated_pending_stardust"] = mission_pending_stardust

    banked = stardust + mission_stardust
    if out["estimated_pending_stardust"] is not None:
        out["estimated_total_stardust"] = banked + out["estimated_pending_stardust"]
    elif mission_stardust:
        out["estimated_total_stardust"] = banked
    return out


def payout_hours(ship: dict[str, Any]) -> float | None:
    """The hours a payout was actually computed on."""
    payout = ship.get("payout")
    multiplier = ship.get("multiplier")
    if not payout or not multiplier:
        return None
    return round(payout / multiplier, 2)


_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _sum(rows: Iterable[dict[str, Any]], key: str) -> int:
    return sum(r.get(key) or 0 for r in rows)


def check_anomalies(
    new_stats: dict[str, Any], previous: dict[str, Any] | None, result: ParseResult
) -> list[str]:
    """Return reasons this snapshot should be rejected. Empty means accept."""
    reasons: list[str] = []

    if previous:
        for key in MONOTONIC:
            old, new = previous.get(key), new_stats.get(key)
            if not isinstance(old, (int, float)) or not isinstance(new, (int, float)):
                continue
            if old > 0 and new < old * (1 - settings.anomaly_drop_threshold):
                reasons.append(f"{key} fell {old} -> {new}")

        # A readable field going absent is the signature of a renamed selector.
        for key in TRACKED:
            if previous.get(key) is not None and new_stats.get(key) is None:
                reasons.append(f"{key} became unreadable")

    if result.missing:
        reasons.append(f"unparsed fields: {sorted(result.missing)}")

    return reasons


def card_sum_drops(
    new_stats: dict[str, Any], previous: dict[str, Any] | None
) -> list[dict[str, Any]]:
    """Card-summed totals that fell. Moderation does this, but so does a short render."""
    if not previous:
        return []

    drops = []
    for key in CARD_SUMMED:
        old, new = previous.get(key), new_stats.get(key)
        if not isinstance(old, (int, float)) or not isinstance(new, (int, float)):
            continue
        if new < old:
            drops.append({"metric": key, "from": old, "to": new, "by": old - new})
    return drops


async def ingest_project(
    db: AsyncIOMotorDatabase,
    result: ParseResult,
    *,
    now: datetime | None = None,
    force_snapshot: bool = False,
    missions: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Persist one parsed project page. Returns a summary of what changed."""
    now = now or utcnow()
    project = result.data["project"]
    devlogs = result.data["devlogs"]
    ships = result.data["ships"]
    pid = project["_id"]

    for ship in ships:
        ship["payout_hours"] = payout_hours(ship)

    if missions is None:
        missions = await load_missions(db)
    _check_known_missions(project, ships, missions, result)

    stats = build_stats(project, devlogs, ships, missions)
    existing = await db.projects.find_one({"_id": pid})
    previous_stats = (existing or {}).get("stats")

    reasons = check_anomalies(stats, previous_stats, result)
    if reasons and existing is not None:
        await _log_crawl(db, pid, "anomaly", now, reasons=reasons)
        raise AnomalyRejected(f"project {pid}: " + "; ".join(reasons))

    # Logged rather than rejected, so a dip in a global total can be traced back here.
    drops = card_sum_drops(stats, previous_stats)
    if drops:
        claimed = project.get("devlogs_count")
        parsed = project.get("parsed_devlogs")
        await _log_crawl(
            db, pid, "sum_drop", now,
            drops=drops, devlogs_claimed=claimed, devlogs_parsed=parsed,
        )
        log.warning(
            "project %s card sums fell (%s) with %s of %s devlog cards rendered",
            pid,
            ", ".join(f"{d['metric']} {d['from']}->{d['to']}" for d in drops),
            parsed, claimed,
        )

    first_ingest = existing is None

    doc = {
        "_id": pid,
        "title": project.get("title"),
        "description": project.get("description"),
        "owner_username": project.get("owner_username"),
        "members": project.get("members", []),
        "is_hardware": project.get("is_hardware", False),
        "is_super_star": project.get("is_super_star", False),
        "mission": project.get("mission"),
        "repo_url": project.get("repo_url"),
        "demo_url": project.get("demo_url"),
        "banner_url": project.get("banner_url"),
        "owner_avatar_url": project.get("owner_avatar_url"),
        "stats": stats,
        "last_crawled": now,
    }
    if first_ingest:
        doc["first_seen"] = now
        doc["created_at_estimate"] = min(
            (d["posted_at"] for d in devlogs if d.get("posted_at")), default=None
        )

    # The card ages off the timeline, so keep what we already have.
    if project.get("super_star_at"):
        doc["super_star_at"] = project["super_star_at"]
        doc["super_star_by"] = project.get("super_star_by")
        doc["super_star_note"] = project.get("super_star_note")

    if (existing or {}).get("owner_username") and not doc["owner_username"]:
        # Legitimate, but it unlinks the project from that user's totals.
        result.warn(
            f"owner {existing['owner_username']!r} no longer rendered on the byline"
        )

    if (existing or {}).get("is_super_star") and not doc["is_super_star"]:
        # Presence-only markup: un-marked and renamed look identical here.
        result.warn("Super Star badge disappeared; was set on a previous crawl")

    was = ((existing or {}).get("mission") or {}).get("slug")
    if was and not (doc["mission"] or {}).get("slug"):
        # Upstream never unsets it after a ship, so this is a detach or a break.
        result.warn(f"mission {was!r} no longer rendered on the panel")

    changed = _changed_keys(previous_stats, stats)
    if changed or first_ingest:
        doc["last_changed"] = now

    await db.projects.update_one({"_id": pid}, {"$set": doc}, upsert=True)

    written_devlogs = await _write_devlogs(db, devlogs, now)
    await _write_ships(db, ships, now)
    linked_users = await _link_known_users(db, doc)

    wrote_snapshot = False
    if force_snapshot or first_ingest or changed or await _heartbeat_due(db, pid, now):
        await db.project_snapshots.insert_one(_snapshot(pid, stats, now))
        wrote_snapshot = True

    backfilled = 0
    if first_ingest:
        backfilled = await backfill_project_history(db, pid, devlogs, ships, stats, now)

    await _log_crawl(
        db, pid, "ok", now,
        changed=sorted(changed), warnings=result.warnings, backfilled=backfilled,
    )

    return {
        "project_id": pid,
        "first_ingest": first_ingest,
        "devlogs": len(devlogs),
        "ships": len(ships),
        "threads_flagged": written_devlogs["flagged"],
        "devlog_snapshots": written_devlogs["snapshots"],
        "changed": sorted(changed),
        "snapshot": wrote_snapshot,
        "backfilled": backfilled,
        "linked_users": sorted(linked_users),
        "warnings": result.warnings,
    }


def _check_known_missions(
    project: dict[str, Any],
    ships: list[dict[str, Any]],
    missions: dict[str, dict[str, Any]],
    result: ParseResult,
) -> None:
    """An unknown mission means its ships get priced as if they were rated."""
    if not missions:
        return
    named = {s.get("mission_slug") for s in ships}
    named.add((project.get("mission") or {}).get("slug"))
    unknown = sorted(s for s in named if s and s not in missions)
    if unknown:
        result.warn(f"mission(s) {unknown} not crawled yet; payout terms unknown")


async def _link_known_users(db: AsyncIOMotorDatabase, project: dict[str, Any]) -> set[int]:
    """Stamp user ids onto this project's rows for handles we already know."""
    pid = project["_id"]
    owner = project.get("owner_username")
    names = list(project.get("members") or [])
    if owner and owner not in names:
        names.append(owner)
    if not names:
        return set()

    handles = [n.lower() for n in names]
    touched: set[int] = set()

    async for user in db.users.find(
        {"username_lower": {"$in": handles}}, {"_id": 1, "username_lower": 1}
    ):
        uid, handle = user["_id"], user["username_lower"]
        await db.devlogs.update_many(
            {"project_id": pid, "username_lower": handle}, {"$set": {"user_id": uid}}
        )
        await db.ships.update_many(
            {"project_id": pid, "username_lower": handle}, {"$set": {"user_id": uid}}
        )

        update: dict[str, Any] = {"$addToSet": {"member_ids": uid}}
        if owner and owner.lower() == handle:
            update["$set"] = {"owner_id": uid}
        await db.projects.update_one({"_id": pid}, update)
        touched.add(uid)

    return touched


def _changed_keys(previous: dict[str, Any] | None, current: dict[str, Any]) -> set[str]:
    if previous is None:
        return set(TRACKED)
    return {k for k in TRACKED if previous.get(k) != current.get(k)}


def _snapshot(pid: int, stats: dict[str, Any], ts: datetime) -> dict[str, Any]:
    doc: dict[str, Any] = {"ts": ts, "pid": pid}
    for key in TRACKED:
        value = stats.get(key)
        if value is not None:
            doc[key] = value
    return doc


async def _heartbeat_due(db: AsyncIOMotorDatabase, pid: int, now: datetime) -> bool:
    cutoff = now - timedelta(hours=settings.snapshot_heartbeat_hours)
    latest = await db.project_snapshots.find_one(
        {"pid": pid, "ts": {"$gte": cutoff}}, projection={"_id": 1}
    )
    return latest is None


async def _write_devlogs(
    db: AsyncIOMotorDatabase, devlogs: list[dict[str, Any]], now: datetime
) -> dict[str, int]:
    """Write the cards, flagging moved threads and snapshotting moved numbers."""
    if not devlogs:
        return {"flagged": 0, "snapshots": 0}

    ids = [d["_id"] for d in devlogs]
    projection = {"comments_crawled_count": 1, "snapshot_at": 1}
    projection.update(dict.fromkeys(DEVLOG_TRACKED, 1))
    previous = {
        row["_id"]: row
        async for row in db.devlogs.find({"_id": {"$in": ids}}, projection)
    }

    # One cutoff for the batch, so the heartbeat costs no query per card.
    cutoff = now - timedelta(hours=settings.snapshot_heartbeat_hours)
    ops = []
    points = []
    flagged = 0
    for d in devlogs:
        doc = dict(d)
        doc["username_lower"] = (doc.get("username") or "").lower() or None
        doc["last_crawled"] = now
        was = previous.get(doc["_id"])
        # Worth a fetch when there is a thread to read, or there was one whose
        # rows now need retiring.
        count = doc.get("comments") or 0
        watermark = (was or {}).get("comments_crawled_count")
        stale = count != watermark and bool(count or watermark)
        doc["comments_stale"] = stale
        flagged += stale

        if _devlog_snapshot_due(was, doc, cutoff):
            doc["snapshot_at"] = now
            points.append(_devlog_snapshot(doc["_id"], doc, now))

        ops.append(UpdateOne({"_id": doc["_id"]}, {"$set": doc}, upsert=True))

    await db.devlogs.bulk_write(ops, ordered=False)
    if points:
        await db.devlog_snapshots.insert_many(points, ordered=False)
    return {"flagged": flagged, "snapshots": len(points)}


def _devlog_snapshot_due(
    previous: dict[str, Any] | None, doc: dict[str, Any], cutoff: datetime
) -> bool:
    """First sighting, a tracked number moving, or the heartbeat coming due."""
    if previous is None:
        return True
    if any(doc.get(field) != previous.get(field) for field in DEVLOG_TRACKED):
        return True
    stamped = previous.get("snapshot_at")
    return stamped is None or stamped <= cutoff


def _devlog_snapshot(did: int, doc: dict[str, Any], ts: datetime) -> dict[str, Any]:
    point: dict[str, Any] = {"ts": ts, "did": did}
    for field in DEVLOG_TRACKED:
        value = doc.get(field)
        if value is not None:
            point[field] = value
    return point


async def _write_ships(
    db: AsyncIOMotorDatabase, ships: list[dict[str, Any]], now: datetime
) -> None:
    if not ships:
        return
    ops = []
    for s in ships:
        doc = dict(s)
        doc["username_lower"] = (doc.get("username") or "").lower() or None
        doc["last_crawled"] = now
        ops.append(ReplaceOne({"_id": doc["_id"]}, doc, upsert=True))
    await db.ships.bulk_write(ops, ordered=False)


async def _log_crawl(
    db: AsyncIOMotorDatabase, pid: int, status: str, ts: datetime, **extra: Any
) -> None:
    try:
        await db.crawl_log.insert_one(
            {"ts": ts, "kind": "project", "ref_id": pid, "status": status, **extra}
        )
    except Exception:  # logging must never break ingest
        log.exception("crawl_log write failed")


async def backfill_project_history(
    db: AsyncIOMotorDatabase,
    pid: int,
    devlogs: list[dict[str, Any]],
    ships: list[dict[str, Any]],
    current: dict[str, Any],
    now: datetime,
) -> int:
    """Reconstruct daily history from timestamps the page already gave us."""
    dated = sorted(
        ((d["posted_at"], d.get("duration_seconds") or 0) for d in devlogs if d.get("posted_at")),
        key=lambda x: x[0],
    )
    ship_points = sorted(
        ((s["shipped_at"], s.get("payout") or 0) for s in ships if s.get("shipped_at")),
        key=lambda x: x[0],
    )
    if not dated and not ship_points:
        return 0

    start = min([d[0] for d in dated] + [s[0] for s in ship_points])
    days = _day_range(start, now)

    docs: list[dict[str, Any]] = []
    di = si = 0
    devlog_count = 0
    seconds = 0
    ship_count = 0
    stardust = 0

    for day in days:
        end = day + timedelta(days=1)
        while di < len(dated) and dated[di][0] < end:
            devlog_count += 1
            seconds += dated[di][1]
            di += 1
        while si < len(ship_points) and ship_points[si][0] < end:
            ship_count += 1
            stardust += ship_points[si][1]
            si += 1

        docs.append({
            "ts": day,
            "pid": pid,
            "synthetic": True,
            "devlogs": devlog_count,
            "total_hours": round(seconds / 3600.0, 2),
            "ships": ship_count,
            "stardust_total": stardust,
        })

    if not docs:
        return 0

    # Today is covered by the observed snapshot written this run.
    docs = [d for d in docs if d["ts"].date() < now.date()]
    if not docs:
        return 0

    await db.project_snapshots.insert_many(docs, ordered=False)
    log.info("backfilled %d synthetic days for project %s", len(docs), pid)
    return len(docs)


def _day_range(start: datetime, end: datetime) -> list[datetime]:
    day = start.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    last = end.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    out = []
    while day <= last:
        out.append(day)
        day += timedelta(days=1)
    return out
