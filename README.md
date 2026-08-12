# API Gateway — Rate Limiter & Circuit Breaker

[![CI](https://github.com/aaravgarai17/api-gateway/actions/workflows/ci.yml/badge.svg)](https://github.com/aaravgarai17/api-gateway/actions/workflows/ci.yml)
![Coverage](https://img.shields.io/badge/coverage-94%25-brightgreen)
![Python](https://img.shields.io/badge/python-3.10%20|%203.11%20|%203.12-blue)
![License](https://img.shields.io/badge/license-MIT-green)

A reverse-proxy API gateway demonstrating two classic distributed-systems
resilience patterns: a **token bucket rate limiter** with per-client tiers, and
a **circuit breaker** protecting a downstream service from cascading failure.

Deployed as **multiple replicas behind a load balancer** with all coordination
state in Redis, instrumented with **Prometheus + Grafana**, and validated by a
**tier-aware k6 load test** and a **chaos test that breaks the upstream under
live traffic and proves the breaker shields it**.

Runnable locally with a single `docker compose up`.

---

## Architecture

```
                        ┌──────────────┐
   client ─────────────▶│  nginx (lb)  │ :8080
   X-API-Key header     └──────┬───────┘
                               │
                ┌──────────────┴──────────────┐
                ▼                             ▼
        ┌───────────────┐            ┌───────────────┐
        │  gateway-1    │            │  gateway-2    │
        │               │            │               │
        │ 1. look up tier            │  (identical)  │
        │ 2. token bucket ───┐       │               │
        │ 3. circuit check ──┼───┐   │               │
        │ 4. proxy ──────┐   │   │   │               │
        └────────────────┼───┼───┼───┴───────┬───────┘
                         │   │   │           │
                         │   ▼   ▼           │  shared state
                         │  ┌──────────┐◀────┘  (both replicas
                         │  │  Redis   │         see one truth)
                         │  │ buckets  │
                         │  │ circuit  │
                         │  └──────────┘
                         ▼
                  ┌──────────────┐
                  │   Upstream   │ :9000
                  │  (+ failure  │
                  │   injection) │
                  └──────────────┘

        ┌────────────┐     ┌────────────────┐
        │ Prometheus │────▶│    Grafana     │ :3000
        │   :9090    │     │   dashboard    │
        └────────────┘     └────────────────┘
           scrapes each gateway replica individually
```

Six services via Docker Compose: **2 gateway replicas**, an **nginx load
balancer**, a **mock upstream** with failure-injection hooks, **Redis** holding
both the rate-limit buckets and circuit-breaker state, and a
**Prometheus/Grafana** observability stack.

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

## Verify it works (one command)

> **Common tasks:** `make help` lists everything — `make install`, `make test`,
> `make verify`, and per-project shortcuts.

Don't take the README's word for anything — run this:

```bash
./verify.sh
```

It boots the full stack and independently checks every claim: proxying works,
missing keys are rejected, the free tier gets throttled while enterprise
doesn't, the circuit trips when the upstream fails, and — the one that
matters — that an open circuit actually stops requests reaching the upstream.

```
 ✓ test suite passed
 ✓ gateway is healthy
 ✓ request proxied to upstream (200)
 ✓ missing API key rejected (401)
 ✓ free tier throttled after its quota (429)
 ✓ enterprise tier unaffected by free tier's limit
 ✓ circuit tripped open after failures
 ✓ upstream shielded (0 of 15 requests leaked through)

 Results: 8 passed, 0 failed
VERIFIED — every README claim checks out.
```

## Running locally

**Requires:** Docker Desktop. Optionally [k6](https://k6.io/) for the load test.

```bash
cp .env.example .env
docker compose up --build
```

Starts 2 gateway replicas plus Redis, nginx, the mock upstream, Prometheus, and
Grafana.

| Service             | URL                        |
| ------------------- | -------------------------- |
| Gateway (via nginx) | http://localhost:8080      |
| Swagger docs        | http://localhost:8080/docs |
| Mock upstream       | http://localhost:9000      |
| Prometheus          | http://localhost:9090      |
| Grafana dashboard   | http://localhost:3000      |

Grafana auto-provisions the datasource and the **API Gateway — Traffic, Limits
& Resilience** dashboard; no login or setup needed.

```bash
# A normal proxied call
curl -H "X-API-Key: demo-free-key" http://localhost:8080/proxy/resource/42

# Drain the free-tier bucket (11th call in a row returns 429)
for i in $(seq 1 11); do
  curl -s -o /dev/null -w "%{http_code}\n" \
    -H "X-API-Key: demo-free-key" http://localhost:8080/proxy/resource/1
done

# Trip the circuit breaker: make upstream start failing, then hammer it
curl -X POST http://localhost:9000/admin/fail
for i in $(seq 1 6); do
  curl -s -o /dev/null -w "%{http_code}\n" \
    -H "X-API-Key: demo-enterprise-key" http://localhost:8080/proxy/resource/1
done
curl http://localhost:8080/admin/circuit-status   # {"state": "open"}

curl -X POST http://localhost:9000/admin/recover  # restore upstream
```

## Observability

Each replica exposes `/metrics`; Prometheus discovers them through Docker DNS
and scrapes each **individually**, which is what lets the dashboard show that
every replica reports the *same* circuit state — visible proof that the
breaker's state is shared through Redis rather than living in process memory.

| Metric                              | Why it matters                                                          |
| ----------------------------------- | ----------------------------------------------------------------------- |
| `gateway_rejections_total`          | **The key gateway metric** — labeled by reason, separating "a noisy client got throttled" from "the backend is down" |
| `gateway_circuit_state`             | Gauge (0=closed, 1=half-open, 2=open) — alertable, and graphs as a state timeline |
| `gateway_rate_limit_allowed_total`  | Allowed traffic by tier, for quota/billing visibility                    |
| `gateway_upstream_results_total`    | Outcomes of calls *actually forwarded* — drops to zero while the circuit is open |
| `gateway_request_duration_seconds`  | End-to-end latency                                                       |
| `gateway_upstream_duration_seconds` | Upstream-only latency; the gap between the two is the gateway's own overhead |

As in the companion URL-shortener project, the `endpoint` label uses the route
template (`/proxy/{path:path}`) rather than the raw path — labeling by raw path
would mint a new time series per proxied URL, and unbounded cardinality is the
standard way to take down a Prometheus server. A regression test asserts this.

## Load testing

The [k6](https://k6.io/) script runs **two client scenarios concurrently** to
prove the limiter actually discriminates by tier:

```bash
k6 run loadtest/gateway_load.js
```

- **enterprise client** (1000/min bucket) ramps to 100 VUs → asserts a >95%
  success rate and p95 < 100ms.
- **free-tier client** (10/min bucket) hammers continuously → asserts that
  **more than half its requests are rejected with 429**.

That second threshold is the unusual one, and the point: most load tests only
check that requests succeed. Here, *correct behavior includes rejecting
traffic* — a limiter that never throttles is broken, and this test fails if
that regresses.

## Chaos test — proving the breaker actually protects the upstream

Unit tests can verify the breaker's state machine. They can't verify that under
real concurrent traffic, across replicas sharing state through Redis, the
breaker actually stops traffic from reaching a dying backend. That's what this
does:

```bash
./loadtest/chaos_test.sh
```

Four phases: **baseline** (healthy, circuit closed) → **break the upstream**
(inject 503s, verify the circuit trips open) → **verify shielding** → **recover**
(heal the upstream, wait out the timeout, verify half-open trial call closes the
circuit).

Phase 3 carries the assertion that matters. It resets the upstream's own
request counter, fires 20 requests into an open circuit, then asks the upstream
how many actually arrived — failing unless **~zero did**:

```
 Phase 3: is the upstream actually shielded?
    sending 20 more requests while circuit is open...
    PASS  all 20 requests short-circuited (got: 20)
    upstream calls that leaked through: 0 (of 20)
    PASS  upstream was shielded
```

A breaker that flips to "open" but still forwards traffic is worse than no
breaker at all — it gives false confidence. This test is what distinguishes
*implementing* the pattern from *proving* it works.

**Why the measurement is taken at the upstream, not from the gateway's
metrics.** The obvious approach is to diff `gateway_upstream_results_total`
before and after. That's wrong here, and subtly so: the gateway is replicated,
each replica keeps independent Prometheus counters, and reading them *through
the load balancer* samples an arbitrary replica each time. An earlier version of
this check did exactly that and reported `-1 of 15 requests leaked` — an
impossible number that still "passed", because −1 ≤ 1. A check that can silently
produce a false pass on the project's central claim is worse than no check at
all. The upstream is the only authoritative observer of what actually reached
it, so the counter lives there (`GET /admin/stats`).

## Tests

The unit/integration suite runs with **no external services** — Redis is
swapped for `fakeredis` and the upstream HTTP call is mocked with `respx`.

```bash
pip install -r requirements.txt
pytest -q          # 24 tests
```

Covers: token bucket burst/refill/per-key isolation/tier differences, circuit
breaker state transitions (closed → open → half-open → closed/open),
gateway-level behavior (missing key rejection, 429 on quota exhaustion, 503
once the circuit trips), and metrics correctness including cardinality safety.

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
│   ├── metrics.py           # Prometheus instrumentation + middleware
│   └── config.py            # env-driven settings + tier limits
├── upstream/
│   └── main.py              # mock backend service (with failure-injection hooks)
├── infra/
│   ├── nginx.conf           # load balancer (deliberately does NOT retry 503s)
│   ├── prometheus.yml       # per-replica scrape config via Docker DNS
│   └── grafana/             # auto-provisioned datasource + dashboard JSON
├── loadtest/
│   ├── gateway_load.js      # k6: tier-aware load test w/ pass-fail thresholds
│   └── chaos_test.sh        # breaks upstream, proves the breaker shields it
├── tests/                   # pytest suite (fakeredis + respx, no services needed)
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```
