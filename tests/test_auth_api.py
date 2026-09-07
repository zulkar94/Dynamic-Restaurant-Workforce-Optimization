"""Authentication flows over HTTP."""

from __future__ import annotations

import pytest

from tests.conftest import ADMIN

pytestmark = pytest.mark.asyncio


async def test_login_success_and_me(client, admin_headers):
    response = await client.get("/api/v1/auth/me", headers=admin_headers)
    assert response.status_code == 200
    assert response.json()["role"] == "admin"


async def test_bad_password_is_401(client):
    response = await client.post(
        "/api/v1/auth/login", json={"email": ADMIN["email"], "password": "WrongPassword99"}
    )
    assert response.status_code == 401
    assert response.json()["detail"] == "invalid credentials"


async def test_unknown_user_indistinguishable(client):
    response = await client.post(
        "/api/v1/auth/login", json={"email": "nobody@rwo.app", "password": "WrongPassword99"}
    )
    assert response.status_code == 401
    assert response.json()["detail"] == "invalid credentials"


async def test_protected_route_requires_token(client):
    assert (await client.get("/api/v1/employees")).status_code == 401
    bad = {"Authorization": "Bearer not.a.token"}
    assert (await client.get("/api/v1/employees", headers=bad)).status_code == 401


async def test_refresh_rotation_and_replay_revokes_family(client):
    tokens = (await client.post("/api/v1/auth/login", json=ADMIN)).json()
    first = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert first.status_code == 200
    rotated = first.json()["refresh_token"]
    assert rotated != tokens["refresh_token"]

    replay = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert replay.status_code == 401

    # family burned: the rotated token no longer works either
    after = await client.post("/api/v1/auth/refresh", json={"refresh_token": rotated})
    assert after.status_code == 401


async def test_logout_revokes_refresh(client):
    tokens = (await client.post("/api/v1/auth/login", json=ADMIN)).json()
    assert (
        await client.post("/api/v1/auth/logout", json={"refresh_token": tokens["refresh_token"]})
    ).status_code == 204
    assert (
        await client.post("/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    ).status_code == 401


async def test_rbac_viewer_cannot_write(client, admin_headers):
    created = await client.post(
        "/api/v1/auth/users",
        json={"email": "viewer@rwo.app", "password": "ViewerPassw0rd", "role": "viewer"},
        headers=admin_headers,
    )
    assert created.status_code == 201
    tokens = (
        await client.post(
            "/api/v1/auth/login", json={"email": "viewer@rwo.app", "password": "ViewerPassw0rd"}
        )
    ).json()
    viewer = {"Authorization": f"Bearer {tokens['access_token']}"}

    assert (await client.get("/api/v1/employees", headers=viewer)).status_code == 200
    blocked = await client.post(
        "/api/v1/employees",
        json={"name": "Test", "roles": ["server"], "hourly_rate": 15.0},
        headers=viewer,
    )
    assert blocked.status_code == 403

    not_admin = await client.post(
        "/api/v1/auth/users",
        json={"email": "x@rwo.app", "password": "AnotherPassw0rd", "role": "manager"},
        headers=viewer,
    )
    assert not_admin.status_code == 403


async def test_weak_password_rejected_on_user_create(client, admin_headers):
    response = await client.post(
        "/api/v1/auth/users",
        json={"email": "weak@rwo.app", "password": "short", "role": "manager"},
        headers=admin_headers,
    )
    assert response.status_code == 422


async def test_account_lockout_after_repeated_failures(client):
    for _ in range(8):
        await client.post(
            "/api/v1/auth/login", json={"email": ADMIN["email"], "password": "Wrong!Password1"}
        )
    locked = await client.post("/api/v1/auth/login", json=ADMIN)
    assert locked.status_code == 423
