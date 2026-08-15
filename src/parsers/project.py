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
    rich_text,
    strip_handle,
    text_of,
    to_float,
    to_int,
)

POST_TYPE_DEVLOG = "Post::Devlog"
POST_TYPE_SHIP = "Post::ShipEvent"
# Upstream schema calls this "fire"; rendered UI calls it "Super Star".
POST_TYPE_FIRE = "Post::FireEvent"

_SHIP_NUMBER_RE = re.compile(r"Ship\s*#(\d+)")
_MISSION_SLUG_RE = re.compile(r"/missions/([^/?#\"]+)")
_COMMENTS_ID_RE = re.compile(r"comments_count_post_devlog_(\d+)")

# A ceiling, not an expectation: the longest post we have seen runs to a few hundred.
BODY_LIMIT = 20_000
_BLESSING_RE = re.compile(r"\b(blessed|cursed)\b", re.IGNORECASE)


def parse_project_page(html: str, project_id: int) -> ParseResult:
    """Parse a project page into {project, devlogs, ships}."""
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
    super_stars: list[dict[str, Any]] = []
    unknown_cards = 0

    # Match on data-* hooks, not layout: upstream restyles more than it rewires.
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
        elif post_type == POST_TYPE_FIRE:
            super_stars.append(_parse_super_star_card(card))
        elif post_type is None:
            unknown_cards += 1

    if unknown_cards:
        result.warn(f"{unknown_cards} feed card(s) had no post-type attribute")

    # The "Ship #N" label is authoritative; fall back to chronological order.
    if ships and not any(s.get("ship_number") for s in ships):
        for n, ship in enumerate(sorted(ships, key=lambda s: s["shipped_at"] or 0), 1):
            ship["ship_number"] = n

    _apply_super_star(project, super_stars, result)
    _apply_mission(project, tree, ships, result)

    result.data["devlogs"] = devlogs
    result.data["ships"] = ships
    result.data["project"] = project

    _cross_check(project, devlogs, ships, result)
    return result


def mission_slug(href: str | None) -> str | None:
    m = _MISSION_SLUG_RE.search(href or "")
    return m.group(1) if m else None


def _parse_mission(tree: HTMLParser) -> dict[str, Any] | None:
    """The mission a project is currently attached to, or None."""
    panel = tree.css_first(".mission-panel")
    if panel is None:
        return None

    link = panel.css_first(".mission-panel__title-link")
    classes = panel.attributes.get("class") or ""
    bar = panel.css_first(".mission-panel__progress-bar")

    return {
        "slug": mission_slug(link.attributes.get("href") if link else None),
        "name": text_of(link),
        # Set by a non-rejected submission; the attachment then stays for display.
        "shipped": "mission-panel--shipped" in classes,
        # Guide progress renders only before the first ship.
        "sections_done": to_int(bar.attributes.get("value")) if bar else None,
        "sections_total": to_int(bar.attributes.get("max")) if bar else None,
    }


def _apply_mission(
    project: dict[str, Any], tree: HTMLParser, ships: list[dict[str, Any]], result: ParseResult
) -> None:
    """Settle the mission fields from the panel, cross-checked against ships."""
    mission = _parse_mission(tree)
    project["mission"] = mission

    ship_slugs = {s["mission_slug"] for s in ships if s.get("mission_slug")}
    if mission is None:
        # No panel and a renamed class look identical; a ship naming one catches it.
        if ship_slugs:
            result.warn(f"ships name mission(s) {sorted(ship_slugs)} but no panel rendered")
        return

    result.found.add("mission_panel")
    if mission["slug"] is None:
        result.missing.add("mission_slug")
    if ship_slugs and mission["slug"] not in ship_slugs:
        result.warn(
            f"panel mission {mission['slug']!r} not among shipped missions {sorted(ship_slugs)}"
        )


def _parse_super_star_card(card: Node) -> dict[str, Any]:
    """A Super Star marking, posted into the timeline as its own card."""
    time_node = card.css_first(".feed-post-card__time")
    return {
        "post_id": to_int(card.attributes.get("data-feed-engagement-post-id-value")),
        "marked_at": parse_datetime(
            time_node.attributes.get("datetime") if time_node else None
        ),
        "marked_by": strip_handle(first_text(card, ".feed-post-card__author")),
        "note": first_text(card, ".feed-post-card__super-star-body"),
    }


def _super_star_badge(tree: HTMLParser) -> bool:
    """Presence-only, so read it two ways before believing it is absent."""
    if tree.css_first(".project-show__badge--fire") is not None:
        return True
    return any(
        "super star" in (text_of(node) or "").lower()
        for node in tree.css(".project-show__badge")
    )


def _apply_super_star(
    project: dict[str, Any], cards: list[dict[str, Any]], result: ParseResult
) -> None:
    """Settle the Super Star fields from the badge and the timeline together."""
    dated = [c for c in cards if c.get("marked_at")]
    latest = max(dated, key=lambda c: c["marked_at"], default=None)
    badge = project.pop("_super_star_badge", False)

    project["is_super_star"] = bool(badge or cards)
    project["super_star_at"] = latest["marked_at"] if latest else None
    project["super_star_by"] = latest["marked_by"] if latest else None
    project["super_star_note"] = latest["note"] if latest else None

    if badge:
        result.found.add("super_star_badge")
    elif cards:
        result.warn("Super Star event card present but the header badge is not")


