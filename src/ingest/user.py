from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase

from ..config import settings
from ..parsers.common import ParseResult, utcnow
from .project import estimate_unpaid

log = logging.getLogger(__name__)

MONOTONIC = ("devlogs", "ships", "ship_stardust")

# Profile figures, as reported by the page itself.
PROFILE_STATS = (
    "followers", "following", "devlogs", "projects", "ships", "votes",
    "streak", "achievements_earned",
)

# Rates and estimates stay out, so a formula change cannot strand history.
COMPUTED_TOTALS = (
    "ship_stardust", "hours", "shipped_hours", "paid_hours", "likes_received",
    "comments_received", "reposts_received", "views_received",
    "best_multiplier", "avg_multiplier",
)

TRACKED = PROFILE_STATS + COMPUTED_TOTALS


class UserAnomalyRejected(Exception):
    """A parsed profile looked structurally fine but its numbers did not."""


def build_user_stats(user: dict[str, Any]) -> dict[str, Any]:
    return {
        "followers": user.get("followers"),
        "following": user.get("following"),
        "devlogs": user.get("devlogs_count"),
        "projects": user.get("projects_count"),
        "ships": user.get("ships_count"),
        "votes": user.get("votes_count"),
        "streak": user.get("streak"),
        "achievements_earned": user.get("achievements_earned"),
        "achievements_total": user.get("achievements_total"),
    }


def check_user_anomalies(
    new_stats: dict[str, Any], previous: dict[str, Any] | None, result: ParseResult
) -> list[str]:
    reasons: list[str] = []
    if previous:
        for key in MONOTONIC:
            old, new = previous.get(key), new_stats.get(key)
            if not isinstance(old, (int, float)) or not isinstance(new, (int, float)):
                continue
            if old > 0 and new < old * (1 - settings.anomaly_drop_threshold):
                reasons.append(f"{key} fell {old} -> {new}")
        for key in PROFILE_STATS:
            if previous.get(key) is not None and new_stats.get(key) is None:
                reasons.append(f"{key} became unreadable")
    if result.missing:
        reasons.append(f"unparsed fields: {sorted(result.missing)}")
    return reasons


