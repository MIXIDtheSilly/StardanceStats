from __future__ import annotations

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient
from pymongo.errors import ExecutionTimeout

from src.api.main import app
from src.api.routers import ask as router
from src.api.services.ask import runner
from src.api.services.ask.guard import AskError
from src.config import settings

pytestmark = pytest.mark.asyncio

PIPELINE = [{"$group": {"_id": None, "words": {"$sum": 1}}}]


class FakeCursor:
    def __init__(self, held: float, fails: Exception | None):
        self.held = held
        self.fails = fails

    async def to_list(self, length):
        await asyncio.sleep(self.held)
        if self.fails:
            raise self.fails
        return [{"words": 1}]


class FakeCollection:
    def __init__(self, ran: list, held: float, fails: Exception | None):
        self.ran = ran
        self.held = held
        self.fails = fails

    def aggregate(self, pipeline, **options):
        self.ran.append(pipeline)
        return FakeCursor(self.held, self.fails)


class FakeDb(dict):
    def __init__(self, ran, held, fails):
        super().__init__()
        self.ran, self.held, self.fails = ran, held, fails

    def __getitem__(self, name):
        return FakeCollection(self.ran, self.held, self.fails)


def fake_client(ran, *, held=0.0, fails=None):
    db = FakeDb(ran, held, fails)
    return lambda: {settings.mongo_db: db}


@pytest.fixture(autouse=True)
def one_at_a_time(monkeypatch):
    """The gate is built once from the setting, so each test gets a fresh one."""
    monkeypatch.setattr(runner, "_gate", None)
    monkeypatch.setattr(settings, "ask_concurrency", 1)
    monkeypatch.setattr(settings, "ask_queue_wait", 0.05)
    yield
    runner._gate = None


async def test_a_second_question_waits_rather_than_doubling_the_work(monkeypatch):
    ran: list = []
    monkeypatch.setattr(runner, "get_client", fake_client(ran, held=0.02))
    first, second = await asyncio.gather(
        runner.run("devlogs", PIPELINE, limit=10),
        runner.run("devlogs", PIPELINE, limit=10),
    )
    assert first == second == [{"words": 1}]
    assert len(ran) == 2


async def test_a_queue_that_will_not_clear_is_refused_not_queued(monkeypatch):
    ran: list = []
    monkeypatch.setattr(runner, "get_client", fake_client(ran, held=0.5))
    slow = asyncio.create_task(runner.run("devlogs", PIPELINE, limit=10))
    await asyncio.sleep(0.01)
    with pytest.raises(runner.QueryBusy):
        await runner.run("devlogs", PIPELINE, limit=10)
    await slow
    # The refused one never reached the database.
    assert len(ran) == 1


async def test_the_gate_reopens_after_a_query_fails(monkeypatch):
    ran: list = []
    monkeypatch.setattr(
        runner, "get_client", fake_client(ran, fails=ExecutionTimeout("too slow"))
    )
    for _ in range(2):
        with pytest.raises(runner.QueryTooSlow):
            await runner.run("devlogs", PIPELINE, limit=10)
    assert len(ran) == 2


async def test_a_query_that_ran_out_of_time_says_how_to_narrow_it(monkeypatch):
    monkeypatch.setattr(
        runner, "get_client", fake_client([], fails=ExecutionTimeout("too slow"))
    )
    with pytest.raises(runner.QueryTooSlow) as raised:
        await runner.run("devlogs", PIPELINE, limit=10)
    assert "narrow it" in str(raised.value)
    # It is still an AskError, so nothing that only knows that breaks.
    assert isinstance(raised.value, AskError)


@pytest.fixture
def client():
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setattr(settings, "ask_api_key", "key")
    monkeypatch.setattr(settings, "ask_mongo_url", "mongodb://localhost/x")
    monkeypatch.setattr(settings, "ask_rate_limit", 10)
    monkeypatch.setattr(settings, "ask_retries", 1)
    router._asked.clear()
    yield
    router._asked.clear()


@pytest.fixture(autouse=True)
def logged(monkeypatch):
    rows: list[dict] = []

    async def keep(entry):
        rows.append(entry)

    monkeypatch.setattr(router, "record", keep)
    return rows


def planner(calls: list):
    async def plan(question, **kwargs):
        calls.append(question)
        return {"collection": "devlogs", "pipeline": PIPELINE, "title": "Words"}, "{}"

    return plan


async def test_a_costly_question_is_not_asked_a_second_time(
    client, logged, monkeypatch
):
    """A repair turn would send the same scan back to the database."""
    planned: list = []
    monkeypatch.setattr(router, "plan", planner(planned))

    async def too_slow(collection, pipeline, *, limit):
        raise runner.QueryTooSlow("that question reads more than this will do; narrow it")

    monkeypatch.setattr(router, "run", too_slow)

    async with client as http:
        response = await http.post("/v1/ask", json={"question": "count every word"})

    assert response.status_code == 422
    assert len(planned) == 1
    assert logged[0]["outcome"] == "too_costly"


async def test_a_busy_database_answers_at_once_rather_than_planning_again(
    client, logged, monkeypatch
):
    planned: list = []
    monkeypatch.setattr(router, "plan", planner(planned))

    async def busy(collection, pipeline, *, limit):
        raise runner.QueryBusy("too many questions are being answered at once")

    monkeypatch.setattr(router, "run", busy)

    async with client as http:
        response = await http.post("/v1/ask", json={"question": "count every word"})

    assert response.status_code == 503
    assert len(planned) == 1
    assert logged[0]["outcome"] == "busy"
