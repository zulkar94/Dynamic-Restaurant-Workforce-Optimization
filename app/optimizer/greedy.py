"""Deterministic greedy fallback. Used when CBC is absent or explicitly requested.

Sorts unmet demand by shortfall, fills with cheapest qualified employee that keeps
every hard rule intact. No optimality guarantee; typically 5-15% costlier than MILP.
"""

from __future__ import annotations

import datetime as dt

from app.optimizer.constraints import (
    EmployeeSpec,
    PlannedShift,
    SolveParams,
    SolveResult,
    TemplateSpec,
    coverage_report,
    rest_violation,
    shift_cost,
)


def _week_key(day: dt.date) -> tuple[int, int]:
    iso = day.isocalendar()
    return (iso[0], iso[1])


def solve(
    employees: list[EmployeeSpec],
    templates: list[TemplateSpec],
    requirements: dict[tuple[dt.date, int, str], int],
    days: list[dt.date],
    params: SolveParams,
) -> SolveResult:
    shifts: list[PlannedShift] = []
    assigned_day: dict[int, dict[dt.date, TemplateSpec]] = {e.id: {} for e in employees}
    weekly_hours: dict[tuple[int, tuple[int, int]], float] = {}
    scheduled: dict[tuple[dt.date, int, str], int] = {}

    def deficit(day: dt.date, hour: int, role: str) -> int:
        return requirements.get((day, hour, role), 0) - scheduled.get((day, hour, role), 0)

    def eligible(e: EmployeeSpec, day: dt.date, t: TemplateSpec) -> bool:
        if t.role not in e.roles or not e.can_work(day, t.start_hour, t.end_hour):
            return False
        if day in assigned_day[e.id]:
            return False
        week = _week_key(day)
        if weekly_hours.get((e.id, week), 0.0) + t.hours > e.max_weekly_hours:
            return False
        prev = assigned_day[e.id].get(day - dt.timedelta(days=1))
        if prev is not None and rest_violation(prev, t, params.min_rest_hours):
            return False
        nxt = assigned_day[e.id].get(day + dt.timedelta(days=1))
        if nxt is not None and rest_violation(t, nxt, params.min_rest_hours):
            return False
        streak = 1
        probe = day - dt.timedelta(days=1)
        while probe in assigned_day[e.id]:
            streak += 1
            probe -= dt.timedelta(days=1)
        probe = day + dt.timedelta(days=1)
        while probe in assigned_day[e.id]:
            streak += 1
            probe += dt.timedelta(days=1)
        return streak <= e.max_consecutive_days

    progress = True
    while progress:
        progress = False
        gaps = sorted(
            (
                (deficit(day, hour, role), day, hour, role)
                for (day, hour, role) in requirements
                if deficit(day, hour, role) > 0
            ),
            key=lambda item: (-item[0], item[1], item[2], item[3]),
        )
        for _gap, day, hour, role in gaps:
            candidates: list[tuple[float, int, EmployeeSpec, TemplateSpec]] = []
            for t in templates:
                if t.role != role or not t.covers_hour(hour):
                    continue
                for e in employees:
                    if not eligible(e, day, t):
                        continue
                    filled = sum(
                        1 for h in range(t.start_hour, t.end_hour) if deficit(day, h, role) > 0
                    )
                    if filled == 0:
                        continue
                    unit = shift_cost(e, t) / filled
                    candidates.append((unit, -e.seniority, e, t))
            if not candidates:
                continue
            candidates.sort(key=lambda c: (c[0], c[1], c[2].id, c[3].id))
            _unit, _sen, employee, template = candidates[0]
            assigned_day[employee.id][day] = template
            weekly_hours[(employee.id, _week_key(day))] = (
                weekly_hours.get((employee.id, _week_key(day)), 0.0) + template.hours
            )
            for h in range(template.start_hour, template.end_hour):
                key = (day, h, role)
                scheduled[key] = scheduled.get(key, 0) + 1
            shifts.append(
                PlannedShift(
                    employee_id=employee.id,
                    employee_name=employee.name,
                    template_id=template.id,
                    role=role,
                    day=day,
                    start_hour=template.start_hour,
                    end_hour=template.end_hour,
                    hours=float(template.hours),
                    cost=shift_cost(employee, template),
                )
            )
            progress = True
            break

    shifts.sort(key=lambda s: (s.day, s.start_hour, s.role, s.employee_name))
    coverage, under_hours, over_hours = coverage_report(shifts, requirements)
    return SolveResult(
        status="heuristic",
        solver="greedy",
        shifts=shifts,
        labor_cost=round(sum(s.cost for s in shifts), 2),
        understaffed_hours=under_hours,
        overstaffed_hours=over_hours,
        coverage=coverage,
        meta={"assignments": len(shifts)},
    )
