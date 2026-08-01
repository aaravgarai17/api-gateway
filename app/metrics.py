"""Prometheus metrics for the gateway.

A gateway's dashboard needs to answer three operational questions that a plain
web service's does not:

  1. How much traffic am I *rejecting*, and why? (rate limited vs. circuit open
     vs. upstream error) — this is the difference between "we're fine, a noisy
     client got throttled" and "the backend is down."
  2. What is the circuit breaker's state right now? Exposed as a gauge so it
     can be alerted on and graphed as a state timeline.
  3. How much latency am I adding on top of the upstream? Tracked as separate
     histograms for total gateway time and upstream call time; the difference
     is the gateway's own overhead.
"""

import os
import time

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

INSTANCE_ID = os.getenv("HOSTNAME", "local")

REQUESTS = Counter(
    "gateway_requests_total",
    "Requests handled by the gateway",
    ["method", "endpoint", "status", "instance_id"],
)

LATENCY = Histogram(
    "gateway_request_duration_seconds",
    "End-to-end gateway request latency (includes upstream call)",
    ["method", "endpoint", "instance_id"],
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)

UPSTREAM_LATENCY = Histogram(
    "gateway_upstream_duration_seconds",
    "Time spent waiting on the upstream service",
    ["instance_id"],
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)

# The key gateway metric: why was a request rejected?
REJECTIONS = Counter(
    "gateway_rejections_total",
    "Requests rejected by the gateway before/instead of reaching upstream",
    ["reason", "tier", "instance_id"],
)

RATE_LIMIT_ALLOWED = Counter(
    "gateway_rate_limit_allowed_total",
    "Requests that passed the rate limiter",
    ["tier", "instance_id"],
)

UPSTREAM_RESULTS = Counter(
    "gateway_upstream_results_total",
    "Outcomes of calls actually forwarded to the upstream",
    ["result", "instance_id"],
)

# 0 = closed (healthy), 1 = half-open (probing), 2 = open (short-circuiting).
# A gauge rather than a counter because it is a state, not an accumulation.
CIRCUIT_STATE = Gauge(
    "gateway_circuit_state",
    "Circuit breaker state: 0=closed, 1=half_open, 2=open",
    ["circuit", "instance_id"],
)

_STATE_VALUES = {"closed": 0, "half_open": 1, "open": 2}


def record_rejection(reason: str, tier: str = "unknown") -> None:
    REJECTIONS.labels(reason=reason, tier=tier, instance_id=INSTANCE_ID).inc()


def record_rate_limit_allowed(tier: str) -> None:
    RATE_LIMIT_ALLOWED.labels(tier=tier, instance_id=INSTANCE_ID).inc()


def record_upstream_result(result: str) -> None:
    UPSTREAM_RESULTS.labels(result=result, instance_id=INSTANCE_ID).inc()


def observe_upstream_latency(seconds: float) -> None:
    UPSTREAM_LATENCY.labels(instance_id=INSTANCE_ID).observe(seconds)


def set_circuit_state(circuit: str, state: str) -> None:
    CIRCUIT_STATE.labels(circuit=circuit, instance_id=INSTANCE_ID).set(
        _STATE_VALUES.get(state, 0)
    )


def _endpoint_label(request) -> str:
    """Route template, not raw path — avoids unbounded label cardinality.

    Every proxied path would otherwise become its own time series.
    """
    route = request.scope.get("route")
    if route is not None and getattr(route, "path", None):
        return route.path
    return "unmatched"


class MetricsMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        start = time.perf_counter()
        response = await call_next(request)
        elapsed = time.perf_counter() - start

        endpoint = _endpoint_label(request)
        REQUESTS.labels(
            method=request.method,
            endpoint=endpoint,
            status=str(response.status_code),
            instance_id=INSTANCE_ID,
        ).inc()
        LATENCY.labels(
            method=request.method,
            endpoint=endpoint,
            instance_id=INSTANCE_ID,
        ).observe(elapsed)

        response.headers["X-Instance-Id"] = INSTANCE_ID
        return response


def metrics_endpoint() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
