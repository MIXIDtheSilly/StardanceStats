from __future__ import annotations

import asyncio

from src.collector import run
from src.collector.run import concurrency_for, crawl_rows
from src.config import settings
from src.fetcher import Endpoint, Fetcher, RateLimiter


class FakeFetcher:
    def __init__(self, routes: int) -> None:
        self.endpoints = [
            Endpoint(name=f"r{i}", limiter=RateLimiter(1.5), kind="proxy",
                     proxy_url=f"http://192.0.2.{i}:3128") for i in range(routes)
        ]


def rows(n: int, kind: str = "project") -> list[dict]:
    return [{"_id": f"{kind}:{i}", "kind": kind, "ref_id": i} for i in range(n)]


def test_concurrency_defaults_to_two_per_route():
    """One per route idles every route through its own task's parse."""
    settings.crawl_concurrency = 0
    assert concurrency_for(FakeFetcher(11)) == 22
    assert concurrency_for(FakeFetcher(1)) == 2


def test_explicit_concurrency_overrides_the_default():
    settings.crawl_concurrency = 5
    try:
        assert concurrency_for(FakeFetcher(11)) == 5
    finally:
        settings.crawl_concurrency = 0


async def test_rows_are_crawled_concurrently(monkeypatch):
    """The point of the change: 12 crawls of 0.1s each must not take 1.2s."""
    in_flight = 0
    peak = 0

    async def fake_crawl(db, fetcher, row, **kwargs):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.05)
        in_flight -= 1
        return {"status": "ok", "linked_users": []}

    monkeypatch.setattr(run, "crawl_one", fake_crawl)

    loop = asyncio.get_running_loop()
    started = loop.time()
    result = await crawl_rows(None, FakeFetcher(4), rows(12), concurrency=8)
    elapsed = loop.time() - started

    assert result["crawled"] == 12
    assert result["statuses"] == {"ok": 12}
    assert peak > 1, "crawls ran one at a time"
    assert peak <= 8, f"exceeded the concurrency limit: {peak}"
    assert elapsed < 0.5, f"serialised: {elapsed:.2f}s for 12 x 0.05s"


async def test_user_totals_are_recomputed_once_per_batch(monkeypatch):
    """Each recompute rescans every row that user owns, and they race."""
    recomputed: list[int] = []

    async def fake_crawl(db, fetcher, row, **kwargs):
        assert kwargs.get("defer_user_totals") is True
        return {"status": "ok", "linked_users": [1234, 5678]}

    async def fake_recompute(db, user_id):
        recomputed.append(user_id)

    monkeypatch.setattr(run, "crawl_one", fake_crawl)
    monkeypatch.setattr(run, "recompute_user_totals", fake_recompute)

    result = await crawl_rows(None, FakeFetcher(2), rows(10), concurrency=4)

    assert result["crawled"] == 10
    assert sorted(recomputed) == [1234, 5678]
    assert result["users_recomputed"] == 2


async def test_one_crash_does_not_take_down_the_batch(monkeypatch):
    async def fake_crawl(db, fetcher, row, **kwargs):
        if row["ref_id"] == 3:
            raise RuntimeError("parser exploded")
        return {"status": "ok", "linked_users": []}

    monkeypatch.setattr(run, "crawl_one", fake_crawl)
    result = await crawl_rows(None, FakeFetcher(2), rows(6), concurrency=3)

    assert result["statuses"] == {"ok": 5, "crashed": 1}
    assert result["stopped"] == "complete"


async def test_an_open_circuit_abandons_the_rest_of_the_batch(monkeypatch):
    """Draining a batch into a struggling origin only deepens the hole."""
    from src.fetcher import CircuitOpen

    attempts = 0

    async def fake_crawl(db, fetcher, row, **kwargs):
        nonlocal attempts
        attempts += 1
        raise CircuitOpen("paused")

    monkeypatch.setattr(run, "crawl_one", fake_crawl)
    result = await crawl_rows(None, FakeFetcher(2), rows(50), concurrency=2)

    assert result["stopped"] == "circuit_open"
    assert attempts < 50, f"kept going after the circuit opened ({attempts} tries)"


async def test_an_empty_batch_is_not_an_error():
    assert (await crawl_rows(None, FakeFetcher(2), []))["crawled"] == 0


async def test_the_pool_paces_concurrent_crawls_at_the_aggregate_rate(monkeypatch):
    """Concurrency must not defeat the limiters: 2 routes at 5/s stay near 10/s."""
    fetcher = Fetcher(proxies=["http://198.51.100.7:1", "http://198.51.100.8:1"])
    for endpoint in fetcher.endpoints_ref:
        endpoint.limiter = RateLimiter(5.0)
    fetcher._open = True

    async def fake_crawl(db, f, row, **kwargs):
        # What get() does: claim a slot, then wait for its turn.
        _, wait = await fetcher._claim()
        if wait > 0:
            await asyncio.sleep(wait)
        return {"status": "ok", "linked_users": []}

    monkeypatch.setattr(run, "crawl_one", fake_crawl)

    loop = asyncio.get_running_loop()
    started = loop.time()
    await crawl_rows(None, fetcher, rows(20), concurrency=20)
    elapsed = loop.time() - started

    # 20 requests over 2 routes at 5/s each = ~2s of reservations.
    assert 1.4 < elapsed < 2.6, f"paced at the wrong rate: {elapsed:.2f}s"
