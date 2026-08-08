"""A trivial mock upstream service the gateway proxies to.

Standing in for "the real backend" so the gateway has something concrete to
route requests to, health-check, and trip a circuit breaker against. Supports
an injectable failure mode (`/admin/fail`) so the gateway's resilience can be
demonstrated end-to-end without touching gateway code.
"""

from fastapi import FastAPI, HTTPException

app = FastAPI(title="Mock Upstream")

_state = {"failing": False, "requests_received": 0}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/admin/stats")
def stats():
    """How many resource requests actually reached this service.

    This is the authoritative measure of whether the circuit breaker is doing
    its job. Counting from the gateway's own metrics is unreliable once the
    gateway is replicated — each replica keeps independent counters, so a
    before/after comparison taken through the load balancer can read two
    different replicas and produce a meaningless delta (even a negative one).
    Asking the upstream directly has no such ambiguity.
    """
    return {"requests_received": _state["requests_received"]}


@app.post("/admin/reset-stats")
def reset_stats():
    _state["requests_received"] = 0
    return {"requests_received": 0}


@app.get("/resource/{item_id}")
def get_resource(item_id: str):
    # Counted before the failure check: a request that arrives and gets a 503
    # still *reached* the upstream, which is exactly what the breaker is meant
    # to prevent.
    _state["requests_received"] += 1

    if _state["failing"]:
        raise HTTPException(status_code=503, detail="upstream unavailable")
    return {"item_id": item_id, "data": f"payload-for-{item_id}"}


@app.post("/admin/fail")
def start_failing():
    """Test hook: make every subsequent request fail with 503."""
    _state["failing"] = True
    return {"failing": True}


@app.post("/admin/recover")
def stop_failing():
    """Test hook: restore normal behavior."""
    _state["failing"] = False
    return {"failing": False}
