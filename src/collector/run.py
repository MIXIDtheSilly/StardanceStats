from __future__ import annotations

import asyncio
import logging
import signal
import time
from typing import Any, Callable, Sequence

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from motor.motor_asyncio import AsyncIOMotorDatabase

from .. import db as database
from ..config import settings
from ..fetcher import CircuitOpen, Fetcher
from ..ingest import recompute_all_users, recompute_user_totals
from ..parsers.common import utcnow
from . import frontier
from .crawl import crawl_project
from .crawl_devlog import crawl_devlog, enqueue_stale_threads
from .crawl_mission import crawl_mission
from .crawl_shop import crawl_shop
from .crawl_user import crawl_user, resolve_project_owners
from .rollup import rollup_global
from .sitemap import sync_sitemap

log = logging.getLogger("collector")

# Statuses that mean the page was reached, whatever it then said.
REACHED = frozenset({"ok", "not_modified", "gone"})

# Upstream throttles at 600 req / 5 min and 120 / min, keyed on the caller's address.
PER_ADDRESS_LIMIT = 2.0

# Each owner costs their profile plus every project of theirs we have not seen.
OWNER_RESOLVE_LIMIT = 100

# How far past the highest id known to be real the scan keeps probing.
ID_SCAN_MARGIN = 1000

# Threads run in their own lane. Sharing one would starve them: they are queued
# at priority 1 with next_due=now, so they sort behind every overdue page.
THREAD_KINDS = ("devlog",)

# Big enough to amortise the round trip, small enough to report progress often.
THREAD_PAGE = 500


async def crawl_one(
    db: AsyncIOMotorDatabase,
    fetcher: Fetcher,
    row: dict[str, Any],
    *,
    defer_user_totals: bool = False,
) -> dict[str, Any]:
    """Crawl a single frontier row. Kind decides which pipeline runs."""
    if row["kind"] == "user":
        # Projects hold their own frontier rows and tiers.
        return await crawl_user(db, fetcher, row["ref_id"], with_projects=False)
    if row["kind"] == "mission":
        return await crawl_mission(db, fetcher, row["ref_id"])
    if row["kind"] == "devlog":
        return await crawl_devlog(
            db, fetcher, row["ref_id"], project_id=row.get("parent_id")
        )
    return await crawl_project(
        db, fetcher, row["ref_id"], defer_user_totals=defer_user_totals
    )


def concurrency_for(fetcher: Fetcher) -> int:
    """How many crawls to keep in flight."""
    if settings.crawl_concurrency > 0:
        return settings.crawl_concurrency
    return max(1, len(fetcher.endpoints) * 2)


