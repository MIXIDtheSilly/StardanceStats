from __future__ import annotations

import logging
from collections import Counter
from typing import Any, Iterator, Sequence

from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo.errors import OperationFailure

from .config import settings

log = logging.getLogger(__name__)

STATE_ID = "blacklist"

# Id lists come from crawled rows, so a prolific account runs to thousands.
CHUNK = 1000

_names: set[str] | None = None


def user_ids() -> frozenset[int]:
    return settings.blacklist_user_ids


def is_blocked(user_id: Any) -> bool:
    """True for a blacklisted numeric id. Handles go through is_blocked_handle."""
    ids = user_ids()
    if not ids or user_id is None:
        return False
    try:
        return int(user_id) in ids
    except (TypeError, ValueError):
        return False


async def blocked_names(db: AsyncIOMotorDatabase) -> set[str]:
    """Usernames of the blacklisted ids, as stored. Cached for the process."""
    global _names
    if _names is None:
        _names = await _load_names(db)
    return _names


async def is_blocked_handle(db: AsyncIOMotorDatabase, handle: str | None) -> bool:
    if not handle:
        return False
    return handle.lower() in {name.lower() for name in await blocked_names(db)}


def forget() -> None:
    """Drop the name cache, so a changed blacklist is picked up."""
    global _names
    _names = None


async def _load_names(db: AsyncIOMotorDatabase) -> set[str]:
    ids = sorted(user_ids())
    if not ids:
        return set()

    # Purging removes the profile, so the handles have to outlive it.
    state = await db.crawl_state.find_one({"_id": STATE_ID}) or {}
    names = {name for name in (state.get("usernames") or []) if name}

    async for user in db.users.find(
        {"_id": {"$in": ids}}, {"username": 1, "previous_usernames": 1}
    ):
        for name in [user.get("username"), *(user.get("previous_usernames") or [])]:
            if name:
                names.add(name)
    return names


async def remember(db: AsyncIOMotorDatabase, name: str | None) -> None:
    """Store a handle, so blocking it survives the profile being deleted."""
    if not name:
        return
    await db.crawl_state.update_one(
        {"_id": STATE_ID}, {"$addToSet": {"usernames": name}}, upsert=True
    )
    forget()


async def retire(db: AsyncIOMotorDatabase, kind: str, ref_id: int | str) -> None:
    """Stop a frontier row being handed out again, without deleting it."""
    await db.crawl_frontier.update_many(
        {"kind": kind, "ref_id": ref_id},
        {"$set": {"gone": True, "last_status": "blacklisted"}},
    )


async def purge(db: AsyncIOMotorDatabase) -> dict[str, Any]:
    """Delete every row belonging to a blacklisted id. Safe to call repeatedly."""
    ids = sorted(user_ids())
    if not ids:
        return {"users": [], "deleted": {}, "affected_users": []}

    names = sorted(await _load_names(db))
    forget()
    if names:
        await db.crawl_state.update_one(
            {"_id": STATE_ID},
            {"$addToSet": {"usernames": {"$each": names}}},
            upsert=True,
        )

    mine: dict[str, Any] = {
        "$or": [
            {"user_id": {"$in": ids}},
            {"username_lower": {"$in": [name.lower() for name in names]}},
        ]
    }

    project_ids = await db.projects.distinct(
        "_id", {"$or": [{"owner_id": {"$in": ids}}, {"owner_username": {"$in": names}}]}
    )
    deleted, affected = await purge_projects(db, project_ids)

    # Their own rows on other people's projects, which the sweep above missed.
    counts, authors = await _purge_devlogs(db, await db.devlogs.distinct("_id", mine))
    deleted += counts
    affected |= authors

    deleted["comments"] += (await db.comments.delete_many(mine)).deleted_count
    deleted["ships"] += (await db.ships.delete_many(mine)).deleted_count
    deleted["user_snapshots"] += await _delete(db.user_snapshots, {"uid": {"$in": ids}})
    deleted["users"] += (await db.users.delete_many({"_id": {"$in": ids}})).deleted_count
    deleted["frontier"] += (
        await db.crawl_frontier.delete_many({"kind": "user", "ref_id": {"$in": ids}})
    ).deleted_count

    # Someone else's project keeps its row; only the membership goes.
    memberships = await db.projects.update_many(
        {"$or": [{"member_ids": {"$in": ids}}, {"members": {"$in": names}}]},
        {"$pull": {"member_ids": {"$in": ids}, "members": {"$in": names}}},
    )
    deleted["memberships"] = memberships.modified_count

    affected -= set(ids)
    return {
        "users": ids,
        "usernames": names,
        "projects": sorted(project_ids),
        "deleted": {key: n for key, n in sorted(deleted.items()) if n},
        "affected_users": sorted(affected),
    }


