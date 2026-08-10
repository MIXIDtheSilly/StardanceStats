from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import db as database  # noqa: E402
from src.collector.crawl_shop import crawl_shop  # noqa: E402
from src.fetcher import Fetcher  # noqa: E402
from src.parsers.shop import REGION_CODES  # noqa: E402


async def main() -> int:
    ap = argparse.ArgumentParser(
        description="Crawl the Stardance shop catalogue, one page per region."
    )
    ap.add_argument(
        "--region", action="append", choices=REGION_CODES, metavar="CODE",
        help=f"limit to these regions (default: all of {', '.join(REGION_CODES)})",
    )
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(name)s: %(message)s")
    await database.bootstrap()
    db = database.get_db()

    async with Fetcher() as fetcher:
        result = await crawl_shop(
            db, fetcher, regions=tuple(args.region) if args.region else None
        )

    print(json.dumps(result, indent=2, default=str))
    await database.close()
    return 0 if result.get("status") in ("ok", "disabled") else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
