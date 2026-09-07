"""Transport hardening: headers, rate limiting, payload caps, ops endpoints."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from app.config import DEV_SECRET, Settings
from app.security.middleware import (
    RateLimitMiddleware,
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
)

pytestmark = pytest.mark.asyncio


def _tiny_app(**overrides) -> Starlette:
    settings = Settings(secret_key="test-secret-key-that-is-long-enough-0123456789", **overrides)

    async def endpoint(request):
        return PlainTextResponse("ok")

    app = Starlette(routes=[Route("/api/v1/thing", endpoint, methods=["GET", "POST"])])
    app.add_middleware(SecurityHeadersMiddleware, settings=settings)
    app.add_middleware(RateLimitMiddleware, settings=settings)
    app.add_middleware(RequestContextMiddleware, settings=settings)
    return app


async def test_security_headers_present(client):
    response = await client.get("/health")
    headers = response.headers
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "DENY"
    assert headers["referrer-policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in headers["content-security-policy"]
    assert "unsafe-inline" not in headers["content-security-policy"]
    assert "server" not in headers
    assert "x-request-id" in headers


async def test_health_and_ready(client):
    assert (await client.get("/health")).json()["status"] == "ok"
    ready = await client.get("/ready")
    assert ready.status_code == 200 and ready.json()["env"] == "test"


async def test_rate_limit_returns_429():
    app = _tiny_app(rate_limit_per_minute=3, auth_rate_limit_per_minute=3)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as http:
        codes = [(await http.get("/api/v1/thing")).status_code for _ in range(5)]
    assert codes[:3] == [200, 200, 200]
    assert codes[3:] == [429, 429]


async def test_oversized_body_rejected():
    app = _tiny_app(max_body_bytes=64)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as http:
        response = await http.post("/api/v1/thing", content=b"x" * 512)
    assert response.status_code == 413


async def test_prod_settings_reject_dev_secret():
    with pytest.raises(ValueError):
        Settings(
            env="prod",
            secret_key=DEV_SECRET,
            admin_password="RealPassw0rd!",
            cors_origins=["https://ops.example"],
        )


async def test_prod_settings_reject_wildcard_cors():
    with pytest.raises(ValueError):
        Settings(
            env="prod",
            secret_key="a-real-production-secret-key-32-chars-long",
            admin_password="RealPassw0rd!",
            cors_origins=["*"],
        )
