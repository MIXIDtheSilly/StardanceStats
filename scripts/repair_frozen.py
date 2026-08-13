from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import db as database  # noqa: E402
from src.collector.tiering import priority  # noqa: E402

log = logging.getLogger("repair_frozen")

# A real delisting carries sitemap_sync; one frozen by the missing-in_sitemap bug never had one.
WRONGLY_FROZEN = {
    "in_sitemap": False,
    "delisted_at": {"$exists": True},
    "sitemap_sync": {"$exists": False},
}


async def survey(db) -> dict:
    """Count the affected rows per kind without touching them."""
    rows = await db.crawl_frontier.aggregate([
        {"$match": WRONGLY_FROZEN},
        {"$group": {"_id": {"kind": "$kind", "tier": "$tier"}, "n": {"$sum": 1}}},
    ]).to_list(length=100)

    by_kind: dict[str, dict] = {}
    for row in rows:
        kind = row["_id"]["kind"] or "unknown"
        bucket = by_kind.setdefault(kind, {"total": 0, "tiers": {}})
        bucket["total"] += row["n"]
        bucket["tiers"][row["_id"]["tier"] or "unassigned"] = row["n"]
    return by_kind


async def repair(db, *, kind: str | None = None) -> int:
    """Thaw the wrongly frozen rows back to cold and make them due."""
    query = dict(WRONGLY_FROZEN)
    if kind:
        query["kind"] = kind

    fixed = 0
    for target in ("project", "user", "devlog", "mission"):
        if kind and target != kind:
            continue
        result = await db.crawl_frontier.update_many(
            {**query, "kind": target},
            {
                "$set": {"tier": "cold", "priority": priority("cold", target), "next_due": None},
                "$unset": {"delisted_at": ""},
            },
        )
        if result.modified_count:
            log.info("thawed %d %s rows", result.modified_count, target)
        fixed += result.modified_count
    return fixed


async def main() -> int:
    ap = argparse.ArgumentParser(
        description="Thaw frontier rows frozen by the missing in_sitemap flag."
    )
    ap.add_argument("--apply", action="store_true", help="write the change; default is a dry run")
    ap.add_argument("--kind", choices=["project", "user", "devlog", "mission"])
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    )
    await database.bootstrap()
    db = database.get_db()

    affected = await survey(db)
    print(json.dumps({"affected": affected}, indent=2, default=str))

    if not args.apply:
        total = sum(b["total"] for b in affected.values())
        print(f"\ndry run: {total} rows would be thawed. Re-run with --apply.")
    else:
        print(f"\nthawed {await repair(db, kind=args.kind)} rows")

    await database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
