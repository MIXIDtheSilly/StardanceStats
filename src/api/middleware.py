from __future__ import annotations

import hashlib

from starlette.requests import Request
from starlette.responses import Response

from ..config import settings

# Nothing here is user-specific, and a crawl is minutes apart at best.
CACHE_CONTROL = f"public, max-age={settings.api_cache_seconds}"


async def cache_headers(request: Request, call_next):
    """ETag + Cache-Control on reads, and a 304 when the client already has it."""
    response = await call_next(request)

    if request.method not in ("GET", "HEAD") or response.status_code != 200:
        return response
    if not request.url.path.startswith("/v1/"):
        return response

    body = b"".join([chunk async for chunk in response.body_iterator])
    etag = '"' + hashlib.sha256(body).hexdigest()[:32] + '"'

    headers = dict(response.headers)
    headers["etag"] = etag
    headers["cache-control"] = CACHE_CONTROL

    if _matches(request.headers.get("if-none-match"), etag):
        headers.pop("content-length", None)
        return Response(status_code=304, headers=headers)

    return Response(
        content=body,
        status_code=response.status_code,
        headers=headers,
        media_type=response.media_type,
    )


def _matches(header: str | None, etag: str) -> bool:
    """RFC 9110 If-None-Match: a list, and `*` matches anything we hold."""
    if not header:
        return False
    candidates = {value.strip() for value in header.split(",")}
    return "*" in candidates or etag in candidates or f"W/{etag}" in candidates
