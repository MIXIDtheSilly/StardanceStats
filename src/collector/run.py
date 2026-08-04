from __future__ import annotations

import asyncio
import logging
import signal

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from .. import db as database
from ..fetcher import Fetcher
from ..parsers.common import utcnow
from .crawl import crawl_project

log = logging.getLogger("collector")


async def crawl_due_projects(limit: int = 50) -> None:
    """Crawl projects whose next_due has passed (or that have never been due)."""
    db = database.get_db()
    now = utcnow()
    cursor = db.crawl_frontier.find(
        {"kind": "project", "$or": [{"next_due": {"$lte": now}}, {"next_due": None}]},
        projection={"ref_id": 1},
    ).limit(limit)
    due = [row["ref_id"] async for row in cursor]

    if not due:
        log.info("no projects due")
        return

    log.info("crawling %d project(s)", len(due))
    async with Fetcher() as fetcher:
        for project_id in due:
            result = await crawl_project(db, fetcher, project_id)
            log.info("project %s -> %s", project_id, result.get("status"))


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    )
    await database.bootstrap()

    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        crawl_due_projects, "interval", minutes=30, id="crawl_projects",
        max_instances=1, coalesce=True,
    )
    scheduler.start()
    log.info("collector started")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows
            pass

    await crawl_due_projects()
    await stop.wait()

    scheduler.shutdown(wait=False)
    await database.close()


if __name__ == "__main__":
    asyncio.run(main())
