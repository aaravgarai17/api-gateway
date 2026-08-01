"""Shared fixtures: fakeredis in place of real Redis so the suite runs with
zero external services.
"""

import fakeredis
import pytest

from app import redis_client


@pytest.fixture(autouse=True)
def fake_redis():
    client = fakeredis.FakeRedis(decode_responses=True)
    redis_client.set_client(client)
    yield client
    redis_client.set_client(None)
