from __future__ import annotations

import logging
import time
from collections import defaultdict, deque
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ...config import settings
from ...parsers.common import utcnow
from ..examples import ASK, example
from ..services.ask import AskError, DISPLAYS, FORMATS, jsonable, plan, run, validate
from ..services.ask.client import ModelError

log = logging.getLogger(__name__)

router = APIRouter()

WINDOW = 3600.0
# Past this many addresses, sweep the ones that have gone quiet.
MAX_CALLERS = 4096
MAX_COLUMNS = 8
MAX_BARS = 40
# Only reached when the model returned whole documents instead of naming columns.
NOISE = frozenset(
    {"first_seen", "last_changed", "last_crawled", "snapshot_at", "comments_stale",
     "comments_crawled_at", "comments_crawled_count", "project_ids_seen_at"}
)

_asked: dict[str, deque[float]] = defaultdict(deque)


class Question(BaseModel):
    question: str = Field(min_length=3, description="A question in plain English.")


def _peer(request: Request) -> str:
    """Who opened the socket, which no header can dress up as somebody else."""
    return request.client.host if request.client else ""


def _caller(request: Request) -> str:
    """The visitor the web server names, believable only because _peer vouched for it."""
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return _peer(request) or "unknown"


def _forget(now: float) -> None:
    """Drop the callers whose hour has run out, so the counter cannot grow forever."""
    stale = [key for key, seen in _asked.items() if not seen or now - seen[-1] > WINDOW]
    for key in stale:
        del _asked[key]


def _allow(caller: str) -> bool:
    now = time.monotonic()
    if len(_asked) > MAX_CALLERS:
        _forget(now)

    seen = _asked[caller]
    while seen and now - seen[0] > WINDOW:
        seen.popleft()
    if len(seen) >= settings.ask_rate_limit:
        return False
    seen.append(now)
    return True


def _figures(key: str, rows: list[dict[str, Any]]) -> bool:
    """Whether a column holds figures, as against ids, which are not counted."""
    if key == "_id" or key.endswith("_id"):
        return False
    values = [row.get(key) for row in rows]
    return bool(values) and all(
        isinstance(value, (int, float)) and not isinstance(value, bool) for value in values
    )


def _columns(given: Any, rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    """What the model named, kept to the keys the rows actually carry."""
    keys = list(rows[0]) if rows else []
    columns = []
    if isinstance(given, list):
        for column in given:
            if not isinstance(column, dict) or not column.get("key"):
                continue
            if rows and column["key"] not in keys:
                continue
            key = str(column["key"])
            kind = column.get("format")
            kind = kind if kind in FORMATS else "text"
            # The model calls a count "text" often enough that the rows get a say.
            if kind == "text" and _figures(key, rows):
                kind = "number"
            columns.append(
                {"key": key, "label": str(column.get("label") or key), "format": kind}
            )
    if columns:
        return columns[:MAX_COLUMNS]

    plain = [key for key in keys if key not in NOISE and not key.endswith("_url")]
    return [
        {"key": key, "label": key, "format": "number" if _figures(key, rows) else "text"}
        for key in plain[:MAX_COLUMNS]
    ]


def _display(asked: Any, chart: Any, columns: list[dict[str, str]], rows: list) -> str:
    """A shape the rows can actually take, whatever the model asked for."""
    if asked not in DISPLAYS:
        return "table"
    keys = {column["key"] for column in columns}
    if asked == "number":
        return "number" if len(rows) == 1 and len(columns) == 1 else "table"
    if asked == "bar":
        if not isinstance(chart, dict) or len(rows) > MAX_BARS:
            return "table"
        pair = {chart.get("label"), chart.get("value")}
        if not pair <= keys or len(pair) != 2:
            return "table"
        if not all(isinstance(row.get(chart["value"]), (int, float)) for row in rows):
            return "table"
    return asked


@router.post("/ask", responses=example(ASK))
async def ask(body: Question, request: Request) -> dict[str, Any]:
    """Turn a plain question into one read-only query, and run it."""
    # Checked before anything else, so a stranger learns nothing about the setup.
    if _peer(request) not in settings.ask_caller_list:
        log.warning(
            "Ask refused peer %r, allowed: %s",
            _peer(request),
            ",".join(sorted(settings.ask_caller_list)),
        )
        raise HTTPException(403, "Ask is served through the site, not through the API")

    if not settings.ask_ready:
        missing = []
        if not settings.ask_api_key:
            missing.append("STARDANCE_ASK_API_KEY")
        if not settings.ask_mongo_url:
            missing.append("STARDANCE_ASK_MONGO_URL")
        log.warning("Ask refused, unset: %s", ", ".join(missing))
        raise HTTPException(503, "Ask is not configured on this deployment")

    caller = _caller(request)
    if not _allow(caller):
        raise HTTPException(
            429, f"{settings.ask_rate_limit} questions an hour is the limit; try later"
        )

    question = body.question.strip()[: settings.ask_max_question]
    if len(question) < 3:
        raise HTTPException(422, "ask a question first")

    started = time.monotonic()
    today = utcnow().strftime("%Y-%m-%d")

    repairs: list[tuple[str, str]] = []
    while True:
        raw = ""
        try:
            wanted, raw = await plan(
                question, today=today, max_rows=settings.ask_max_rows, repairs=repairs
            )
            if isinstance(wanted.get("error"), str):
                raise HTTPException(422, wanted["error"][:300])

            pipeline = validate(
                wanted.get("collection"),
                wanted.get("pipeline"),
                max_rows=settings.ask_max_rows,
            )
            rows = await run(
                wanted["collection"], pipeline, limit=settings.ask_max_rows
            )
            break
        except ModelError as exc:
            log.warning("ask model failed: %s", exc)
            raise HTTPException(502, str(exc)[:200]) from exc
        except AskError as exc:
            if len(repairs) >= settings.ask_retries:
                log.info("ask gave up on %r: %s", question[:80], exc)
                raise HTTPException(422, str(exc)[:400]) from exc
            repairs.append((exc.wrote or raw, str(exc)))

    columns = _columns(wanted.get("columns"), rows)
    display = _display(wanted.get("display"), wanted.get("chart"), columns, rows)

    return {
        "question": question,
        "title": str(wanted.get("title") or "Answer")[:120],
        "summary": str(wanted.get("summary") or "")[:400],
        "display": display,
        "chart": wanted.get("chart") if display == "bar" else None,
        "collection": wanted["collection"],
        "pipeline": jsonable(pipeline),
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "truncated": len(rows) >= settings.ask_max_rows,
        "attempts": len(repairs) + 1,
        "elapsed_ms": round((time.monotonic() - started) * 1000),
        "model": settings.ask_model,
    }
