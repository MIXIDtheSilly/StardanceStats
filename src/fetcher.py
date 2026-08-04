from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass

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

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            wait = self._next_at - now
            if wait > 0:
                await asyncio.sleep(wait)
                now = time.monotonic()
            self._next_at = now + self._interval


class Fetcher:
    """Async HTTP client with rate limiting, retries and a circuit breaker."""

    RETRY_STATUSES = {429, 500, 502, 503, 504}

    def __init__(
        self,
        *,
        base_url: str | None = None,
        rps: float | None = None,
        breaker_threshold: int = 10,
        breaker_cooldown: float = 900.0,
    ) -> None:
        self.base_url = (base_url or settings.base_url).rstrip("/")
        self._limiter = RateLimiter(rps if rps is not None else settings.requests_per_second)
        self._semaphore = asyncio.Semaphore(settings.max_concurrency)
        self._client: httpx.AsyncClient | None = None
        self._consecutive_failures = 0
        self._breaker_threshold = breaker_threshold
        self._breaker_cooldown = breaker_cooldown
        self._breaker_until = 0.0

    async def __aenter__(self) -> Fetcher:
        self._client = httpx.AsyncClient(
            http2=True,
            timeout=settings.request_timeout,
            follow_redirects=True,
            # Leave Accept-Encoding to httpx; it advertises only the codecs
            # actually installed. Hard-coding "br" yields undecodable bodies.
            headers={
                "User-Agent": settings.user_agent,
                "Accept": "text/html,application/xhtml+xml",
            },
        )
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    def _url(self, path: str) -> str:
        if path.startswith("http"):
            return path
        return f"{self.base_url}/{path.lstrip('/')}"

    async def get(
        self, path: str, *, etag: str | None = None, last_modified: str | None = None
    ) -> FetchResult:
        if self._client is None:
            raise FetchError("Fetcher used outside its async context manager")

        if time.monotonic() < self._breaker_until:
            raise CircuitOpen(
                f"circuit open for another {self._breaker_until - time.monotonic():.0f}s"
            )

        url = self._url(path)
        headers: dict[str, str] = {}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified

        last_exc: Exception | None = None
        for attempt in range(settings.max_retries):
            await self._limiter.acquire()
            try:
                async with self._semaphore:
                    response = await self._client.get(url, headers=headers)
            except httpx.HTTPError as exc:
                last_exc = exc
                await self._backoff(attempt)
                continue

            if response.status_code == 304:
                self._on_success()
                return FetchResult(url, 304, None, etag, last_modified, from_cache=True)

            if response.status_code in self.RETRY_STATUSES:
                retry_after = _retry_after(response)
                log.warning(
                    "%s -> %s (attempt %d/%d)",
                    url, response.status_code, attempt + 1, settings.max_retries,
                )
                await self._backoff(attempt, floor=retry_after)
                last_exc = FetchError(f"{url} -> {response.status_code}")
                continue

            if response.status_code >= 400:
                # 404/410 are real answers about the resource, not failures of
                # ours, so they must not trip the breaker.
                self._on_success()
                return FetchResult(url, response.status_code, None, None, None, False)

            if len(response.content) > settings.max_response_bytes:
                raise FetchError(f"{url}: response exceeds {settings.max_response_bytes} bytes")

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


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None
