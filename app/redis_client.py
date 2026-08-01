import redis

from app.config import settings

_client: redis.Redis | None = None


def get_client() -> redis.Redis:
    """Lazily create a shared Redis client (module-level singleton)."""
    global _client
    if _client is None:
        _client = redis.Redis.from_url(settings.redis_url, decode_responses=True)
    return _client


def set_client(client) -> None:
    """Inject a client (used by tests to swap in fakeredis)."""
    global _client
    _client = client
