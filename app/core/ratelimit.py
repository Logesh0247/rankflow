"""API rate limiting (Prompt 12, Phase 5).

In-memory token buckets, one per client IP: `rate_limit_per_minute`
requests per minute, refilled continuously. Exceeding the limit
returns HTTP 429 with a Retry-After header instead of running the
handler. State lives in the process (single-worker local service);
a multi-worker deployment must move this to Redis — see
docs/DEPLOYMENT.md.

The limiter is deliberately generous by default (600/min) for a
single-user service and is bypassed entirely when the limit is 0.
It is also bypassed for the /health read, so monitoring never gets
locked out by its own checks.
"""
from __future__ import annotations

import time
from collections import defaultdict

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

_EXEMPT_PATHS = {"/health", "/docs", "/openapi.json", "/redoc"}


class RateLimiter:
    def __init__(self, per_minute: int):
        self.per_minute = max(0, int(per_minute))
        self._buckets: dict[str, tuple[float, float]] = defaultdict(lambda: (0.0, 0.0))
        self._last_seen: dict[str, float] = {}

    def allow(self, key: str, now: float | None = None) -> tuple[bool, float]:
        """Return (allowed, retry_after_seconds)."""
        if self.per_minute <= 0:
            return True, 0.0
        now = time.monotonic() if now is None else now
        tokens, updated = self._buckets[key]
        # Refill: per_minute tokens per 60 s.
        tokens = min(self.per_minute, tokens + (now - updated) * self.per_minute / 60.0)
        if tokens >= 1.0:
            self._buckets[key] = (tokens - 1.0, now)
            return True, 0.0
        self._buckets[key] = (tokens, now)
        retry_after = (1.0 - tokens) * 60.0 / self.per_minute
        return False, retry_after

    def reset(self) -> None:
        self._buckets.clear()


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, limiter: RateLimiter):
        super().__init__(app)
        self.limiter = limiter

    async def dispatch(self, request: Request, call_next):
        if request.url.path in _EXEMPT_PATHS:
            return await call_next(request)
        client = request.client.host if request.client else "unknown"
        allowed, retry_after = self.limiter.allow(client)
        if not allowed:
            return JSONResponse(
                status_code=429,
                content={"code": "rate_limited",
                         "message": "too many requests — slow down and retry"},
                headers={"Retry-After": str(max(1, int(retry_after + 0.5)))})
        return await call_next(request)
