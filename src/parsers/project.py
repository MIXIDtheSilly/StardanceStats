from __future__ import annotations

import re
from typing import Any

from selectolax.parser import HTMLParser, Node

from .common import (
    ParseError,
    ParseResult,
    first_text,
    id_from_path,
    parse_datetime,
    parse_duration_seconds,
    strip_handle,
    text_of,
    to_float,
    to_int,
)

POST_TYPE_DEVLOG = "Post::Devlog"
POST_TYPE_SHIP = "Post::ShipEvent"

_SHIP_NUMBER_RE = re.compile(r"Ship\s*#(\d+)")
_COMMENTS_ID_RE = re.compile(r"comments_count_post_devlog_(\d+)")
_BLESSING_RE = re.compile(r"\b(blessed|cursed)\b", re.IGNORECASE)


def parse_project_page(html: str, project_id: int) -> ParseResult:
    """Parse a project page into {project, devlogs, ships}.

    Raises ParseError when the document is not recognisably a project page, so
    an error page is never persisted as an empty project.
    """
    tree = HTMLParser(html)
    result = ParseResult()

    header = tree.css_first(".project-show__panel--read") or tree.css_first(
        ".project-show__panel"
    )
    title = first_text(tree, ".project-show__title")
    if header is None and title is None:
        raise ParseError(f"project {project_id}: no project-show panel or title found")

    project = _parse_header(tree, project_id, result)

    devlogs: list[dict[str, Any]] = []
    ships: list[dict[str, Any]] = []
    unknown_cards = 0

    # Selectors target semantic hooks (data-*, BEM classes), not layout
    # position, since upstream restyles more often than it renames wiring.
    for card in tree.css("article.feed-post-card"):
        post_type = card.attributes.get("data-feed-engagement-post-type-value")
        if post_type == POST_TYPE_DEVLOG:
            devlog = _parse_devlog_card(card, project_id, result)
            if devlog:
                devlogs.append(devlog)
        elif post_type == POST_TYPE_SHIP or "project-show__latest-ship" in (
            card.attributes.get("class") or ""
        ):
            ship = _parse_ship_card(card, project_id, result)
            if ship:
                ships.append(ship)
        elif post_type is None:
            unknown_cards += 1

    if unknown_cards:
        result.warn(f"{unknown_cards} feed card(s) had no post-type attribute")

    # The "Ship #N" label is authoritative; fall back to chronological order.
    if ships and not any(s.get("ship_number") for s in ships):
        for n, ship in enumerate(sorted(ships, key=lambda s: s["shipped_at"] or 0), 1):
            ship["ship_number"] = n

    result.data["devlogs"] = devlogs
    result.data["ships"] = ships
    result.data["project"] = project

    _cross_check(project, devlogs, ships, result)
    return result


def _parse_header(tree: HTMLParser, project_id: int, result: ParseResult) -> dict[str, Any]:
    project: dict[str, Any] = {"_id": project_id}

    project["title"] = first_text(tree, ".project-show__title")
    result.set("title", project["title"])

    project["description"] = first_text(tree, ".project-show__description")

    stats = _labelled_stats(
        tree, ".project-show__stats-item", ".project-show__stats-num", ".project-show__stats-label"
    )
    project["devlogs_count"] = stats.get("devlogs")
    project["total_hours"] = stats.get("total hours")
    result.set("devlogs_count", project["devlogs_count"])
    result.set("total_hours", project["total_hours"])

    followers = to_int(first_text(tree, ".project-show__followers"))
    project["followers"] = followers
    result.set("followers", followers)

    members = []
    for link in tree.css(".project-show__author"):
        handle = strip_handle(text_of(link))
        if handle:
            members.append(handle)
    project["members"] = members
    project["owner_username"] = members[0] if members else None
    result.set("owner_username", project["owner_username"])

    banner = tree.css_first(".project-show__banner-image")
    project["banner_url"] = banner.attributes.get("src") if banner else None

    project["is_hardware"] = bool(tree.css_first(".project-show__tag--hardware"))

    repo = tree.css_first("a.project-show__pill--github")
    project["repo_url"] = repo.attributes.get("href") if repo else None

    demo = tree.css_first("a.project-show__latest-ship-btn--primary")
    project["demo_url"] = demo.attributes.get("href") if demo else None

    avatar = tree.css_first(".project-show__avatar")
    project["owner_avatar_url"] = avatar.attributes.get("src") if avatar else None

    og = tree.css_first('meta[property="og:description"]')
    project["og_description"] = og.attributes.get("content") if og else None

    return project


