from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from .schema import COLLECTIONS, DATE_FIELDS, FORBIDDEN

MAX_STAGES = 24
MAX_DEPTH = 18
MAX_STRING = 2000

# A date left as text matches nothing, so one on a date field is read as a date.
ISO = re.compile(r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?)?(Z|[+-]\d{2}:?\d{2})?$")
# Under these a string is a pattern or a phrase, never a date.
TEXTUAL = frozenset({"$regex", "$options", "$text", "$search", "$language"})

# $out, $merge, $unionWith, $graphLookup and friends are refused by absence.
STAGES: frozenset[str] = frozenset(
    {
        "$addFields",
        "$bucket",
        "$bucketAuto",
        "$count",
        "$facet",
        "$group",
        "$limit",
        "$lookup",
        "$match",
        "$project",
        "$replaceRoot",
        "$replaceWith",
        "$sample",
        "$set",
        "$skip",
        "$sort",
        "$sortByCount",
        "$unset",
        "$unwind",
    }
)

# $where, $function and $accumulator run server-side JavaScript, so they are not here.
OPERATORS: frozenset[str] = frozenset(
    {
        "$eq", "$ne", "$gt", "$gte", "$lt", "$lte", "$in", "$nin", "$cmp",
        "$and", "$or", "$not", "$nor", "$expr", "$exists", "$type",
        "$regex", "$options", "$regexMatch", "$regexFind", "$regexFindAll",
        "$text", "$search", "$language", "$caseSensitive", "$diacriticSensitive",
        "$meta", "$mod", "$all", "$elemMatch", "$size", "$slice",
        "$abs", "$add", "$ceil", "$divide", "$exp", "$floor", "$ln", "$log",
        "$log10", "$multiply", "$pow", "$round", "$sqrt", "$subtract", "$trunc",
        "$sum", "$avg", "$min", "$max", "$first", "$last", "$push", "$addToSet",
        "$count", "$stdDevPop", "$stdDevSamp", "$top", "$bottom", "$topN",
        "$bottomN", "$firstN", "$lastN", "$maxN", "$minN", "$n", "$sortBy",
        "$output", "$median", "$percentile", "$p", "$method",
        "$arrayElemAt", "$arrayToObject", "$concatArrays", "$filter", "$as",
        "$indexOfArray", "$isArray", "$map", "$mergeObjects", "$objectToArray",
        "$range", "$reduce", "$reverseArray", "$sortArray", "$zip",
        "$initialValue", "$setDifference", "$setEquals", "$setIntersection",
        "$setIsSubset", "$setUnion", "$anyElementTrue", "$allElementsTrue",
        "$cond", "$if", "$then", "$else", "$ifNull", "$switch", "$branches",
        "$case", "$default", "$let", "$vars", "$literal",
        "$concat", "$split", "$strLenCP", "$strLenBytes", "$strcasecmp",
        "$substr", "$substrBytes", "$substrCP", "$toLower", "$toUpper",
        "$trim", "$ltrim", "$rtrim", "$indexOfCP", "$indexOfBytes",
        "$replaceOne", "$replaceAll", "$find", "$replacement", "$chars",
        "$dateToString", "$dateFromString", "$dateFromParts", "$dateToParts",
        "$dateAdd", "$dateSubtract", "$dateDiff", "$dateTrunc", "$year",
        "$month", "$dayOfMonth", "$dayOfYear", "$dayOfWeek", "$hour", "$minute",
        "$second", "$millisecond", "$week", "$isoWeek", "$isoWeekYear",
        "$isoDayOfWeek", "$format", "$timezone", "$unit", "$binSize", "$amount",
        "$startDate", "$endDate", "$startOfWeek", "$dateString", "$onNull",
        "$convert", "$toBool", "$toDate", "$toDecimal", "$toDouble", "$toInt",
        "$toLong", "$toObjectId", "$toString", "$isNumber", "$to", "$input",
        "$onError",
        # validate() turns this one into a datetime.
        "$date",
    }
)


# In a $match these compare against the literal object and quietly match nothing.
EXPRESSION_ONLY: frozenset[str] = frozenset(
    {
        "$add", "$arrayElemAt", "$concat", "$cond", "$dateAdd", "$dateDiff",
        "$dateFromParts", "$dateFromString", "$dateSubtract", "$dateTrunc",
        "$divide", "$ifNull", "$let", "$multiply", "$subtract", "$switch",
        "$toDate", "$toLower", "$toUpper",
    }
)


class AskError(Exception):
    """A refusal the user can read, and the model can be asked to fix."""

    def __init__(self, message: str, *, wrote: str = "") -> None:
        super().__init__(message)
        self.wrote = wrote


def validate(collection: Any, pipeline: Any, *, max_rows: int) -> list[dict[str, Any]]:
    """Refuse anything but a read, and return the pipeline capped at max_rows."""
    if collection in FORBIDDEN or collection not in COLLECTIONS:
        known = ", ".join(sorted(COLLECTIONS))
        raise AskError(f"collection {collection!r} is not one we hold; pick from: {known}")
    if not isinstance(pipeline, list) or not pipeline:
        raise AskError("pipeline must be a non-empty array of stages")

    stages = _pipeline(pipeline, depth=0, max_rows=max_rows)
    # A $lookup can fan the row count back out past the model's own $limit.
    stages.append({"$limit": max_rows})
    return stages


