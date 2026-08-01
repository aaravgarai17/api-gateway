import time

from app.rate_limiter import check_and_consume


def test_allows_up_to_capacity(fake_redis):
    # free tier: capacity 10
    for _ in range(10):
        result = check_and_consume("key-a", "free")
        assert result.allowed

    denied = check_and_consume("key-a", "free")
    assert not denied.allowed
    assert denied.retry_after is not None


def test_buckets_are_independent_per_key(fake_redis):
    for _ in range(10):
        assert check_and_consume("key-b", "free").allowed
    assert not check_and_consume("key-b", "free").allowed

    # A different key has its own full bucket.
    assert check_and_consume("key-c", "free").allowed


def test_higher_tier_gets_higher_capacity(fake_redis):
    for _ in range(10):
        assert check_and_consume("pro-key", "pro").allowed
    # Still allowed past the free-tier limit because pro capacity is 100.
    assert check_and_consume("pro-key", "pro").allowed


def test_tokens_refill_over_time(fake_redis, monkeypatch):
    # free tier: capacity 10, refill 10/min => 1 token per 6s
    for _ in range(10):
        assert check_and_consume("key-d", "free").allowed
    assert not check_and_consume("key-d", "free").allowed

    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() + 6.5)

    result = check_and_consume("key-d", "free")
    assert result.allowed
