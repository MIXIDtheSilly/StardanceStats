from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo.errors import PyMongoError

from src.config import settings
from src.db import bootstrap
from src.ingest import ShopRejected, ingest_shop, merge_regions
from src.parsers import parse_shop_page
from src.parsers.common import ParseResult
from src.parsers.shop import REGION_CODES

FIXTURES = Path(__file__).parent / "fixtures"
TEST_DB = "stardance_stats_test"
UTC = timezone.utc
NOW = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def db():
    client = AsyncIOMotorClient(settings.mongo_url, tz_aware=True, serverSelectionTimeoutMS=1500)
    try:
        await client.admin.command("ping")
    except PyMongoError as exc:
        pytest.skip(f"no MongoDB at {settings.mongo_url}: {exc}")

    await client.drop_database(TEST_DB)
    database = client[TEST_DB]
    await bootstrap(database)
    try:
        yield database
    finally:
        await client.drop_database(TEST_DB)
        client.close()


def fixture_sweep() -> dict[str, ParseResult]:
    """The two real regions we hold pages for, as a sweep."""
    return {
        "US": parse_shop_page((FIXTURES / "shop_all_us.html").read_text(encoding="utf-8"), "US"),
        "IN": parse_shop_page((FIXTURES / "shop_all_in.html").read_text(encoding="utf-8"), "IN"),
    }


def card(item_id: int, price: int, **over):
    item = {
        "_id": item_id, "name": f"item {item_id}", "description": None,
        "url": f"/shop/items/{item_id}", "image_url": None,
        "price": price, "full_price": price, "on_sale": False, "sale_percentage": None,
        "categories": [], "enabled_regions": list(REGION_CODES), "purchases": None,
        "is_new": False, "remaining_stock": None, "out_of_stock": False,
        "achievement_locked": False, "mission_locked": False, "enabled_until": None,
        "hours_low": None, "hours_high": None,
    }
    item.update(over)
    return item


def sweep(prices: dict[str, list[dict]]) -> dict[str, ParseResult]:
    out = {}
    for region, items in prices.items():
        result = ParseResult()
        result.data["region"] = region
        result.data["items"] = items
        out[region] = result
    return out


def all_regions(items_for) -> dict[str, ParseResult]:
    """The same catalogue in every region, priced by a callable."""
    return sweep({region: items_for(region) for region in REGION_CODES})


async def test_a_sweep_writes_one_row_per_item_priced_per_region(db):
    summary = await ingest_shop(db, fixture_sweep(), now=NOW)

    # The union, not either page: India sells three things the US page does not.
    assert summary["items"] == 104
    assert await db.shop_items.count_documents({"regions": "US"}) == 101
    assert await db.shop_items.count_documents({"regions": "IN"}) == 90
    assert summary["regions"] == ["IN", "US"]
    # Two of seven regions, so nothing may be retired off the back of it.
    assert summary["complete"] is False

    pinecil = await db.shop_items.find_one({"_id": 8})
    assert pinecil["name"] == "Pinecil"
    assert pinecil["prices"] == {"US": 185, "IN": 379}
    assert pinecil["price_min"] == 185
    assert pinecil["price_max"] == 379
    assert pinecil["price_spread"] == 194
    assert pinecil["regions"] == ["US", "IN"]
    assert pinecil["regions_available"] == 2
    assert pinecil["first_seen"] == NOW


async def test_an_item_one_region_does_not_sell_is_still_priced_for_the_other(db):
    await ingest_shop(db, fixture_sweep(), now=NOW)

    us_only = await db.shop_items.find_one({"regions": ["US"]})
    assert us_only is not None
    assert list(us_only["prices"]) == ["US"]
    assert us_only["price_spread"] == 0


async def test_a_sale_keeps_the_price_it_was_cut_from(db):
    await ingest_shop(db, fixture_sweep(), now=NOW)

    tote = await db.shop_items.find_one({"_id": 183})
    assert tote["on_sale"] is True
    assert tote["sale_percentage"] == 30
    assert tote["prices"]["US"] == 116
    assert tote["full_prices"]["US"] == 165


