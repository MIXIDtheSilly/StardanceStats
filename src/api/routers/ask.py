from __future__ import annotations

import json
import logging
import time
from collections import defaultdict, deque
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ...config import settings
from ...parsers.common import utcnow
from ..examples import ASK, example
from ..services.ask import (
    AskError,
    DISPLAYS,
    FORMATS,
    QueryBusy,
    QueryTooSlow,
    jsonable,
    plan,
    record,
    run,
    validate,
)
from ..services.ask.client import ModelError

log = logging.getLogger(__name__)

router = APIRouter()

WINDOW = 3600.0
# Past this many addresses, sweep the ones that have gone quiet.
MAX_CALLERS = 4096
MAX_COLUMNS = 8
MAX_BARS = 40
MAX_ERROR = 400
MAX_PIPELINE = 4000
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
    for header in ("cf-connecting-ip", "x-real-ip"):
        named = request.headers.get(header, "").strip()
        if named:
            return named

    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()

    peer = _peer(request)
    log.warning("ask saw no forwarded address from %s, so the site shares one bucket", peer)
    return peer or "unknown"


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


@router.post("/ask", responses=example(ASK), response_model=None)
async def ask(body: Question, request: Request) -> dict[str, Any]:
    """Turn a plain question into one read-only query, and run it."""
    entry: dict[str, Any] = {
        "ts": utcnow(),
        "peer": _peer(request),
        "caller": _caller(request),
        "forwarded": request.headers.get("x-forwarded-for", ""),
        "agent": request.headers.get("user-agent", "")[:300],
        "referer": request.headers.get("referer", "")[:300],
        "body": body.model_dump(),
    }
    try:
        answer = await _answer(body, entry)
    except HTTPException as exc:
        entry["status"] = exc.status_code
        entry.setdefault("outcome", "refused")
        entry["error"] = str(exc.detail)[:MAX_ERROR]
        raise
    except Exception as exc:
        entry["status"] = 500
        entry["outcome"] = "crashed"
        entry["error"] = f"{type(exc).__name__}: {exc}"[:MAX_ERROR]
        raise
    else:
        entry["status"] = 200
        entry["outcome"] = "answered"
        return answer
    finally:
        await record(entry)


async def _answer(body: Question, entry: dict[str, Any]) -> dict[str, Any]:
    peer, caller = entry["peer"], entry["caller"]

    # Checked before anything else, so a stranger learns nothing about the setup.
    if peer not in settings.ask_caller_list:
        log.warning(
            "ask refused peer %r, allowed: %s",
            peer,
            ",".join(sorted(settings.ask_caller_list)),
        )
        entry["outcome"] = "off_site"
        raise HTTPException(403, "Ask is served through the site, not through the API")

    if not settings.ask_ready:
        missing = []
        if not settings.ask_api_key:
            missing.append("STARDANCE_ASK_API_KEY")
        if not settings.ask_mongo_url:
            missing.append("STARDANCE_ASK_MONGO_URL")
        log.warning("ask refused caller=%s, unset: %s", caller, ", ".join(missing))
        entry["outcome"] = "unconfigured"
        raise HTTPException(503, "Ask is not configured on this deployment")

    if not _allow(caller):
        log.info(
            "ask rate limited caller=%s at %s/h over %s callers",
            caller,
            settings.ask_rate_limit,
            len(_asked),
        )
        entry["outcome"] = "rate_limited"
        raise HTTPException(
            429, f"{settings.ask_rate_limit} questions an hour is the limit; try later"
        )

    question = body.question.strip()[: settings.ask_max_question]
    entry["question"] = question
    if len(question) < 3:
        entry["outcome"] = "too_short"
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
                log.info(
                    "ask declined caller=%s question=%r: %s",
                    caller,
                    question,
                    wanted["error"][:300],
                )
                entry["outcome"] = "declined"
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
            log.warning("ask model failed caller=%s question=%r: %s", caller, question, exc)
            entry["outcome"] = "model_failed"
            entry["elapsed_ms"] = round((time.monotonic() - started) * 1000)
            raise HTTPException(502, str(exc)[:200]) from exc
        except QueryBusy as exc:
            entry["outcome"] = "busy"
            entry["elapsed_ms"] = round((time.monotonic() - started) * 1000)
            raise HTTPException(503, str(exc)) from exc
        # Ahead of AskError, which it subclasses: a repair would read it all again.
        except QueryTooSlow as exc:
            log.info("ask too costly caller=%s question=%r", caller, question)
            entry["outcome"] = "too_costly"
            entry["attempts"] = len(repairs) + 1
            entry["elapsed_ms"] = round((time.monotonic() - started) * 1000)
            raise HTTPException(422, str(exc)) from exc
        except AskError as exc:
            if len(repairs) >= settings.ask_retries:
                log.info("ask gave up caller=%s on %r: %s", caller, question, exc)
                entry["outcome"] = "gave_up"
                entry["attempts"] = len(repairs) + 1
                entry["elapsed_ms"] = round((time.monotonic() - started) * 1000)
                raise HTTPException(422, str(exc)[:400]) from exc
            repairs.append((exc.wrote or raw, str(exc)))

    columns = _columns(wanted.get("columns"), rows)
    display = _display(wanted.get("display"), wanted.get("chart"), columns, rows)
    elapsed = round((time.monotonic() - started) * 1000)
    entry.update(
        {
            "collection": wanted["collection"],
            "pipeline": json.dumps(jsonable(pipeline))[:MAX_PIPELINE],
            "display": display,
            "row_count": len(rows),
            "attempts": len(repairs) + 1,
            "elapsed_ms": elapsed,
            "model": settings.ask_model,
        }
    )
    log.info(
        "ask answered caller=%s question=%r collection=%s rows=%s attempts=%s in %sms",
        caller,
        question,
        wanted["collection"],
        len(rows),
        len(repairs) + 1,
        elapsed,
    )

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
        "elapsed_ms": elapsed,
        "model": settings.ask_model,
    }
