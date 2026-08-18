from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.api.services.ask.guard import AskError, validate

ROWS = 50


def ok(pipeline, collection="users"):
    return validate(collection, pipeline, max_rows=ROWS)


def refused(pipeline, collection="users") -> str:
    with pytest.raises(AskError) as caught:
        validate(collection, pipeline, max_rows=ROWS)
    return str(caught.value)


def test_a_plain_ranking_passes():
    pipeline = ok([{"$sort": {"totals.hours": -1}}, {"$limit": 5}])
    assert pipeline[:2] == [{"$sort": {"totals.hours": -1}}, {"$limit": 5}]


def test_every_pipeline_ends_capped():
    assert ok([{"$match": {}}, {"$limit": 5}])[-1] == {"$limit": ROWS}


def test_a_limit_past_the_cap_comes_down_to_it():
    assert ok([{"$match": {}}, {"$limit": 100_000}])[1] == {"$limit": ROWS}


def test_a_collection_we_do_not_hold_is_refused():
    assert "not one we hold" in refused([{"$match": {}}], collection="admin")


def test_the_crawler_tables_are_not_readable():
    assert "not one we hold" in refused([{"$match": {}}], collection="crawl_frontier")


def test_an_empty_pipeline_is_refused():
    assert "non-empty" in refused([])


@pytest.mark.parametrize(
    "stage",
    [
        {"$out": "users"},
        {"$merge": {"into": "users"}},
        {"$unionWith": "projects"},
        {"$graphLookup": {"from": "users"}},
        {"$collStats": {}},
        {"$indexStats": {}},
        {"$currentOp": {}},
    ],
)
def test_a_stage_that_writes_or_pokes_the_server_is_refused(stage):
    assert "not allowed" in refused([stage])


@pytest.mark.parametrize(
    "expression",
    [
        {"$where": "this.hours > 1"},
        {"$expr": {"$function": {"body": "f", "args": [], "lang": "js"}}},
        {"$expr": {"$accumulator": {}}},
    ],
)
def test_server_side_javascript_is_refused(expression):
    assert "not allowed" in refused([{"$match": expression}])


def test_a_lookup_can_only_reach_a_collection_we_hold():
    lookup = {"$lookup": {"from": "system.users", "localField": "_id",
                          "foreignField": "uid", "as": "x"}}
    assert "cannot read" in refused([lookup])


def test_a_lookup_into_a_known_collection_passes():
    lookup = {"$lookup": {"from": "projects", "localField": "_id",
                          "foreignField": "owner_id", "as": "projects"}}
    assert ok([lookup])[0] == lookup


def test_a_nested_lookup_pipeline_is_checked_too():
    lookup = {"$lookup": {"from": "projects", "as": "p", "pipeline": [{"$out": "x"}]}}
    assert "not allowed" in refused([lookup])


def test_a_facet_checks_each_of_its_pipelines():
    assert "not allowed" in refused([{"$facet": {"a": [{"$out": "x"}]}}])


def test_a_facet_of_real_pipelines_passes():
    assert ok([{"$facet": {"a": [{"$count": "n"}]}}])[0]["$facet"]["a"][0] == {"$count": "n"}


def test_a_sample_is_capped_as_well():
    assert ok([{"$sample": {"size": 5000}}])[0] == {"$sample": {"size": ROWS}}


def test_a_limit_must_be_a_number():
    assert "whole number" in refused([{"$limit": "all"}])


def test_a_date_literal_becomes_a_date():
    stage = ok([{"$match": {"joined_at": {"$gte": {"$date": "2026-08-01T00:00:00Z"}}}}])[0]
    assert stage["$match"]["joined_at"]["$gte"] == datetime(2026, 8, 1, tzinfo=timezone.utc)


def test_a_date_written_as_a_string_is_read_as_one():
    """Left as text it would match nothing at all, which reads as a real answer."""
    stage = ok([{"$match": {"joined_at": {"$lt": "2026-08-01"}}}])[0]
    assert stage["$match"]["joined_at"]["$lt"] == datetime(2026, 8, 1, tzinfo=timezone.utc)


def test_a_date_shaped_string_on_a_text_field_stays_text():
    stage = ok([{"$match": {"username_lower": "2026-08-01"}}])[0]
    assert stage["$match"]["username_lower"] == "2026-08-01"


def test_a_grouped_day_string_stays_text():
    """The key is _id, not a date field, so the day label is compared as written."""
    pipeline = ok(
        [
            {"$group": {"_id": {"$dateToString": {"format": "%Y-%m-%d", "date": "$joined_at"}}}},
            {"$match": {"_id": "2026-08-01"}},
        ]
    )
    assert pipeline[1]["$match"]["_id"] == "2026-08-01"


def test_computed_dates_in_a_match_are_refused():
    subtract = {"$dateSubtract": {"startDate": "$$NOW", "unit": "day", "amount": 7}}
    assert "compares against nothing" in refused([{"$match": {"joined_at": {"$gte": subtract}}}])


def test_the_same_arithmetic_inside_expr_passes():
    stage = {"$match": {"$expr": {"$gt": ["$totals.hours", {"$multiply": ["$stats.ships", 2]}]}}}
    assert ok([stage])[0] == stage


def test_a_variable_in_a_plain_match_is_refused():
    assert "inside $expr" in refused([{"$match": {"joined_at": "$$NOW"}}])


def test_a_text_search_passes():
    stage = {"$match": {"$text": {"$search": "soldering"}}}
    assert ok([stage], collection="devlogs")[0] == stage


def test_a_stage_with_two_keys_is_refused():
    assert "exactly one key" in refused([{"$match": {}, "$limit": 5}])


def test_too_many_stages_is_refused():
    assert "ceiling" in refused([{"$match": {}}] * 30)


def test_a_pipeline_that_nests_too_deep_is_refused():
    deep = {"$and": []}
    node = deep
    for _ in range(25):
        child = {"$and": []}
        node["$and"].append(child)
        node = child
    assert "too deeply" in refused([{"$match": deep}])
