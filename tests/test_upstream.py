"""Tests for the mock upstream's request counter.

The counter is load-bearing: the chaos test and verify script both use it to
prove the circuit breaker actually shields the upstream. If it under-counts,
those checks would report success while traffic was leaking through.
"""

import pytest
from fastapi.testclient import TestClient

from upstream.main import app


@pytest.fixture
def client():
    c = TestClient(app)
    c.post("/admin/reset-stats")
    c.post("/admin/recover")
    yield c
    c.post("/admin/recover")


def test_counts_successful_requests(client):
    for _ in range(5):
        client.get("/resource/1")
    assert client.get("/admin/stats").json()["requests_received"] == 5


def test_counts_requests_that_arrive_while_failing(client):
    """A request that arrives and gets a 503 still *reached* the upstream.

    This is the whole point — the breaker exists to stop requests arriving at
    all. Counting only successes would make a leaking breaker look perfect.
    """
    client.post("/admin/fail")
    for _ in range(3):
        assert client.get("/resource/1").status_code == 503

    assert client.get("/admin/stats").json()["requests_received"] == 3


def test_reset_clears_the_counter(client):
    client.get("/resource/1")
    assert client.get("/admin/stats").json()["requests_received"] == 1

    client.post("/admin/reset-stats")
    assert client.get("/admin/stats").json()["requests_received"] == 0


def test_health_and_admin_calls_are_not_counted(client):
    client.get("/health")
    client.get("/admin/stats")
    assert client.get("/admin/stats").json()["requests_received"] == 0
