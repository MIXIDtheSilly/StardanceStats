from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.responses import ORJSONResponse
from httpx import ASGITransport, AsyncClient

from src.api.middleware import cache_headers
from src.api.services.counting import cached_count
from src.config import settings

pytestmark = pytest.mark.asyncio


@pytest.fixture
def reads():
    """How many times the endpoint behind the cache actually ran."""
    return []


@pytest.fixture
def client(reads):
    app = FastAPI(default_response_class=ORJSONResponse)
    app.middleware("http")(cache_headers)

    @app.get("/v1/thing", response_model=None)
    async def thing(page: int = 1):
        reads.append(page)
        return {"page": page, "reads": len(reads)}

    @app.get("/v1/health", response_model=None)
    async def health():
        reads.append("health")
        return {"status": "ok"}

    @app.get("/elsewhere", response_model=None)
    async def elsewhere():
        reads.append("elsewhere")
        return {"ok": True}

    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


async def test_a_repeat_read_never_reaches_the_endpoint(client, reads):
    async with client as http:
        first = await http.get("/v1/thing")
        second = await http.get("/v1/thing")
    assert first.json() == second.json()
    assert reads == [1]


async def test_the_body_still_says_what_it_is(client):
    async with client as http:
        fresh = await http.get("/v1/thing")
        held = await http.get("/v1/thing")
    assert fresh.headers["content-type"].startswith("application/json")
    assert held.headers["content-type"].startswith("application/json")


async def test_a_different_query_is_a_different_read(client, reads):
    async with client as http:
        await http.get("/v1/thing", params={"page": 1})
        await http.get("/v1/thing", params={"page": 2})
    assert reads == [1, 2]


async def test_the_order_of_the_query_does_not_split_the_entry(client, reads):
    async with client as http:
        await http.get("/v1/thing?page=2")
        await http.get("/v1/thing?page=2")
    assert reads == [2]


async def test_a_client_holding_the_body_is_told_so(client):
    async with client as http:
        fresh = await http.get("/v1/thing")
        again = await http.get(
            "/v1/thing", headers={"if-none-match": fresh.headers["etag"]}
        )
    assert again.status_code == 304
    assert not again.content


async def test_nothing_outside_the_api_is_held(client, reads):
    async with client as http:
        await http.get("/elsewhere")
        await http.get("/elsewhere")
    assert reads == ["elsewhere", "elsewhere"]


async def test_health_is_held_only_as_long_as_its_own_setting(
    client, reads, monkeypatch
):
    """The container polls it to prove Mongo answers, so it may not be stale."""
    monkeypatch.setattr(settings, "api_cache_health_seconds", 0)
    async with client as http:
        await http.get("/v1/health")
        await http.get("/v1/health")
        await http.get("/v1/thing")
        await http.get("/v1/thing")
    assert reads == ["health", "health", 1]


async def test_turning_the_cache_off_reaches_the_endpoint_every_time(
    client, reads, monkeypatch
):
    monkeypatch.setattr(settings, "api_cache_seconds", 0)
    async with client as http:
        await http.get("/v1/thing")
        await http.get("/v1/thing")
    assert reads == [1, 1]


class FakeCollection:
    def __init__(self, counted):
        self.counted = counted

    async def count_documents(self, query):
        self.counted.append(query)
        return len(self.counted)


class FakeDb:
    def __init__(self):
        self.counted: list[dict] = []

    def __getitem__(self, name):
        return FakeCollection(self.counted)


async def test_a_total_is_counted_once_for_every_page_of_it():
    db = FakeDb()
    first = await cached_count(db, "users", {"hidden": {"$ne": True}})
    second = await cached_count(db, "users", {"hidden": {"$ne": True}})
    assert first == second == 1
    assert len(db.counted) == 1


async def test_a_different_query_is_counted_on_its_own():
    db = FakeDb()
    await cached_count(db, "users", {"a": 1})
    await cached_count(db, "users", {"a": 2})
    await cached_count(db, "projects", {"a": 1})
    assert len(db.counted) == 3


async def test_the_cache_can_be_turned_off(monkeypatch):
    monkeypatch.setattr(settings, "api_cache_seconds", 0)
    db = FakeDb()
    await cached_count(db, "users", {"a": 1})
    await cached_count(db, "users", {"a": 1})
    assert len(db.counted) == 2
