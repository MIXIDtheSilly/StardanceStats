from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from src.api.main import app
from src.api.routers import ask as router
from src.config import settings


@pytest.fixture
def client():
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


@pytest.fixture
def stranger():
    """Someone reaching the API directly rather than through the site."""
    transport = ASGITransport(app=app, client=("203.0.113.7", 40404))
    return AsyncClient(transport=transport, base_url="http://test")


@pytest.fixture(autouse=True)
def clean_counters():
    router._asked.clear()
    yield
    router._asked.clear()


@pytest.fixture(autouse=True)
def logged(monkeypatch):
    rows: list[dict] = []

    async def keep(entry):
        rows.append(entry)

    monkeypatch.setattr(router, "record", keep)
    return rows


async def test_ask_is_off_until_both_halves_are_configured(client, monkeypatch):
    monkeypatch.setattr(settings, "ask_api_key", "")
    async with client as http:
        response = await http.post("/v1/ask", json={"question": "how many users?"})
    assert response.status_code == 503


async def test_a_refused_question_is_still_written_down(stranger, logged):
    async with stranger as http:
        await http.post("/v1/ask", json={"question": "how many users?"})
    assert len(logged) == 1
    assert logged[0]["body"] == {"question": "how many users?"}
    assert logged[0]["outcome"] == "off_site"
    assert logged[0]["status"] == 403
    assert logged[0]["peer"] == "203.0.113.7"


async def test_the_row_names_the_visitor_the_site_forwarded(client, logged, monkeypatch):
    monkeypatch.setattr(settings, "ask_api_key", "")
    async with client as http:
        await http.post(
            "/v1/ask",
            json={"question": "how many users?"},
            headers={"x-forwarded-for": "9.9.9.9"},
        )
    assert logged[0]["caller"] == "9.9.9.9"
    assert logged[0]["outcome"] == "unconfigured"


async def test_a_caller_out_of_questions_is_written_down(client, logged, monkeypatch):
    monkeypatch.setattr(settings, "ask_rate_limit", 0)
    monkeypatch.setattr(settings, "ask_api_key", "key")
    monkeypatch.setattr(settings, "ask_mongo_url", "mongodb://localhost/x")
    async with client as http:
        response = await http.post("/v1/ask", json={"question": "how many users?"})
    assert response.status_code == 429
    assert logged[0]["outcome"] == "rate_limited"


def test_a_caller_runs_out_of_questions(monkeypatch):
    monkeypatch.setattr(settings, "ask_rate_limit", 3)
    assert [router._allow("1.2.3.4") for _ in range(4)] == [True, True, True, False]


def test_one_caller_running_out_does_not_stop_another(monkeypatch):
    monkeypatch.setattr(settings, "ask_rate_limit", 1)
    assert router._allow("1.2.3.4") and router._allow("5.6.7.8")


async def test_the_public_api_cannot_ask(stranger):
    async with stranger as http:
        response = await http.post("/v1/ask", json={"question": "how many users?"})
    assert response.status_code == 403


async def test_a_forged_forwarded_header_does_not_open_the_gate(stranger):
    """The header names the visitor; the socket says whether we asked at all."""
    async with stranger as http:
        response = await http.post(
            "/v1/ask",
            json={"question": "how many users?"},
            headers={"x-forwarded-for": "127.0.0.1"},
        )
    assert response.status_code == 403


def test_the_first_hop_is_the_caller():
    request = type("R", (), {"headers": {"x-forwarded-for": "9.9.9.9, 10.0.0.1"}, "client": None})
    assert router._caller(request) == "9.9.9.9"


def test_a_single_address_from_the_proxy_beats_the_forwarded_chain():
    headers = {"cf-connecting-ip": "9.9.9.9", "x-forwarded-for": "1.2.3.4, 9.9.9.9, 10.0.0.1"}
    request = type("R", (), {"headers": headers, "client": None})
    assert router._caller(request) == "9.9.9.9"


