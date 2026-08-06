from __future__ import annotations

import argparse
import asyncio
import sys
from collections import Counter
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import settings  # noqa: E402
from src.fetcher import Fetcher  # noqa: E402

PROBE_PATH = "/projects/8100"
TRACE_URL = "https://cloudflare.com/cdn-cgi/trace"
# Upstream throttles per address, so this is the line a single route must stay under.
PER_ADDRESS_LIMIT = 2.0


async def health(proxy_url: str, gate: asyncio.Semaphore) -> dict:
    """Dial the trace endpoint through the proxy and read the address back."""
    row: dict = {"route": _safe(proxy_url)}
    try:
        async with gate:
            async with httpx.AsyncClient(proxy=proxy_url, timeout=20.0, http2=False) as client:
                response = await client.get(TRACE_URL)
        row["status"] = response.status_code
        fields = dict(
            line.split("=", 1) for line in response.text.splitlines() if "=" in line
        )
        row["egress_ip"] = fields.get("ip")
        row["colo"] = fields.get("colo", "-")
    except ImportError as exc:
        row["status"] = "error"
        row["error"] = f'{exc} -- install with: pip install "httpx[socks]"'
    except Exception as exc:
        row["status"] = "error"
        row["error"] = f"{type(exc).__name__}: {exc}"
    return row


def _safe(url: str) -> str:
    """Strip credentials, since this gets printed."""
    parts = httpx.URL(url)
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{parts.host}{port}"


async def main() -> int:
    ap = argparse.ArgumentParser(description="Verify the crawl route pool.")
    ap.add_argument(
        "--probe", action="store_true",
        help="also fetch one real page through the pool (costs 1 upstream request)",
    )
    ap.add_argument("--concurrency", type=int, default=25)
    args = ap.parse_args()

    proxies = settings.proxy_list
    if not proxies:
        print("No proxies configured. Set STARDANCE_PROXIES or STARDANCE_PROXIES_FILE.")
        return 1

    gate = asyncio.Semaphore(args.concurrency)
    rows = list(await asyncio.gather(*(health(p, gate) for p in proxies)))
    rate = settings.proxy_requests_per_second

    width = max(len(r["route"]) for r in rows)
    print(f"{'route'.ljust(width)}  {'status':>6}  {'colo':>5}  {'rps':>5}  egress ip")
    for row in rows:
        print(
            f"{row['route'].ljust(width)}  {str(row['status']):>6}  "
            f"{str(row.get('colo', '-')):>5}  {rate:>5}  "
            f"{row.get('egress_ip') or row.get('error', '-')}"
        )

    live = [r for r in rows if r.get("egress_ip")]
    distinct = sorted({r["egress_ip"] for r in live})
    print()
    print(f"{len(rows)} route(s), {len(live)} live, {len(distinct)} distinct egress address(es)")
    print(f"aggregate rate: {len(rows) * rate:.2f} req/s")

    if live:
        by_ip: Counter = Counter()
        for row in live:
            by_ip[row["egress_ip"]] += rate
        over = [(ip, r) for ip, r in by_ip.most_common() if r > PER_ADDRESS_LIMIT]
        if over:
            print(f"\n{len(over)} address(es) over the {PER_ADDRESS_LIMIT}/s per-address limit:")
            for ip, per_ip in over:
                n = sum(1 for r in live if r["egress_ip"] == ip)
                print(f"  {ip}: {n} route(s) -> {per_ip:.2f} req/s on one bucket")
        else:
            print(
                f"every address is under the {PER_ADDRESS_LIMIT}/s limit "
                f"(busiest: {by_ip.most_common(1)[0][1]:.2f} req/s)"
            )

    if args.probe:
        print(f"\nprobing {PROBE_PATH} through the pool...")
        async with Fetcher() as fetcher:
            print(f"aggregate configured rate: {fetcher.aggregate_rps:.1f} req/s")
            result = await fetcher.get(PROBE_PATH)
        print(f"  {result.status}, {len(result.text or ''):,} bytes, etag={result.etag}")
        if not result.ok:
            return 1

    return 0 if all(r.get("status") == 200 for r in rows) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
