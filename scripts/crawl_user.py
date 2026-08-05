from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import db as database  # noqa: E402
from src.collector.crawl_user import (  # noqa: E402
    crawl_user,
    crawl_user_by_handle,
    resolve_project_owners,
)
from src.fetcher import Fetcher  # noqa: E402
from src.ingest import recompute_all_users  # noqa: E402


async def main() -> int:
    ap = argparse.ArgumentParser(description="Crawl Stardance users into MongoDB.")
    ap.add_argument("user_ids", type=int, nargs="*")
    ap.add_argument("--handle", action="append", default=[], help="crawl by @handle")
    ap.add_argument(
        "--resolve-owners", action="store_true",
        help="crawl owners of crawled projects that have no numeric id yet",
    )
    ap.add_argument(
        "--recompute", action="store_true",
        help="recompute every user's totals from stored rows, no network",
    )
    ap.add_argument(
        "--no-projects", action="store_true",
        help="skip the user's projects; totals then cover only what we hold",
    )
    ap.add_argument(
        "--refresh-projects", action="store_true",
        help="also re-crawl the user's projects we already have",
    )
    ap.add_argument("--no-cache", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(name)s: %(message)s")
    await database.bootstrap()
    db = database.get_db()

    if not (args.user_ids or args.handle or args.resolve_owners or args.recompute):
        ap.error("give user ids, --handle, --resolve-owners, or --recompute")

    failures = 0

    if args.recompute:
        count = await recompute_all_users(db)
        print(json.dumps({"recomputed": count}, indent=2))

    if args.user_ids or args.handle or args.resolve_owners:
        project_opts = {
            "with_projects": not args.no_projects,
            "refresh_projects": args.refresh_projects,
        }
        async with Fetcher() as fetcher:
            for user_id in args.user_ids:
                result = await crawl_user(
                    db, fetcher, user_id, use_cache=not args.no_cache, **project_opts
                )
                print(json.dumps(result, indent=2, default=str))
                if result.get("status") not in ("ok", "not_modified"):
                    failures += 1

            for handle in args.handle:
                result = await crawl_user_by_handle(db, fetcher, handle, **project_opts)
                print(json.dumps(result, indent=2, default=str))
                if result.get("status") != "ok":
                    failures += 1

            if args.resolve_owners:
                result = await resolve_project_owners(db, fetcher)
                print(json.dumps(result, indent=2, default=str))
                failures += len(result["failed"])

    await database.close()
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
