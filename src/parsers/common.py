from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from dateutil import parser as dateparser
from selectolax.parser import Node

_INT_RE = re.compile(r"-?[\d,]+")
_FLOAT_RE = re.compile(r"-?[\d,]*\.?\d+")
_DURATION_RE = re.compile(r"(\d+)\s*([dhms])")


class ParseError(Exception):
    """Raised when a document is structurally not what we expected at all."""


@dataclass
class ParseResult:
    """A parsed payload plus provenance about what we could and could not read."""

    data: dict[str, Any] = field(default_factory=dict)
    found: set[str] = field(default_factory=set)
    missing: set[str] = field(default_factory=set)
    warnings: list[str] = field(default_factory=list)

    def set(self, key: str, value: Any) -> None:
        if value is None:
            self.missing.add(key)
        else:
            self.data[key] = value
            self.found.add(key)

    def set_optional(self, key: str, value: Any, *, present: bool) -> None:
        """Record a field whose emptiness is a fact rather than a failure."""
        if not present:
            self.missing.add(key)
            return
        self.found.add(key)
        if value is not None:
            self.data[key] = value

    def warn(self, message: str) -> None:
        self.warnings.append(message)

    @property
    def ok(self) -> bool:
        return not self.missing

    def require(self, *keys: str) -> None:
        absent = [k for k in keys if k not in self.data]
        if absent:
            raise ParseError(f"required field(s) missing: {', '.join(absent)}")


def text_of(node: Node | None) -> str | None:
    """Collapsed text content, or None when the node is absent/empty."""
    if node is None:
        return None
    value = " ".join(node.text(strip=True).split())
    return value or None


def first_text(node: Node, selector: str) -> str | None:
    return text_of(node.css_first(selector))


def to_int(value: str | None) -> int | None:
    """First integer in a string. '1,234 followers' -> 1234."""
    if value is None:
        return None
    m = _INT_RE.search(value)
    if not m:
        return None
    try:
        return int(m.group(0).replace(",", ""))
    except ValueError:
        return None


def to_float(value: str | None) -> float | None:
    """First float in a string. '19.65x multiplier' -> 19.65."""
    if value is None:
        return None
    m = _FLOAT_RE.search(value.replace(",", ""))
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


def parse_duration_seconds(value: str | None) -> int | None:
    """Parse format_seconds output ('1h 28m 9s logged') into seconds."""
    if value is None:
        return None
    units = {"d": 86400, "h": 3600, "m": 60, "s": 1}
    matches = _DURATION_RE.findall(value)
    if not matches:
        return None
    return sum(int(n) * units[u] for n, u in matches)


def parse_datetime(value: str | None) -> datetime | None:
    """Parse an ISO-8601 <time datetime="..."> into an aware UTC datetime."""
    if not value:
        return None
    try:
        dt = dateparser.isoparse(value)
    except (ValueError, OverflowError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def strip_handle(value: str | None) -> str | None:
    """'@The_Craw' -> 'The_Craw'."""
    if not value:
        return None
    return value.lstrip("@").strip() or None


def id_from_path(href: str | None, *, segment: str) -> int | None:
    """Pull the id after `segment`: '/projects/8100/devlogs/33892' -> 33892."""
    if not href:
        return None
    m = re.search(rf"/{re.escape(segment)}/(\d+)", href)
    return int(m.group(1)) if m else None


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
