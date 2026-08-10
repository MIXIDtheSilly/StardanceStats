from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import db as database  # noqa: E402
from src.collector.crawl_devlog import crawl_devlog, enqueue_stale_threads  # noqa: E402
from src.fetcher import Fetcher  # noqa: E402


async def main() -> int:
    ap = argparse.ArgumentParser(
        description="Crawl Stardance devlog comment threads into MongoDB."
    )
    ap.add_argument("devlog_ids", type=int, nargs="*")
    ap.add_argument("--project", type=int, help="skip the devlog lookup")
    ap.add_argument("--no-cache", action="store_true", help="ignore stored ETags")
    ap.add_argument(
        "--stale", type=int, metavar="N",
        help="crawl the N threads whose comment counter has moved",
    )
    args = ap.parse_args()

    if not args.devlog_ids and args.stale is None:
        ap.error("give devlog ids, --stale N, or both")

    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(name)s: %(message)s")
    await database.bootstrap()
    db = database.get_db()

    todo = [(did, args.project) for did in args.devlog_ids]
    if args.stale:
        await enqueue_stale_threads(db, limit=args.stale)
        cursor = db.devlogs.find(
            {"comments_stale": True, "gone": {"$ne": True}}, {"project_id": 1}
        ).limit(args.stale)
        todo += [(row["_id"], row.get("project_id")) async for row in cursor]

    failures = 0
    async with Fetcher() as fetcher:
        for devlog_id, project_id in todo:
            result = await crawl_devlog(
                db, fetcher, devlog_id,
                project_id=project_id, use_cache=not args.no_cache,
            )
            print(json.dumps(result, indent=2, default=str))
            if result.get("status") not in ("ok", "not_modified"):
                failures += 1

    await database.close()
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
