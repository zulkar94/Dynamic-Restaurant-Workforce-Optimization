"""Orchestration: load DB state, forecast demand, solve, persist schedule."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.ml.forecaster import DemandForecaster, ForecastResult
from app.models import Assignment, Employee, SalesRecord, Schedule, ShiftTemplate
from app.optimizer import (
    EmployeeSpec,
    SolveParams,
    SolveResult,
    TemplateSpec,
    demand_to_requirements,
    solve,
)


async def load_sales(session: AsyncSession, *, before: dt.date | None = None) -> list[dict]:
    stmt = select(SalesRecord).order_by(SalesRecord.day, SalesRecord.hour)
    if before is not None:
        stmt = stmt.where(SalesRecord.day < before)
    rows = (await session.execute(stmt)).scalars().all()
    return [
        {
            "day": r.day,
            "hour": r.hour,
            "covers": r.covers,
            "is_holiday": r.is_holiday,
            "weather_score": r.weather_score,
        }
        for r in rows
    ]


async def load_employee_specs(session: AsyncSession) -> list[EmployeeSpec]:
    rows = (
        (await session.execute(select(Employee).where(Employee.active.is_(True)))).scalars().all()
    )
    specs: list[EmployeeSpec] = []
    for e in rows:
        availability = {
            a.weekday: (a.earliest_hour, a.latest_hour) for a in e.availability if a.available
        }
        if not availability:  # no rows configured => always available
            availability = dict.fromkeys(range(7), (0, 24))
        specs.append(
            EmployeeSpec(
                id=e.id,
                name=e.name,
                roles=tuple(e.roles or ()),
                hourly_rate=e.hourly_rate,
                max_weekly_hours=e.max_weekly_hours,
                min_weekly_hours=e.min_weekly_hours,
                max_consecutive_days=e.max_consecutive_days,
                seniority=e.seniority,
                availability=availability,
                days_off=frozenset(t.day for t in e.time_off),
            )
        )
    return specs


async def load_template_specs(session: AsyncSession) -> list[TemplateSpec]:
    rows = (
        (await session.execute(select(ShiftTemplate).where(ShiftTemplate.active.is_(True))))
        .scalars()
        .all()
    )
    return [
        TemplateSpec(
            id=t.id, name=t.name, role=t.role, start_hour=t.start_hour, end_hour=t.end_hour
        )
        for t in rows
    ]


async def train_forecaster(
    session: AsyncSession, model: str, test_days: int = 14
) -> DemandForecaster:
    settings = get_settings()
    records = await load_sales(session)
    forecaster = DemandForecaster(model_name=model, service_hours=settings.service_hours)
    forecaster.fit(records, test_days=test_days)
    return forecaster


async def forecast_range(
    session: AsyncSession, model: str, start_day: dt.date, days: int
) -> ForecastResult:
    forecaster = await train_forecaster(session, model)
    return forecaster.predict_range(start_day, days)


async def build_schedule(
    session: AsyncSession,
    *,
    start_day: dt.date,
    days: int,
    model: str,
    solver: str,
    params: SolveParams,
    actor: str,
) -> tuple[Schedule, SolveResult, ForecastResult]:
    settings = get_settings()
    employees = await load_employee_specs(session)
    templates = await load_template_specs(session)
    if not employees:
        raise ValueError("no active employees")
    if not templates:
        raise ValueError("no active shift templates")

    forecast = await forecast_range(session, model, start_day, days)
    roles = {t.role for t in templates}
    requirements = demand_to_requirements(forecast.as_map(), roles)
    day_list = [start_day + dt.timedelta(days=i) for i in range(days)]

    params.time_limit_seconds = settings.solver_time_limit_seconds
    result = solve(solver, employees, templates, requirements, day_list, params)

    schedule = Schedule(
        start_day=start_day,
        end_day=day_list[-1],
        status="draft",
        solver=result.solver,
        labor_cost=result.labor_cost,
        understaffed_hours=result.understaffed_hours,
        overstaffed_hours=result.overstaffed_hours,
        forecast_model=forecast.model,
        created_by=actor,
        meta={
            "solve_status": result.status,
            "forecast_metrics": forecast.metrics,
            "forecast_rows": forecast.trained_rows,
            "employees_considered": len(employees),
            "templates": len(templates),
            **result.meta,
        },
    )
    schedule.assignments = [
        Assignment(
            employee_id=s.employee_id,
            employee_name=s.employee_name,
            template_id=s.template_id,
            role=s.role,
            day=s.day,
            start_hour=s.start_hour,
            end_hour=s.end_hour,
            hours=s.hours,
            cost=s.cost,
        )
        for s in result.shifts
    ]
    session.add(schedule)
    await session.commit()
    await session.refresh(schedule)
    return schedule, result, forecast
