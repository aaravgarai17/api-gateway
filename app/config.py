from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application configuration loaded from environment variables."""

    redis_url: str = "redis://localhost:6379/0"
    upstream_url: str = "http://localhost:9000"

    cb_failure_threshold: int = 5
    cb_recovery_timeout_seconds: int = 30
    cb_half_open_max_calls: int = 1

    default_tier: str = "free"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()

# Requests-per-minute and burst capacity per subscription tier.
# "capacity" is the bucket size (max burst); "refill_per_minute" is the
# sustained throughput once the bucket is drained.
TIER_LIMITS = {
    "free": {"capacity": 10, "refill_per_minute": 10},
    "pro": {"capacity": 100, "refill_per_minute": 100},
    "enterprise": {"capacity": 1000, "refill_per_minute": 1000},
}
