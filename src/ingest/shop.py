from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase

from ..config import settings
from ..parsers.common import ParseResult, utcnow
from ..parsers.shop import REGION_CODES

log = logging.getLogger(__name__)

# Flat, so price_in is asked for like any other metric.
PRICE_METRICS = tuple(f"price_{code.lower()}" for code in REGION_CODES)

TRACKED = PRICE_METRICS + (
    "sale_percentage", "purchases", "remaining_stock", "regions_available",
)

# Region-blind facts: whichever page rendered the item first settles them.
SHARED = (
    "name", "description", "url", "image_url", "categories", "enabled_regions",
    "purchases", "is_new", "remaining_stock", "out_of_stock", "sale_percentage",
    "on_sale", "achievement_locked", "mission_locked", "enabled_until",
    "hours_low", "hours_high",
)


# Under this, one item delisted is already a large fraction of the catalogue.
MIN_CATALOG_FOR_DROP_CHECK = 10


class ShopRejected(Exception):
    """A catalogue page parsed structurally but not well enough to write from."""


def merge_regions(results: dict[str, ParseResult]) -> dict[int, dict[str, Any]]:
    """Fold one parse per region into one row per item, priced in each."""
    merged: dict[int, dict[str, Any]] = {}

    for region in REGION_CODES:
        result = results.get(region)
        if result is None:
            continue
        for item in result.data["items"]:
            row = merged.setdefault(
                item["_id"], {"_id": item["_id"], "prices": {}, "full_prices": {}, "regions": []}
            )
            if item["price"] is not None:
                row["prices"][region] = item["price"]
            if item["full_price"] is not None:
                row["full_prices"][region] = item["full_price"]
            row["regions"].append(region)
            for key in SHARED:
                if row.get(key) is None:
                    row[key] = item.get(key)

    for row in merged.values():
        prices = list(row["prices"].values())
        row["price_min"] = min(prices) if prices else None
        row["price_max"] = max(prices) if prices else None
        # What the same item costs at its dearest over its cheapest region.
        row["price_spread"] = (
            row["price_max"] - row["price_min"] if prices else None
        )
        row["regions_available"] = len(row["regions"])

    return merged


def check_shop_anomalies(
    results: dict[str, ParseResult], held: dict[str, int]
) -> list[str]:
    """Return reasons this sweep should be rejected. Empty means accept."""
    reasons: list[str] = []
    for region, result in results.items():
        if result.missing:
            reasons.append(f"{region}: unparsed fields {sorted(result.missing)}")

        was, now_count = held.get(region, 0), len(result.data["items"])
        if was < MIN_CATALOG_FOR_DROP_CHECK:
            continue
        if now_count < was * (1 - settings.anomaly_drop_threshold):
            reasons.append(f"{region}: catalogue fell {was} -> {now_count} items")
    return reasons


async def ingest_shop(
    db: AsyncIOMotorDatabase,
    results: dict[str, ParseResult],
    *,
    now: datetime | None = None,
    complete: bool | None = None,
) -> dict[str, Any]:
    """Persist one sweep of the catalogue. `complete` means every region loaded."""
    now = now or utcnow()
    if not results:
        return {"items": 0, "regions": [], "retired": 0, "snapshots": 0}

    if complete is None:
        complete = set(results) == set(REGION_CODES)

    held = await _held_per_region(db)
    reasons = check_shop_anomalies(results, held)
    if reasons:
        await _log_crawl(db, "anomaly", now, reasons=reasons)
        raise ShopRejected("; ".join(reasons))

    merged = merge_regions(results)
    existing = {
        doc["_id"]: doc
        async for doc in db.shop_items.find({"_id": {"$in": list(merged)}})
    }

    snapshots: list[dict[str, Any]] = []
    changed_items = 0
    for item_id, row in merged.items():
        was = existing.get(item_id)
        doc = dict(row)
        doc["last_crawled"] = now
        if was is None:
            doc["first_seen"] = now

        changed = sorted(_changed_keys(was, doc))
        if changed or was is None:
            doc["last_changed"] = now
            changed_items += 1

        await db.shop_items.update_one(
            {"_id": item_id},
            {"$set": doc, "$unset": {"gone": "", "gone_at": ""}},
            upsert=True,
        )

        if changed or was is None or await _heartbeat_due(db, item_id, now):
            snapshots.append(_snapshot(item_id, doc, now))

    if snapshots:
        await db.shop_snapshots.insert_many(snapshots, ordered=False)

    # A region that failed to load is not evidence that its items are gone.
    retired = 0
    if complete:
        outcome = await db.shop_items.update_many(
            {"_id": {"$nin": list(merged)}, "gone": {"$ne": True}},
            {"$set": {"gone": True, "gone_at": now}},
        )
        retired = outcome.modified_count

    regions = sorted(results)
    await _log_crawl(
        db, "ok", now,
        regions=regions, items=len(merged), changed=changed_items, retired=retired,
        warnings=[w for r in results.values() for w in r.warnings],
    )

    return {
        "items": len(merged),
        "regions": regions,
        "complete": complete,
        "changed": changed_items,
        "retired": retired,
        "snapshots": len(snapshots),
    }


async def _held_per_region(db: AsyncIOMotorDatabase) -> dict[str, int]:
    """How many items each region's page gave us last time."""
    rows = await db.shop_items.aggregate([
        {"$match": {"gone": {"$ne": True}}},
        {"$unwind": "$regions"},
        {"$group": {"_id": "$regions", "n": {"$sum": 1}}},
    ]).to_list(length=len(REGION_CODES))
    return {row["_id"]: row["n"] for row in rows}


def _changed_keys(previous: dict[str, Any] | None, current: dict[str, Any]) -> set[str]:
    keys = (*SHARED, "prices", "full_prices", "regions")
    if previous is None:
        return {k for k in keys if current.get(k) is not None}
    return {k for k in keys if previous.get(k) != current.get(k)}


def _snapshot(item_id: int, row: dict[str, Any], ts: datetime) -> dict[str, Any]:
    doc: dict[str, Any] = {"ts": ts, "sid": item_id}
    for region, price in (row.get("prices") or {}).items():
        doc[f"price_{region.lower()}"] = price
    for key in ("sale_percentage", "purchases", "remaining_stock", "regions_available"):
        if row.get(key) is not None:
            doc[key] = row[key]
    return doc


async def _heartbeat_due(db: AsyncIOMotorDatabase, item_id: int, now: datetime) -> bool:
    cutoff = now - timedelta(hours=settings.snapshot_heartbeat_hours)
    latest = await db.shop_snapshots.find_one(
        {"sid": item_id, "ts": {"$gte": cutoff}}, projection={"_id": 1}
    )
    return latest is None


async def _log_crawl(
    db: AsyncIOMotorDatabase, status: str, ts: datetime, **extra: Any
) -> None:
    try:
        await db.crawl_log.insert_one(
            {"ts": ts, "kind": "shop", "ref_id": "catalog", "status": status, **extra}
        )
    except Exception:  # logging must never break ingest
        log.exception("crawl_log write failed")
