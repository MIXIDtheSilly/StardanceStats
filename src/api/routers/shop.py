from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from motor.motor_asyncio import AsyncIOMotorDatabase

from ...parsers.shop import REGIONS
from ..deps import db as db_dep
from ..examples import HISTORY, SHOP_ITEM, SHOP_LIST, SHOP_REGIONS, example
from ..services import HistoryError, Interval, bucketed_series, cached_count, stamp
from ..services.history import parse_metrics

router = APIRouter()

SORTS = {
    "price": "price_min",
    "spread": "price_spread",
    "purchases": "purchases",
    "name": "name",
}


def _region(value: str | None) -> str | None:
    if value is None:
        return None
    code = value.upper()
    if code not in REGIONS:
        raise HTTPException(400, f"unknown region {value!r}; valid: {sorted(REGIONS)}")
    return code


def _priced(item: dict[str, Any], region: str | None) -> dict[str, Any]:
    """Pin an item to one region's price, keeping the full table alongside."""
    if region is None:
        return item
    item["region"] = region
    item["price"] = (item.get("prices") or {}).get(region)
    item["full_price"] = (item.get("full_prices") or {}).get(region)
    return item


async def _as_of(db: AsyncIOMotorDatabase) -> datetime | None:
    doc = await db.shop_items.find_one(
        {"last_crawled": {"$ne": None}},
        {"last_crawled": 1},
        sort=[("last_crawled", -1)],
    )
    return (doc or {}).get("last_crawled")


@router.get("/shop", responses=example(SHOP_LIST), response_model=None)
async def list_shop_items(
    region: str | None = Query(None, description="US, EU, UK, IN, CA, AU or XX."),
    category: str | None = None,
    on_sale: bool | None = None,
    include_gone: bool = Query(False, description="Items no longer listed."),
    sort: Literal["price", "spread", "purchases", "name"] = "price",
    order: Literal["asc", "desc"] = "asc",
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: AsyncIOMotorDatabase = Depends(db_dep),
) -> dict[str, Any]:
    """The catalogue, priced per region. Prices are Stardust, not currency."""
    code = _region(region)

    query: dict[str, Any] = {}
    if not include_gone:
        query["gone"] = {"$ne": True}
    if code:
        # Availability is regional too, not only the price.
        query["regions"] = code
    if category:
        query["categories"] = category
    if on_sale is not None:
        query["on_sale"] = on_sale

    field = SORTS[sort]
    if code and sort == "price":
        field = f"prices.{code}"

    total = await cached_count(db, "shop_items", query)
    cursor = (
        db.shop_items.find(query)
        .sort([(field, 1 if order == "asc" else -1)])
        .skip(offset)
        .limit(limit)
    )
    items = [_priced(item, code) for item in await cursor.to_list(length=limit)]

    return stamp(
        {
            "region": code,
            "total": total,
            "limit": limit,
            "offset": offset,
            "items": items,
        },
        await _as_of(db),
    )


@router.get("/shop/regions", responses=example(SHOP_REGIONS), response_model=None)
async def shop_regions(db: AsyncIOMotorDatabase = Depends(db_dep)) -> dict[str, Any]:
    """Catalogue size and price range per region."""
    rows = await db.shop_items.aggregate([
        {"$match": {"gone": {"$ne": True}}},
        {"$unwind": "$regions"},
        {"$group": {
            "_id": "$regions",
            "items": {"$sum": 1},
            "on_sale": {"$sum": {"$cond": ["$on_sale", 1, 0]}},
        }},
    ]).to_list(length=len(REGIONS))
    counts = {row["_id"]: row for row in rows}

    # Over the items a region actually sells, so the figures compare.
    priced = await db.shop_items.find(
        {"gone": {"$ne": True}}, {"prices": 1, "regions": 1}
    ).to_list(length=1000)

    out = []
    for code, name in REGIONS.items():
        prices = [
            item["prices"][code]
            for item in priced
            if code in (item.get("prices") or {})
        ]
        row = counts.get(code, {})
        out.append({
            "region": code,
            "name": name,
            "items": row.get("items", 0),
            "on_sale": row.get("on_sale", 0),
            "cheapest": min(prices) if prices else None,
            "dearest": max(prices) if prices else None,
            "median_price": _median(prices),
        })

    return stamp({"regions": out}, await _as_of(db))


@router.get("/shop/{item_id}", responses=example(SHOP_ITEM), response_model=None)
async def get_shop_item(
    item_id: int,
    region: str | None = None,
    db: AsyncIOMotorDatabase = Depends(db_dep),
) -> dict[str, Any]:
    """One catalogue item, priced for the region asked for."""
    doc = await db.shop_items.find_one({"_id": item_id})
    if not doc:
        raise HTTPException(404, f"shop item {item_id} not tracked")
    return stamp(_priced(doc, _region(region)), doc.get("last_crawled"))


@router.get("/shop/{item_id}/history", responses=example(HISTORY), response_model=None)
async def get_shop_item_history(
    item_id: int,
    metrics: str = Query("price_us", description="Comma-separated; see /v1/meta."),
    interval: Interval = "1d",
    start: datetime | None = None,
    end: datetime | None = None,
    delta: bool = Query(True),
    fill: Literal["none", "locf"] = Query(
        "none", description="locf carries the last observation into empty buckets."
    ),
    db: AsyncIOMotorDatabase = Depends(db_dep),
) -> dict[str, Any]:
    """How one item's price moved, per region."""
    doc = await db.shop_items.find_one({"_id": item_id}, {"name": 1, "last_crawled": 1})
    if not doc:
        raise HTTPException(404, f"shop item {item_id} not tracked")

    try:
        result = await bucketed_series(
            db, "shop", item_id,
            metrics=parse_metrics("shop", metrics), interval=interval,
            start=start, end=end, delta=delta, fill=fill,
        )
    except HistoryError as exc:
        raise HTTPException(400, str(exc)) from exc

    result["entity"] = {"type": "shop_item", "id": item_id, "name": doc.get("name")}
    return stamp(result, doc.get("last_crawled"))


def _median(values: list[int]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[middle])
    return (ordered[middle - 1] + ordered[middle]) / 2
