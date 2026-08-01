# API Gateway — Rate Limiter & Circuit Breaker

A reverse-proxy API gateway demonstrating two classic distributed-systems
resilience patterns: a **token bucket rate limiter** with per-client tiers, and
a **circuit breaker** protecting a downstream service from cascading failure.
Written in Python (FastAPI), state shared in Redis so it works correctly
across multiple gateway replicas, runnable locally with a single
`docker compose up`.

---

## Architecture

```
  client ──▶ X-API-Key header ──▶ ┌─────────────────────────┐
                                   │        Gateway           │
                                   │  1. look up tier          │
                                   │  2. token-bucket check ───┼──▶ Redis
                                   │  3. circuit-breaker check ─┼──▶ Redis
                                   │  4. proxy request ─────────┼──▶ Upstream
                                   └─────────────────────────┘
```

Three services via Docker Compose: the **gateway**, a **mock upstream**
service it proxies to, and **Redis** holding both the rate-limit buckets and
the circuit-breaker state.

## Key design decisions

**Token bucket over sliding window.** A companion project (URL shortener) uses
a sliding-window counter for rate limiting — good for a simple pass/fail quota.
A gateway fronting bursty API clients benefits from a **token bucket** instead:
it allows a short burst up to `capacity` tokens while still capping the
sustained rate at `refill_per_minute`. Buckets refill lazily (computed from
elapsed time on each request) rather than via a background job, so there's no
scheduler to run or drift to correct.

**Per-tier quotas via API key.** Each `X-API-Key` maps to a tier
(`free`/`pro`/`enterprise`) with its own bucket capacity and refill rate,
stored in Redis so keys can be provisioned without redeploying the gateway —
demonstrating the kind of tiered-access control real API products (Stripe,
Twilio, etc.) expose.

**Atomicity via WATCH/MULTI.** Reading a bucket's token count, computing the
refill, and writing the new balance is a classic check-then-act sequence.
Under concurrency, two requests could both read "1 token left" and both
succeed. The limiter guards this with Redis's optimistic-locking primitive
(`WATCH`): if another request mutates the bucket between our read and write,
the transaction aborts and we retry. This is the same atomicity guarantee a
Lua script would give, using a more portable primitive.

**Circuit breaker shared in Redis, not in-process.** If the upstream starts
failing, an in-process breaker only protects *that* gateway instance — with N
replicas, the upstream still receives N× the failing traffic until every
instance independently trips. Storing breaker state (`closed`/`open`/`half_open`)
in Redis means one replica's failures immediately shield the upstream from all
of them. The half-open state lets a single trial call through after the
recovery timeout, closing the circuit on success or re-opening it on failure,
instead of an all-or-nothing retry storm.

**Ordering of checks.** Rate limiting runs before the circuit-breaker check —
a client that's already over quota gets a cheap 429 without spending one of
the limited "trial calls" a half-open circuit allows through.

## API

| Method | Path                        | Description                                  |
| ------ | --------------------------- | --------------------------------------------- |
| *      | `/proxy/{path}`              | Rate-limited, circuit-broken proxy to upstream |
| GET    | `/health`                    | Liveness probe                                 |
| GET    | `/admin/circuit-status`      | Current breaker state (closed/open/half_open) |

Requires an `X-API-Key` header. Interactive docs at `/docs`.

Demo keys (seeded on startup):

| API key                | Tier       | Bucket capacity | Refill/min |
| ----------------------- | ---------- | ---------------- | ---------- |
| `demo-free-key`         | free       | 10               | 10         |
| `demo-pro-key`          | pro        | 100              | 100        |
| `demo-enterprise-key`   | enterprise | 1000             | 1000       |

## Running locally

```bash
cp .env.example .env
docker compose up --build
```

```bash
# A normal proxied call
curl -H "X-API-Key: demo-free-key" http://localhost:8000/proxy/resource/42

# Drain the free-tier bucket (11th call in a row returns 429)
for i in $(seq 1 11); do
  curl -s -o /dev/null -w "%{http_code}\n" \
    -H "X-API-Key: demo-free-key" http://localhost:8000/proxy/resource/1
done

# Trip the circuit breaker: make upstream start failing, then hammer it
curl -X POST http://localhost:9000/admin/fail
for i in $(seq 1 6); do
  curl -s -o /dev/null -w "%{http_code}\n" \
    -H "X-API-Key: demo-enterprise-key" http://localhost:8000/proxy/resource/1
done
curl http://localhost:8000/admin/circuit-status   # {"state": "open"}

curl -X POST http://localhost:9000/admin/recover  # restore upstream
```

## Tests

Runs with **no external services** — Redis is swapped for `fakeredis` and the
upstream HTTP call is mocked with `respx`, so the suite exercises real
gateway/limiter/breaker code paths offline.

```bash
pip install -r requirements.txt
pytest -q
```

Covers: token bucket burst/refill/per-key isolation/tier differences, circuit
breaker state transitions (closed → open → half-open → closed/open), and
gateway-level behavior (missing key rejection, 429 on quota exhaustion, 503
once the circuit trips).

## Scaling notes

- **Horizontal gateway scaling:** the gateway is stateless; all shared state
  (buckets, breaker) lives in Redis, so any number of replicas can sit behind a
  load balancer without coordination.
- **Redis becomes the new bottleneck at very high QPS:** shard buckets across a
  Redis Cluster keyed by API key, or move to a local-approximate limiter
  (e.g., each replica tracks its own share of the quota) if perfect global
  accuracy isn't required.
- **Multiple upstreams:** the breaker is keyed by service name (`"upstream"`),
  so extending to multiple backend services (or per-route breakers) is a
  matter of adding more keys — no structural change.
- **Observability:** in production, breaker state transitions and 429/503
  rates are exactly the metrics you'd wire into alerting.

## Project layout

```
api-gateway/
├── app/
│   ├── main.py             # FastAPI gateway: auth, rate limit, breaker, proxy
│   ├── rate_limiter.py      # Redis token bucket (WATCH/MULTI atomicity)
│   ├── circuit_breaker.py   # Redis-backed closed/open/half-open breaker
│   ├── api_keys.py          # API key -> tier lookup
│   ├── redis_client.py      # shared Redis client
│   └── config.py            # env-driven settings + tier limits
├── upstream/
│   └── main.py              # mock backend service (with failure-injection hooks)
├── tests/                   # pytest suite (fakeredis + respx, no services needed)
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```
