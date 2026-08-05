from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_prefix="STARDANCE_", extra="ignore"
    )

    base_url: str = "https://stardance.hackclub.com"

    # Keep the contact address real; being reachable is the point of the header.
    user_agent: str = (
        "stardance-stats/0.1 (+https://github.com/stardance-stats; "
        "contact: mixidmb@gmail.com)"
    )

    # Upstream throttles at 600 req / 5 min and 120 / min per IP.
    requests_per_second: float = 1.0
    max_concurrency: int = 1
    request_timeout: float = 30.0
    max_response_bytes: int = 5 * 1024 * 1024
    max_retries: int = 4

    mongo_url: str = "mongodb://localhost:27017"
    mongo_db: str = "stardance_stats"

    # Snapshot at least this often so charts get a heartbeat, not gaps.
    snapshot_heartbeat_hours: int = 24
    # Reject a snapshot whose monotonic counters fall further than this.
    anomaly_drop_threshold: float = 0.20

    @property
    def api_title(self) -> str:
        return "Stardance Stats API"


settings = Settings()
