"""Employee, availability, time-off and shift-template management."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.deps import client_ip, require_manager, require_viewer
from app.models import Availability, Employee, ShiftTemplate, TimeOff, User
from app.schemas import (
    AvailabilityIn,
    EmployeeCreate,
    EmployeeOut,
    EmployeeUpdate,
    ShiftTemplateIn,
    ShiftTemplateOut,
    TimeOffIn,
)
from app.services import audit

router = APIRouter(tags=["workforce"])


async def _get_employee(session: AsyncSession, employee_id: int) -> Employee:
    employee = await session.scalar(select(Employee).where(Employee.id == employee_id))
    if employee is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="employee not found")
    return employee


@router.get("/employees", response_model=list[EmployeeOut])
async def list_employees(
    active_only: bool = Query(default=False),
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    _user: User = Depends(require_viewer),
    session: AsyncSession = Depends(get_session),
) -> list[Employee]:
    stmt = select(Employee).order_by(Employee.id).limit(limit).offset(offset)
    if active_only:
        stmt = stmt.where(Employee.active.is_(True))
    return list((await session.execute(stmt)).scalars().all())


@router.post("/employees", response_model=EmployeeOut, status_code=status.HTTP_201_CREATED)
async def create_employee(
    payload: EmployeeCreate,
    request: Request,
    actor: User = Depends(require_manager),
    session: AsyncSession = Depends(get_session),
) -> Employee:
    if payload.min_weekly_hours > payload.max_weekly_hours:
        raise HTTPException(status_code=422, detail="min_weekly_hours exceeds max_weekly_hours")
    employee = Employee(
        name=payload.name,
        roles=payload.roles,
        hourly_rate=payload.hourly_rate,
        max_weekly_hours=payload.max_weekly_hours,
        min_weekly_hours=payload.min_weekly_hours,
        max_consecutive_days=payload.max_consecutive_days,
        seniority=payload.seniority,
        active=payload.active,
    )
    employee.availability = [
        Availability(
            weekday=a.weekday,
            earliest_hour=a.earliest_hour,
            latest_hour=a.latest_hour,
            available=a.available,
        )
        for a in payload.availability
    ]
    session.add(employee)
    await session.commit()
    await session.refresh(employee)
    await audit.record(
        session,
        actor=actor.email,
        action="employee_created",
        detail=f"id={employee.id} name={employee.name}",
        ip=client_ip(request),
    )
    return employee


@router.get("/employees/{employee_id}", response_model=EmployeeOut)
async def get_employee(
    employee_id: int,
    _user: User = Depends(require_viewer),
    session: AsyncSession = Depends(get_session),
) -> Employee:
    return await _get_employee(session, employee_id)


@router.patch("/employees/{employee_id}", response_model=EmployeeOut)
async def update_employee(
    employee_id: int,
    payload: EmployeeUpdate,
    _actor: User = Depends(require_manager),
    session: AsyncSession = Depends(get_session),
) -> Employee:
    employee = await _get_employee(session, employee_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(employee, field, value)
    if employee.min_weekly_hours > employee.max_weekly_hours:
        raise HTTPException(status_code=422, detail="min_weekly_hours exceeds max_weekly_hours")
    await session.commit()
    await session.refresh(employee)
    return employee


@router.delete("/employees/{employee_id}", status_code=status.HTTP_204_NO_CONTENT)
async def deactivate_employee(
    employee_id: int,
    _actor: User = Depends(require_manager),
    session: AsyncSession = Depends(get_session),
) -> None:
    employee = await _get_employee(session, employee_id)
    employee.active = False  # soft delete keeps historical schedules intact
    await session.commit()
    return None


@router.put("/employees/{employee_id}/availability", response_model=EmployeeOut)
async def set_availability(
    employee_id: int,
    payload: list[AvailabilityIn],
    _actor: User = Depends(require_manager),
    session: AsyncSession = Depends(get_session),
) -> Employee:
    employee = await _get_employee(session, employee_id)
    if len({a.weekday for a in payload}) != len(payload):
        raise HTTPException(status_code=422, detail="duplicate weekday in payload")
    await session.execute(delete(Availability).where(Availability.employee_id == employee_id))
    session.add_all(
        Availability(
            employee_id=employee_id,
            weekday=a.weekday,
            earliest_hour=a.earliest_hour,
            latest_hour=a.latest_hour,
            available=a.available,
        )
        for a in payload
    )
    await session.commit()
    await session.refresh(employee)
    return employee


@router.post("/employees/{employee_id}/time-off", response_model=EmployeeOut, status_code=201)
async def add_time_off(
    employee_id: int,
    payload: TimeOffIn,
    _actor: User = Depends(require_manager),
    session: AsyncSession = Depends(get_session),
) -> Employee:
    employee = await _get_employee(session, employee_id)
    exists = await session.scalar(
        select(TimeOff).where(TimeOff.employee_id == employee_id, TimeOff.day == payload.day)
    )
    if exists is None:
        session.add(TimeOff(employee_id=employee_id, day=payload.day, reason=payload.reason))
        await session.commit()
    await session.refresh(employee)
    return employee


# ------------------------------------------------------------------- templates
@router.get("/shift-templates", response_model=list[ShiftTemplateOut])
async def list_templates(
    _user: User = Depends(require_viewer), session: AsyncSession = Depends(get_session)
) -> list[ShiftTemplate]:
    stmt = select(ShiftTemplate).order_by(ShiftTemplate.role, ShiftTemplate.start_hour)
    return list((await session.execute(stmt)).scalars().all())


@router.post("/shift-templates", response_model=ShiftTemplateOut, status_code=201)
async def create_template(
    payload: ShiftTemplateIn,
    _actor: User = Depends(require_manager),
    session: AsyncSession = Depends(get_session),
) -> ShiftTemplate:
    template = ShiftTemplate(**payload.model_dump())
    session.add(template)
    await session.commit()
    await session.refresh(template)
    return template


@router.delete("/shift-templates/{template_id}", status_code=status.HTTP_204_NO_CONTENT)
async def deactivate_template(
    template_id: int,
    _actor: User = Depends(require_manager),
    session: AsyncSession = Depends(get_session),
) -> None:
    template = await session.scalar(select(ShiftTemplate).where(ShiftTemplate.id == template_id))
    if template is None:
        raise HTTPException(status_code=404, detail="template not found")
    template.active = False
    await session.commit()
    return None
