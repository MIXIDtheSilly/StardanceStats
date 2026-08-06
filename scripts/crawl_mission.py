from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import db as database  # noqa: E402
from src.collector.crawl_mission import crawl_mission  # noqa: E402
from src.fetcher import Fetcher  # noqa: E402


async def main() -> int:
    ap = argparse.ArgumentParser(description="Crawl Stardance missions into MongoDB.")
    ap.add_argument("slugs", nargs="+")
    ap.add_argument("--no-cache", action="store_true", help="ignore stored ETags")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(name)s: %(message)s")
    await database.bootstrap()
    db = database.get_db()

    failures = 0
    async with Fetcher() as fetcher:
        for slug in args.slugs:
            result = await crawl_mission(db, fetcher, slug, use_cache=not args.no_cache)
            print(json.dumps(result, indent=2, default=str))
            if result.get("status") not in ("ok", "not_modified"):
                failures += 1

    await database.close()
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