def _labelled_stats(
    tree: HTMLParser, item_sel: str, num_sel: str, label_sel: str
) -> dict[str, int]:
    """Read stat tiles keyed by their label, so an added tile shifts nothing."""
    out: dict[str, int] = {}
    for item in tree.css(item_sel):
        label = first_text(item, label_sel)
        value = to_int(first_text(item, num_sel))
        if label and value is not None:
            out[label.lower()] = value
    return out


def _parse_devlog_card(
    card: Node, project_id: int, result: ParseResult
) -> dict[str, Any] | None:
    post_id = to_int(card.attributes.get("data-feed-engagement-post-id-value"))

    # Two independent routes to the devlog id; either one surviving a redesign
    # is enough to keep identity stable.
    devlog_id = None
    counts_node = card.css_first('[id^="comments_count_post_devlog_"]')
    if counts_node:
        m = _COMMENTS_ID_RE.search(counts_node.attributes.get("id") or "")
        if m:
            devlog_id = int(m.group(1))
    if devlog_id is None:
        devlog_id = id_from_path(
            card.attributes.get("data-card-link-url-value"), segment="devlogs"
        )

    if devlog_id is None and post_id is None:
        result.warn("devlog card with no resolvable id, skipped")
        return None

    time_node = card.css_first(".feed-post-card__time")
    posted_at = parse_datetime(time_node.attributes.get("datetime") if time_node else None)

    devlog: dict[str, Any] = {
        "_id": devlog_id if devlog_id is not None else -post_id,
        "post_id": post_id,
        "project_id": project_id,
        "username": strip_handle(first_text(card, ".feed-post-card__author")),
        "posted_at": posted_at,
        "duration_seconds": parse_duration_seconds(
            first_text(card, ".feed-post-card__duration")
        ),
        "likes": to_int(first_text(card, ".like-button__count")),
        "comments": to_int(text_of(counts_node)) if counts_node else None,
        "reposts": to_int(first_text(card, ".feed-post-card__repost")),
        "views": _views(card),
        "body_preview": _body_preview(card),
    }

    for key in ("likes", "comments", "reposts", "views", "duration_seconds", "posted_at"):
        if devlog[key] is None:
            result.missing.add(f"devlog.{key}")
        else:
            result.found.add(f"devlog.{key}")

    return devlog


def _views(card: Node) -> int | None:
    """Views live in an aria-label ("Seen by 8 people"), not a class of its own."""
    for node in card.css(".feed-post-card__action"):
        label = node.attributes.get("aria-label") or ""
        if label.startswith("Seen by"):
            return to_int(label)
    return None


def _body_preview(card: Node, limit: int = 280) -> str | None:
    body = card.css_first(".feed-post-card__body")
    text = text_of(body)
    if not text:
        return None
    return text[:limit]


