from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import db as database  # noqa: E402
from src.collector import frontier  # noqa: E402
from src.collector.rollup import rollup_global  # noqa: E402
from src.collector.run import REACHED, concurrency_for, crawl_rows  # noqa: E402
from src.collector.sitemap import sync_sitemap  # noqa: E402
from src.fetcher import Fetcher  # noqa: E402

log = logging.getLogger("frontier")


async def sweep(
    db, fetcher, *, kind: str | None, max_pages: int, max_seconds: float, batch: int
) -> dict:
    """Crawl due rows until one of the budgets runs out."""
    started = time.monotonic()
    statuses: dict[str, int] = {}
    done = 0

    def progress(row: dict, result: dict) -> None:
        nonlocal done
        done += 1
        if done % 50 == 0:
            rate = done / max(time.monotonic() - started, 1e-9)
            log.info("%d pages, %.2f/s, %s", done, rate, statuses)

    while done < max_pages and time.monotonic() - started < max_seconds:
        rows = await frontier.due(db, kind=kind, limit=min(batch, max_pages - done))
        if not rows:
            log.info("frontier drained")
            break

        result = await crawl_rows(db, fetcher, rows, on_result=progress)
        for status, n in result["statuses"].items():
            statuses[status] = statuses.get(status, 0) + n
        if result["stopped"] == "circuit_open":
            return _summary(statuses, done, started, "circuit_open", fetcher)

    return _summary(statuses, done, started, fetcher=fetcher)


def _summary(
    statuses: dict, done: int, started: float, stopped: str = "budget", fetcher=None
) -> dict:
    elapsed = time.monotonic() - started
    summary = {
        "crawled": done,
        "seconds": round(elapsed, 1),
        "pages_per_second": round(done / elapsed, 3) if elapsed else None,
        "statuses": statuses,
        "stopped": stopped,
    }
    if fetcher is not None:
        # An uneven split means a route was parked or is slower.
        summary["by_route"] = {
            e.name: e.requests for e in fetcher.endpoints if e.requests
        }
    return summary


async def main() -> int:
    ap = argparse.ArgumentParser(description="Sitemap frontier operations.")
    ap.add_argument("--sync", action="store_true", help="fetch sitemap.xml and reconcile")
    ap.add_argument("--status", action="store_true", help="queue depth by kind and tier")
    ap.add_argument("--crawl", action="store_true", help="crawl due rows")
    ap.add_argument("--rollup", action="store_true", help="write a global snapshot")
    ap.add_argument(
        "--kind", choices=["project", "user", "mission"], help="restrict --crawl"
    )
    ap.add_argument("--max-pages", type=int, default=1_000_000)
    ap.add_argument("--max-seconds", type=float, default=float("inf"))
    ap.add_argument("--batch", type=int, default=200)
    ap.add_argument("--no-cache", action="store_true", help="ignore the stored sitemap ETag")
    args = ap.parse_args()

    if not (args.sync or args.status or args.crawl or args.rollup):
        ap.error("pick at least one of --sync, --status, --crawl, --rollup")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    await database.bootstrap()
    db = database.get_db()

    out = {}
    if args.sync or args.crawl:
        async with Fetcher() as fetcher:
            if args.sync:
                out["sitemap"] = await sync_sitemap(db, fetcher, use_cache=not args.no_cache)
                print(json.dumps(out["sitemap"], indent=2, default=str))
            if args.crawl:
                log.info(
                    "%d route(s), %.1f req/s aggregate, %d crawls in flight",
                    len(fetcher.endpoints), fetcher.aggregate_rps,
                    concurrency_for(fetcher),
                )
                out["sweep"] = await sweep(
                    db, fetcher, kind=args.kind, max_pages=args.max_pages,
                    max_seconds=args.max_seconds, batch=args.batch,
                )
                print(json.dumps(out["sweep"], indent=2, default=str))

    if args.rollup:
        out["rollup"] = await rollup_global(db)
        print(json.dumps(out["rollup"], indent=2, default=str))

    if args.status:
        print(json.dumps(await frontier.queue_depth(db), indent=2, default=str))

    await database.close()
    failed = sum(
        n for s, n in out.get("sweep", {}).get("statuses", {}).items() if s not in REACHED
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
