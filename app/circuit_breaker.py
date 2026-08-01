"""Circuit breaker guarding calls to the upstream service.

Three states, stored in Redis so they're shared across gateway replicas:

  CLOSED     -- normal operation. Failures are counted in a rolling counter.
               Crossing `failure_threshold` trips the breaker to OPEN.
  OPEN       -- calls are short-circuited immediately (no request sent to the
               already-struggling upstream) until `recovery_timeout` elapses.
  HALF_OPEN  -- after the timeout, a small number of trial calls are let
               through. Success closes the breaker (reset failure count);
               any failure re-opens it and restarts the timeout.

Sharing state via Redis matters here for the same reason as the rate limiter:
with multiple gateway instances, an in-process breaker would let each replica
independently hammer a dying upstream until *it* personally accumulates enough
failures, multiplying load on a service that's already failing.
"""

import time

from app.config import settings
from app.redis_client import get_client

CLOSED = "closed"
OPEN = "open"
HALF_OPEN = "half_open"


class CircuitOpenError(Exception):
    """Raised when a call is rejected because the circuit is open."""


def _state_key(name: str) -> str:
    return f"cb:{name}:state"


def _failures_key(name: str) -> str:
    return f"cb:{name}:failures"


def _opened_at_key(name: str) -> str:
    return f"cb:{name}:opened_at"


def _half_open_calls_key(name: str) -> str:
    return f"cb:{name}:half_open_calls"


def get_state(name: str) -> str:
    client = get_client()
    state = client.get(_state_key(name)) or CLOSED

    if state == OPEN:
        opened_at = client.get(_opened_at_key(name))
        if opened_at and time.time() - float(opened_at) >= settings.cb_recovery_timeout_seconds:
            # Recovery window elapsed; allow trial calls.
            client.set(_state_key(name), HALF_OPEN)
            client.set(_half_open_calls_key(name), 0)
            return HALF_OPEN
    return state


def before_call(name: str) -> None:
    """Raise CircuitOpenError if this call should be short-circuited."""
    client = get_client()
    state = get_state(name)

    if state == OPEN:
        raise CircuitOpenError(f"circuit '{name}' is open")

    if state == HALF_OPEN:
        calls = client.incr(_half_open_calls_key(name))
        if calls > settings.cb_half_open_max_calls:
            raise CircuitOpenError(f"circuit '{name}' is half-open, trial call in flight")


def record_success(name: str) -> None:
    client = get_client()
    state = get_state(name)
    if state in (HALF_OPEN, OPEN):
        # Recovered: fully close and reset counters.
        client.set(_state_key(name), CLOSED)
        client.set(_failures_key(name), 0)
    else:
        client.set(_failures_key(name), 0)


def record_failure(name: str) -> None:
    client = get_client()
    state = get_state(name)

    if state == HALF_OPEN:
        # Trial call failed; re-open and restart the recovery clock.
        client.set(_state_key(name), OPEN)
        client.set(_opened_at_key(name), time.time())
        return

    failures = client.incr(_failures_key(name))
    if failures >= settings.cb_failure_threshold:
        client.set(_state_key(name), OPEN)
        client.set(_opened_at_key(name), time.time())
