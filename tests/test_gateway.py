import respx
from fastapi.testclient import TestClient
from httpx import Response

from app.api_keys import seed_demo_keys
from app.main import app


@respx.mock
def test_proxy_forwards_to_upstream(fake_redis):
    seed_demo_keys()
    respx.get("http://localhost:9000/resource/42").mock(
        return_value=Response(200, json={"item_id": "42", "data": "payload-for-42"})
    )

    client = TestClient(app)
    resp = client.get("/proxy/resource/42", headers={"X-API-Key": "demo-free-key"})

    assert resp.status_code == 200
    assert resp.json()["item_id"] == "42"


def test_missing_api_key_rejected(fake_redis):
    client = TestClient(app)
    resp = client.get("/proxy/resource/1")
    assert resp.status_code == 401


@respx.mock
def test_rate_limit_enforced_per_tier(fake_redis):
    seed_demo_keys()
    respx.get(url__regex=r".*").mock(
        return_value=Response(200, json={"ok": True})
    )

    client = TestClient(app)
    # free tier capacity is 10
    for _ in range(10):
        resp = client.get("/proxy/resource/1", headers={"X-API-Key": "demo-free-key"})
        assert resp.status_code == 200

    resp = client.get("/proxy/resource/1", headers={"X-API-Key": "demo-free-key"})
    assert resp.status_code == 429
    assert "Retry-After" in resp.headers


@respx.mock
def test_circuit_opens_after_repeated_upstream_failures(fake_redis, monkeypatch):
    from app import circuit_breaker as cb

    monkeypatch.setattr(cb.settings, "cb_failure_threshold", 2)
    seed_demo_keys()

    respx.get(url__regex=r".*").mock(return_value=Response(503))

    client = TestClient(app)
    # First two failures trip the breaker (each proxied call gets a 502/503
    # from upstream and is recorded as a circuit failure).
    for _ in range(2):
        client.get("/proxy/resource/1", headers={"X-API-Key": "demo-enterprise-key"})

    resp = client.get("/proxy/resource/1", headers={"X-API-Key": "demo-enterprise-key"})
    assert resp.status_code == 503
    assert "circuit open" in resp.json()["detail"]
