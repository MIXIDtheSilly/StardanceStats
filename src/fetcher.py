from __future__ import annotations

import asyncio
import logging
import random
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator
from urllib.parse import urlsplit

import httpx

from .config import settings

log = logging.getLogger(__name__)


class FetchError(Exception):
    """Non-retryable, or retries exhausted."""


class CircuitOpen(FetchError):
    """Too many consecutive failures, stop hammering a struggling host."""


@dataclass
class FetchResult:
    url: str
    status: int
    text: str | None
    etag: str | None
    last_modified: str | None
    from_cache: bool  # True on a 304: body unchanged, nothing to re-parse

    @property
    def ok(self) -> bool:
        return self.status == 200 and self.text is not None


class RateLimiter:
    """Single-token bucket: at most one request every 1/rps seconds."""

    def __init__(self, rps: float) -> None:
        self._interval = 1.0 / rps if rps > 0 else 0.0
        self._lock = asyncio.Lock()
        self._next_at = 0.0

    @property
    def rps(self) -> float:
        return 1.0 / self._interval if self._interval else 0.0

    @property
    def ready_at(self) -> float:
        """Monotonic time this limiter can next issue a request."""
        return self._next_at

    def reserve(self) -> float:
        """Claim the next slot and return how long to wait before using it."""
        now = time.monotonic()
        start = max(now, self._next_at)
        self._next_at = start + self._interval
        return start - now

    async def acquire(self) -> None:
        async with self._lock:
            wait = self.reserve()
        if wait > 0:
            await asyncio.sleep(wait)


@dataclass
class Endpoint:
    """One route upstream: this machine, or a forward proxy."""

    name: str
    limiter: RateLimiter
    kind: str = "direct"
    proxy_url: str | None = None
    client: httpx.AsyncClient | None = None
    failures: int = 0
    disabled_until: float = 0.0
    requests: int = 0

    @property
    def is_proxy(self) -> bool:
        return self.proxy_url is not None

    def available_at(self) -> float:
        return max(self.limiter.ready_at, self.disabled_until)


