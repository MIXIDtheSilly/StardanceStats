from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from src.config import load_proxy_file, parse_proxy_line, settings
from src.fetcher import Endpoint, Fetcher, RateLimiter, _build_endpoints

# RFC 5737 documentation addresses, never a real route.
PROXIES = [
    "http://192.0.2.1:3128",
    "http://192.0.2.2:3128",
    "http://192.0.2.3:3128",
    "http://192.0.2.4:3128",
]
NAMES = ["192.0.2.1:3128", "192.0.2.2:3128", "192.0.2.3:3128", "192.0.2.4:3128"]

SITE = "https://stardance.hackclub.com/projects/8100"


@pytest.fixture(autouse=True)
def isolate_from_local_env(monkeypatch):
    """Never inherit the routes a developer happens to have in .env."""
    monkeypatch.setattr(settings, "proxies", "")
    monkeypatch.setattr(settings, "proxies_file", "")


def html(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, html="<html><body>ok</body></html>")


async def attach(fetcher: Fetcher, make_handler) -> Fetcher:
    """One stub client per route: a proxy leaves the URL alone, so only the client differs."""
    for endpoint in fetcher.endpoints_ref:
        handler = make_handler(endpoint.name)
        endpoint.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    fetcher._open = True
    return fetcher


def always(handler):
    """Same handler whichever route it is."""
    return lambda name: handler


def test_nothing_configured_keeps_a_single_direct_route():
    endpoints = _build_endpoints([], direct_rps=1.0)
    assert len(endpoints) == 1
    assert endpoints[0].name == "direct"
    assert endpoints[0].is_proxy is False
    assert endpoints[0].proxy_url is None


def test_a_proxy_route_dials_the_proxy_and_leaves_the_url_alone():
    """Rewriting the URL would ask the proxy for a page hosted on itself."""
    (proxy,) = _build_endpoints(["http://198.51.100.7:3128"], direct_rps=1.0)
    assert proxy.kind == "proxy"
    assert proxy.is_proxy
    assert proxy.proxy_url == "http://198.51.100.7:3128"


def test_route_names_drop_credentials():
    """The name goes into logs, so a password must not ride along."""
    (proxy,) = _build_endpoints(["socks5://user:hunter2@198.51.100.7:1080"], direct_rps=1.0)
    assert proxy.name == "198.51.100.7:1080"
    assert "hunter2" not in proxy.name


def test_provider_host_port_user_pass_lines_become_urls():
    """Providers hand out host:port:user:pass, which no client accepts as-is."""
    assert parse_proxy_line("198.51.100.7:6754:exampleuser:examplepass") == (
        "http://exampleuser:examplepass@198.51.100.7:6754"
    )
    assert parse_proxy_line("198.51.100.7:3128") == "http://198.51.100.7:3128"
    assert parse_proxy_line("203.0.113.9:1080:u:p", scheme="socks5") == (
        "socks5://u:p@203.0.113.9:1080"
    )


def test_full_urls_pass_through_untouched():
    for line in ("http://198.51.100.7:3128", "socks5://user:pass@host:1080"):
        assert parse_proxy_line(line) == line


def test_credentials_are_percent_encoded():
    """A password holding @ or : would reshape the URL and redirect the client."""
    url = parse_proxy_line("1.2.3.4:8080:user@corp:p@ss:word")
    assert url is None  # five fields: ambiguous, so rejected rather than guessed

    url = parse_proxy_line("1.2.3.4:8080:user:p@ss")
    assert url == "http://user:p%40ss@1.2.3.4:8080"
    assert httpx.URL(url).host == "1.2.3.4"
    assert httpx.URL(url).password == "p@ss"


def test_blanks_and_comments_are_skipped():
    assert parse_proxy_line("") is None
    assert parse_proxy_line("   ") is None
    assert parse_proxy_line("# a comment") is None
    assert parse_proxy_line("nonsense") is None


def test_proxy_file_is_read_and_filtered(tmp_path):
    path = tmp_path / "proxies.txt"
    path.write_text(
        "# provider list\n"
        "198.51.100.7:6754:user:pass\n"
        "\n"
        "socks5://203.0.113.9:1080\n"
        "garbage\n"
    )
    assert load_proxy_file(str(path)) == [
        "http://user:pass@198.51.100.7:6754",
        "socks5://203.0.113.9:1080",
    ]


def test_a_missing_proxy_file_is_empty_not_an_error():
    """The collector must still start when the list has not been fetched yet."""
    assert load_proxy_file("no/such/file.txt") == []


def test_inline_entries_and_the_file_merge_without_duplicates(tmp_path, monkeypatch):
    """A provider list belongs in a file, a one-off route inline, both in one pool."""
    path = tmp_path / "list.txt"
    path.write_text("198.51.100.7:6754\n203.0.113.9:3128\n")
    monkeypatch.setattr(settings, "proxies", "203.0.113.9:3128, 192.0.2.5:8080")
    monkeypatch.setattr(settings, "proxies_file", str(path))

    assert settings.proxy_list == [
        "http://203.0.113.9:3128",
        "http://192.0.2.5:8080",
        "http://198.51.100.7:6754",
    ]


def test_the_scheme_setting_reaches_both_sources(tmp_path, monkeypatch):
    path = tmp_path / "list.txt"
    path.write_text("198.51.100.7:6754\n")
    monkeypatch.setattr(settings, "proxies", "203.0.113.9:1080")
    monkeypatch.setattr(settings, "proxies_file", str(path))
    monkeypatch.setattr(settings, "proxy_scheme", "socks5")

    assert settings.proxy_list == [
        "socks5://203.0.113.9:1080",
        "socks5://198.51.100.7:6754",
    ]