async def ingest_user(
    db: AsyncIOMotorDatabase,
    result: ParseResult,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Persist one parsed profile, resolve its id, recompute its totals."""
    now = now or utcnow()
    user = result.data["user"]
    uid = user["_id"]

    existing = await db.users.find_one({"_id": uid})
    previous_stats = (existing or {}).get("stats")

    stats = build_user_stats(user)

    if not user["hidden"]:
        reasons = check_user_anomalies(stats, previous_stats, result)
        if reasons and existing is not None:
            await _log_crawl(db, uid, "anomaly", now, reasons=reasons)
            raise UserAnomalyRejected(f"user {uid}: " + "; ".join(reasons))

    first_ingest = existing is None
    username = user["username"]
    renamed_from = None
    if existing and existing.get("username") and existing["username"] != username:
        renamed_from = existing["username"]
        log.info("user %s renamed %s -> %s", uid, renamed_from, username)

    doc = {
        "_id": uid,
        "username": username,
        "username_lower": username.lower(),
        "avatar_url": user.get("avatar_url"),
        "avatar_kind": user.get("avatar_kind"),
        "slack_id": user.get("slack_id"),
        "bio": user.get("bio"),
        "banner_url": user.get("banner_url"),
        "joined_at": user.get("joined_at"),
        "hidden": user.get("hidden", False),
        "stats": stats,
        "last_crawled": now,
    }
    # Only the projects tab carries this; never clobber a known list with None.
    if user.get("project_ids") is not None:
        doc["project_ids"] = user["project_ids"]
        doc["project_ids_seen_at"] = now

    if first_ingest:
        doc["first_seen"] = now
    if renamed_from:
        doc["previous_usernames"] = sorted(
            set((existing.get("previous_usernames") or []) + [renamed_from])
        )

    changed = _changed_keys(previous_stats, stats, PROFILE_STATS)
    if changed or first_ingest:
        doc["last_changed"] = now

    await db.users.update_one({"_id": uid}, {"$set": doc}, upsert=True)

    linked = await link_user_id(db, uid, username)
    previous_totals = (existing or {}).get("totals")
    # This crawl owns the snapshot; recompute writing its own would duplicate it.
    totals = await recompute_user_totals(db, uid, now=now, snapshot=False)
    # Out of `changed` deliberately: tiering must not recrawl a page for this.
    totals_changed = _changed_keys(previous_totals, totals, COMPUTED_TOTALS)

    wrote_snapshot = False
    if not user["hidden"] and (
        first_ingest or changed or totals_changed or await _heartbeat_due(db, uid, now)
    ):
        await db.user_snapshots.insert_one(_snapshot(uid, {**stats, **totals}, now))
        wrote_snapshot = True

    await _log_crawl(db, uid, "ok", now, changed=sorted(changed), linked=linked)

    return {
        "user_id": uid,
        "username": username,
        "first_ingest": first_ingest,
        "hidden": user.get("hidden", False),
        "renamed_from": renamed_from,
        "changed": sorted(changed),
        "totals_changed": sorted(totals_changed),
        "snapshot": wrote_snapshot,
        "linked": linked,
        "totals": totals,
    }


async def link_user_id(
    db: AsyncIOMotorDatabase, user_id: int, username: str
) -> dict[str, int]:
    """Stamp the numeric id onto rows crawled knowing only a handle."""
    handle = username.lower()

    owner = await db.projects.update_many(
        {"owner_username": username}, {"$set": {"owner_id": user_id}}
    )
    member = await db.projects.update_many(
        {"members": username}, {"$addToSet": {"member_ids": user_id}}
    )
    devlogs = await db.devlogs.update_many(
        {"username_lower": handle}, {"$set": {"user_id": user_id}}
    )
    ships = await db.ships.update_many(
        {"username_lower": handle}, {"$set": {"user_id": user_id}}
    )

    return {
        "projects_owned": owner.modified_count,
        "projects_member": member.modified_count,
        "devlogs": devlogs.modified_count,
        "ships": ships.modified_count,
    }


async def recompute_user_totals(
    db: AsyncIOMotorDatabase,
    user_id: int,
    *,
    now: datetime | None = None,
    snapshot: bool = True,
) -> dict[str, Any]:
    """Roll a user's crawled rows into ranking figures, plus a coverage flag."""
    now = now or utcnow()
    ships = await db.ships.aggregate([
        {"$match": {"user_id": user_id}},
        {"$group": {
            "_id": None,
            "ships": {"$sum": 1},
            "ships_paid": {"$sum": {"$cond": [{"$gt": ["$payout", None]}, 1, 0]}},
            "ship_stardust": {"$sum": {"$ifNull": ["$payout", 0]}},
            "shipped_hours": {"$sum": {"$ifNull": ["$hours_at_ship", 0]}},
            # Only ships that have paid out, so the rate keeps one basis.
            "paid_hours": {"$sum": {
                "$cond": [{"$gt": ["$payout", None]}, {"$ifNull": ["$hours_at_ship", 0]}, 0]
            }},
            "best_multiplier": {"$max": "$multiplier"},
            "avg_multiplier": {"$avg": "$multiplier"},
        }},
    ]).to_list(length=1)

    devlogs = await db.devlogs.aggregate([
        {"$match": {"user_id": user_id}},
        {"$group": {
            "_id": None,
            "devlogs": {"$sum": 1},
            "seconds": {"$sum": {"$ifNull": ["$duration_seconds", 0]}},
            "likes_received": {"$sum": {"$ifNull": ["$likes", 0]}},
            "comments_received": {"$sum": {"$ifNull": ["$comments", 0]}},
            "reposts_received": {"$sum": {"$ifNull": ["$reposts", 0]}},
            "views_received": {"$sum": {"$ifNull": ["$views", 0]}},
        }},
    ]).to_list(length=1)

    s = ships[0] if ships else {}
    d = devlogs[0] if devlogs else {}

    projects_seen = await db.projects.count_documents(
        {"$or": [{"owner_id": user_id}, {"member_ids": user_id}]}
    )

    shipped_hours = round(s.get("shipped_hours") or 0.0, 2)
    paid_hours = round(s.get("paid_hours") or 0.0, 2)

    totals = {
        "ship_stardust": int(s.get("ship_stardust") or 0),
        "hours": round((d.get("seconds") or 0) / 3600.0, 2),
        "shipped_hours": shipped_hours,
        "paid_hours": paid_hours,
        "likes_received": int(d.get("likes_received") or 0),
        "comments_received": int(d.get("comments_received") or 0),
        "reposts_received": int(d.get("reposts_received") or 0),
        "views_received": int(d.get("views_received") or 0),
        "best_multiplier": s.get("best_multiplier"),
        "avg_multiplier": round(s["avg_multiplier"], 3) if s.get("avg_multiplier") else None,
    }
    # Over the hours payouts were actually for, not every hour logged.
    totals["stardust_per_paid_hour"] = (
        round(totals["ship_stardust"] / paid_hours, 2) if paid_hours else None
    )
    totals.update(
        estimate_unpaid(totals["ship_stardust"], totals["hours"], paid_hours)
    )

    user = await db.users.find_one(
        {"_id": user_id}, {"stats": 1, "project_ids": 1, "totals": 1, "hidden": 1}
    )
    reported = (user or {}).get("stats") or {}
    claimed = (user or {}).get("project_ids")
    coverage = {
        "projects_listed": len(claimed) if claimed is not None else None,
        "projects_seen": projects_seen,
        "projects_reported": reported.get("projects"),
        "devlogs_seen": int(d.get("devlogs") or 0),
        "devlogs_reported": reported.get("devlogs"),
        "ships_seen": int(s.get("ships") or 0),
        "ships_reported": reported.get("ships"),
    }
    coverage["projects_missing"] = await missing_project_ids(db, user or {})
    coverage["complete"] = _is_complete(coverage)

    await db.users.update_one(
        {"_id": user_id},
        {
            "$set": {"totals": totals, "coverage": coverage},
            "$unset": {
                "stats.stardust_balance": "",
                "stats.stardust_earned": "",
                "stats.stardust_source": "",
                "stardust_seen_at": "",
            },
        },
    )

    # Their totals move on a project crawl, so history cannot wait for a profile one.
    if snapshot and user and not user.get("hidden"):
        if _changed_keys((user or {}).get("totals"), totals, COMPUTED_TOTALS):
            await db.user_snapshots.insert_one(
                _snapshot(user_id, {**reported, **totals}, now)
            )

    return totals


async def missing_project_ids(
    db: AsyncIOMotorDatabase, user: dict[str, Any]
) -> list[int]:
    """Projects the profile lists that we have never crawled."""
    claimed = user.get("project_ids")
    if not claimed:
        return []
    crawled = await db.projects.distinct("_id", {"_id": {"$in": claimed}})
    return sorted(set(claimed) - set(crawled))


def _is_complete(coverage: dict[str, Any]) -> bool:
    """True when we hold every project the account lists."""
    if coverage.get("projects_listed") is None:
        return False
    if coverage.get("projects_missing"):
        return False
    reported = coverage.get("projects_reported")
    return reported is not None and (coverage.get("projects_seen") or 0) >= reported


async def recompute_all_users(db: AsyncIOMotorDatabase) -> int:
    """Refresh every user's totals. Run after a batch of project crawls."""
    count = 0
    async for user in db.users.find({}, {"_id": 1}):
        await recompute_user_totals(db, user["_id"])
        count += 1
    return count


def _changed_keys(
    previous: dict[str, Any] | None, current: dict[str, Any], keys: tuple[str, ...]
) -> set[str]:
    if previous is None:
        return {k for k in keys if current.get(k) is not None}
    return {k for k in keys if previous.get(k) != current.get(k)}


def _snapshot(uid: int, values: dict[str, Any], ts: datetime) -> dict[str, Any]:
    doc: dict[str, Any] = {"ts": ts, "uid": uid}
    for key in TRACKED:
        value = values.get(key)
        if value is not None:
            doc[key] = value
    return doc


async def _heartbeat_due(db: AsyncIOMotorDatabase, uid: int, now: datetime) -> bool:
    cutoff = now - timedelta(hours=settings.snapshot_heartbeat_hours)
    latest = await db.user_snapshots.find_one(
        {"uid": uid, "ts": {"$gte": cutoff}}, projection={"_id": 1}
    )
    return latest is None


async def _log_crawl(
    db: AsyncIOMotorDatabase, uid: int, status: str, ts: datetime, **extra: Any
) -> None:
    try:
        await db.crawl_log.insert_one(
            {"ts": ts, "kind": "user", "ref_id": uid, "status": status, **extra}
        )
    except Exception:
        log.exception("crawl_log write failed")
