"""
Simple in-process rate limiter — no external dependencies.

Usage (as a FastAPI dependency):

    from app.limiter import rate_limit

    @router.post("/login")
    def login(request: Request, _rl=rate_limit(5, 60), ...):
        ...

rate_limit(max_calls, window_seconds) returns a Depends() that raises
HTTP 429 when the IP exceeds max_calls within window_seconds.
State is in-memory and resets on process restart — fine for a
single-process deployment. Swap for Redis if you ever go multi-instance.
"""
from collections import defaultdict
from time import monotonic
from threading import Lock

from fastapi import Depends, HTTPException, Request

# {key: [timestamp, ...]}
_store: dict[str, list[float]] = defaultdict(list)
_lock = Lock()


def _check(key: str, max_calls: int, window: float) -> bool:
    """Return True if the request is allowed; False if rate-limited."""
    now = monotonic()
    with _lock:
        ts = _store[key]
        # Drop timestamps outside the window
        cutoff = now - window
        _store[key] = [t for t in ts if t > cutoff]
        if len(_store[key]) >= max_calls:
            return False
        _store[key].append(now)
        return True


def rate_limit(max_calls: int, window_seconds: int):
    """
    FastAPI dependency factory.

        _rl = rate_limit(5, 60)   # 5 requests per 60 seconds per IP

    The underscore-prefixed parameter name tells FastAPI it is a
    side-effect-only dependency (no value used by the handler).
    """
    def dependency(request: Request):
        ip = request.client.host if request.client else "unknown"
        key = f"{request.url.path}:{ip}"
        if not _check(key, max_calls, window_seconds):
            raise HTTPException(
                status_code=429,
                detail=f"Too many requests — please wait {window_seconds} seconds.",
            )
    return Depends(dependency)
