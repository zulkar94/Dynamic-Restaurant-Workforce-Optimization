"""Transport-layer hardening middleware."""

from __future__ import annotations

import time
import uuid
from collections import defaultdict, deque

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.config import Settings

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; "
    "form-action 'self'"
)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, settings: Settings) -> None:
        super().__init__(app)
        self.hsts = settings.enable_hsts

    async def dispatch(self, request: Request, call_next):
        response: Response = await call_next(request)
        headers = response.headers
        headers.setdefault("Content-Security-Policy", CSP)
        headers.setdefault("X-Content-Type-Options", "nosniff")
        headers.setdefault("X-Frame-Options", "DENY")
        headers.setdefault("Referrer-Policy", "no-referrer")
        headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
        headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        headers.setdefault("Cache-Control", "no-store")
        if "server" in headers:
            del headers["server"]
        if self.hsts:
            headers.setdefault(
                "Strict-Transport-Security", "max-age=63072000; includeSubDomains; preload"
            )
        return response


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Attach request id, enforce max body size, expose duration header."""

    def __init__(self, app, settings: Settings) -> None:
        super().__init__(app)
        self.max_body = settings.max_body_bytes

    async def dispatch(self, request: Request, call_next):
        raw_len = request.headers.get("content-length")
        if raw_len and raw_len.isdigit() and int(raw_len) > self.max_body:
            return JSONResponse({"detail": "payload too large"}, status_code=413)
        request_id = request.headers.get("X-Request-ID", uuid.uuid4().hex)
        request.state.request_id = request_id
        started = time.perf_counter()
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Response-Time-ms"] = f"{(time.perf_counter() - started) * 1000:.1f}"
        return response


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Sliding-window limiter per client IP. Auth paths get a tighter bucket.

    In-process only. Behind multiple replicas, swap for Redis (see ARCHITECTURE.md).
    """

    def __init__(self, app, settings: Settings) -> None:
        super().__init__(app)
        self.general = settings.rate_limit_per_minute
        self.auth = settings.auth_rate_limit_per_minute
        self._hits: dict[tuple[str, str], deque[float]] = defaultdict(deque)

    @staticmethod
    def _client_ip(request: Request) -> str:
        forwarded = request.headers.get("X-Forwarded-For", "")
        if forwarded:
            return forwarded.split(",")[0].strip()[:64]
        return request.client.host if request.client else "unknown"

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path in ("/health", "/ready"):
            return await call_next(request)
        bucket = "auth" if path.startswith("/api/v1/auth") else "general"
        limit = self.auth if bucket == "auth" else self.general
        key = (self._client_ip(request), bucket)
        now = time.monotonic()
        window = self._hits[key]
        while window and now - window[0] > 60.0:
            window.popleft()
        if len(window) >= limit:
            retry = max(1, int(60 - (now - window[0])))
            return JSONResponse(
                {"detail": "rate limit exceeded"},
                status_code=429,
                headers={"Retry-After": str(retry)},
            )
        window.append(now)
        return await call_next(request)