class Session:
    """A cookie-keeping conversation over one route, paced but not retried."""

    def __init__(self, fetcher: Fetcher, endpoint: Endpoint, client: httpx.AsyncClient):
        self._fetcher = fetcher
        self._endpoint = endpoint
        self._client = client

    async def get(self, path: str, **kwargs: Any) -> FetchResult:
        return await self._send("GET", path, **kwargs)

    async def patch(
        self, path: str, data: dict[str, str], *, headers: dict[str, str] | None = None
    ) -> FetchResult:
        return await self._send("PATCH", path, data=data, headers=headers)

    async def _send(
        self,
        method: str,
        path: str,
        *,
        data: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> FetchResult:
        await self._endpoint.limiter.acquire()
        self._endpoint.requests += 1
        url = self._fetcher._url(path)
        try:
            response = await self._client.request(
                method, url, data=data, headers=headers or {}
            )
        except httpx.HTTPError as exc:
            raise FetchError(f"{url}: {exc}") from exc

        text = response.text if response.status_code < 400 else None
        return FetchResult(
            url=url,
            status=response.status_code,
            text=text,
            etag=response.headers.get("ETag"),
            last_modified=response.headers.get("Last-Modified"),
            from_cache=False,
        )


class Fetcher:
    """Async HTTP client with rate limiting, retries and a circuit breaker."""

    RETRY_STATUSES = {429, 500, 502, 503, 504}

    def __init__(
        self,
        *,
        base_url: str | None = None,
        rps: float | None = None,
        proxies: list[str] | None = None,
        breaker_threshold: int = 10,
        breaker_cooldown: float = 900.0,
    ) -> None:
        self.base_url = (base_url or settings.base_url).rstrip("/")
        self._endpoints = _build_endpoints(
            proxies if proxies is not None else settings.proxy_list,
            direct_rps=rps if rps is not None else settings.requests_per_second,
        )
        self._pick_lock = asyncio.Lock()
        # One in flight per route; the rest queue on the limiters.
        self._semaphore = asyncio.Semaphore(
            max(settings.max_concurrency, len(self._endpoints))
        )
        self._open = False
        self._consecutive_failures = 0
        self._breaker_threshold = breaker_threshold
        self._breaker_cooldown = breaker_cooldown
        self._breaker_until = 0.0

    @property
    def endpoints(self) -> list[Endpoint]:
        return list(self._endpoints)

    @property
    def endpoints_ref(self) -> list[Endpoint]:
        """Live endpoint objects, for tests that stub their clients."""
        return self._endpoints

    @property
    def aggregate_rps(self) -> float:
        """What upstream experiences, split across buckets only if the routes differ."""
        return sum(e.limiter.rps for e in self._endpoints)

    async def _claim(self) -> tuple[Endpoint, float]:
        """Pick the route that can go soonest and book its slot."""
        async with self._pick_lock:
            endpoint = min(self._endpoints, key=lambda e: e.available_at())
            cooldown = endpoint.disabled_until - time.monotonic()
            wait = endpoint.limiter.reserve()
            endpoint.requests += 1
            return endpoint, max(wait, cooldown)

    def _endpoint_failed(self, endpoint: Endpoint) -> None:
        endpoint.failures += 1
        if endpoint.is_proxy and endpoint.failures >= settings.proxy_failure_threshold:
            endpoint.disabled_until = time.monotonic() + settings.proxy_failure_cooldown
            endpoint.failures = 0
            log.warning(
                "proxy %s parked for %.0fs after repeated failures",
                endpoint.name, settings.proxy_failure_cooldown,
            )

    def _endpoint_ok(self, endpoint: Endpoint) -> None:
        endpoint.failures = 0

    def _park(self, endpoint: Endpoint, seconds: float) -> None:
        endpoint.disabled_until = time.monotonic() + seconds
        log.warning("route %s parked %.0fs (throttled)", endpoint.name, seconds)

    async def __aenter__(self) -> Fetcher:
        # httpx binds a forward proxy at construction, so each route needs one.
        for endpoint in self._endpoints:
            endpoint.client = self._new_client(endpoint)
        self._open = True
        return self

    async def __aexit__(self, *exc: object) -> None:
        for endpoint in self._endpoints:
            if endpoint.client is not None:
                await endpoint.client.aclose()
                endpoint.client = None
        self._open = False

    def _new_client(self, endpoint: Endpoint) -> httpx.AsyncClient:
        # Accept-Encoding is httpx's: hard-coding "br" gives undecodable bodies.
        kwargs: dict[str, Any] = {
            "timeout": settings.request_timeout,
            "follow_redirects": True,
            "headers": {
                "User-Agent": settings.user_agent,
                "Accept": "text/html,application/xhtml+xml",
            },
        }
        if endpoint.proxy_url:
            kwargs["proxy"] = endpoint.proxy_url
            # HTTP/2 over a CONNECT tunnel is where proxies are flakiest.
            kwargs["http2"] = False
        else:
            kwargs["http2"] = True

        try:
            return httpx.AsyncClient(**kwargs)
        except ImportError as exc:  # socks5:// without the socksio extra
            raise FetchError(
                f"{endpoint.name}: {exc}. Install the SOCKS extra: "
                'pip install "httpx[socks]"'
            ) from exc

    def _url(self, path: str) -> str:
        if path.startswith("http"):
            return path
        return f"{self.base_url}/{path.lstrip('/')}"

    @asynccontextmanager
    async def session(self) -> AsyncIterator[Session]:
        """Borrow one route for a conversation with its own, discarded cookies."""
        if not self._open:
            raise FetchError("Fetcher used outside its async context manager")

        # A preference left in a shared jar would re-answer every later crawl.
        async with self._pick_lock:
            endpoint = min(self._endpoints, key=lambda e: e.available_at())

        client = self._new_client(endpoint)
        try:
            yield Session(self, endpoint, client)
        finally:
            await client.aclose()

    async def get(
        self,
        path: str,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
        max_bytes: int | None = None,
        accept: str | None = None,
        cookies: dict[str, str] | None = None,
    ) -> FetchResult:
        if not self._open:
            raise FetchError("Fetcher used outside its async context manager")

        if time.monotonic() < self._breaker_until:
            raise CircuitOpen(
                f"circuit open for another {self._breaker_until - time.monotonic():.0f}s"
            )

        url = self._url(path)
        limit = max_bytes if max_bytes is not None else settings.max_response_bytes
        headers: dict[str, str] = {}
        if accept:
            headers["Accept"] = accept
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified

        last_exc: Exception | None = None
        for attempt in range(settings.max_retries):
            endpoint, wait = await self._claim()
            if wait > 0:
                await asyncio.sleep(wait)

            try:
                async with self._semaphore:
                    # Per request, not on the client: routes are shared.
                    response = await endpoint.client.get(
                        url, headers=headers, cookies=cookies
                    )
            except httpx.HTTPError as exc:
                # A dead or unauthenticated proxy surfaces here, not as a status.
                last_exc = exc
                self._endpoint_failed(endpoint)
                await self._backoff(attempt)
                continue

            self._endpoint_ok(endpoint)

            if response.status_code == 304:
                self._on_success()
                return FetchResult(url, 304, None, etag, last_modified, from_cache=True)

            if response.status_code in self.RETRY_STATUSES:
                retry_after = _retry_after(response)
                log.warning(
                    "%s via %s -> %s (attempt %d/%d)",
                    url, endpoint.name, response.status_code, attempt + 1, settings.max_retries,
                )
                last_exc = FetchError(f"{url} -> {response.status_code}")

                # The throttle is per address, so this is about the route.
                if response.status_code == 429 and len(self._endpoints) > 1:
                    self._park(endpoint, retry_after or settings.proxy_failure_cooldown)
                    continue

                await self._backoff(attempt, floor=retry_after)
                continue

            if response.status_code >= 400:
                # An answer about the resource, not a failure of ours.
                self._on_success()
                return FetchResult(url, response.status_code, None, None, None, False)

            if len(response.content) > limit:
                raise FetchError(f"{url}: response exceeds {limit} bytes")

            self._on_success()
            return FetchResult(
                url=url,
                status=response.status_code,
                text=response.text,
                etag=response.headers.get("ETag"),
                last_modified=response.headers.get("Last-Modified"),
                from_cache=False,
            )

        self._on_failure()
        raise FetchError(f"{url}: giving up after {settings.max_retries} attempts") from last_exc

    async def _backoff(self, attempt: int, floor: float | None = None) -> None:
        delay = min(60.0, 2.0**attempt) + random.uniform(0, 1.0)
        if floor:
            delay = max(delay, floor)
        await asyncio.sleep(delay)

    def _on_success(self) -> None:
        self._consecutive_failures = 0

    def _on_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._breaker_threshold:
            self._breaker_until = time.monotonic() + self._breaker_cooldown
            self._consecutive_failures = 0
            log.error("circuit breaker tripped; pausing %.0fs", self._breaker_cooldown)


def _build_endpoints(proxies: list[str], *, direct_rps: float) -> list[Endpoint]:
    """One endpoint per route. Nothing configured means one direct route."""
    if not proxies:
        return [Endpoint(name="direct", limiter=RateLimiter(direct_rps))]
    return [
        Endpoint(
            name=_proxy_name(url),
            kind="proxy",
            limiter=RateLimiter(settings.proxy_requests_per_second),
            proxy_url=url,
        )
        for url in proxies
    ]


def _proxy_name(url: str) -> str:
    """Host and port, credentials stripped, since this reaches the logs."""
    parts = urlsplit(url)
    host = parts.hostname or url
    return f"{host}:{parts.port}" if parts.port else host


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None
