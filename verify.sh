#!/usr/bin/env bash
#
# One-command verification: boots the whole stack and proves every claim the
# README makes, then tears it down. Intended for someone who just cloned this
# repo and wants evidence it works without reading anything.
#
# Usage:  ./verify.sh

set -uo pipefail

GATEWAY="http://localhost:8080"
UPSTREAM="http://localhost:9000"
pass=0
fail=0

ok()   { echo "  ✓ $1"; pass=$(( pass + 1 )); }
bad()  { echo "  ✗ $1"; fail=$(( fail + 1 )); }

cleanup() {
  echo ""
  echo "Tearing down..."
  docker compose down -v >/dev/null 2>&1
}
trap cleanup EXIT

echo "=================================================="
echo " 0/5  Preflight"
echo "=================================================="

command -v docker >/dev/null 2>&1 || {
  echo "  ✗ docker not found. Install Docker Desktop and start it."; exit 1; }
docker info >/dev/null 2>&1 || {
  echo "  ✗ Docker daemon not running. Launch Docker Desktop and retry."; exit 1; }
ok "docker is available"

if ! python3 -c "import fakeredis, respx, pytest" >/dev/null 2>&1; then
  echo "  ✗ Python test dependencies missing."
  echo ""
  echo "    Install them first:"
  echo "      python3 -m venv .venv && source .venv/bin/activate"
  echo "      pip install -r requirements.txt"
  echo ""
  exit 1
fi
ok "python test dependencies present"

# Ports this stack needs. A sibling project (the url-shortener) also uses 8080,
# so a stale stack is the most common reason this script hangs.
for port in 8080 9000 9090 3000; do
  if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "  ✗ port $port is already in use."
    echo ""
    echo "    Something else is bound to it — most likely another project's"
    echo "    stack. Find and stop it:"
    echo "      docker ps"
    echo "      docker compose -p <that-project> down"
    echo ""
    exit 1
  fi
done
ok "required ports are free (8080, 9000, 9090, 3000)"

echo ""
echo "=================================================="
echo " 1/5  Unit tests (no services needed)"
echo "=================================================="
if python3 -m pytest -q 2>&1 | tail -3; then
  ok "test suite passed"
else
  bad "test suite failed"
fi

echo ""
echo "=================================================="
echo " 2/5  Booting the stack"
echo "=================================================="
if ! docker compose up -d --build >/tmp/gw_build.log 2>&1; then
  bad "docker compose failed to start"
  echo ""
  tail -30 /tmp/gw_build.log
  exit 1
fi

printf "  waiting for gateway"
up=false
for _ in $(seq 1 40); do
  if curl -fs "$GATEWAY/health" >/dev/null 2>&1; then up=true; break; fi
  printf "."
  sleep 2
done
echo ""

if [[ "$up" == true ]]; then
  ok "gateway is healthy"
else
  bad "gateway never came up"
  echo ""
  docker compose logs --tail=30
  echo ""
  echo "Results: $pass passed, $fail failed"
  exit 1
fi

echo ""
echo "=================================================="
echo " 3/5  Proxying works"
echo "=================================================="
code=$(curl -s -o /dev/null -w '%{http_code}' \
  -H 'X-API-Key: demo-enterprise-key' "$GATEWAY/proxy/resource/42")
[[ "$code" == "200" ]] && ok "request proxied to upstream (200)" \
                       || bad "expected 200, got $code"

code=$(curl -s -o /dev/null -w '%{http_code}' "$GATEWAY/proxy/resource/42")
[[ "$code" == "401" ]] && ok "missing API key rejected (401)" \
                       || bad "expected 401, got $code"

echo ""
echo "=================================================="
echo " 4/5  Rate limiting discriminates by tier"
echo "=================================================="
# Free tier capacity is 10; drain it.
for _ in $(seq 1 12); do
  curl -s -o /dev/null -H 'X-API-Key: demo-free-key' "$GATEWAY/proxy/resource/1"
done
code=$(curl -s -o /dev/null -w '%{http_code}' \
  -H 'X-API-Key: demo-free-key' "$GATEWAY/proxy/resource/1")
[[ "$code" == "429" ]] && ok "free tier throttled after its quota (429)" \
                       || bad "expected 429 for free tier, got $code"

# Enterprise capacity is 1000; the same volume should sail through.
code=$(curl -s -o /dev/null -w '%{http_code}' \
  -H 'X-API-Key: demo-enterprise-key' "$GATEWAY/proxy/resource/1")
[[ "$code" == "200" ]] && ok "enterprise tier unaffected by free tier's limit" \
                       || bad "expected 200 for enterprise, got $code"

echo ""
echo "=================================================="
echo " 5/5  Circuit breaker protects a failing upstream"
echo "=================================================="
curl -s -X POST "$UPSTREAM/admin/fail" >/dev/null
for _ in $(seq 1 10); do
  curl -s -o /dev/null -H 'X-API-Key: demo-enterprise-key' "$GATEWAY/proxy/resource/1"
  sleep 0.2
done

state=$(curl -s "$GATEWAY/admin/circuit-status")
echo "$state" | grep -q '"open"' && ok "circuit tripped open after failures" \
                                 || bad "circuit did not open (got: $state)"

# The claim that matters: an open circuit must stop calls reaching upstream.
# Measured at the upstream itself — the gateway's own counters are per-replica
# and reading them through the load balancer gives a meaningless delta.
curl -s -X POST "$UPSTREAM/admin/reset-stats" >/dev/null
for _ in $(seq 1 15); do
  curl -s -o /dev/null -H 'X-API-Key: demo-enterprise-key' "$GATEWAY/proxy/resource/1"
done
leaked=$(curl -s "$UPSTREAM/admin/stats" \
  | sed -n 's/.*"requests_received":\([0-9]*\).*/\1/p')
leaked=${leaked:-unknown}

if [[ "$leaked" =~ ^[0-9]+$ ]] && [[ $leaked -le 1 ]]; then
  ok "upstream shielded ($leaked of 15 requests reached it)"
else
  bad "upstream NOT shielded ($leaked of 15 reached it)"
fi

curl -s -X POST "$UPSTREAM/admin/recover" >/dev/null

echo ""
echo "=================================================="
echo " Results: $pass passed, $fail failed"
echo "=================================================="
[[ $fail -eq 0 ]] && echo "VERIFIED — every README claim checks out." \
                  || echo "FAILED — see above."
exit $(( fail > 0 ? 1 : 0 ))
