"""A trivial mock upstream service the gateway proxies to.

Standing in for "the real backend" so the gateway has something concrete to
route requests to, health-check, and trip a circuit breaker against. Supports
an injectable failure mode (`/admin/fail`) so the gateway's resilience can be
demonstrated end-to-end without touching gateway code.
"""

from fastapi import FastAPI, HTTPException

app = FastAPI(title="Mock Upstream")

_state = {"failing": False}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/resource/{item_id}")
def get_resource(item_id: str):
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
