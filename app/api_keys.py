"""API key -> tier lookup.

Keys and their tiers live in Redis (`apikey:{key}` -> tier name) so they can be
provisioned without redeploying the gateway. A handful of demo keys are seeded
on startup; in a real system this table would be managed by a signup/billing
service.
"""

from app.config import settings
from app.redis_client import get_client

DEMO_KEYS = {
    "demo-free-key": "free",
    "demo-pro-key": "pro",
    "demo-enterprise-key": "enterprise",
}


def seed_demo_keys() -> None:
    client = get_client()
    for key, tier in DEMO_KEYS.items():
        client.set(f"apikey:{key}", tier)


def get_tier(api_key: str) -> str:
    client = get_client()
    tier = client.get(f"apikey:{api_key}")
    return tier or settings.default_tier
