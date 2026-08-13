from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import db as database  # noqa: E402
from src.collector import frontier  # noqa: E402
from src.collector.rollup import rollup_global  # noqa: E402
from src.collector.run import REACHED, concurrency_for, drain  # noqa: E402
from src.collector.sitemap import sync_sitemap  # noqa: E402
from src.fetcher import Fetcher  # noqa: E402

log = logging.getLogger("scrape_all")

KINDS = ["project", "user", "mission", "devlog"]


def progress_logger():
    """One line per thousand pages, so a long sweep says where it is."""
    milestone = 0

    def report(state: dict) -> None:
        nonlocal milestone
        if state["crawled"] >= milestone + 1000:
            milestone = state["crawled"] - state["crawled"] % 1000
            rate = state["crawled"] / max(state["seconds"], 1e-9)
            log.info("%d pages, %.2f/s, %s", state["crawled"], rate, state["statuses"])

    return report


async def main() -> int:
    ap = argparse.ArgumentParser(description="Mark the whole frontier due and crawl it.")
    ap.add_argument("--kind", choices=KINDS, help="restrict to one kind")
    ap.add_argument(
        "--mark-only", action="store_true",
        help="mark everything due and stop, leaving the crawling to the running collector",
    )
    ap.add_argument("--no-sync", action="store_true", help="skip the sitemap sync first")
    ap.add_argument(
        "--include-gone", action="store_true",
        help="also re-read pages last seen as 404s, and clear the flag",
    )
    ap.add_argument("--no-rollup", action="store_true", help="skip the global snapshot at the end")
    ap.add_argument("--max-pages", type=int, default=1_000_000)
    ap.add_argument("--max-seconds", type=float, default=float("inf"))
    ap.add_argument("--batch", type=int, default=200)
    ap.add_argument(
        "--dry-run", action="store_true", help="report what would be marked, change nothing"
    )
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    )
    await database.bootstrap()
    db = database.get_db()

    out: dict = {"before": await frontier.queue_depth(db)}

    if args.dry_run:
        query: dict = {} if args.include_gone else {"gone": {"$ne": True}}
        if args.kind:
            query["kind"] = args.kind
        out["would_mark"] = await db.crawl_frontier.count_documents(query)
        print(json.dumps(out, indent=2, default=str))
        await database.close()
        return 0

    async with Fetcher() as fetcher:
        if not args.no_sync:
            # New pages first, so the sweep picks them up in the same pass.
            out["sitemap"] = await sync_sitemap(db, fetcher)
            log.info("sitemap: %s", out["sitemap"])

        out["marked"] = await frontier.mark_all_due(
            db, kind=args.kind, include_gone=args.include_gone
        )
        log.info("marked %(marked)d of %(matched)d rows due", out["marked"])

        if args.mark_only:
            out["note"] = "left for the running collector to drain"
        else:
            log.info(
                "%d route(s), %.1f req/s aggregate, %d crawls in flight",
                len(fetcher.endpoints), fetcher.aggregate_rps, concurrency_for(fetcher),
            )
            log.warning(
                "stop the collector first if it is running, or both will crawl the same rows"
            )
            out["sweep"] = await drain(
                db, fetcher, kind=args.kind, max_pages=args.max_pages,
                max_seconds=args.max_seconds, batch=args.batch,
                on_batch=progress_logger(),
            )

    if not args.no_rollup and not args.mark_only:
        out["rollup"] = await rollup_global(db)

    out["after"] = await frontier.queue_depth(db)
    print(json.dumps(out, indent=2, default=str))

    await database.close()
    failed = sum(
        n for s, n in out.get("sweep", {}).get("statuses", {}).items() if s not in REACHED
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