def test_x_real_ip_is_read_when_there_is_no_cloudflare():
    headers = {"x-real-ip": "9.9.9.9", "x-forwarded-for": "1.2.3.4, 9.9.9.9"}
    request = type("R", (), {"headers": headers, "client": None})
    assert router._caller(request) == "9.9.9.9"


def test_without_a_forwarded_header_the_peer_is_the_caller():
    request = type("R", (), {"headers": {}, "client": type("C", (), {"host": "10.0.0.9"})})
    assert router._caller(request) == "10.0.0.9"


def test_columns_keep_only_keys_the_rows_carry():
    given = [
        {"key": "username", "label": "Who", "format": "username"},
        {"key": "ghost", "label": "Nothing", "format": "text"},
    ]
    columns = router._columns(given, [{"username": "ada"}])
    assert columns == [{"key": "username", "label": "Who", "format": "username"}]


def test_an_unknown_format_falls_back_to_text():
    columns = router._columns([{"key": "a", "label": "A", "format": "sparkline"}], [{"a": "x"}])
    assert columns[0]["format"] == "text"


def test_columns_are_read_off_the_rows_when_the_model_names_none():
    columns = router._columns(None, [{"title": "x", "banner_url": "y", "last_crawled": "z"}])
    assert [column["key"] for column in columns] == ["title"]


def test_an_empty_answer_keeps_the_headings_it_was_given():
    given = [{"key": "day", "label": "Day", "format": "date"}]
    assert router._columns(given, []) == [{"key": "day", "label": "Day", "format": "date"}]


def test_a_single_figure_needs_one_row_and_one_column():
    one = [{"key": "n", "label": "N", "format": "number"}]
    assert router._display("number", None, one, [{"n": 5}]) == "number"
    assert router._display("number", None, one, [{"n": 5}, {"n": 6}]) == "table"


def test_a_bar_needs_a_pair_of_columns_it_can_plot():
    columns = [
        {"key": "title", "label": "Project", "format": "text"},
        {"key": "views", "label": "Views", "format": "number"},
    ]
    rows = [{"title": "a", "views": 2}]
    assert router._display("bar", {"label": "title", "value": "views"}, columns, rows) == "bar"
    assert router._display("bar", {"label": "title", "value": "gone"}, columns, rows) == "table"


def test_a_bar_of_text_is_a_table():
    columns = [{"key": "a", "label": "A", "format": "text"}, {"key": "b", "label": "B", "format": "text"}]
    rows = [{"a": "x", "b": "y"}]
    assert router._display("bar", {"label": "a", "value": "b"}, columns, rows) == "table"


def test_too_many_bars_read_better_as_a_table():
    columns = [
        {"key": "title", "label": "Project", "format": "text"},
        {"key": "views", "label": "Views", "format": "number"},
    ]
    rows = [{"title": str(n), "views": n} for n in range(router.MAX_BARS + 1)]
    assert router._display("bar", {"label": "title", "value": "views"}, columns, rows) == "table"


def test_a_count_the_model_called_text_is_read_as_a_figure():
    given = [{"key": "ships", "label": "Ships", "format": "text"}]
    columns = router._columns(given, [{"ships": 3}, {"ships": 5}])
    assert columns[0]["format"] == "number"


def test_an_id_is_left_as_a_name_not_a_figure():
    given = [{"key": "project_id", "label": "Project", "format": "text"}]
    assert router._columns(given, [{"project_id": 8100}])[0]["format"] == "text"


def test_a_column_of_mixed_values_stays_text():
    given = [{"key": "payout", "label": "Payout", "format": "text"}]
    assert router._columns(given, [{"payout": 3}, {"payout": None}])[0]["format"] == "text"


def test_a_yes_or_no_column_is_not_a_figure():
    given = [{"key": "on_sale", "label": "On sale", "format": "text"}]
    assert router._columns(given, [{"on_sale": True}])[0]["format"] == "text"
