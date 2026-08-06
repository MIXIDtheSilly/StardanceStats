from __future__ import annotations

import re
from typing import Any

from selectolax.parser import HTMLParser, Node

from .common import ParseError, ParseResult, first_text, text_of, to_float, to_int

# Upstream's enum has only the first two; the third postdates the source we hold.
PAYOUT_VOTING = "voting"
PAYOUT_STATIC = "static_prize"
PAYOUT_FLAT = "flat_rate"

_MISSION_ID_RE = re.compile(r'id="mission-patch-top-(\d+)-')
_STARDUST_RE = re.compile(r"^([\d,]+)\s+stardust\b", re.IGNORECASE)
_PER_HOUR_RE = re.compile(r"([\d.]+)[^\d]{0,24}?per hour", re.IGNORECASE)
_DURATION_RE = re.compile(r"(\d+)\s*(hr|min)")
_SLUG_RE = re.compile(r"/missions/([^/?#\"]+)")


def parse_mission_page(html: str, slug: str) -> ParseResult:
    """Parse a mission page into {mission}."""
    tree = HTMLParser(html)
    result = ParseResult()

    hero = tree.css_first(".mission-home__hero")
    name = first_text(tree, ".mission-home__hero-title")
    if hero is None and name is None:
        raise ParseError(f"mission {slug}: no mission-home hero or title found")

    mission_id = _MISSION_ID_RE.search(html)
    banner = tree.css_first(".mission-home__windowed-banner")

    mission: dict[str, Any] = {
        "_id": slug,
        "slug": slug,
        "mission_id": int(mission_id.group(1)) if mission_id else None,
        "name": name,
        "description": first_text(tree, ".mission-home__hero-brief"),
        "difficulty": first_text(tree, ".mission-home__hero-chip--difficulty"),
        "is_hardware": tree.css_first(".mission-home__hero-chip--hardware") is not None,
        "estimated_label": first_text(tree, ".mission-home__hero-chip--time"),
        "guide_sections": to_int(first_text(tree, ".mission-home__hero-chip--sections")),
        "prerequisites": _prerequisites(tree),
        "availability_note": text_of(banner),
        "available": banner is None,
        "criteria": [
            t for t in (text_of(n) for n in tree.css(".mission-home__criterion-text")) if t
        ],
    }
    mission["estimated_minutes"] = _minutes(mission["estimated_label"])
    mission["prizes"] = _prizes(tree)
    mission["unlocks"] = [p["unlocks"] for p in mission["prizes"] if p["unlocks"]]
    mission.update(_gallery(tree))
    mission.update(_payout(tree, mission["prizes"], result))

    result.set("name", mission["name"])
    result.set("description", mission["description"])
    result.set("payout_path", mission["payout_path"])

    result.data["mission"] = mission
    return result


def mission_slug(href: str | None) -> str | None:
    m = _SLUG_RE.search(href or "")
    return m.group(1) if m else None


def _minutes(label: str | None) -> int | None:
    """'~1 hr 30 min' -> 90."""
    if not label:
        return None
    parts = _DURATION_RE.findall(label)
    if not parts:
        return None
    return sum(int(n) * (60 if unit == "hr" else 1) for n, unit in parts)


def _prerequisites(tree: HTMLParser) -> list[str]:
    """Guests never meet a prerequisite, so the locked panel always lists them."""
    slugs = [mission_slug(a.attributes.get("href")) for a in tree.css(".mission-home__locked-link")]
    return [s for s in slugs if s]


def _joined(node: Node | None) -> str | None:
    if node is None:
        return None
    return " ".join(node.text(separator=" ").split()) or None


def _closest_li(node: Node | None) -> Node | None:
    while node is not None and node.tag != "li":
        node = node.parent
    return node