def _pipeline(pipeline: Any, *, depth: int, max_rows: int) -> list[dict[str, Any]]:
    if not isinstance(pipeline, list):
        raise AskError("a pipeline must be an array of stages")
    if len(pipeline) > MAX_STAGES:
        raise AskError(f"pipeline has {len(pipeline)} stages; the ceiling is {MAX_STAGES}")
    return [_stage(stage, depth=depth, max_rows=max_rows) for stage in pipeline]


def _stage(stage: Any, *, depth: int, max_rows: int) -> dict[str, Any]:
    if not isinstance(stage, dict) or len(stage) != 1:
        raise AskError(f"each stage must be an object with exactly one key, got {stage!r}")

    (name, body), = stage.items()
    if name not in STAGES:
        raise AskError(f"stage {name} is not allowed here")

    if name in ("$limit", "$skip", "$sample"):
        return {name: _bounded(name, body, max_rows)}
    if name == "$lookup":
        return {name: _lookup(body, depth=depth, max_rows=max_rows)}
    if name == "$facet":
        if not isinstance(body, dict):
            raise AskError("$facet takes an object of named pipelines")
        return {
            name: {
                key: _pipeline(sub, depth=depth + 1, max_rows=max_rows)
                for key, sub in body.items()
            }
        }
    return {name: _value(body, depth=depth + 1, literal=name == "$match")}


def _bounded(name: str, body: Any, max_rows: int) -> int | dict[str, int]:
    """$limit, $skip and $sample take one number, and it is not unbounded."""
    if name == "$sample":
        size = body.get("size") if isinstance(body, dict) else body
    else:
        size = body
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise AskError(f"{name} takes a non-negative whole number, got {body!r}")
    size = min(size, max_rows if name != "$skip" else 100_000)
    return {"size": size} if name == "$sample" else size


def _lookup(body: Any, *, depth: int, max_rows: int) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise AskError("$lookup takes an object")
    if body.get("from") in FORBIDDEN or body.get("from") not in COLLECTIONS:
        raise AskError(f"$lookup cannot read {body.get('from')!r}")

    checked: dict[str, Any] = {}
    for key, value in body.items():
        if key == "pipeline":
            checked[key] = _pipeline(value, depth=depth + 1, max_rows=max_rows)
        elif key in ("from", "localField", "foreignField", "as"):
            if not isinstance(value, str):
                raise AskError(f"$lookup.{key} must be a string")
            checked[key] = value
        else:
            checked[key] = _value(value, depth=depth + 1)
    return checked


def _value(value: Any, *, depth: int, literal: bool = False, dates: bool = False) -> Any:
    """Walk an expression, refusing unknown $keys and rewriting date literals."""
    if depth > MAX_DEPTH:
        raise AskError("query nests too deeply")

    if isinstance(value, dict):
        if set(value) == {"$date"}:
            return _date(value["$date"])

        walked = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise AskError("object keys must be strings")
            if key.startswith("$") and key not in OPERATORS:
                raise AskError(f"operator {key} is not allowed")
            if literal and key in EXPRESSION_ONLY:
                raise AskError(
                    f"{key} in a $match compares against nothing; write the value "
                    'literally, as {"$date": "2026-08-01T00:00:00Z"} for a date, '
                    "or wrap the whole comparison in $expr"
                )
            walked[key] = _value(
                item,
                depth=depth + 1,
                # $expr is where a $match starts evaluating rather than comparing.
                literal=literal and key != "$expr",
                dates=_dated(key, dates),
            )
        return walked

    if isinstance(value, list):
        return [_value(item, depth=depth + 1, literal=literal, dates=dates) for item in value]

    if isinstance(value, str):
        if len(value) > MAX_STRING:
            raise AskError("a string in the query is too long")
        if literal and value.startswith("$$"):
            raise AskError(
                f"{value} in a $match reads as text, not a variable; "
                "put the comparison inside $expr"
            )
        if literal and dates and ISO.match(value):
            return _date(value)
        return value

    if value is None or isinstance(value, (bool, int, float, datetime)):
        return value

    raise AskError(f"{type(value).__name__} is not a value a query can carry")


def _dated(key: str, inherited: bool) -> bool:
    """Whether the subtree under this key holds dates. Operators pass it down."""
    if key.startswith("$"):
        return inherited and key not in TEXTUAL
    return key.rsplit(".", 1)[-1] in DATE_FIELDS


def _date(raw: Any) -> datetime:
    """{"$date": "2026-08-01"} as the driver will not read extended JSON for us."""
    if isinstance(raw, datetime):
        return raw
    if not isinstance(raw, str):
        raise AskError("$date takes an ISO 8601 string")
    text = raw.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise AskError(f"{raw!r} is not an ISO 8601 date") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
