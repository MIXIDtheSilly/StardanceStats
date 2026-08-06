from __future__ import annotations

import re
from typing import Any

from dateutil import parser as dateparser
from selectolax.parser import HTMLParser

from .common import (
    ParseError,
    ParseResult,
    first_text,
    strip_handle,
    text_of,
    to_int,
)

_ORDINAL_RE = re.compile(r"(\d+)(st|nd|rd|th)\b")
_JOINED_RE = re.compile(r"^\s*Joined\s+", re.IGNORECASE)
_CACHET_RE = re.compile(r"cachet\.dunkirk\.sh/users/([A-Z0-9]+)", re.IGNORECASE)
_OG_USER_ID_RE = re.compile(r"/users/(\d+)")
# A profile's og_title is "@handle | Stardance"; other pages get "title - Stardance".
_OG_HANDLE_RE = re.compile(r"^@(.+?)\s*\|")
_PROJECT_HREF_RE = re.compile(r"/projects/(\d+)")
_STREAK_RE = re.compile(r"(\d+)\s*-?\s*day\s+streak", re.IGNORECASE)

STAT_LABELS = ("devlogs", "projects", "ships", "votes")


def parse_user_page(html: str, user_id: int | None = None) -> ParseResult:
    """Parse a profile page into a user document."""
    tree = HTMLParser(html)
    result = ParseResult()

    # Banned and unverified users get a placeholder page that still names them.
    handle = strip_handle(first_text(tree, ".profile__handle"))
    if handle is None:
        handle = strip_handle(first_text(tree, ".profile-placeholder__handle"))
    if handle is None:
        handle = _handle_from_og(tree)
    if handle is None:
        raise ParseError(f"user {user_id}: no handle found")

    canonical_id = _id_from_og(tree)
    if user_id is None:
        if canonical_id is None:
            raise ParseError(f"user @{handle}: no numeric id in og tags")
        user_id = canonical_id
    elif canonical_id is not None and canonical_id != user_id:
        result.warn(f"og id {canonical_id} does not match requested id {user_id}")

    user: dict[str, Any] = {"_id": user_id, "username": handle}
    result.set("username", handle)

    stats_block = tree.css_first(".profile__stats")
    user["hidden"] = stats_block is None

    user["bio"] = first_text(tree, ".profile__bio")
    user["joined_at"] = _joined_at(tree)
    user["banner_url"] = _attr(tree, ".profile__banner-image", "src")

    avatar = _attr(tree, ".profile__avatar", "src") or _attr(
        tree, ".profile-placeholder__avatar", "src"
    )
    user["avatar_url"] = avatar
    slack = _CACHET_RE.search(avatar or "")
    user["slack_id"] = slack.group(1) if slack else None
    user["avatar_kind"] = "slack" if slack else "guest"

    stats = _labelled_stats(tree)
    for label in STAT_LABELS:
        value = stats.get(label)
        user[f"{label}_count"] = value
        if not user["hidden"]:
            result.set(f"{label}_count", value)

    following, followers = _follow_counts(tree)
    user["followers"] = followers
    user["following"] = following
    if not user["hidden"]:
        result.set("followers", followers)
        result.set("following", following)

    streak = _streak(tree)
    user["streak"] = streak
    if not user["hidden"]:
        result.set("streak", streak)

    earned, total = _achievements(tree)
    user["achievements_earned"] = earned
    user["achievements_total"] = total

    project_ids = _project_ids(tree)
    user["project_ids"] = project_ids
    if project_ids is not None and not user["hidden"]:
        result.set("project_ids", project_ids)

    result.data["user"] = user
    return result


def _streak(tree: HTMLParser) -> int | None:
    """Days in the user's current posting streak."""
    badge = tree.css_first(".streak-badge")
    if badge is None:
        return 0

    for source in (
        badge.attributes.get("aria-label"),
        badge.attributes.get("data-tooltip-message-value"),
        text_of(badge),
    ):
        match = _STREAK_RE.search(source or "")
        if match:
            return int(match.group(1))
    return None


def _project_ids(tree: HTMLParser) -> list[int] | None:
    """Every project the user belongs to, or None if this is not that tab."""
    panel = tree.css_first('.profile-tab-content[aria-label="Projects"]')
    if panel is None:
        return None

    ids: set[int] = set()
    for card in panel.css("a.profile-project-card"):
        match = _PROJECT_HREF_RE.search(card.attributes.get("href") or "")
        if match:
            ids.add(int(match.group(1)))
    return sorted(ids)


def _handle_from_og(tree: HTMLParser) -> str | None:
    og = tree.css_first('meta[property="og:title"]')
    content = og.attributes.get("content") if og else None
    if not content:
        return None
    # Match the shape, not just split: a plain page title would otherwise pass as one.
    match = _OG_HANDLE_RE.match(content.strip())
    return strip_handle(match.group(1)) if match else None


def _id_from_og(tree: HTMLParser) -> int | None:
    for selector, attr in (
        ('meta[property="og:url"]', "content"),
        ('meta[property="og:image"]', "content"),
    ):
        node = tree.css_first(selector)
        m = _OG_USER_ID_RE.search((node.attributes.get(attr) or "") if node else "")
        if m:
            return int(m.group(1))
    return None


def _attr(tree: HTMLParser, selector: str, name: str) -> str | None:
    node = tree.css_first(selector)
    return node.attributes.get(name) if node else None


def _joined_at(tree: HTMLParser):
    raw = first_text(tree, ".profile__joined")
    if not raw:
        return None
    cleaned = _ORDINAL_RE.sub(r"\1", _JOINED_RE.sub("", raw))
    try:
        return dateparser.parse(cleaned)
    except (ValueError, OverflowError):
        return None


def _labelled_stats(tree: HTMLParser) -> dict[str, int]:
    out: dict[str, int] = {}
    for item in tree.css(".profile__stat"):
        label = first_text(item, ".profile__stat-label")
        value = to_int(first_text(item, ".profile__stat-num"))
        if label and value is not None:
            out[label.lower()] = value
    return out


def _follow_counts(tree: HTMLParser) -> tuple[int | None, int | None]:
    """Read the follow pills by their wording, since both share one class."""
    following = followers = None
    for node in tree.css(".profile__follow-count"):
        text = (text_of(node) or "").lower()
        if "follower" in text:
            followers = to_int(text)
        elif "following" in text:
            following = to_int(text)
    return following, followers


def _achievements(tree: HTMLParser) -> tuple[int | None, int | None]:
    earned = to_int(first_text(tree, ".achievements-widget__count"))
    total = to_int(first_text(tree, ".achievements-widget__total"))
    return earned, total
