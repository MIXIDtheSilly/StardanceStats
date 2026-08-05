from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo import ReplaceOne, UpdateOne

from ..config import settings
from ..parsers.common import ParseResult, utcnow

log = logging.getLogger(__name__)

# Counters that only go up in reality. A large fall means a parse failure;
# a small one is ordinary moderation, such as a deleted devlog.
MONOTONIC = ("devlogs", "total_hours", "stardust_total", "ships")

# Fields whose change is worth a new snapshot.
TRACKED = (
    "devlogs", "total_hours", "shipped_hours", "paid_hours", "followers",
    "likes", "comments", "reposts", "views", "ships", "stardust_total",
    "latest_multiplier",
)


class AnomalyRejected(Exception):
    """A parsed page looked structurally fine but its numbers did not."""


def build_stats(
    project: dict[str, Any], devlogs: list[dict[str, Any]], ships: list[dict[str, Any]]
) -> dict[str, Any]:
    """Roll cards up into the project's stat block."""
    summed_hours = sum(d.get("duration_seconds") or 0 for d in devlogs) / 3600.0
    multipliers = [s["multiplier"] for s in ships if s.get("multiplier") is not None]
    payouts = [s["payout"] for s in ships if s.get("payout") is not None]
    latest = max(ships, key=lambda s: s.get("shipped_at") or _EPOCH, default=None)

    # The page's own figure counts devlogs we cannot see, such as deleted ones.
    total_hours = project.get("total_hours")
    if total_hours is None:
        total_hours = round(summed_hours, 2)

    stardust_total = sum(payouts) if payouts else 0

    # A ship pays out only once its review window closes, so shipped and paid
    # hours are different sets, kept apart to avoid diluting the rate.
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
        "latest_multiplier": latest.get("multiplier") if latest else None,
        "avg_multiplier": round(sum(multipliers) / len(multipliers), 3) if multipliers else None,
    }
    # Payouts over the hours those payouts were for; dividing by every hour
    # logged instead would mix paid and unpaid hours. Not the multiplier
    # either, since payouts run on capped hours (see payout_hours()).
    stats["stardust_per_paid_hour"] = (
        round(stardust_total / paid_hours, 2) if paid_hours else None
    )
    stats.update(estimate_unpaid(stardust_total, summed_hours, paid_hours))
    return stats


def _hours_of(ships: list[dict[str, Any]]) -> float | None:
    hours = [s["hours_at_ship"] for s in ships if s.get("hours_at_ship") is not None]
    return round(sum(hours), 2) if hours else None


def estimate_unpaid(
    stardust: int, logged_hours: float, paid_hours: float | None
) -> dict[str, Any]:
    """Value every hour that has not been paid for yet.

    Covers hours in a ship under review and hours not yet shipped at all,
    priced at the realised stardust-per-paid-hour rate. Needs a paid ship to
    derive that rate from, so an account with none gets no estimate.
    """
    unpaid = round(max(logged_hours - (paid_hours or 0.0), 0.0), 2)
    if not paid_hours or not stardust:
        return {
            "unpaid_hours": unpaid,
            "estimated_pending_stardust": None,
            "estimated_total_stardust": None,
        }

    pending = round(unpaid * (stardust / paid_hours))
    return {
        "unpaid_hours": unpaid,
        "estimated_pending_stardust": pending,
        "estimated_total_stardust": stardust + pending,
    }


def payout_hours(ship: dict[str, Any]) -> float | None:
    """The hours a payout was actually computed on.

    Upstream pays on `hours_at_payout` (devlogs capped at 10h each), which is
    never rendered, so this recovers it from payout / multiplier. Accurate to
    roughly 0.1%, not exact, since both inputs are display-rounded.
    """
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


async def ingest_project(
    db: AsyncIOMotorDatabase,
    result: ParseResult,
    *,
    now: datetime | None = None,
    force_snapshot: bool = False,
) -> dict[str, Any]:
    """Persist one parsed project page. Returns a summary of what changed."""
    now = now or utcnow()
    project = result.data["project"]
    devlogs = result.data["devlogs"]
    ships = result.data["ships"]
    pid = project["_id"]

    for ship in ships:
        ship["payout_hours"] = payout_hours(ship)

    stats = build_stats(project, devlogs, ships)
    existing = await db.projects.find_one({"_id": pid})
    previous_stats = (existing or {}).get("stats")

    reasons = check_anomalies(stats, previous_stats, result)
    if reasons and existing is not None:
        await _log_crawl(db, pid, "anomaly", now, reasons=reasons)
        raise AnomalyRejected(f"project {pid}: " + "; ".join(reasons))

    first_ingest = existing is None

    doc = {
        "_id": pid,
        "title": project.get("title"),
        "description": project.get("description"),
        "owner_username": project.get("owner_username"),
        "members": project.get("members", []),
        "is_hardware": project.get("is_hardware", False),
        "is_super_star": project.get("is_super_star", False),
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

    # The award details only render while the event card is on the timeline;
    # keep the ones we have rather than blanking them when it ages out.
    if project.get("super_star_at"):
        doc["super_star_at"] = project["super_star_at"]
        doc["super_star_by"] = project.get("super_star_by")
        doc["super_star_note"] = project.get("super_star_note")

    if (existing or {}).get("is_super_star") and not doc["is_super_star"]:
        # Presence-only markup: an admin un-marking and a renamed badge class
        # look identical here, so say so out loud rather than flipping quietly.
        result.warn("Super Star badge disappeared; was set on a previous crawl")

    changed = _changed_keys(previous_stats, stats)
    if changed or first_ingest:
        doc["last_changed"] = now

    await db.projects.update_one({"_id": pid}, {"$set": doc}, upsert=True)

    await _write_devlogs(db, devlogs, now)
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
        "changed": sorted(changed),
        "snapshot": wrote_snapshot,
        "backfilled": backfilled,
        "linked_users": sorted(linked_users),
        "warnings": result.warnings,
    }


async def _link_known_users(db: AsyncIOMotorDatabase, project: dict[str, Any]) -> set[int]:
    """Stamp user ids onto this project's rows for handles we already know.

    Without this a project crawled after its owner keeps handle-only rows.
    Returns the ids touched so their totals can be recomputed.
    """
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
) -> None:
    if not devlogs:
        return
    ops = []
    for d in devlogs:
        doc = dict(d)
        doc["username_lower"] = (doc.get("username") or "").lower() or None
        doc["last_crawled"] = now
        ops.append(UpdateOne({"_id": doc["_id"]}, {"$set": doc}, upsert=True))
    await db.devlogs.bulk_write(ops, ordered=False)


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
    """Reconstruct daily history from timestamps the page already gave us.

    Engagement exposes only current totals, so those series cannot be
    reconstructed and start at first crawl.
    """
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