async def crawl_rows(
    db: AsyncIOMotorDatabase,
    fetcher: Fetcher,
    rows: list[dict[str, Any]],
    *,
    concurrency: int | None = None,
    on_result: Callable[[dict[str, Any], dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Crawl a batch concurrently, then settle the user totals it touched."""
    if not rows:
        return {"crawled": 0, "statuses": {}}

    limit = concurrency or concurrency_for(fetcher)
    gate = asyncio.Semaphore(limit)
    statuses: dict[str, int] = {}
    touched_users: set[int] = set()
    stopped = False

    async def run(row: dict[str, Any]) -> None:
        nonlocal stopped
        if stopped:
            return
        async with gate:
            if stopped:
                return
            try:
                result = await crawl_one(db, fetcher, row, defer_user_totals=True)
            except CircuitOpen as exc:
                log.error("circuit open, abandoning batch: %s", exc)
                stopped = True
                statuses["circuit_open"] = statuses.get("circuit_open", 0) + 1
                return
            except Exception:
                log.exception("crawl failed for %s:%s", row["kind"], row["ref_id"])
                statuses["crashed"] = statuses.get("crashed", 0) + 1
                return

        status = result.get("status", "unknown")
        statuses[status] = statuses.get(status, 0) + 1
        touched_users.update(result.get("linked_users") or [])
        if status not in REACHED:
            log.warning("%s %s -> %s", row["kind"], row["ref_id"], result)
        if on_result:
            on_result(row, result)

    await asyncio.gather(*(run(row) for row in rows))

    # Concurrent crawls of one user's projects would race on their totals.
    for user_id in touched_users:
        try:
            await recompute_user_totals(db, user_id)
        except Exception:
            log.exception("recompute failed for user %s", user_id)

    return {
        "crawled": sum(statuses.values()),
        "statuses": statuses,
        "users_recomputed": len(touched_users),
        "stopped": "circuit_open" if stopped else "complete",
    }


async def crawl_batch(
    db: AsyncIOMotorDatabase,
    fetcher: Fetcher,
    *,
    kind: str | None = None,
    exclude: Sequence[str] | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Take one batch of due rows off the frontier and crawl it."""
    limit = limit or settings.crawl_batch_size
    rows = await frontier.due(db, kind=kind, exclude=exclude, limit=limit)
    if not rows:
        return {"crawled": 0, "statuses": {}}

    started = time.monotonic()
    result = await crawl_rows(db, fetcher, rows)
    elapsed = time.monotonic() - started
    log.info(
        "%s batch: %d crawled in %.1fs (%.2f/s) %s",
        kind or "page", result["crawled"], elapsed,
        result["crawled"] / elapsed if elapsed else 0.0, result["statuses"],
    )
    return result


async def drain(
    db: AsyncIOMotorDatabase,
    fetcher: Fetcher,
    *,
    kind: str | None = None,
    max_pages: int = 1_000_000,
    max_seconds: float = float("inf"),
    batch: int | None = None,
    on_batch: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Crawl due rows until the frontier empties or a budget runs out."""
    started = time.monotonic()
    statuses: dict[str, int] = {}
    done = 0
    stopped = "drained"

    while done < max_pages and time.monotonic() - started < max_seconds:
        limit = min(batch or settings.crawl_batch_size, max_pages - done)
        result = await crawl_batch(db, fetcher, kind=kind, limit=limit)
        if result["crawled"] == 0:
            break

        done += result["crawled"]
        for status, n in result["statuses"].items():
            statuses[status] = statuses.get(status, 0) + n
        if on_batch:
            on_batch({"crawled": done, "statuses": statuses, "seconds": time.monotonic() - started})
        if result.get("stopped") == "circuit_open":
            stopped = "circuit_open"
            break

        # Crashes do not reschedule the row, so an all-crashed batch repeats forever.
        if not set(result["statuses"]) - {"crashed"}:
            log.error("every row in the batch crashed, stopping: %s", result["statuses"])
            stopped = "stuck"
            break
    else:
        stopped = "budget"

    elapsed = time.monotonic() - started
    return {
        "crawled": done,
        "seconds": round(elapsed, 1),
        "pages_per_second": round(done / elapsed, 3) if elapsed else None,
        "statuses": statuses,
        "stopped": stopped,
    }


async def crawl_loop(
    db: AsyncIOMotorDatabase,
    fetcher: Fetcher,
    stop: asyncio.Event,
    *,
    kind: str | None = None,
    exclude: Sequence[str] | None = None,
) -> None:
    """Drain one lane of the frontier continuously, idling when nothing is due."""
    while not stop.is_set():
        try:
            batch = await crawl_batch(db, fetcher, kind=kind, exclude=exclude)
        except Exception:
            log.exception("crawl batch failed; backing off")
            batch = {"crawled": 0}

        if batch["crawled"] == 0:
            try:
                await asyncio.wait_for(stop.wait(), timeout=settings.crawl_idle_seconds)
            except asyncio.TimeoutError:
                pass


def _thread_query(only_new: bool, after: int | None) -> dict[str, Any]:
    """Threads worth reading. Without a project there is no url to fetch."""
    query: dict[str, Any] = {"gone": {"$ne": True}, "project_id": {"$ne": None}}
    if only_new:
        # Matches missing as well as null, so threads never read are included.
        query["comments_crawled_at"] = None
    if after is not None:
        query["_id"] = {"$gt": after}
    return query


async def pending_threads(
    db: AsyncIOMotorDatabase, *, only_new: bool = True, after: int | None = None
) -> int:
    return await db.devlogs.count_documents(_thread_query(only_new, after))


async def backfill_threads(
    db: AsyncIOMotorDatabase,
    fetcher: Fetcher,
    *,
    only_new: bool = True,
    limit: int | None = None,
    concurrency: int | None = None,
    after: int | None = None,
) -> dict[str, Any]:
    """Read threads straight from `devlogs`, ignoring the frontier's ordering."""
    done = 0
    statuses: dict[str, int] = {}

    while limit is None or done < limit:
        size = THREAD_PAGE if limit is None else min(THREAD_PAGE, limit - done)
        # Paging by id survives a sweep longer than a cursor would live.
        cursor = (
            db.devlogs.find(_thread_query(only_new, after), {"project_id": 1})
            .sort("_id", 1)
            .limit(size)
        )
        rows = [row async for row in cursor]
        if not rows:
            break
        after = rows[-1]["_id"]

        result = await crawl_rows(
            db,
            fetcher,
            [
                {"kind": "devlog", "ref_id": r["_id"], "parent_id": r["project_id"]}
                for r in rows
            ],
            concurrency=concurrency,
        )
        done += result["crawled"]
        for status, count in result["statuses"].items():
            statuses[status] = statuses.get(status, 0) + count
        log.info("backfill: %d thread(s) read, through devlog %s", done, after)

        if result.get("stopped") == "circuit_open":
            log.error("circuit open, stopping the backfill after devlog %s", after)
            break

    return {"threads": done, "statuses": statuses, "last_id": after}


async def run_backfill(db: AsyncIOMotorDatabase, fetcher: Fetcher) -> None:
    """The one-off sweep, running beside the lanes rather than delaying them."""
    log.info("backfill: %d thread(s) never read", await pending_threads(db))
    try:
        log.info("backfill finished: %s", await backfill_threads(db, fetcher))
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("backfill failed")


async def extend_scan(db: AsyncIOMotorDatabase) -> dict[str, Any]:
    """Queue the id ranges the sitemap leaves out. No network of its own."""
    out = {}
    for kind in ("project", "user"):
        out[kind] = await frontier.extend_scan(db, kind, margin=ID_SCAN_MARGIN)
    seeded = sum(v["seeded"] for v in out.values())
    if seeded:
        log.info("id scan queued %d new rows: %s", seeded, out)
    return out


async def resolve_owners(db: AsyncIOMotorDatabase, fetcher: Fetcher) -> dict[str, Any]:
    """Crawl owners the sitemap never listed, by the handle their project names."""
    outcome = await resolve_project_owners(db, fetcher, limit=OWNER_RESOLVE_LIMIT)
    if outcome["pending"]:
        log.info(
            "owners: %d resolved, %d failed, of %d pending",
            len(outcome["resolved"]), len(outcome["failed"]), outcome["pending"],
        )
    return outcome


async def recompute_derived(db: AsyncIOMotorDatabase) -> dict[str, Any]:
    """Re-derive user totals from stored rows. No network."""
    count = await recompute_all_users(db)
    log.info("recomputed totals for %d users", count)
    return {"users": count}


def build_scheduler(db: AsyncIOMotorDatabase, fetcher: Fetcher) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone="UTC")
    common = {"max_instances": 1, "coalesce": True, "misfire_grace_time": 600}

    scheduler.add_job(
        sync_sitemap, "interval", hours=1, args=[db, fetcher], id="sync_sitemap", **common
    )
    scheduler.add_job(
        rollup_global, "interval", hours=1, args=[db], id="rollup_global", **common
    )
    scheduler.add_job(
        extend_scan, "interval", hours=1, args=[db], id="extend_scan", **common
    )
    scheduler.add_job(
        resolve_owners, "interval", hours=1, args=[db, fetcher],
        id="resolve_owners", **common,
    )
    scheduler.add_job(
        enqueue_stale_threads, "interval", minutes=5, args=[db],
        id="enqueue_stale_threads", **common,
    )
    scheduler.add_job(
        crawl_shop, "interval", minutes=settings.shop_interval_minutes,
        args=[db, fetcher], id="crawl_shop", **common,
    )
    scheduler.add_job(
        recompute_derived, "cron", hour=3, args=[db], id="recompute_derived", **common
    )
    return scheduler


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    )
    await database.bootstrap()
    db = database.get_db()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows
            pass

    async with Fetcher() as fetcher:
        log.info(
            "fetching over %d route(s) at %.1f req/s each, %.1f req/s aggregate",
            len(fetcher.endpoints), settings.proxy_requests_per_second,
            fetcher.aggregate_rps,
        )
        hot = [e.name for e in fetcher.endpoints if e.limiter.rps > PER_ADDRESS_LIMIT]
        if hot:
            # Only check_proxies.py knows which routes share an address.
            log.warning(
                "%d route(s) are configured above upstream's %.1f req/s "
                "per-address limit: %s",
                len(hot), PER_ADDRESS_LIMIT, ", ".join(hot[:5]),
            )

        scheduler = build_scheduler(db, fetcher)
        scheduler.start()

        # Otherwise the loop idles for an hour waiting on the first sync.
        if await db.crawl_frontier.count_documents({}) == 0:
            log.info("frontier empty, syncing sitemap")
            log.info("sitemap: %s", await sync_sitemap(db, fetcher))

        # At startup too, so a first deploy does not wait an hour to widen.
        log.info("id scan: %s", await extend_scan(db))

        depth = await frontier.queue_depth(db)
        log.info("collector started at %s; frontier %s", utcnow().isoformat(), depth)

        workers = [
            asyncio.create_task(crawl_loop(db, fetcher, stop, exclude=THREAD_KINDS)),
            asyncio.create_task(crawl_loop(db, fetcher, stop, kind="devlog")),
        ]
        if settings.backfill_comments:
            workers.append(asyncio.create_task(run_backfill(db, fetcher)))

        await stop.wait()

        log.info("shutting down")
        scheduler.shutdown(wait=False)
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)

    await database.close()


if __name__ == "__main__":
    asyncio.run(main())
