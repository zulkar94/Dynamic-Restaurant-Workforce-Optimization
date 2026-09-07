"""End-to-end: ingest sales, forecast, optimise, publish."""

from __future__ import annotations

import datetime as dt

import pytest

pytestmark = pytest.mark.asyncio

EMPLOYEE_TEMPLATE = {
    "roles": ["server"],
    "hourly_rate": 15.0,
    "max_weekly_hours": 40,
    "min_weekly_hours": 0,
    "max_consecutive_days": 5,
}


async def _create_staff(client, headers, count: int = 14) -> None:
    roles = ["server", "cook", "host", "busser", "bartender"]
    for i in range(count):
        payload = dict(EMPLOYEE_TEMPLATE)
        payload["name"] = f"Staff {i:02d}"
        payload["roles"] = [roles[i % len(roles)]]
        payload["hourly_rate"] = 14.0 + i * 0.25
        response = await client.post("/api/v1/employees", json=payload, headers=headers)
        assert response.status_code == 201, response.text


async def _ingest(client, headers, sales_history) -> None:
    records = [
        {
            "day": r["day"].isoformat(),
            "hour": r["hour"],
            "covers": r["covers"],
            "revenue": round(r["covers"] * 32, 2),
            "is_holiday": r["is_holiday"],
            "weather_score": r["weather_score"],
        }
        for r in sales_history
    ]
    response = await client.post(
        "/api/v1/demand/ingest", json={"records": records}, headers=headers
    )
    assert response.status_code == 202
    assert response.json()["ingested"] == len(records)


async def test_full_planning_cycle(client, admin_headers, sales_history):
    await _create_staff(client, admin_headers)
    await _ingest(client, admin_headers, sales_history)

    templates = await client.get("/api/v1/shift-templates", headers=admin_headers)
    assert templates.status_code == 200 and len(templates.json()) >= 5

    trained = await client.post(
        "/api/v1/forecast/train", json={"model": "hist_gbr", "test_days": 14}, headers=admin_headers
    )
    assert trained.status_code == 200
    assert trained.json()["fallback"] is False
    assert trained.json()["metrics"]["mae"] < 10

    start = (max(r["day"] for r in sales_history) + dt.timedelta(days=1)).isoformat()
    forecast = await client.get(f"/api/v1/forecast?start_day={start}&days=7", headers=admin_headers)
    assert forecast.status_code == 200
    assert len(forecast.json()["points"]) == 7 * 13

    optimized = await client.post(
        "/api/v1/schedules/optimize",
        json={"start_day": start, "days": 7, "model": "hist_gbr", "solver": "milp"},
        headers=admin_headers,
    )
    assert optimized.status_code == 201, optimized.text
    schedule = optimized.json()
    assert schedule["status"] == "draft"
    assert schedule["assignments"], "solver returned no shifts"
    assert schedule["labor_cost"] > 0
    assert schedule["coverage"], "coverage report missing"

    # no employee assigned twice on the same day
    keys = {(a["employee_id"], a["day"]) for a in schedule["assignments"]}
    assert len(keys) == len(schedule["assignments"])

    detail = await client.get(f"/api/v1/schedules/{schedule['id']}", headers=admin_headers)
    assert detail.status_code == 200

    summary = await client.get(
        f"/api/v1/schedules/{schedule['id']}/labor-summary", headers=admin_headers
    )
    assert summary.status_code == 200
    body = summary.json()
    assert body["total_hours"] > 0
    assert sum(v["hours"] for v in body["per_employee"].values()) == body["total_hours"]

    published = await client.post(
        f"/api/v1/schedules/{schedule['id']}/publish", headers=admin_headers
    )
    assert published.status_code == 200
    assert published.json()["status"] == "published"


async def test_optimize_without_employees_conflicts(client, admin_headers, sales_history):
    await _ingest(client, admin_headers, sales_history)
    start = (max(r["day"] for r in sales_history) + dt.timedelta(days=1)).isoformat()
    response = await client.post(
        "/api/v1/schedules/optimize",
        json={"start_day": start, "days": 3},
        headers=admin_headers,
    )
    assert response.status_code == 409
    assert "no active employees" in response.json()["detail"]


async def test_ingest_is_idempotent(client, admin_headers):
    record = {"day": "2026-02-02", "hour": 12, "covers": 40.0, "revenue": 1200.0}
    for covers in (40.0, 55.0):
        record["covers"] = covers
        response = await client.post(
            "/api/v1/demand/ingest", json={"records": [record]}, headers=admin_headers
        )
        assert response.status_code == 202
    history = await client.get(
        "/api/v1/demand/history?start=2026-02-02&end=2026-02-02", headers=admin_headers
    )
    rows = history.json()
    assert len(rows) == 1 and rows[0]["covers"] == 55.0


async def test_unknown_model_rejected(client, admin_headers):
    response = await client.get(
        "/api/v1/forecast?start_day=2026-03-01&days=3&model=magic", headers=admin_headers
    )
    assert response.status_code == 422


async def test_horizon_cap_enforced(client, admin_headers):
    response = await client.post(
        "/api/v1/schedules/optimize",
        json={"start_day": "2026-03-01", "days": 60},
        headers=admin_headers,
    )
    assert response.status_code == 422


async def test_employee_lifecycle_and_availability(client, admin_headers):
    created = await client.post(
        "/api/v1/employees",
        json={"name": "Ana", "roles": ["server", "host"], "hourly_rate": 17.5},
        headers=admin_headers,
    )
    assert created.status_code == 201
    employee_id = created.json()["id"]

    availability = await client.put(
        f"/api/v1/employees/{employee_id}/availability",
        json=[{"weekday": 0, "earliest_hour": 15, "latest_hour": 24}],
        headers=admin_headers,
    )
    assert availability.status_code == 200
    assert availability.json()["availability"][0]["earliest_hour"] == 15

    duplicate = await client.put(
        f"/api/v1/employees/{employee_id}/availability",
        json=[
            {"weekday": 1, "earliest_hour": 8, "latest_hour": 16},
            {"weekday": 1, "earliest_hour": 9, "latest_hour": 17},
        ],
        headers=admin_headers,
    )
    assert duplicate.status_code == 422

    time_off = await client.post(
        f"/api/v1/employees/{employee_id}/time-off",
        json={"day": "2026-04-01", "reason": "leave"},
        headers=admin_headers,
    )
    assert time_off.status_code == 201
    assert time_off.json()["time_off"][0]["day"] == "2026-04-01"

    patched = await client.patch(
        f"/api/v1/employees/{employee_id}", json={"hourly_rate": 19.0}, headers=admin_headers
    )
    assert patched.status_code == 200 and patched.json()["hourly_rate"] == 19.0

    deleted = await client.delete(f"/api/v1/employees/{employee_id}", headers=admin_headers)
    assert deleted.status_code == 204
    after = await client.get(f"/api/v1/employees/{employee_id}", headers=admin_headers)
    assert after.json()["active"] is False


async def test_invalid_shift_template_rejected(client, admin_headers):
    too_long = await client.post(
        "/api/v1/shift-templates",
        json={"name": "Marathon", "role": "server", "start_hour": 8, "end_hour": 22},
        headers=admin_headers,
    )
    assert too_long.status_code == 422
    unknown_role = await client.post(
        "/api/v1/shift-templates",
        json={"name": "Sommelier", "role": "sommelier", "start_hour": 16, "end_hour": 22},
        headers=admin_headers,
    )
    assert unknown_role.status_code == 422
