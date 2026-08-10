from __future__ import annotations

import re
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo.errors import PyMongoError

from src.collector.crawl_shop import crawl_shop
from src.config import settings
from src.db import bootstrap
from src.fetcher import FetchResult
from src.parsers.shop import (
    CATALOG_PATH,
    COOKIE_BLIND_REGIONS,
    REGION_CODES,
    REGION_COOKIE,
    REGION_PATH,
    SHOP_PATH,
)

FIXTURES = Path(__file__).parent / "fixtures"
TEST_DB = "stardance_stats_test"

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


class RegionFetcher:
    """Serves each region a page that names that region, and logs the cookies."""

    def __init__(self, *, fail: set[str] | None = None, csrf: str | None = "tok"):
        self.fail = fail or set()
        self.csrf = csrf
        self.calls: list[tuple[str, dict | None]] = []
        self.session_calls: list[tuple[str, str, dict | None]] = []
        self.template = (FIXTURES / "shop_all_us.html").read_text(encoding="utf-8")

    def page_for(self, region: str) -> str:
        return self.template.replace(
            'data-shop-user-region-value="US"',
            f'data-shop-user-region-value="{region}"',
        )

    async def get(self, path, etag=None, last_modified=None, cookies=None, **kw):
        self.calls.append((path, cookies))
        region = (cookies or {}).get(REGION_COOKIE, "US")
        if region in self.fail:
            return FetchResult(path, 503, None, None, None, from_cache=False)
        return FetchResult(path, 200, self.page_for(region), None, None, from_cache=False)

    @asynccontextmanager
    async def session(self):
        yield StubSession(self)


class StubSession:
    """Stands in for the region handshake: no region until one is chosen."""

    def __init__(self, fetcher: RegionFetcher):
        self.fetcher = fetcher
        self.region = "US"

    async def get(self, path, **kw):
        self.fetcher.session_calls.append(("GET", path, None))
        if self.region in self.fetcher.fail:
            return FetchResult(path, 503, None, None, None, from_cache=False)
        # The fixture is a real page and ships a token of its own.
        body = re.sub(
            r'<meta[^>]+name="csrf-token"[^>]*>', "", self.fetcher.page_for(self.region)
        )
        if self.fetcher.csrf:
            body = f'<meta name="csrf-token" content="{self.fetcher.csrf}">' + body
        return FetchResult(path, 200, body, None, None, from_cache=False)

    async def patch(self, path, data, headers=None):
        self.fetcher.session_calls.append(("PATCH", path, data))
        if (headers or {}).get("X-CSRF-Token") != self.fetcher.csrf:
            return FetchResult(path, 422, None, None, None, from_cache=False)
        self.region = data["region"]
        return FetchResult(path, 200, "", None, None, from_cache=False)


async def test_every_region_is_asked_for_by_cookie(db):
    fetcher = RegionFetcher()

    result = await crawl_shop(db, fetcher)

    by_cookie = [r for r in REGION_CODES if r not in COOKIE_BLIND_REGIONS]
    assert result["status"] == "ok"
    assert [path for path, _ in fetcher.calls] == [CATALOG_PATH] * len(by_cookie)
    assert [c[REGION_COOKIE] for _, c in fetcher.calls] == by_cookie
    assert result["regions"] == sorted(REGION_CODES)
    assert result["complete"] is True


async def test_rest_of_world_is_chosen_on_a_session_instead(db):
    """The cookie cannot ask for XX, so it takes a token and a round trip."""
    fetcher = RegionFetcher()

    await crawl_shop(db, fetcher)

    assert fetcher.session_calls == [
        ("GET", SHOP_PATH, None),
        ("PATCH", REGION_PATH, {"region": "XX"}),
        ("GET", CATALOG_PATH, None),
    ]
    # And no XX cookie was ever sent, because it would have been ignored.
    assert "XX" not in [c[REGION_COOKIE] for _, c in fetcher.calls]

    pinecil = await db.shop_items.find_one({"_id": 8})
    assert "XX" in pinecil["prices"]


async def test_rest_of_world_without_a_token_is_dropped_not_guessed(db):
    fetcher = RegionFetcher(csrf=None)

    result = await crawl_shop(db, fetcher)

    assert "no csrf token" in result["failed"]["XX"]
    assert result["complete"] is False
    # The one thing that must not happen: US prices filed under XX.
    pinecil = await db.shop_items.find_one({"_id": 8})
    assert "XX" not in pinecil["prices"]


async def test_one_page_per_region_prices_every_region(db):
    await crawl_shop(db, RegionFetcher())

    pinecil = await db.shop_items.find_one({"_id": 8})
    assert sorted(pinecil["prices"]) == sorted(REGION_CODES)
    # The stub serves one body to all seven, so the spread is flat by construction.
    assert pinecil["price_spread"] == 0
    assert await db.shop_snapshots.count_documents({"sid": 8}) == 1


async def test_a_region_that_fails_does_not_retire_the_rest(db):
    await crawl_shop(db, RegionFetcher())
    held = await db.shop_items.count_documents({})

    result = await crawl_shop(db, RegionFetcher(fail={"IN", "CA"}))

    assert result["status"] == "ok"
    assert sorted(result["failed"]) == ["CA", "IN"]
    assert result["complete"] is False
    assert result["retired"] == 0
    assert await db.shop_items.count_documents({"gone": {"$ne": True}}) == held


async def test_a_sweep_that_reaches_nothing_is_reported_not_written(db):
    result = await crawl_shop(db, RegionFetcher(fail=set(REGION_CODES)))

    assert result["status"] == "fetch_error"
    assert await db.shop_items.count_documents({}) == 0


async def test_a_page_priced_for_the_wrong_region_is_dropped(db):
    class StuckFetcher(RegionFetcher):
        async def get(self, path, etag=None, last_modified=None, cookies=None, **kw):
            # The cookie is ignored upstream and every region renders as US.
            self.calls.append((path, cookies))
            return FetchResult(path, 200, self.template, None, None, from_cache=False)

    fetcher = StuckFetcher()
    result = await crawl_shop(db, fetcher)

    assert result["status"] == "ok"
    # Only the cookie regions are stuck; XX comes off its own session.
    assert sorted(result["failed"]) == sorted(
        set(REGION_CODES) - {"US", *COOKIE_BLIND_REGIONS}
    )
    assert all("priced for US" in reason for reason in result["failed"].values())

    pinecil = await db.shop_items.find_one({"_id": 8})
    assert sorted(pinecil["prices"]) == ["US", "XX"]


async def test_the_shop_crawl_can_be_switched_off(db):
    settings.crawl_shop = False
    try:
        assert await crawl_shop(db, RegionFetcher()) == {"status": "disabled"}
    finally:
        settings.crawl_shop = True
    assert await db.shop_items.count_documents({}) == 0


async def test_a_single_region_can_be_crawled_on_its_own(db):
    fetcher = RegionFetcher()

    result = await crawl_shop(db, fetcher, regions=("IN",))

    assert [c[REGION_COOKIE] for _, c in fetcher.calls] == ["IN"]
    assert result["regions"] == ["IN"]
    assert result["complete"] is False
