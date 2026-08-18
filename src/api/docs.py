from __future__ import annotations

import json

from fastapi.responses import HTMLResponse

from ..config import settings

# Pinned to a major so a CDN release cannot restyle the page unannounced.
SCALAR_JS = "https://cdn.jsdelivr.net/npm/@scalar/api-reference@1"

DESCRIPTION = """
Time-series statistics for the Stardance platform: projects, devlogs, users,
ships and the shop, each with the history behind its current numbers.

Every figure here is derived from Stardance's **public** pages. Rejected ships,
deleted devlogs and unverified profiles never appear to our crawler, so these
numbers describe the public view of the platform rather than platform truth.

## Getting started

No key, no signup, no auth header. Every read is a plain `GET`.

```bash
curl https://stardance.stats/v1/projects?limit=5
```

Endpoints are grouped by what they describe. Within a group the pattern is
consistent: a list endpoint that ranks, a detail endpoint keyed by id, and a
`/history` endpoint that returns the same numbers over time.

## Freshness

We crawl continuously, so an answer is a snapshot rather than a live read.
Every response carries the pair:

| Field | Meaning |
| --- | --- |
| `data_as_of` | When the underlying page was last read. `null` if never. |
| `stale` | `true` once `data_as_of` is more than %(stale)g hours old. |

On a list, `data_as_of` is the **oldest** row in the response, so it bounds the
whole page rather than flattering it. Treat `stale: true` as a hint that the
collector is behind, not that the numbers are wrong.

`GET /v1/health` reports whether the collector is actually moving.

## Caching

Reads under `/v1/` are cached and carry a strong `ETag`:

```
Cache-Control: public, max-age=%(cache)d
ETag: "6f1c0a…"
```

Send it back as `If-None-Match` and an unchanged resource answers `304` with no
body. Nothing served here is user-specific, so a shared cache is safe.

## Pagination

List endpoints take `limit` and `offset` and answer with `total` alongside
`items`, so you can page without a second call to learn the size. Ordering
always breaks ties on id, so paging never repeats or skips a row.

## History

Every `/history` endpoint shares one shape. Pick metrics, a bucket width, and a
window:

```
GET /v1/projects/1234/history?metrics=devlogs,likes&interval=1d&fill=locf
```

| Parameter | Notes |
| --- | --- |
| `metrics` | Comma-separated. The valid names differ per entity; each group's `metrics` endpoint lists them. |
| `interval` | `1h`, `1d` or `1w`. |
| `start`, `end` | ISO-8601 instants. Omit for everything we hold. |
| `delta` | Adds `d`, the change against the previous bucket. |
| `fill` | `none` omits empty buckets; `locf` carries the last observation forward and marks the point `filled: true`. |

Two flags can appear on a point, and both mean "not observed here":

- `filled: true` comes from `fill=locf`, carrying the previous value forward.
- `synthetic: true` marks a point reconstructed from timestamps rather than read
  off a page. Backfilled history is synthetic, so early buckets often carry it.

A bucket reports the **last** observation inside it, not an average. Points
come back per metric:

```json
{
  "interval": "1d",
  "buckets": 30,
  "observed_buckets": 28,
  "series": {
    "likes": [{ "ts": "2026-08-01T00:00:00Z", "v": 412, "d": 7 }]
  }
}
```

Because these are counters read off a page, a series can sit flat while a crawl
is pending and then step. `observed_buckets` tells you how many buckets we
actually saw.

## Errors

Plain HTTP codes with a JSON `detail` string.

| Code | When |
| --- | --- |
| `400` | A metric or filter that does not exist. The message lists the valid ones. |
| `404` | The id or handle is not in our corpus, which is not the same as not existing on Stardance. |
| `422` | A parameter failed validation. |
| `429` | Rate limited. Applies to `/v1/ask` only. |
| `503` | A feature is not configured on this deployment. |
""" % {"stale": settings.api_stale_after_hours, "cache": settings.api_cache_seconds}


TAGS = [
    {
        "name": "meta",
        "description": (
            "Is the service up, is the collector moving, and how much of the "
            "platform have we actually seen. Start here when a number looks wrong."
        ),
    },
    {
        "name": "global",
        "description": (
            "The platform summed into one row, and that row over time. Counts "
            "what we have crawled, so it trails the real platform slightly."
        ),
    },
    {
        "name": "projects",
        "description": (
            "Projects, ranked or fetched by id, with their ships, comments and "
            "history. `stardust_total` counts rated ship payouts plus what a "
            "mission pays directly; earnings off a project's own page are invisible to us."
        ),
    },
    {
        "name": "devlogs",
        "description": (
            "Individual devlogs across every project, searchable by text and "
            "rankable by engagement. Search matches word stems, so `solder` "
            "finds `soldering`; quote a phrase to match it as written."
        ),
    },
    {
        "name": "users",
        "description": (
            "Makers, by id or handle. Previous handles resolve too, so a link "
            "survives a rename. The leaderboard is our own ranking, computed "
            "from crawled rows rather than read off the platform."
        ),
    },
    {
        "name": "shop",
        "description": (
            "The reward catalogue and how its prices move. Prices are Stardust, "
            "not currency, and differ per region."
        ),
    },
    {
        "name": "ask",
        "description": (
            "Natural-language questions, answered by turning them into one "
            "read-only aggregation. Served through the site rather than the "
            "public API, so a direct call answers `403`."
        ),
    },
]


def scalar_page(openapi_url: str, title: str) -> HTMLResponse:
    """The reference UI, rendered from the schema the app already publishes."""
    configuration = {
        # The bundle reads the spec from here; it ignores a data-url attribute.
        "url": openapi_url,
        "theme": "purple",
        "darkMode": True,
        "layout": "modern",
        "hideDownloadButton": False,
        "defaultHttpClient": {"targetKey": "shell", "clientKey": "curl"},
        "searchHotKey": "k",
    }
    return HTMLResponse(f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>{title}</title>
    <link rel="icon" href="data:," />
    <style>
      body {{ margin: 0; }}
    </style>
  </head>
  <body>
    <div id="app"></div>
    <script src="{SCALAR_JS}"></script>
    <script>
      Scalar.createApiReference('#app', {json.dumps(configuration)});
    </script>
  </body>
</html>""")