def _parse_ship_card(
    card: Node, project_id: int, result: ParseResult
) -> dict[str, Any] | None:
    post_id = to_int(card.attributes.get("data-feed-engagement-post-id-value"))
    if post_id is None:
        result.warn("ship card with no post id, skipped")
        return None

    label = first_text(card, ".project-show__latest-ship-label")
    m = _SHIP_NUMBER_RE.search(label or "")
    ship_number = int(m.group(1)) if m else None

    time_node = card.css_first(".feed-post-card__time")
    shipped_at = parse_datetime(time_node.attributes.get("datetime") if time_node else None)

    meta = _ship_meta(card)
    status, blessing = _ship_pills(card)

    ship: dict[str, Any] = {
        "_id": post_id,
        "post_id": post_id,
        "project_id": project_id,
        "ship_number": ship_number,
        "username": strip_handle(first_text(card, ".feed-post-card__author")),
        "shipped_at": shipped_at,
        "devlogs_at_ship": meta.get("devlogs"),
        "hours_at_ship": meta.get("hours"),
        "multiplier": meta.get("multiplier"),
        "payout": meta.get("payout"),
        "status": status,
        # "blessed" (+20% payout), "cursed" (-50%), or None for neutral.
        "payout_blessing": blessing,
        "mission": first_text(card, ".project-show__latest-ship-mission-link"),
        "body": first_text(card, ".project-show__latest-ship-text"),
    }

    for key in ("hours_at_ship", "multiplier", "payout", "devlogs_at_ship"):
        if ship[key] is None:
            result.missing.add(f"ship.{key}")
        else:
            result.found.add(f"ship.{key}")

    return ship


def _ship_meta(card: Node) -> dict[str, float | int]:
    """Read the ship totals row.

    Each item is matched on both its modifier class and its wording, so a class
    rename alone does not lose the value.
    """
    out: dict[str, float | int] = {}
    for item in card.css(".profile-project-card__meta-item"):
        classes = item.attributes.get("class") or ""
        title = (item.attributes.get("title") or "").lower()
        text = text_of(item) or ""
        low = text.lower()

        if "--multiplier" in classes or "multiplier" in low:
            value = to_float(text)
            if value is not None:
                out["multiplier"] = value
        elif "--payout" in classes or "payout" in title or "stardust" in low:
            value = to_int(text)
            if value is not None:
                out["payout"] = value
        elif "--time" in classes or re.search(r"\d+\s*h\b", low):
            value = to_float(text)
            if value is not None:
                out["hours"] = value
        elif "devlog" in low:
            value = to_int(text)
            if value is not None:
                out["devlogs"] = value
    return out


def _ship_pills(card: Node) -> tuple[str, str | None]:
    """Split the status-pill row into (certification status, payout blessing).

    Both pills share the latest-ship-status class and a blessing reuses the
    --approved / --returned modifiers, so the class alone cannot tell them
    apart and a blessed ship would report its status as "blessed". The blessing
    is identified by its wording instead, which appears in both the title
    attribute and the pill text.

    No status pill means no pending/returned marker, i.e. approved.
    """
    status: str | None = None
    blessing: str | None = None

    for pill in card.css("[class*=latest-ship-status]"):
        text = text_of(pill) or ""
        title = pill.attributes.get("title") or ""

        match = _BLESSING_RE.search(title) or _BLESSING_RE.search(text)
        if match:
            blessing = match.group(1).lower()
            continue

        if status is not None:
            continue
        low = text.lower()
        if "pending" in low:
            status = "pending"
        elif "changes requested" in low or "returned" in low:
            status = "returned"
        elif low:
            # Mission pills only render for members and admins, so a guest
            # crawl should never reach here. Keep the text rather than drop it.
            status = low

    return status or "approved", blessing


def _cross_check(
    project: dict[str, Any],
    devlogs: list[dict[str, Any]],
    ships: list[dict[str, Any]],
    result: ParseResult,
) -> None:
    """Compare the page's own totals against what we summed from the cards.

    Small drift is normal (deleted devlogs stay in the counter, hours are
    rounded for display), so this warns rather than fails. A large gap is the
    earliest signal that a selector has gone stale.
    """
    header_count = project.get("devlogs_count")
    if header_count is not None and devlogs:
        if abs(header_count - len(devlogs)) > max(2, header_count * 0.1):
            result.warn(
                f"devlog count drift: header={header_count} parsed={len(devlogs)}"
            )

    summed = sum(d["duration_seconds"] or 0 for d in devlogs) / 3600.0
    header_hours = project.get("total_hours")
    if header_hours and summed:
        if abs(header_hours - summed) > max(2.0, header_hours * 0.15):
            result.warn(f"hours drift: header={header_hours} summed={summed:.1f}")

    project["parsed_devlogs"] = len(devlogs)
    project["parsed_ships"] = len(ships)
