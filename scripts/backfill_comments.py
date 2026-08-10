from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import db as database  # noqa: E402
from src.collector.run import backfill_threads, pending_threads  # noqa: E402
from src.fetcher import Fetcher  # noqa: E402


async def main() -> int:
    ap = argparse.ArgumentParser(
        description="Read devlog comment threads once, ignoring the frontier. "
        "The collector does this itself when STARDANCE_BACKFILL_COMMENTS is set."
    )
    ap.add_argument(
        "--all", action="store_true",
        help="re-read threads already read (default: only those never read)",
    )
    ap.add_argument("--limit", type=int, help="stop after this many threads")
    ap.add_argument(
        "--after", type=int, metavar="DEVLOG_ID", help="resume from just past this id"
    )
    ap.add_argument(
        "--concurrency", type=int, help="in-flight crawls (default: the collector's)"
    )
    ap.add_argument("--dry-run", action="store_true", help="count the work, fetch nothing")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    )
    await database.bootstrap()
    db = database.get_db()

    only_new = not args.all
    pending = await pending_threads(db, only_new=only_new, after=args.after)

    if args.dry_run:
        print(json.dumps({"pending": pending, "dry_run": True}, indent=2))
        await database.close()
        return 0

    async with Fetcher() as fetcher:
        result = await backfill_threads(
            db, fetcher,
            only_new=only_new, limit=args.limit,
            concurrency=args.concurrency, after=args.after,
        )

    print(json.dumps(result, indent=2, default=str))
    await database.close()
    failed = sum(
        n for status, n in result["statuses"].items()
        if status not in ("ok", "not_modified", "gone")
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
