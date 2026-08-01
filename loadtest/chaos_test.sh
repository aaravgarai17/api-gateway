#!/usr/bin/env bash
#
# Chaos test: break the upstream under live traffic and verify the circuit
# breaker actually protects it.
#
# This tests the claim that unit tests can't: that under real concurrent
# traffic, across multiple gateway replicas sharing state through Redis, the
# breaker trips, stops forwarding calls to a dying backend, and then recovers
# on its own once the backend is healthy again.
#
# The critical assertion is UPSTREAM_SHIELDED: after the breaker opens, the
# number of requests that actually reach the upstream must drop to ~zero. A
# breaker that opens but still lets traffic through is useless.
#
# Phases:
#   1. baseline   — healthy upstream, expect 200s, circuit closed
#   2. break it   — upstream starts 503ing, breaker should trip to open
#   3. shielded   — verify upstream receives ~no traffic while open
#   4. recover    — upstream healed, wait out timeout, breaker closes
#
# Usage:  ./loadtest/chaos_test.sh
# Expects the stack running:  docker compose up --scale gateway=2

set -uo pipefail

GATEWAY="${GATEWAY:-http://localhost:8080}"
UPSTREAM="${UPSTREAM:-http://localhost:9000}"
KEY="demo-enterprise-key"          # high quota so rate limiting never interferes
RECOVERY="${CB_RECOVERY_TIMEOUT_SECONDS:-30}"

pass=0
fail=0

check() {   # check <description> <actual> <expected>
  if [[ "$2" == "$3" ]]; then
    echo "    PASS  $1 (got: $2)"
    pass=$(( pass + 1 ))
  else
    echo "    FAIL  $1 (got: $2, expected: $3)"
    fail=$(( fail + 1 ))
  fi
}

circuit_state() {
  curl -s "$GATEWAY/admin/circuit-status" | sed -n 's/.*"state":"\([^"]*\)".*/\1/p'
}

hit() {     # hit -> prints status code
  curl -s -o /dev/null -w '%{http_code}' --max-time 5 \
    -H "X-API-Key: $KEY" "$GATEWAY/proxy/resource/1"
}

echo "=============================================="
echo " Phase 1: baseline (upstream healthy)"
echo "=============================================="
curl -s -X POST "$UPSTREAM/admin/recover" >/dev/null
sleep 1
# Drive a success through so a breaker left open by a previous run closes.
for _ in $(seq 1 3); do hit >/dev/null; sleep 0.2; done

check "circuit is closed" "$(circuit_state)" "closed"
check "requests succeed"  "$(hit)"           "200"

echo ""
echo "=============================================="
echo " Phase 2: upstream starts failing"
echo "=============================================="
curl -s -X POST "$UPSTREAM/admin/fail" >/dev/null
echo "    upstream now returns 503 for every request"

# Send enough traffic to cross the failure threshold.
echo "    sending 10 requests to trip the breaker..."
for _ in $(seq 1 10); do hit >/dev/null; sleep 0.2; done

STATE=$(circuit_state)
check "circuit tripped open" "$STATE" "open"

echo ""
echo "=============================================="
echo " Phase 3: is the upstream actually shielded?"
echo "=============================================="
# Count how many calls reach the upstream while the circuit is open. The
# gateway exposes this as gateway_upstream_results_total; if the breaker is
# doing its job, this counter should barely move.
BEFORE=$(curl -s "$GATEWAY/metrics" \
  | awk '/^gateway_upstream_results_total/ {s+=$2} END {printf "%d", s+0}')

echo "    sending 20 more requests while circuit is open..."
FAST_REJECTS=0
for _ in $(seq 1 20); do
  [[ "$(hit)" == "503" ]] && FAST_REJECTS=$(( FAST_REJECTS + 1 ))
done

AFTER=$(curl -s "$GATEWAY/metrics" \
  | awk '/^gateway_upstream_results_total/ {s+=$2} END {printf "%d", s+0}')
LEAKED=$(( AFTER - BEFORE ))

check "all 20 requests short-circuited" "$FAST_REJECTS" "20"
echo "    upstream calls that leaked through: $LEAKED (of 20)"
if [[ $LEAKED -le 1 ]]; then
  echo "    PASS  upstream was shielded"
  pass=$(( pass + 1 ))
else
  echo "    FAIL  upstream was NOT shielded ($LEAKED calls got through)"
  fail=$(( fail + 1 ))
fi

echo ""
echo "=============================================="
echo " Phase 4: recovery"
echo "=============================================="
curl -s -X POST "$UPSTREAM/admin/recover" >/dev/null
echo "    upstream healed; waiting ${RECOVERY}s for the recovery timeout..."
sleep "$(( RECOVERY + 2 ))"

# First call after the timeout is the half-open trial call.
TRIAL=$(hit)
check "trial call succeeds" "$TRIAL" "200"
sleep 1
check "circuit closed again" "$(circuit_state)" "closed"
check "traffic flowing normally" "$(hit)" "200"

echo ""
echo "=============================================="
echo " Results:  $pass passed, $fail failed"
echo "=============================================="
[[ $fail -eq 0 ]] && { echo "RESULT: PASS"; exit 0; } || { echo "RESULT: FAIL"; exit 1; }
