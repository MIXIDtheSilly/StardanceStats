from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from motor.motor_asyncio import AsyncIOMotorDatabase

from ...collector.rollup import TRACKED as GLOBAL_TRACKED
from ...ingest.project import DEVLOG_TRACKED, TRACKED as PROJECT_TRACKED
from ...ingest.shop import TRACKED as SHOP_TRACKED
from ...ingest.user import TRACKED as USER_TRACKED

Interval = Literal["1h", "1d", "1w"]
Fill = Literal["none", "locf"]

# The dense axis is built in Python, so a wide window is refused, not allocated.
MAX_BUCKETS = 20_000

_TRUNC: dict[str, dict[str, Any]] = {
    "1h": {"unit": "hour", "binSize": 1},
    "1d": {"unit": "day", "binSize": 1},
    "1w": {"unit": "week", "binSize": 1},
}

_STEP: dict[str, timedelta] = {
    "1h": timedelta(hours=1),
    "1d": timedelta(days=1),
    "1w": timedelta(days=7),
}


class HistoryError(ValueError):
    """A history request that cannot be served as asked."""


@dataclass(frozen=True)
class Source:
    """Where one entity kind's history lives, and what it may be asked for."""

    collection: str
    meta_field: str
    metrics: frozenset[str]


METRICS: dict[str, Source] = {
    "project": Source("project_snapshots", "pid", frozenset(PROJECT_TRACKED)),
    "devlog": Source("devlog_snapshots", "did", frozenset(DEVLOG_TRACKED)),
    "user": Source("user_snapshots", "uid", frozenset(USER_TRACKED)),
    "shop": Source("shop_snapshots", "sid", frozenset(SHOP_TRACKED)),
    "global": Source("global_snapshots", "scope", frozenset(GLOBAL_TRACKED)),
}


def parse_metrics(kind: str, requested: str) -> list[str]:
    """Split and validate a comma-separated metric list."""
    source = METRICS[kind]
    names = list(dict.fromkeys(m.strip() for m in requested.split(",") if m.strip()))
    if not names:
        raise HistoryError("no metrics requested")
    unknown = [m for m in names if m not in source.metrics]
    if unknown:
        raise HistoryError(
            f"unknown metric(s): {unknown}; valid: {sorted(source.metrics)}"
        )
    return names


async def latest_snapshot(
    db: AsyncIOMotorDatabase, kind: str, meta_value: Any
) -> dict[str, Any] | None:
    source = METRICS[kind]
    return await db[source.collection].find_one(
        {source.meta_field: meta_value}, sort=[("ts", -1)]
    )


async def bucketed_series(
    db: AsyncIOMotorDatabase,
    kind: str,
    meta_value: Any,
    *,
    metrics: list[str],
    interval: Interval = "1d",
    start: datetime | None = None,
    end: datetime | None = None,
    delta: bool = True,
    fill: Fill = "none",
    now: datetime | None = None,
) -> dict[str, Any]:
    """One bucketed series per metric, last observation wins inside a bucket."""
    source = METRICS[kind]
    now = now or datetime.now(timezone.utc)

    rows = await _aggregate(
        db, source, meta_value,
        metrics=metrics, interval=interval, start=start, end=end, delta=delta,
    )

    seed = None
    if fill == "locf" and start is not None:
        # Otherwise a window opening on a flat stretch renders as empty.
        seed = await db[source.collection].find_one(
            {source.meta_field: meta_value, "ts": {"$lt": start}}, sort=[("ts", -1)]
        )

    if fill == "locf":
        axis = _dense_axis(rows, interval, start, end, now, seeded=seed is not None)
    else:
        axis = [row["_id"] for row in rows]

    series = _to_series(rows, axis, metrics, delta=delta, seed=seed, carry=fill == "locf")

    return {
        "interval": interval,
        "fill": fill,
        "buckets": len(axis),
        "observed_buckets": len(rows),
        "from": axis[0] if axis else None,
        "to": axis[-1] if axis else None,
        "series": series,
    }


