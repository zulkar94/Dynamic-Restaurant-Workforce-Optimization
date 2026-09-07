"""Test fixtures. Every test runs against a throwaway SQLite file and a
deterministic settings object; nothing touches the developer's dev database."""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import random
import tempfile
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
import pytest_asyncio

TMP_DB = Path(tempfile.gettempdir()) / "rwo_test.db"
TMP_DB.unlink(missing_ok=True)

os.environ.update(
    RWO_ENV="test",
    RWO_DATABASE_URL=f"sqlite+aiosqlite:///{TMP_DB}",
    RWO_SECRET_KEY="test-secret-key-that-is-long-enough-0123456789",
    RWO_ADMIN_EMAIL="admin@rwo.app",
    RWO_ADMIN_PASSWORD="TestAdminPassw0rd",
    RWO_RATE_LIMIT_PER_MINUTE="100000",
    RWO_AUTH_RATE_LIMIT_PER_MINUTE="100000",
    RWO_ARGON2_TIME_COST="1",
    RWO_ARGON2_MEMORY_COST="8192",
    RWO_SOLVER_TIME_LIMIT_SECONDS="20",
    RWO_ARTIFACTS_DIR=str(Path(tempfile.gettempdir()) / "rwo_artifacts"),
)

from httpx import ASGITransport, AsyncClient  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db import dispose_db, get_sessionmaker  # noqa: E402
from app.main import create_app  # noqa: E402

ADMIN = {"email": "admin@rwo.app", "password": "TestAdminPassw0rd"}


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture
async def client() -> AsyncGenerator[AsyncClient, None]:
    TMP_DB.unlink(missing_ok=True)
    await dispose_db()
    app = create_app()
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http_client,
    ):
        yield http_client
    await dispose_db()


@pytest_asyncio.fixture
async def admin_headers(client: AsyncClient) -> dict[str, str]:
    response = await client.post("/api/v1/auth/login", json=ADMIN)
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest_asyncio.fixture
async def session():
    async with get_sessionmaker()() as db_session:
        yield db_session


@pytest.fixture
def sales_history() -> list[dict]:
    """~10 weeks of hourly covers with weekday and lunch/dinner structure."""
    rng = random.Random(7)
    settings = get_settings()
    start = dt.date(2026, 1, 5)  # a Monday
    weekday_factor = {0: 0.8, 1: 0.85, 2: 0.95, 3: 1.0, 4: 1.3, 5: 1.45, 6: 1.05}
    hour_shape = {12: 1.0, 13: 0.9, 19: 1.2, 20: 1.1}
    records = []
    for offset in range(70):
        day = start + dt.timedelta(days=offset)
        for hour in settings.service_hours:
            base = 40 * weekday_factor[day.weekday()] * hour_shape.get(hour, 0.4)
            records.append(
                {
                    "day": day,
                    "hour": hour,
                    "covers": round(max(0.0, base + rng.gauss(0, 2.5)), 1),
                    "is_holiday": False,
                    "weather_score": 0.6,
                }
            )
    return records
