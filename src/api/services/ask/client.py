from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from ....config import settings
from .guard import AskError
from .schema import SCHEMA

log = logging.getLogger(__name__)

DISPLAYS = ("table", "number", "bar")
FORMATS = ("text", "number", "hours", "seconds", "date", "username", "project")


class ModelError(AskError):
    """The model never answered, so there is nothing for a repair turn to fix."""

INSTRUCTIONS = """\
You are the query writer behind the Ask tab of Stardance Stats, a site that \
tracks the Stardance platform. Someone has asked a question about the data it \
holds. Answer it by writing one MongoDB aggregation pipeline against that \
database, and by saying how the answer should be shown.

{schema}
Today is {today}, and the newest rows are from about now. Answer about what
the database holds, never from what you already know about Stardance.

Reply with one JSON object and nothing else:
{{
  "collection": "the one collection the pipeline runs on",
  "pipeline": [ aggregation stages ],
  "title": "a short heading, at most 60 characters",
  "summary": "one plain sentence saying what the rows below show",
  "display": "table" | "number" | "bar",
  "columns": [{{"key": "field in the result rows", "label": "column heading",
               "format": "text|number|hours|seconds|date|username|project"}}],
  "chart": {{"label": "column key", "value": "column key"}}
}}

Rules
- Stages you may use: $match $project $group $sort $limit $skip $count $unwind
  $addFields $set $unset $replaceRoot $replaceWith $sortByCount $facet $bucket
  $bucketAuto $lookup $sample. Anything else is refused, as is $where and any
  stage that writes.
- End with $sort then $limit, and never ask for more than {max_rows} rows.
- $project the answer down to the few fields it needs. Never return whole
  documents, and never return body, description or bio unless they were asked for.
- 2 to 6 columns, each with a key that really exists in the rows you build.
- Rank on a field only alongside a null guard, such as
  {{"$match": {{"totals.hours": {{"$ne": null}}}}}}.
- A $match compares against stored values, so put dates in it literally, as
  {{"$date": "2026-08-01T00:00:00Z"}}, worked out from today's date above.
  $dateSubtract, $$NOW and other computed values match nothing there unless the
  whole comparison sits inside $expr.
- Group dates into days or weeks with $dateToString or $dateTrunc, which do
  work inside $group and $project.
- display "number" is for one row holding one figure; give it a single column.
  display "bar" needs "chart" naming a text column and a number column, and
  suits up to 30 rows. Everything else is "table".
- format "username" links to that person's page, so its value must be the
  handle. format "project" links to a project, so its value must be the project
  _id, and pair it with a title column.
- If the database cannot answer the question, reply with
  {{"error": "one sentence saying why"}} instead. A question about you, the
  site or the crawler rather than about the rows is one of these; say briefly
  what the database does cover. Do not go looking for the question's words in
  the data.\
"""


def _instructions(*, today: str, max_rows: int) -> str:
    return INSTRUCTIONS.format(schema=SCHEMA, today=today, max_rows=max_rows)


async def plan(
    question: str, *, today: str, max_rows: int, repairs: list[tuple[str, str]] = ()
) -> tuple[dict[str, Any], str]:
    """Ask the model for a pipeline; repairs replay what it wrote and what broke."""
    messages: list[dict[str, str]] = [
        {"role": "system", "content": _instructions(today=today, max_rows=max_rows)},
        {"role": "user", "content": question},
    ]
    for wrote, failed in repairs:
        messages.append({"role": "assistant", "content": wrote})
        messages.append(
            {
                "role": "user",
                "content": (
                    f"That query did not run: {failed}\n"
                    "Fix it and reply with the whole JSON object again."
                ),
            }
        )

    raw = await _complete(messages)
    return _parse(raw), raw


async def _complete(messages: list[dict[str, str]]) -> str:
    body = {
        "model": settings.ask_model,
        "messages": messages,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "reasoning": {"enabled": settings.ask_reasoning},
    }
    url = settings.ask_api_url.rstrip("/") + "/chat/completions"

    async with httpx.AsyncClient(timeout=settings.ask_timeout) as http:
        try:
            response = await http.post(
                url,
                json=body,
                headers={"Authorization": f"Bearer {settings.ask_api_key}"},
            )
        except httpx.HTTPError as exc:
            raise ModelError(f"the model could not be reached: {exc}") from exc

    if response.status_code != 200:
        log.warning("ask model returned %s: %s", response.status_code, response.text[:400])
        raise ModelError(f"the model returned {response.status_code}")

    try:
        return response.json()["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, ValueError) as exc:
        raise ModelError("the model's reply had no content") from exc


def _parse(raw: str) -> dict[str, Any]:
    """The reply as JSON, tolerating a fenced block or prose around it."""
    text = raw.strip()
    if text.startswith("```"):
        fenced = text[3:].split("```")[0]
        text = fenced.removeprefix("json").strip()

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise AskError("the reply was not JSON", wrote=raw) from None
        try:
            parsed = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise AskError("the reply was not JSON", wrote=raw) from exc

    if not isinstance(parsed, dict):
        raise AskError("the reply was not a JSON object", wrote=raw)
    return parsed
