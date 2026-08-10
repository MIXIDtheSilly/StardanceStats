from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

from pydantic_settings import BaseSettings, SettingsConfigDict


def _split(value: str) -> list[str]:
    """Comma-separated list, tolerant of whitespace and trailing slashes."""
    return [item.strip().rstrip("/") for item in value.split(",") if item.strip()]


def parse_proxy_line(line: str, *, scheme: str = "http") -> str | None:
    """One proxy list entry to a URL, or None for blanks and comments."""
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if "://" in line:
        return line.rstrip("/")

    parts = line.split(":")
    if len(parts) == 4:
        host, port, user, password = parts
        return f"{scheme}://{quote(user, safe='')}:{quote(password, safe='')}@{host}:{port}"
    if len(parts) == 2:
        return f"{scheme}://{line}"
    return None


def load_proxy_file(path: str, *, scheme: str = "http") -> list[str]:
    file = Path(path)
    if not file.is_file():
        return []
    parsed = (parse_proxy_line(line, scheme=scheme) for line in file.read_text().splitlines())
    return [url for url in parsed if url]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_prefix="STARDANCE_", extra="ignore"
    )

    base_url: str = "https://stardance.hackclub.com"

    # Being reachable is the point of this header; keep the address real.
    user_agent: str = (
        "stardance-stats/0.1 (+https://github.com/stardance-stats; "
        "contact: mixidmb@gmail.com)"
    )

    # Upstream throttles at 600 req / 5 min and 120 / min per address.
    requests_per_second: float = 1.0
    max_concurrency: int = 1
    request_timeout: float = 30.0
    max_response_bytes: int = 5 * 1024 * 1024
    max_retries: int = 4

    # Only raises the real ceiling if routes egress from different addresses.
    proxies: str = ""
    proxies_file: str = ""
    proxy_scheme: str = "http"
    proxy_requests_per_second: float = 1.0
    proxy_failure_threshold: int = 3
    proxy_failure_cooldown: float = 60.0

    mongo_url: str = "mongodb://localhost:27017"
    mongo_db: str = "stardance_stats"

    sitemap_path: str = "/sitemap.xml"
    # One 5 MB document, so it gets its own ceiling.
    sitemap_max_bytes: int = 64 * 1024 * 1024

    tier_hot_days: int = 3
    tier_warm_days: int = 30
    tier_hot_hours: float = 6.0
    tier_warm_hours: float = 24.0
    tier_cold_hours: float = 24.0 * 7
    tier_frozen_hours: float = 24.0 * 30
    # This, not the lastmod window, is what makes 30k pages fit in a day.
    # Quiet time, not crawl count, so a faster tier cannot demote sooner.
    cold_after_unchanged_hours: float = 30.0

    # No ETag on the catalogue, so the interval is the whole cost control:
    # 7 regions x ~220 KB a sweep.
    crawl_shop: bool = True
    shop_interval_minutes: int = 60

    # One fetch per thread whose counter moved.
    crawl_comments: bool = True
    comment_queue_limit: int = 2000
    # Only a backstop: the project page's counter is what queues a thread.
    comment_recheck_hours: float = 24.0 * 14

    max_error_backoff_hours: float = 12.0
    crawl_batch_size: int = 50
    # 0 means two per route, so a route is not idle while its task parses.
    crawl_concurrency: int = 0
    crawl_idle_seconds: float = 60.0

    snapshot_heartbeat_hours: int = 24
    anomaly_drop_threshold: float = 0.20

    # Tiers crawl every 6 h to 7 d, so two quiet days is worth telling a reader.
    api_stale_after_hours: float = 48.0
    api_cache_seconds: int = 300

    @property
    def proxy_list(self) -> list[str]:
        """Inline entries plus the file's, de-duplicated."""
        entries = [
            url
            for url in (
                parse_proxy_line(item, scheme=self.proxy_scheme)
                for item in _split(self.proxies)
            )
            if url
        ]
        if self.proxies_file:
            entries += load_proxy_file(self.proxies_file, scheme=self.proxy_scheme)
        return list(dict.fromkeys(entries))

    @property
    def api_title(self) -> str:
        return "Stardance Stats API"


settings = Settings()
