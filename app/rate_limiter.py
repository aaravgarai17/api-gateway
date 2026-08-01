"""Token bucket rate limiter, implemented as an atomic Redis WATCH/MULTI
read-modify-write per key.

Each API key gets a bucket: `tokens` (float, current balance) and
`last_refill` (timestamp). On each request we lazily refill based on elapsed
time * refill_rate, capped at `capacity`, then attempt to withdraw one token.

Why token bucket over the sliding-window counter used in the URL shortener
project: token bucket naturally allows short bursts up to `capacity` while
still enforcing a sustained average rate — better fit for an API gateway
fronting bursty client traffic. The state read + check + write must happen
atomically or two concurrent requests could both read the same balance and
both succeed when only one token remains (a classic check-then-act race).
We guard against that with Redis WATCH: if another client mutates the key
between our read and write, the transaction fails and we retry.
"""

import time

from redis import WatchError

from app.config import TIER_LIMITS, settings
from app.redis_client import get_client


class RateLimitResult:
    def __init__(self, allowed: bool, remaining: float, retry_after: float | None):
        self.allowed = allowed
        self.remaining = remaining
        self.retry_after = retry_after


def _bucket_key(api_key: str) -> str:
    return f"bucket:{api_key}"


def check_and_consume(api_key: str, tier: str) -> RateLimitResult:
    """Attempt to withdraw one token for `api_key`. Returns whether it was
    allowed, tokens remaining, and (if denied) seconds until a token is next
    available.
    """
    limits = TIER_LIMITS.get(tier, TIER_LIMITS[settings.default_tier])
    capacity = limits["capacity"]
    refill_per_minute = limits["refill_per_minute"]
    refill_per_second = refill_per_minute / 60.0

    client = get_client()
    key = _bucket_key(api_key)

    for _ in range(10):  # bounded retries under contention
        with client.pipeline() as pipe:
            try:
                pipe.watch(key)
                raw = pipe.hgetall(key)
                now = time.time()

                if raw:
                    tokens = float(raw["tokens"])
                    last_refill = float(raw["last_refill"])
                else:
                    tokens = float(capacity)
                    last_refill = now

                elapsed = max(0.0, now - last_refill)
                tokens = min(capacity, tokens + elapsed * refill_per_second)

                if tokens >= 1.0:
                    tokens -= 1.0
                    allowed = True
                    retry_after = None
                else:
                    allowed = False
                    deficit = 1.0 - tokens
                    retry_after = deficit / refill_per_second

                pipe.multi()
                pipe.hset(key, mapping={"tokens": tokens, "last_refill": now})
                # Expire idle buckets so we don't leak memory for old keys.
                pipe.expire(key, max(60, int(capacity / max(refill_per_second, 0.001))))
                pipe.execute()

                return RateLimitResult(allowed=allowed, remaining=tokens, retry_after=retry_after)
            except WatchError:
                continue  # another request mutated the bucket; retry

    # Contention exhausted retries; fail closed (deny) rather than risk
    # double-spending tokens.
    return RateLimitResult(allowed=False, remaining=0.0, retry_after=1.0)