async def test_a_price_change_writes_a_point_per_region(db):
    await ingest_shop(db, all_regions(lambda r: [card(1, 100)]), now=NOW)
    first = await db.shop_snapshots.find_one({"sid": 1})
    assert first["price_us"] == 100
    assert first["price_in"] == 100

    later = NOW + timedelta(hours=1)
    summary = await ingest_shop(
        db, all_regions(lambda r: [card(1, 150 if r == "IN" else 100)]), now=later
    )

    assert summary["changed"] == 1
    assert summary["snapshots"] == 1
    points = await db.shop_snapshots.find({"sid": 1}).sort([("ts", 1)]).to_list(10)
    assert [p["price_in"] for p in points] == [100, 150]
    assert [p["price_us"] for p in points] == [100, 100]


async def test_an_unchanged_catalogue_writes_nothing_new(db):
    await ingest_shop(db, all_regions(lambda r: [card(1, 100)]), now=NOW)
    summary = await ingest_shop(
        db, all_regions(lambda r: [card(1, 100)]), now=NOW + timedelta(minutes=30)
    )

    assert summary["changed"] == 0
    assert summary["snapshots"] == 0
    assert await db.shop_snapshots.count_documents({"sid": 1}) == 1


async def test_a_daily_heartbeat_lands_even_when_nothing_moves(db):
    await ingest_shop(db, all_regions(lambda r: [card(1, 100)]), now=NOW)
    later = NOW + timedelta(hours=settings.snapshot_heartbeat_hours + 1)

    summary = await ingest_shop(db, all_regions(lambda r: [card(1, 100)]), now=later)

    assert summary["snapshots"] == 1
    assert await db.shop_snapshots.count_documents({"sid": 1}) == 2


async def test_a_delisted_item_is_retired_not_deleted(db):
    await ingest_shop(db, all_regions(lambda r: [card(1, 100), card(2, 50)]), now=NOW)

    later = NOW + timedelta(hours=1)
    summary = await ingest_shop(db, all_regions(lambda r: [card(1, 100)]), now=later)

    assert summary["retired"] == 1
    gone = await db.shop_items.find_one({"_id": 2})
    assert gone["gone"] is True
    assert gone["gone_at"] == later
    assert gone["name"] == "item 2"


async def test_a_delisted_item_that_returns_is_listed_again(db):
    await ingest_shop(db, all_regions(lambda r: [card(1, 100)]), now=NOW)
    await ingest_shop(db, all_regions(lambda r: []), now=NOW)
    assert (await db.shop_items.find_one({"_id": 1}))["gone"] is True

    await ingest_shop(db, all_regions(lambda r: [card(1, 100)]), now=NOW)
    row = await db.shop_items.find_one({"_id": 1})
    assert "gone" not in row and "gone_at" not in row


async def test_a_partial_sweep_retires_nothing(db):
    await ingest_shop(db, all_regions(lambda r: [card(1, 100), card(2, 50)]), now=NOW)

    # One region loaded, and it happens not to sell item 2. That is not proof.
    summary = await ingest_shop(db, sweep({"US": [card(1, 100)]}), now=NOW)

    assert summary["complete"] is False
    assert summary["retired"] == 0
    assert (await db.shop_items.find_one({"_id": 2})).get("gone") is None


async def test_a_collapsed_catalogue_is_refused(db):
    await ingest_shop(db, all_regions(lambda r: [card(i, 10) for i in range(20)]), now=NOW)

    with pytest.raises(ShopRejected, match="fell 20 -> 1"):
        await ingest_shop(db, all_regions(lambda r: [card(1, 10)]), now=NOW)

    assert await db.shop_items.count_documents({"gone": {"$ne": True}}) == 20


async def test_an_unreadable_field_is_refused(db):
    broken = all_regions(lambda r: [card(1, 100)])
    broken["US"].missing.add("shop.price")

    with pytest.raises(ShopRejected, match="unparsed fields"):
        await ingest_shop(db, broken, now=NOW)

    assert await db.shop_items.count_documents({}) == 0


async def test_merging_keeps_the_first_region_to_render_a_field():
    merged = merge_regions(sweep({
        "US": [card(1, 100, name="Widget", purchases=5)],
        "IN": [card(1, 200, name="Widget", purchases=5)],
    }))

    row = merged[1]
    assert row["prices"] == {"US": 100, "IN": 200}
    assert row["name"] == "Widget"
    assert row["purchases"] == 5
    assert row["price_spread"] == 100
