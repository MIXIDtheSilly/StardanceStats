from __future__ import annotations

import hashlib
import time
from collections import OrderedDict

from starlette.requests import Request
from starlette.responses import Response

from ..config import settings

# The container's liveness probe, so it may only be held between two polls of it.
HEALTH_PATH = "/v1/health"

# path?query -> (expires, etag, body, content type)
_pages: OrderedDict[str, tuple[float, str, bytes, str | None]] = OrderedDict()


def clear() -> None:
    _pages.clear()


def _ttl(path: str) -> int:
    if path == HEALTH_PATH:
        return settings.api_cache_health_seconds
    return settings.api_cache_seconds


def _key(request: Request) -> str:
    query = sorted(request.query_params.multi_items())
    return request.url.path + "?" + "&".join(f"{k}={v}" for k, v in query)


def _stored(key: str, now: float) -> tuple[str, bytes, str | None] | None:
    held = _pages.get(key)
    if held is None:
        return None
    if held[0] <= now:
        del _pages[key]
        return None
    _pages.move_to_end(key)
    return held[1], held[2], held[3]


def _keep(key: str, ttl: int, etag: str, body: bytes, kind: str | None) -> None:
    if len(body) > settings.api_cache_max_bytes:
        return
    _pages[key] = (time.monotonic() + ttl, etag, body, kind)
    _pages.move_to_end(key)
    while len(_pages) > settings.api_cache_entries:
        _pages.popitem(last=False)


def _served(
    request: Request, ttl: int, etag: str, body: bytes, kind: str | None
) -> Response:
    """The body, or a 304 when the client already holds this exact one."""
    # Nothing here is user-specific, and the client may hold it for as long as we do.
    headers = {"etag": etag, "cache-control": f"public, max-age={ttl}"}
    if _matches(request.headers.get("if-none-match"), etag):
        return Response(status_code=304, headers=headers)
    return Response(content=body, status_code=200, headers=headers, media_type=kind)


async def cache_headers(request: Request, call_next):
    """Serve reads from memory for their stated lifetime, with an ETag on top."""
    ttl = _ttl(request.url.path)

    if ttl > 0 and request.method == "GET" and request.url.path.startswith("/v1/"):
        held = _stored(_key(request), time.monotonic())
        if held is not None:
            return _served(request, ttl, *held)

    response = await call_next(request)

    if request.method not in ("GET", "HEAD") or response.status_code != 200:
        return response
    if not request.url.path.startswith("/v1/"):
        return response

    body = b"".join([chunk async for chunk in response.body_iterator])
    etag = '"' + hashlib.sha256(body).hexdigest()[:32] + '"'
    # call_next hands back a stream, which carries its type in the header alone.
    kind = response.headers.get("content-type")

    if ttl > 0:
        _keep(_key(request), ttl, etag, body, kind)

    return _served(request, ttl, etag, body, kind)


def _matches(header: str | None, etag: str) -> bool:
    """RFC 9110 If-None-Match: a list, and `*` matches anything we hold."""
    if not header:
        return False
    candidates = {value.strip() for value in header.split(",")}
    return "*" in candidates or etag in candidates or f"W/{etag}" in candidates
