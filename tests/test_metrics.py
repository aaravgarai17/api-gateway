import respx
from fastapi.testclient import TestClient
from httpx import Response

from app.api_keys import seed_demo_keys
from app.main import app


def test_metrics_endpoint_exposes_prometheus_format(fake_redis):
    client = TestClient(app)
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert "# HELP" in resp.text
    assert "gateway_requests_total" in resp.text


def test_instance_id_header_present(fake_redis):
    client = TestClient(app)
    assert "X-Instance-Id" in client.get("/health").headers


def test_rate_limit_rejection_is_recorded_with_tier(fake_redis):
    seed_demo_keys()
    client = TestClient(app)

    # Drain the free-tier bucket (capacity 10) with no upstream mocked; the
    # rejections happen before any upstream call is attempted.
    with respx.mock:
        respx.get(url__regex=r".*").mock(return_value=Response(200, json={"ok": True}))
        for _ in range(12):
            client.get("/proxy/resource/1", headers={"X-API-Key": "demo-free-key"})

    body = client.get("/metrics").text
    assert 'reason="rate_limited"' in body
    assert 'tier="free"' in body


def test_missing_api_key_rejection_recorded(fake_redis):
    client = TestClient(app)
    client.get("/proxy/resource/1")
    body = client.get("/metrics").text
    assert 'reason="missing_api_key"' in body


@respx.mock
def test_upstream_success_recorded(fake_redis):
    seed_demo_keys()
    respx.get(url__regex=r".*").mock(return_value=Response(200, json={"ok": True}))

    client = TestClient(app)
    client.get("/proxy/resource/1", headers={"X-API-Key": "demo-enterprise-key"})

    body = client.get("/metrics").text
    assert 'gateway_upstream_results_total{instance_id="local",result="success"}' in body
    assert "gateway_upstream_duration_seconds" in body


@respx.mock
def test_circuit_state_gauge_reflects_open_circuit(fake_redis, monkeypatch):
    from app import circuit_breaker as cb

    monkeypatch.setattr(cb.settings, "cb_failure_threshold", 2)
    seed_demo_keys()
    respx.get(url__regex=r".*").mock(return_value=Response(503))

    client = TestClient(app)
    for _ in range(3):
        client.get("/proxy/resource/1", headers={"X-API-Key": "demo-enterprise-key"})

    body = client.get("/metrics").text
    # 2.0 == open
    assert 'gateway_circuit_state{circuit="upstream",instance_id="local"} 2.0' in body
    assert 'reason="circuit_open"' in body


@respx.mock
def test_endpoint_label_uses_route_template(fake_redis):
    """Guards against unbounded cardinality from proxied paths."""
    seed_demo_keys()
    respx.get(url__regex=r".*").mock(return_value=Response(200, json={"ok": True}))

    client = TestClient(app)
    client.get(
        "/proxy/some/deep/unique/path", headers={"X-API-Key": "demo-enterprise-key"}
    )

    body = client.get("/metrics").text
    assert 'endpoint="/proxy/{path:path}"' in body
    assert 'endpoint="/proxy/some/deep/unique/path"' not in body