async def _aggregate(
    db: AsyncIOMotorDatabase,
    source: Source,
    meta_value: Any,
    *,
    metrics: list[str],
    interval: Interval,
    start: datetime | None,
    end: datetime | None,
    delta: bool,
) -> list[dict[str, Any]]:
    match: dict[str, Any] = {source.meta_field: meta_value}
    if start or end:
        window: dict[str, Any] = {}
        if start:
            window["$gte"] = start
        if end:
            window["$lte"] = end
        match["ts"] = window

    group: dict[str, Any] = {
        "_id": {"$dateTrunc": {"date": "$ts", **_TRUNC[interval]}},
        "synthetic": {"$last": "$synthetic"},
    }
    for metric in metrics:
        group[metric] = {"$last": f"${metric}"}

    stages: list[dict[str, Any]] = [
        {"$match": match},
        {"$sort": {"ts": 1}},
        {"$group": group},
        {"$sort": {"_id": 1}},
    ]

    if delta:
        # Never stored, so a formula change cannot strand old deltas.
        stages.append({
            "$setWindowFields": {
                "sortBy": {"_id": 1},
                "output": {
                    f"{metric}__prev": {
                        "$shift": {"output": f"${metric}", "by": -1, "default": None}
                    }
                    for metric in metrics
                },
            }
        })
        stages.append({
            "$set": {
                f"{metric}__d": {
                    "$cond": [
                        {"$or": [
                            {"$eq": [f"${metric}", None]},
                            {"$eq": [f"${metric}__prev", None]},
                        ]},
                        None,
                        {"$subtract": [f"${metric}", f"${metric}__prev"]},
                    ]
                }
                for metric in metrics
            }
        })

    rows = await db[source.collection].aggregate(stages).to_list(length=MAX_BUCKETS + 1)
    if len(rows) > MAX_BUCKETS:
        raise HistoryError(
            f"more than {MAX_BUCKETS} buckets; narrow the range or use a coarser interval"
        )
    return rows


def trunc(ts: datetime, interval: Interval) -> datetime:
    """Mirror `$dateTrunc` for the interval units we offer."""
    ts = ts.astimezone(timezone.utc)
    if interval == "1h":
        return ts.replace(minute=0, second=0, microsecond=0)
    day = ts.replace(hour=0, minute=0, second=0, microsecond=0)
    if interval == "1d":
        return day
    # $dateTrunc weeks start on Sunday; Python's weekday() has Monday at 0.
    return day - timedelta(days=(day.weekday() + 1) % 7)


def _dense_axis(
    rows: list[dict[str, Any]],
    interval: Interval,
    start: datetime | None,
    end: datetime | None,
    now: datetime,
    *,
    seeded: bool,
) -> list[datetime]:
    """Every bucket between the bounds, whether or not it was observed."""
    if start is not None and (rows or seeded):
        lo = trunc(start, interval)
    elif rows:
        lo = rows[0]["_id"]
    else:
        return []

    # A window running past now would fill a future the crawler cannot know.
    hi = trunc(min(end, now) if end else now, interval)
    if rows:
        hi = max(hi, rows[-1]["_id"])
    if hi < lo:
        return []

    step = _STEP[interval]
    count = int((hi - lo) / step) + 1
    if count > MAX_BUCKETS:
        raise HistoryError(
            f"{count} buckets exceeds the {MAX_BUCKETS} cap; "
            "narrow the range or use a coarser interval"
        )

    axis = []
    bucket = lo
    while bucket <= hi:
        axis.append(bucket)
        bucket += step
    return axis


def _to_series(
    rows: list[dict[str, Any]],
    axis: list[datetime],
    metrics: list[str],
    *,
    delta: bool,
    seed: dict[str, Any] | None,
    carry: bool,
) -> dict[str, list[dict[str, Any]]]:
    by_bucket = {row["_id"]: row for row in rows}
    series: dict[str, list[dict[str, Any]]] = {}

    for metric in metrics:
        points: list[dict[str, Any]] = []
        carried = (seed or {}).get(metric) if carry else None
        for bucket in axis:
            row = by_bucket.get(bucket)
            value = row.get(metric) if row else None

            if value is None:
                if not carry or carried is None:
                    continue
                # Carried forward: the value is unchanged, so the delta is zero.
                point: dict[str, Any] = {"ts": bucket, "v": carried, "filled": True}
                if delta:
                    point["d"] = 0
                points.append(point)
                continue

            point = {"ts": bucket, "v": value}
            if delta:
                change = row.get(f"{metric}__d")
                if change is None and carried is not None:
                    # First observed point of a window that opened on a seed.
                    change = value - carried
                if change is not None:
                    point["d"] = round(change, 4)
            if row.get("synthetic"):
                point["synthetic"] = True
            points.append(point)
            carried = value

        series[metric] = points

    return series