def test_every_route_is_rated_the_same():
    """One knob, because every proxy is one address with one budget."""
    fetcher = Fetcher(proxies=PROXIES)
    assert {e.limiter.rps for e in fetcher.endpoints} == {
        settings.proxy_requests_per_second
    }


def test_aggregate_rate_is_the_sum_of_the_pool():
    """What upstream experiences, split across buckets only if the routes differ."""
    fetcher = Fetcher(proxies=PROXIES)
    assert fetcher.aggregate_rps == pytest.approx(4 * settings.proxy_requests_per_second)
    assert Fetcher(proxies=[], rps=1.0).aggregate_rps == pytest.approx(1.0)


def test_reserve_is_atomic_and_spaces_requests():
    limiter = RateLimiter(2.0)  # every 0.5s
    waits = [limiter.reserve() for _ in range(3)]
    assert waits[0] == pytest.approx(0.0, abs=0.01)
    assert waits[1] == pytest.approx(0.5, abs=0.01)
    assert waits[2] == pytest.approx(1.0, abs=0.01)


async def test_requests_spread_across_the_pool():
    """Eight requests over four routes, not eight against whichever came first."""
    seen: list[str] = []

    def make(name):
        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(name)
            return html(request)
        return handler

    fetcher = Fetcher(proxies=PROXIES)
    await attach(fetcher, make)
    for _ in range(8):
        await fetcher.get("/projects/1")

    assert sorted(seen) == sorted(NAMES * 2)
    # And the first four go out without waiting on each other.
    assert seen[:4] == NAMES


async def test_the_pool_paces_itself_at_the_aggregate_rate():
    fetcher = Fetcher(proxies=PROXIES[:2])
    await attach(fetcher, always(html))

    started = time.monotonic()
    for _ in range(4):
        await fetcher.get("/projects/1")
    elapsed = time.monotonic() - started

    # Two routes: four requests means one full interval of waiting.
    expected = 1.0 / settings.proxy_requests_per_second
    assert elapsed == pytest.approx(expected, abs=0.25)


async def test_conditional_headers_go_out_and_upstream_ones_come_back():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("If-None-Match") == 'W/"abc"'
        return httpx.Response(200, html="<html></html>", headers={"ETag": 'W/"xyz"'})

    fetcher = Fetcher(proxies=PROXIES[:1])
    await attach(fetcher, always(handler))
    result = await fetcher.get("/projects/1", etag='W/"abc"')

    assert result.etag == 'W/"xyz"'


async def test_the_request_url_is_the_site_not_the_proxy():
    """The proxy is dialled by the transport; the URL it carries is upstream's."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return html(request)

    fetcher = Fetcher(proxies=PROXIES[:1])
    await attach(fetcher, always(handler))
    await fetcher.get("/projects/8100")
    await fetcher.get(SITE)

    assert seen == [SITE, SITE]


async def test_a_429_parks_one_route_and_the_next_request_uses_another():
    """The throttle is per address, so one route says nothing about the others."""
    calls: list[str] = []

    def make(name):
        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(name)
            if name == NAMES[0]:
                return httpx.Response(429, headers={"Retry-After": "30"}, json={})
            return html(request)
        return handler

    fetcher = Fetcher(proxies=PROXIES[:2])
    await attach(fetcher, make)
    result = await fetcher.get("/projects/1")

    assert result.ok
    assert calls == NAMES[:2]
    parked, healthy = fetcher.endpoints
    assert parked.disabled_until > time.monotonic()
    assert healthy.disabled_until == 0.0


async def test_upstream_403_reads_as_an_answer_about_the_page():
    """Upstream's own refusal, returned rather than retried against the whole pool."""
    fetcher = Fetcher(proxies=PROXIES[:2])
    await attach(fetcher, always(lambda r: httpx.Response(403, html="<html>nope</html>")))
    result = await fetcher.get("/projects/1")

    assert result.status == 403 and not result.ok


async def test_a_failing_proxy_is_rested_so_the_pool_routes_around_it():
    def make(name):
        def handler(request: httpx.Request) -> httpx.Response:
            if name == NAMES[0]:
                raise httpx.ConnectError("proxy down")
            return html(request)
        return handler

    fetcher = Fetcher(proxies=PROXIES[:2])
    await attach(fetcher, make)

    for _ in range(settings.proxy_failure_threshold + 2):
        await fetcher.get("/projects/1")

    dead = fetcher.endpoints[0]
    assert dead.disabled_until > time.monotonic()


async def test_a_deallocated_proxy_rests_rather_than_failing_the_fetch():
    """Reissued endpoints answer 407; ten went at once, so the pool must shed them."""
    def make(name):
        def handler(request: httpx.Request) -> httpx.Response:
            if name == NAMES[0]:
                raise httpx.ProxyError("407 Proxy Authentication Required")
            return html(request)
        return handler

    fetcher = Fetcher(proxies=PROXIES[:2])
    await attach(fetcher, make)

    for _ in range(settings.proxy_failure_threshold):
        assert (await fetcher.get("/projects/1")).ok

    assert fetcher.endpoints[0].disabled_until > time.monotonic()
    assert fetcher.endpoints[1].disabled_until == 0.0


async def test_concurrent_callers_never_share_a_slot():
    """Picking a route and booking its slot must be one step, or the rate doubles."""
    endpoint = Endpoint(name="one", limiter=RateLimiter(10.0))
    fetcher = Fetcher(proxies=[])
    fetcher._endpoints = [endpoint]

    claims = await asyncio.gather(*(fetcher._claim() for _ in range(5)))
    waits = sorted(w for _, w in claims)
    assert waits == pytest.approx([0.0, 0.1, 0.2, 0.3, 0.4], abs=0.02)
