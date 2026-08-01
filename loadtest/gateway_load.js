/**
 * k6 load test — gateway proxy path, with tier-aware assertions.
 *
 * Runs two scenarios concurrently to prove the rate limiter actually
 * discriminates between subscription tiers under real load:
 *
 *   - enterprise traffic (1000 req/min bucket): should almost never be
 *     throttled, so we assert a HIGH success rate.
 *   - free-tier traffic (10 req/min bucket): should be throttled hard, so we
 *     assert we actually SEE 429s. A limiter that never rejects is broken, and
 *     this threshold catches that.
 *
 * That second assertion is the interesting one — most load tests only check
 * that things succeed. Here, correct behavior includes rejecting traffic.
 *
 * Run:  k6 run loadtest/gateway_load.js
 *       k6 run -e BASE_URL=http://localhost:8080 loadtest/gateway_load.js
 */

import http from "k6/http";
import { check, sleep } from "k6";
import { Rate } from "k6/metrics";

const BASE_URL = __ENV.BASE_URL || "http://localhost:8080";

const enterpriseOk = new Rate("enterprise_success");
const freeThrottled = new Rate("free_tier_throttled");

export const options = {
  scenarios: {
    // High-quota client: sustained load that should sail through.
    enterprise_traffic: {
      executor: "ramping-vus",
      startVUs: 0,
      stages: [
        { duration: "15s", target: 30 },
        { duration: "45s", target: 100 },
        { duration: "20s", target: 100 },
        { duration: "10s", target: 0 },
      ],
      exec: "enterpriseClient",
      gracefulRampDown: "10s",
    },
    // Low-quota client hammering well past its limit.
    free_tier_traffic: {
      executor: "constant-vus",
      vus: 10,
      duration: "90s",
      exec: "freeClient",
    },
  },
  thresholds: {
    // Enterprise tier should be served, not throttled.
    enterprise_success: ["rate>0.95"],
    // Free tier MUST get throttled — proves the limiter discriminates by tier.
    free_tier_throttled: ["rate>0.5"],
    // Gateway overhead should stay low even under load.
    "http_req_duration{scenario:enterprise_traffic}": ["p(95)<100"],
  },
};

export function enterpriseClient() {
  const res = http.get(`${BASE_URL}/proxy/resource/42`, {
    headers: { "X-API-Key": "demo-enterprise-key" },
  });

  const ok = check(res, { "enterprise: 200": (r) => r.status === 200 });
  enterpriseOk.add(ok);
  sleep(0.1);
}

export function freeClient() {
  const res = http.get(`${BASE_URL}/proxy/resource/1`, {
    headers: { "X-API-Key": "demo-free-key" },
  });

  // A 429 here is the CORRECT outcome, not a failure.
  const throttled = res.status === 429;
  freeThrottled.add(throttled);
  check(res, {
    "free: 200 or 429 (never 5xx)": (r) => r.status === 200 || r.status === 429,
    "429 carries Retry-After": (r) =>
      r.status !== 429 || !!r.headers["Retry-After"],
  });
  sleep(0.1);
}