def _prizes(tree: HTMLParser) -> list[dict[str, Any]]:
    """Every reward tile bar the rating-status one, in rendered order."""
    listing = tree.css_first(".mission-home__prize-list")
    if listing is None:
        return []

    prizes: list[dict[str, Any]] = []
    stage: str | None = None

    for item in listing.css("li"):
        classes = item.attributes.get("class") or ""
        if "mission-home__prize-group-label" in classes:
            stage = text_of(item)
            continue
        if "mission-home__prize" not in classes:
            continue
        if item.css_first(".mission-home__rating-icon") is not None:
            continue

        title_node = item.css_first(".mission-home__prize-title")
        link = title_node.css_first('a[href*="/missions/"]') if title_node else None
        prizes.append({
            # Separated, or "Unlocks <a>WebOS 2</a>" runs the two words together.
            "title": _joined(title_node),
            "blurb": first_text(item, ".mission-home__prize-blurb"),
            "stage": stage,
            "unlocks": mission_slug(link.attributes.get("href")) if link else None,
        })

    return prizes


def _gallery(tree: HTMLParser) -> dict[str, Any]:
    """The showcase strip, capped at 12 by upstream."""
    entries = []
    for item in tree.css(".mission-home__gallery-item"):
        card = item.css_first("[data-feed-engagement-project-id-value]")
        if card is None:
            continue
        project_id = to_int(card.attributes.get("data-feed-engagement-project-id-value"))
        if project_id is None:
            continue
        badge = item.css_first(".mission-home__gallery-badge")
        entries.append({
            "project_id": project_id,
            "approved": "--approved" in (badge.attributes.get("class") or "") if badge else None,
        })
    return {
        "gallery": entries,
        "gallery_truncated": tree.css_first(".mission-home__gallery-show-more") is not None,
    }


def _payout(
    tree: HTMLParser, prizes: list[dict[str, Any]], result: ParseResult
) -> dict[str, Any]:
    """Which of the three ways this mission pays, and at what rate."""
    out: dict[str, Any] = {
        "payout_path": None,
        "rated": None,
        "fixed_stardust": None,
        "stardust_per_hour": None,
    }

    icon = tree.css_first(".mission-home__rating-icon")
    if icon is None:
        rate = _flat_rate(prizes)
        if rate is None:
            # A flat-rate mission renders its own tile, so neither showing is a break.
            result.warn("no rating status and no per-hour tile; payout markup changed")
            return out
        out["payout_path"] = PAYOUT_FLAT
        out["rated"] = False
        out["stardust_per_hour"] = rate
        result.found.add("flat_rate_tile")
        return out

    result.found.add("rating_status")
    rated = "mission-home__rating-icon--excluded" not in (icon.attributes.get("class") or "")
    out["rated"] = rated
    out["payout_path"] = PAYOUT_VOTING if rated else PAYOUT_STATIC

    row = _closest_li(icon)
    label = first_text(row, ".mission-home__prize-title") if row is not None else None
    tone = "mint" if rated else "salmon"
    said_rated = (label or "").lower().startswith("goes into rating")
    if label and said_rated is not rated:
        result.warn(f"rating icon says rated={rated} but the tile says {label!r}")
    row_classes = row.attributes.get("class") or "" if row is not None else ""
    if row is not None and f"mission-home__prize--{tone}" not in row_classes:
        result.warn(f"rating tile is not toned {tone} for rated={rated}")

    if rated:
        return out

    out["fixed_stardust"] = _fixed_stardust(tree, prizes, result)
    if out["fixed_stardust"] is None:
        result.missing.add("fixed_stardust")
    return out


def _fixed_stardust(
    tree: HTMLParser, prizes: list[dict[str, Any]], result: ParseResult
) -> int | None:
    """The flat award, read from the prize tile and the modal both."""
    from_tile = next(
        (to_int(p["title"]) for p in prizes if _STARDUST_RE.match(p["title"] or "")), None
    )
    from_modal = to_int(first_text(tree, ".rating-info-modal__text strong"))

    if from_tile is not None and from_modal is not None and from_tile != from_modal:
        result.warn(f"fixed payout disagrees: tile={from_tile} modal={from_modal}")
    return from_tile if from_tile is not None else from_modal


def _flat_rate(prizes: list[dict[str, Any]]) -> float | None:
    for prize in prizes:
        m = _PER_HOUR_RE.search(prize["blurb"] or "")
        if m:
            return to_float(m.group(1))
    return None
