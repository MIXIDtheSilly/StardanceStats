from __future__ import annotations

import re
from typing import Any

from selectolax.parser import HTMLParser, Node

from .common import (
    ParseError,
    ParseResult,
    parse_datetime,
    rich_text,
    strip_handle,
    text_of,
    to_int,
)

_COMMENT_ID_RE = re.compile(r"comment_(\d+)")
_COUNT_ID_RE = re.compile(r"comments_count_post_devlog_(\d+)")
_HANDLE_HREF_RE = re.compile(r"^/@([A-Za-z0-9_-]+)")

# Upstream's own Mentionable::MENTION_PATTERN, so our list matches who they notify.
_MENTION_RE = re.compile(r"(?:\A|(?<=[\s(]))@([A-Za-z0-9_-]+)")


def parse_devlog_page(
    html: str, devlog_id: int, project_id: int | None = None
) -> ParseResult:
    """Parse a devlog page into {devlog, comments}."""
    tree = HTMLParser(html)
    result = ParseResult()

    section = tree.css_first(".devlog-detail__comments")
    nodes = tree.css(".devlog-comment")
    if section is None and not nodes:
        raise ParseError(f"devlog {devlog_id}: no comments section on the page")

    # A redirect would parse cleanly and file another thread's replies here.
    counts_node = tree.css_first('[id^="comments_count_post_devlog_"]')
    rendered_id = None
    if counts_node:
        m = _COUNT_ID_RE.search(counts_node.attributes.get("id") or "")
        rendered_id = int(m.group(1)) if m else None
    if rendered_id is not None and rendered_id != devlog_id:
        raise ParseError(
            f"devlog {devlog_id}: page rendered devlog {rendered_id} instead"
        )

    count = to_int(text_of(counts_node)) if counts_node else None
    result.set_optional("comments_count", count, present=counts_node is not None)

    comments: list[dict[str, Any]] = []
    for position, node in enumerate(nodes):
        comment = _parse_comment(node, devlog_id, project_id, position, result)
        if comment:
            comments.append(comment)

    # An empty thread says so in words, so silence means the selector moved.
    if not comments and tree.css_first(".devlog-detail__empty") is None:
        result.missing.add("comments")
    else:
        result.found.add("comments")

    # Banned authors stay in the counter but not on the page, so only a surplus is impossible.
    if count is not None:
        if len(comments) > count:
            result.warn(f"thread renders {len(comments)} comments but counts {count}")
        elif count - len(comments) > max(2, count * 0.25):
            result.warn(f"comment count drift: header={count} parsed={len(comments)}")

    result.data["comments"] = comments
    result.data["devlog"] = {
        "_id": devlog_id,
        "project_id": project_id,
        "comments_count": count,
        "comments_seen": len(comments),
    }
    return result


def _parse_comment(
    node: Node,
    devlog_id: int,
    project_id: int | None,
    position: int,
    result: ParseResult,
) -> dict[str, Any] | None:
    m = _COMMENT_ID_RE.search(node.attributes.get("id") or "")
    if m is None:
        result.warn("comment with no resolvable id, skipped")
        return None

    author = node.css_first(".devlog-comment__author")
    # The href is the canonical handle; the link text is a rendered display name.
    username = _handle_from_href(author) or strip_handle(text_of(author))

    time_node = node.css_first(".devlog-comment__time")
    posted_at = parse_datetime(
        time_node.attributes.get("datetime") if time_node else None
    )

    # An emote-only comment renders no text, so only an absent element is a broken selector.
    body_node = node.css_first(".devlog-comment__body")
    body = _body_text(body_node)

    comment: dict[str, Any] = {
        "_id": int(m.group(1)),
        "devlog_id": devlog_id,
        "project_id": project_id,
        "username": username,
        "posted_at": posted_at,
        "position": position,
        "body": body,
        "body_length": len(body) if body else 0,
        "images": _image_alts(body_node),
        "mentions": mentions_in(body),
    }

    for key in ("username", "posted_at"):
        target = result.missing if comment[key] is None else result.found
        target.add(f"comment.{key}")
    (result.missing if body_node is None else result.found).add("comment.body")

    return comment


def _handle_from_href(node: Node | None) -> str | None:
    if node is None:
        return None
    m = _HANDLE_HREF_RE.match(node.attributes.get("href") or "")
    return m.group(1) if m else None


def _body_text(node: Node | None) -> str | None:
    """Markdown renders to several block elements; keep them apart."""
    return rich_text(node)


def _image_alts(node: Node | None) -> list[str]:
    """Emotes and images carry their name in the alt, which the text misses."""
    if node is None:
        return []
    return [
        alt for alt in (img.attributes.get("alt") or "" for img in node.css("img")) if alt
    ]


def mentions_in(body: str | None) -> list[str]:
    """Handles this comment addressed, lowercased and de-duplicated."""
    if not body:
        return []
    return sorted({m.group(1).lower() for m in _MENTION_RE.finditer(body)})