async def purge_projects(
    db: AsyncIOMotorDatabase, project_ids: Sequence[int]
) -> tuple[Counter[str], set[int]]:
    """Delete projects and everything hanging off them, with the users to re-total."""
    deleted: Counter[str] = Counter()
    affected: set[int] = set()

    pids = sorted({pid for pid in project_ids})
    if not pids:
        return deleted, affected

    counts, authors = await _purge_devlogs(
        db, await db.devlogs.distinct("_id", {"project_id": {"$in": pids}})
    )
    deleted += counts
    affected |= authors

    for chunk in _chunks(pids):
        query = {"project_id": {"$in": chunk}}
        affected.update(await db.comments.distinct("user_id", query))
        deleted["comments"] += (await db.comments.delete_many(query)).deleted_count
        affected.update(await db.ships.distinct("user_id", query))
        deleted["ships"] += (await db.ships.delete_many(query)).deleted_count
        deleted["project_snapshots"] += await _delete(
            db.project_snapshots, {"pid": {"$in": chunk}}
        )
        deleted["projects"] += (
            await db.projects.delete_many({"_id": {"$in": chunk}})
        ).deleted_count
        # Kept and retired rather than deleted: the sitemap would only list it again.
        await db.crawl_frontier.update_many(
            {"kind": "project", "ref_id": {"$in": chunk}},
            {"$set": {"gone": True, "last_status": "blacklisted"}},
        )

    affected.discard(None)
    return deleted, affected


async def _purge_devlogs(
    db: AsyncIOMotorDatabase, devlog_ids: Sequence[int]
) -> tuple[Counter[str], set[int]]:
    deleted: Counter[str] = Counter()
    affected: set[int] = set()

    for chunk in _chunks(sorted({did for did in devlog_ids})):
        query = {"devlog_id": {"$in": chunk}}
        # Everyone who commented on the thread loses rows, so their totals move.
        affected.update(await db.comments.distinct("user_id", query))
        deleted["comments"] += (await db.comments.delete_many(query)).deleted_count
        deleted["devlog_snapshots"] += await _delete(
            db.devlog_snapshots, {"did": {"$in": chunk}}
        )
        deleted["devlogs"] += (
            await db.devlogs.delete_many({"_id": {"$in": chunk}})
        ).deleted_count
        deleted["frontier"] += (
            await db.crawl_frontier.delete_many(
                {"kind": "devlog", "ref_id": {"$in": chunk}}
            )
        ).deleted_count

    affected.discard(None)
    return deleted, affected


async def _delete(collection, query: dict[str, Any]) -> int:
    """Delete from a time-series collection, which older servers refuse."""
    try:
        return (await collection.delete_many(query)).deleted_count
    except OperationFailure as exc:
        log.warning("could not purge %s (%s)", collection.name, exc)
        return 0


def _chunks(values: list[Any]) -> Iterator[list[Any]]:
    for start in range(0, len(values), CHUNK):
        yield values[start : start + CHUNK]
