"""Schedule generation, retrieval, publication and labour analytics."""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_session
from app.deps import client_ip, require_manager, require_viewer
from app.ml.registry import MODEL_REGISTRY
from app.models import Schedule, User
from app.optimizer import ROLE_POLICY, SolveParams, demand_to_requirements
from app.schemas import CoverageRow, OptimizeRequest, ScheduleDetail, ScheduleOut
from app.services import audit, planner

router = APIRouter(tags=["schedule"])


async def _get_schedule(session: AsyncSession, schedule_id: int) -> Schedule:
    schedule = await session.scalar(select(Schedule).where(Schedule.id == schedule_id))
    if schedule is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="schedule not found")
    return schedule


@router.post("/schedules/optimize", response_model=ScheduleDetail, status_code=201)
async def optimize(
    payload: OptimizeRequest,
    request: Request,
    actor: User = Depends(require_manager),
    session: AsyncSession = Depends(get_session),
) -> ScheduleDetail:
    settings = get_settings()
    if payload.model not in MODEL_REGISTRY:
        raise HTTPException(
            status_code=422, detail=f"unknown model; available {sorted(MODEL_REGISTRY)}"
        )
    if payload.days > settings.horizon_days_max:
        raise HTTPException(
            status_code=422, detail=f"horizon exceeds {settings.horizon_days_max} days"
        )

    params = SolveParams(
        understaff_penalty=payload.understaff_penalty,
        overstaff_penalty=payload.overstaff_penalty,
        min_hours_penalty=payload.min_hours_penalty,
        min_rest_hours=payload.min_rest_hours,
        time_limit_seconds=settings.solver_time_limit_seconds,
    )
    try:
        schedule, result, _forecast = await planner.build_schedule(
            session,
            start_day=payload.start_day,
            days=payload.days,
            model=payload.model,
            solver=payload.solver,
            params=params,
            actor=actor.email,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    await audit.record(
        session,
        actor=actor.email,
        action="schedule_optimized",
        detail=f"id={schedule.id} solver={result.solver} cost={result.labor_cost}",
        ip=client_ip(request),
    )
    detail = ScheduleDetail.model_validate(schedule)
    detail.coverage = [CoverageRow(**row) for row in result.coverage]
    return detail


@router.get("/schedules", response_model=list[ScheduleOut])
async def list_schedules(
    limit: int = Query(default=50, ge=1, le=200),
    _user: User = Depends(require_viewer),
    session: AsyncSession = Depends(get_session),
) -> list[Schedule]:
    stmt = select(Schedule).order_by(Schedule.id.desc()).limit(limit)
    return list((await session.execute(stmt)).scalars().all())


@router.get("/schedules/{schedule_id}", response_model=ScheduleDetail)
async def get_schedule(
    schedule_id: int,
    _user: User = Depends(require_viewer),
    session: AsyncSession = Depends(get_session),
) -> ScheduleDetail:
    schedule = await _get_schedule(session, schedule_id)
    detail = ScheduleDetail.model_validate(schedule)

    days = (schedule.end_day - schedule.start_day).days + 1
    forecast = await planner.forecast_range(
        session,
        schedule.forecast_model if schedule.forecast_model in MODEL_REGISTRY else "hist_gbr",
        schedule.start_day,
        days,
    )
    roles = {a.role for a in schedule.assignments} or set(ROLE_POLICY)
    requirements = demand_to_requirements(forecast.as_map(), roles)
    scheduled: dict[tuple[dt.date, int, str], int] = {}
    for a in schedule.assignments:
        for hour in range(a.start_hour, a.end_hour):
            key = (a.day, hour, a.role)
            scheduled[key] = scheduled.get(key, 0) + 1
    detail.coverage = [
        CoverageRow(
            day=key[0],
            hour=key[1],
            role=key[2],
            required=requirements.get(key, 0),
            scheduled=scheduled.get(key, 0),
        )
        for key in sorted(set(requirements) | set(scheduled))
    ]
    return detail


@router.post("/schedules/{schedule_id}/publish", response_model=ScheduleOut)
async def publish(
    schedule_id: int,
    request: Request,
    actor: User = Depends(require_manager),
    session: AsyncSession = Depends(get_session),
) -> Schedule:
    schedule = await _get_schedule(session, schedule_id)
    if schedule.status == "published":
        return schedule
    if not schedule.assignments:
        raise HTTPException(status_code=409, detail="cannot publish an empty schedule")
    schedule.status = "published"
    await session.commit()
    await session.refresh(schedule)
    await audit.record(
        session,
        actor=actor.email,
        action="schedule_published",
        detail=f"id={schedule_id}",
        ip=client_ip(request),
    )
    return schedule


@router.get("/schedules/{schedule_id}/labor-summary")
async def labor_summary(
    schedule_id: int,
    _user: User = Depends(require_viewer),
    session: AsyncSession = Depends(get_session),
) -> dict:
    schedule = await _get_schedule(session, schedule_id)
    per_employee: dict[str, dict] = {}
    per_role: dict[str, dict] = {}
    per_day: dict[str, dict] = {}
    for a in schedule.assignments:
        emp = per_employee.setdefault(a.employee_name, {"hours": 0.0, "cost": 0.0, "shifts": 0})
        emp["hours"] += a.hours
        emp["cost"] = round(emp["cost"] + a.cost, 2)
        emp["shifts"] += 1
        role = per_role.setdefault(a.role, {"hours": 0.0, "cost": 0.0})
        role["hours"] += a.hours
        role["cost"] = round(role["cost"] + a.cost, 2)
        day = per_day.setdefault(a.day.isoformat(), {"hours": 0.0, "cost": 0.0})
        day["hours"] += a.hours
        day["cost"] = round(day["cost"] + a.cost, 2)

    overtime = {
        name: round(v["hours"] - 40, 2) for name, v in per_employee.items() if v["hours"] > 40
    }
    return {
        "schedule_id": schedule.id,
        "status": schedule.status,
        "labor_cost": schedule.labor_cost,
        "understaffed_hours": schedule.understaffed_hours,
        "overstaffed_hours": schedule.overstaffed_hours,
        "total_hours": round(sum(a.hours for a in schedule.assignments), 2),
        "per_employee": per_employee,
        "per_role": per_role,
        "per_day": per_day,
        "overtime_hours": overtime,
        "meta": schedule.meta,
    }
