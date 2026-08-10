from __future__ import annotations

import logging
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase

from ..config import settings
from ..fetcher import Fetcher, FetchError
from ..ingest import ShopRejected, ingest_shop
from ..parsers import ParseError, parse_shop_page
from ..parsers.common import ParseResult, utcnow
from ..parsers.shop import (
    CATALOG_PATH,
    COOKIE_BLIND_REGIONS,
    REGION_CODES,
    REGION_COOKIE,
    REGION_PATH,
    SHOP_PATH,
    csrf_token,
)

log = logging.getLogger(__name__)


async def crawl_shop(
    db: AsyncIOMotorDatabase,
    fetcher: Fetcher,
    *,
    regions: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Read the catalogue once per region. Expected outcomes are returned, not raised."""
    if not settings.crawl_shop:
        return {"status": "disabled"}

    regions = regions or REGION_CODES
    results: dict[str, ParseResult] = {}
    failed: dict[str, str] = {}

    for region in regions:
        outcome = await _fetch_region(fetcher, region)
        if isinstance(outcome, ParseResult):
            results[region] = outcome
        else:
            failed[region] = outcome
            log.warning("shop %s: %s", region, outcome)

    if not results:
        return {"status": "fetch_error", "failed": failed}

    try:
        summary = await ingest_shop(
            db, results, now=utcnow(), complete=not failed and set(regions) == set(REGION_CODES)
        )
    except ShopRejected as exc:
        log.error("shop sweep rejected: %s", exc)
        return {"status": "anomaly", "error": str(exc), "failed": failed}

    log.info(
        "shop: %d items across %d region(s), %d changed, %d snapshot(s)%s",
        summary["items"], len(summary["regions"]), summary["changed"],
        summary["snapshots"], f", {len(failed)} region(s) failed" if failed else "",
    )
    return {"status": "ok", **summary, "failed": failed}


async def _fetch_region(fetcher: Fetcher, region: str) -> ParseResult | str:
    """One region's catalogue, or a string saying why not."""
    try:
        if region in COOKIE_BLIND_REGIONS:
            html = await _catalog_via_session(fetcher, region)
        else:
            # No ETag and must-revalidate, so every sweep pays for the body.
            response = await fetcher.get(CATALOG_PATH, cookies={REGION_COOKIE: region})
            if not response.ok:
                return f"http {response.status}"
            html = response.text
    except FetchError as exc:
        return str(exc)

    try:
        return parse_shop_page(html, region)
    except ParseError as exc:
        return f"parse: {exc}"


async def _catalog_via_session(fetcher: Fetcher, region: str) -> str:
    """Set the region on a throwaway session, then read the catalogue with it."""
    async with fetcher.session() as session:
        landing = await session.get(SHOP_PATH)
        token = csrf_token(landing.text)
        if token is None:
            raise FetchError(f"no csrf token on {SHOP_PATH} (http {landing.status})")

        chosen = await session.patch(
            REGION_PATH, {"region": region}, headers={"X-CSRF-Token": token}
        )
        if chosen.status != 200:
            raise FetchError(f"selecting {region} -> http {chosen.status}")

        catalog = await session.get(CATALOG_PATH)
        if not catalog.ok:
            raise FetchError(f"http {catalog.status}")
        return catalog.text
