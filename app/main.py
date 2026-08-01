from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Header, HTTPException, Request, Response

from app import circuit_breaker as cb
from app.api_keys import get_tier, seed_demo_keys
from app.config import settings
from app.rate_limiter import check_and_consume

UPSTREAM_CIRCUIT = "upstream"


@asynccontextmanager
async def lifespan(app: FastAPI):
    seed_demo_keys()
    yield


app = FastAPI(
    title="API Gateway",
    description="A rate-limiting, circuit-breaking reverse proxy in front of "
    "an upstream service. Demonstrates a Redis-backed token bucket limiter "
    "with per-tier quotas, plus a shared circuit breaker for upstream "
    "resilience.",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/admin/circuit-status")
def circuit_status():
    return {"state": cb.get_state(UPSTREAM_CIRCUIT)}


@app.api_route("/proxy/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy(path: str, request: Request, x_api_key: str | None = Header(default=None)):
    """Authenticate, rate-limit, and forward a request to the upstream service.

    Order of checks matters: API-key validation is free (in-memory-cheap
    Redis GET), so it runs first. Rate limiting runs before the circuit
    breaker check so that a client that's already over quota gets a 429
    without spending a "trial call" slot against a recovering upstream.
    """
    if not x_api_key:
        raise HTTPException(status_code=401, detail="Missing X-API-Key header")

    tier = get_tier(x_api_key)
    result = check_and_consume(x_api_key, tier)
    if not result.allowed:
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded",
            headers={"Retry-After": str(round(result.retry_after or 1))},
        )

    try:
        cb.before_call(UPSTREAM_CIRCUIT)
    except cb.CircuitOpenError:
        raise HTTPException(
            status_code=503,
            detail="Upstream temporarily unavailable (circuit open)",
        )

    body = await request.body()
    async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
        try:
            upstream_response = await client.request(
                method=request.method,
                url=f"{settings.upstream_url}/{path}",
                params=dict(request.query_params),
                content=body,
            )
        except httpx.RequestError:
            cb.record_failure(UPSTREAM_CIRCUIT)
            raise HTTPException(status_code=502, detail="Upstream request failed")

    if upstream_response.status_code >= 500:
        cb.record_failure(UPSTREAM_CIRCUIT)
    else:
        cb.record_success(UPSTREAM_CIRCUIT)

    return Response(
        content=upstream_response.content,
        status_code=upstream_response.status_code,
        media_type=upstream_response.headers.get("content-type"),
    )