def _parse_header(tree: HTMLParser, project_id: int, result: ParseResult) -> dict[str, Any]:
    project: dict[str, Any] = {"_id": project_id}

    project["title"] = first_text(tree, ".project-show__title")
    result.set("title", project["title"])

    project["description"] = first_text(tree, ".project-show__description")
    project["_super_star_badge"] = _super_star_badge(tree)

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

    # A bare "By" is an answer; the byline vanishing is a selector break.
    byline = tree.css_first(".project-show__authors")
    members = []
    for link in tree.css(".project-show__author"):
        handle = strip_handle(text_of(link))
        if handle:
            members.append(handle)
    project["members"] = members
    project["owner_username"] = members[0] if members else None
    result.set_optional(
        "owner_username", project["owner_username"], present=byline is not None
    )
    if byline is not None and not members:
        result.warn("byline rendered with no author; project has no visible owner")

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

    # Two routes to the id; either surviving a redesign keeps identity stable.
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
        "body": _body(card, result),
        "media": _media(card, result),
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


def _media(card: Node, result: ParseResult) -> list[dict[str, str]]:
    """The card's attachments, in carousel order. Most devlogs have one; some none."""
    items: list[dict[str, str]] = []
    for slide in card.css(".feed-post-card__media-slide"):
        video = slide.css_first("video")
        if video is not None:
            source = video.css_first("source")
            url = video.attributes.get("src") or (
                source.attributes.get("src") if source else None
            )
            if url:
                item = {"kind": "video", "url": url}
                poster = video.attributes.get("poster")
                if poster:
                    item["poster"] = poster
                items.append(item)
            continue

        image = slide.css_first("img")
        if image is not None and image.attributes.get("src"):
            items.append({"kind": "image", "url": image.attributes["src"]})
            continue

        # A slide we cannot read is a renamed tag, not an empty post.
        result.warn("media slide held neither an image nor a video")

    if items:
        result.found.add("devlog.media")
    return items


def _body(card: Node, result: ParseResult) -> str | None:
    """The whole post. Upstream clamps long ones with CSS, so the card holds it all."""
    text = rich_text(card.css_first(".feed-post-card__body"))
    if text and len(text) > BODY_LIMIT:
        result.warn(f"devlog body of {len(text)} characters cut to {BODY_LIMIT}")
        return text[:BODY_LIMIT]
    return text


def _ship_body(card: Node) -> str | None:
    """Upstream simple_formats this into paragraphs, then sits them tight with margin: 0."""
    text = rich_text(card.css_first(".project-show__latest-ship-text"))
    return re.sub(r"\n{2,}", "\n", text) if text else text


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

    meta, meta_rows = _ship_meta(card)
    status, blessing = _ship_pills(card)
    mission_link = card.css_first(".project-show__latest-ship-mission-link")

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
        "mission": text_of(mission_link),
        "mission_slug": mission_slug(
            mission_link.attributes.get("href") if mission_link else None
        ),
        "body": _ship_body(card),
    }

    # Devlogs and hours render on every ship card.
    for key in ("hours_at_ship", "devlogs_at_ship"):
        target = result.missing if ship[key] is None else result.found
        target.add(f"ship.{key}")

    # These render only once review closes, so missing is a real state.
    for key in ("multiplier", "payout"):
        if ship[key] is not None:
            result.found.add(f"ship.{key}")
        elif key in meta_rows:
            result.missing.add(f"ship.{key}")

    return ship


def _ship_meta(card: Node) -> tuple[dict[str, float | int], set[str]]:
    """Read the ship totals row, matching on class and wording both."""
    out: dict[str, float | int] = {}
    rows: set[str] = set()
    for item in card.css(".profile-project-card__meta-item"):
        classes = item.attributes.get("class") or ""
        title = (item.attributes.get("title") or "").lower()
        text = text_of(item) or ""
        low = text.lower()

        if "--multiplier" in classes or "multiplier" in low:
            rows.add("multiplier")
            value = to_float(text)
            if value is not None:
                out["multiplier"] = value
        elif "--payout" in classes or "payout" in title or "stardust" in low:
            rows.add("payout")
            value = to_int(text)
            if value is not None:
                out["payout"] = value
        elif "--time" in classes or re.search(r"\d+\s*h\b", low):
            rows.add("hours")
            value = to_float(text)
            if value is not None:
                out["hours"] = value
        elif "devlog" in low:
            rows.add("devlogs")
            value = to_int(text)
            if value is not None:
                out["devlogs"] = value
    return out, rows


def _ship_pills(card: Node) -> tuple[str, str | None]:
    """Split the status-pill row into (certification status, payout blessing)."""
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
            # Mission pills are member-only, so a guest crawl never lands here.
            status = low

    return status or "approved", blessing


def _cross_check(
    project: dict[str, Any],
    devlogs: list[dict[str, Any]],
    ships: list[dict[str, Any]],
    result: ParseResult,
) -> None:
    """Compare the page's own totals against what we summed from the cards."""
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
