from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from ...config import settings


def freshness(as_of: datetime | None, *, now: datetime | None = None) -> dict[str, Any]:
    """When the numbers were last observed, and whether that is too long ago."""
    now = now or datetime.now(timezone.utc)
    if as_of is None:
        return {"data_as_of": None, "stale": True}
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=timezone.utc)
    cutoff = timedelta(hours=settings.api_stale_after_hours)
    return {"data_as_of": as_of, "stale": (now - as_of) > cutoff}


def stamp(payload: dict[str, Any], as_of: datetime | None, **extra: Any) -> dict[str, Any]:
    """Attach the freshness pair to a response body."""
    payload.update(freshness(as_of), **extra)
    return payload
