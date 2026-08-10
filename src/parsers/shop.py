from __future__ import annotations

import re
from typing import Any

from selectolax.parser import HTMLParser, Node

from .common import (
    ParseError,
    ParseResult,
    first_text,
    id_from_path,
    text_of,
    to_int,
)

# Upstream's Shop::Regionalizable::REGIONS, in its order.
REGIONS: dict[str, str] = {
    "US": "United States",
    "EU": "EU",
    "UK": "United Kingdom",
    "IN": "India",
    "CA": "Canada",
    "AU": "Australia",
    "XX": "Rest of World",
}

REGION_CODES = tuple(REGIONS)

# One request serves the whole catalogue; the region rides on a cookie.
CATALOG_PATH = "/shop/category/all"
SHOP_PATH = "/shop"
REGION_PATH = "/shop/region"
REGION_COOKIE = "geoip_region"

# Upstream reads the cookie as a geolocation guess and treats "rest of world"
# as not having guessed, so XX has to be set on the session instead.
COOKIE_BLIND_REGIONS = ("XX",)

_CSRF_RE = re.compile(
    r'<meta[^>]+name="csrf-token"[^>]+content="([^"]+)"', re.IGNORECASE
)

_HOURS_RE = re.compile(r"~?\s*(\d+)(?:\s*-\s*(\d+))?\s*hours?")
_STOCK_RE = re.compile(r"(\d+)\s*left", re.IGNORECASE)
_GIVEN_OUT_RE = re.compile(r"([\d,]+)\s*given out", re.IGNORECASE)


def csrf_token(html: str | None) -> str | None:
    """The page's form token, needed to set a region on the session."""
    if not html:
        return None
    m = _CSRF_RE.search(html)
    return m.group(1) if m else None


def parse_shop_page(html: str, region: str) -> ParseResult:
    """Parse the catalogue as one region sees it into {region, items}."""
    region = region.upper()
    tree = HTMLParser(html)
    result = ParseResult()

    root = tree.css_first(".shop-category")
    if root is None:
        raise ParseError(f"shop {region}: no shop-category page found")

    # Trusting the cookie instead would file one region's prices under another.
    rendered = (root.attributes.get("data-shop-user-region-value") or "").upper()
    if not rendered:
        raise ParseError(f"shop {region}: page does not say which region it priced")
    if rendered != region:
        raise ParseError(f"shop {region}: page priced for {rendered} instead")

    if tree.css_first(".shop-category__items") is None:
        # "The shop is not opened yet!" renders instead of the whole listing.
        raise ParseError(f"shop {region}: catalogue did not render (shop closed?)")

    items: list[dict[str, Any]] = []
    for card in tree.css(".shop-item-card"):
        item = _parse_card(card, region, result)
        if item:
            items.append(item)

    if not items and tree.css_first(".shop-category__empty") is None:
        result.missing.add("shop.items")
    else:
        result.found.add("shop.items")

    duplicates = len(items) - len({i["_id"] for i in items})
    if duplicates:
        result.warn(f"{duplicates} duplicate item card(s) on the {region} page")

    result.data["region"] = region
    result.data["items"] = items
    return result


def _parse_card(card: Node, region: str, result: ParseResult) -> dict[str, Any] | None:
    item_id = to_int(card.attributes.get("data-shop-id"))
    if item_id is None:
        # Without an id the row cannot be merged with the other regions' prices.
        item_id = id_from_path(_href(card), segment="items")
    if item_id is None:
        result.warn("shop card with no resolvable id, skipped")
        return None

    classes = card.attributes.get("class") or ""
    price = to_int(card.attributes.get("data-price"))
    on_sale = "shop-item-card--on-sale" in classes

    # Struck through only while on sale; otherwise the price is the full price.
    original = to_int(first_text(card, ".shop-item-card__original-price"))
    if original is None and not on_sale:
        original = price

    low, high = _hours(first_text(card, ".shop-item-card__hours"))
    stock, out_of_stock = _stock(card, classes)

    item: dict[str, Any] = {
        "_id": item_id,
        "name": first_text(card, ".shop-item-card__title"),
        "description": first_text(card, ".shop-item-card__description"),
        "url": _href(card),
        "image_url": _image(card),
        "price": price,
        "full_price": original,
        "on_sale": on_sale,
        "sale_percentage": to_int(first_text(card, ".shop-item-card__sale-badge")),
        "categories": _split(card.attributes.get("data-categories")),
        # What the item itself claims, against the pages it actually appears on.
        "enabled_regions": _split(card.attributes.get("data-regions")),
        "purchases": _purchases(card),
        "is_new": card.css_first(".shop-item-card__ribbon--new") is not None,
        "remaining_stock": stock,
        "out_of_stock": out_of_stock,
        "achievement_locked": card.attributes.get("data-achievement-locked") == "true",
        # A guest has completed nothing, so this reads as "needs a mission".
        "mission_locked": "shop-item-card--mission-locked" in classes,
        "enabled_until": first_text(card, ".shop-item-card__enabled-until"),
        "hours_low": low,
        "hours_high": high,
    }

    for key in ("name", "price"):
        target = result.missing if item[key] is None else result.found
        target.add(f"shop.{key}")

    if item["enabled_regions"] and region not in item["enabled_regions"]:
        result.warn(
            f"item {item_id} rendered on the {region} page but claims "
            f"{item['enabled_regions']}"
        )

    return item


def _href(card: Node) -> str | None:
    link = card.css_first("a.shop-item-card__image-wrap")
    return link.attributes.get("href") if link else None


def _image(card: Node) -> str | None:
    img = card.css_first("img.shop-item-card__image")
    return img.attributes.get("src") if img else None


def _split(value: str | None) -> list[str]:
    return [part.strip() for part in (value or "").split(",") if part.strip()]


def _hours(label: str | None) -> tuple[int | None, int | None]:
    """'~10-19 hours' -> (10, 19); '~6 hours' -> (6, 6)."""
    if not label:
        return None, None
    m = _HOURS_RE.search(label)
    if not m:
        return None, None
    low = int(m.group(1))
    return low, int(m.group(2)) if m.group(2) else low


def _purchases(card: Node) -> int | None:
    """The ribbon reads 'N given out', but only once N is over two."""
    m = _GIVEN_OUT_RE.search(text_of(card.css_first(".shop-item-card__ribbon")) or "")
    return int(m.group(1).replace(",", "")) if m else None


def _stock(card: Node, classes: str) -> tuple[int | None, bool]:
    """Stock shows only under ten left, so absent means plenty, not unknown."""
    out_of_stock = "shop-item-card--out-of-stock" in classes
    badge = text_of(card.css_first(".shop-item-card__stock-badge"))
    if badge is None:
        return None, out_of_stock
    if "out of stock" in badge.lower():
        return 0, True
    m = _STOCK_RE.search(badge)
    return (int(m.group(1)) if m else None), out_of_stock
