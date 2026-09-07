"""Mixed-integer shift assignment.

Decision var x[e,d,t] = 1 when employee e works template t on day d.

Objective  min  sum(wage cost) + overtime premium
                + understaff_penalty * missing staff-hours
                + overstaff_penalty  * surplus staff-hours
                + min_hours_penalty  * contracted-hours shortfall

Hard constraints
  C1 one shift per employee per day
  C2 role qualification, availability window, approved time off
  C3 weekly hours <= contractual maximum
  C4 daily rest >= min_rest_hours between consecutive days
  C5 consecutive working days <= employee limit
"""

from __future__ import annotations

import datetime as dt

import pulp

from app.optimizer.constraints import (
    OVERTIME_MULTIPLIER,
    OVERTIME_THRESHOLD_HOURS,
    EmployeeSpec,
    PlannedShift,
    SolveParams,
    SolveResult,
    TemplateSpec,
    coverage_report,
    rest_violation,
    shift_cost,
)


class SolverUnavailable(RuntimeError):
    """CBC (or any configured backend) not usable in this environment."""


def _week_index(day: dt.date, start_day: dt.date) -> int:
    return (day - start_day).days // 7


def solve(
    employees: list[EmployeeSpec],
    templates: list[TemplateSpec],
    requirements: dict[tuple[dt.date, int, str], int],
    days: list[dt.date],
    params: SolveParams,
) -> SolveResult:
    if not employees or not templates or not days:
        return SolveResult("infeasible", "milp", [], 0.0, 0.0, 0.0, [], {"reason": "empty inputs"})

    problem = pulp.LpProblem("workforce", pulp.LpMinimize)
    start_day = min(days)

    # ---- decision variables (only feasible triples are created: C2 baked in) ----
    x: dict[tuple[int, dt.date, int], pulp.LpVariable] = {}
    by_employee_day: dict[tuple[int, dt.date], list[pulp.LpVariable]] = {}
    for e in employees:
        for day in days:
            for t in templates:
                if t.role not in e.roles:
                    continue
                if not e.can_work(day, t.start_hour, t.end_hour):
                    continue
                var = pulp.LpVariable(f"x_{e.id}_{day.isoformat()}_{t.id}", cat="Binary")
                x[(e.id, day, t.id)] = var
                by_employee_day.setdefault((e.id, day), []).append(var)

    if not x:
        return SolveResult(
            "infeasible", "milp", [], 0.0, 0.0, 0.0, [], {"reason": "no feasible assignments"}
        )

    template_by_id = {t.id: t for t in templates}
    employee_by_id = {e.id: e for e in employees}

    # ---- slack variables for soft coverage ----
    under = {
        key: pulp.LpVariable(f"u_{key[0].isoformat()}_{key[1]}_{key[2]}", lowBound=0)
        for key in requirements
    }
    over = {
        key: pulp.LpVariable(f"o_{key[0].isoformat()}_{key[1]}_{key[2]}", lowBound=0)
        for key in requirements
    }

    # ---- coverage constraints ----
    for (day, hour, role), need in requirements.items():
        staffed = [
            var
            for (eid, d, tid), var in x.items()
            if d == day
            and template_by_id[tid].role == role
            and template_by_id[tid].covers_hour(hour)
        ]
        expr = pulp.lpSum(staffed) if staffed else 0
        problem += expr + under[(day, hour, role)] >= need, f"cov_min_{day}_{hour}_{role}"
        problem += expr - over[(day, hour, role)] <= need, f"cov_max_{day}_{hour}_{role}"

    # ---- C1 one shift per employee per day ----
    for (eid, day), vars_ in by_employee_day.items():
        problem += pulp.lpSum(vars_) <= 1, f"one_shift_{eid}_{day}"

    # ---- C3 weekly hours + overtime split, C? contracted minimum shortfall ----
    weeks = sorted({_week_index(d, start_day) for d in days})
    overtime_hours: dict[tuple[int, int], pulp.LpVariable] = {}
    shortfall: dict[tuple[int, int], pulp.LpVariable] = {}
    for e in employees:
        for week in weeks:
            week_days = [d for d in days if _week_index(d, start_day) == week]
            hours_expr = pulp.lpSum(
                x[(e.id, d, t.id)] * t.hours
                for d in week_days
                for t in templates
                if (e.id, d, t.id) in x
            )
            problem += hours_expr <= e.max_weekly_hours, f"maxh_{e.id}_{week}"

            ot = pulp.LpVariable(f"ot_{e.id}_{week}", lowBound=0)
            problem += ot >= hours_expr - OVERTIME_THRESHOLD_HOURS, f"ot_def_{e.id}_{week}"
            overtime_hours[(e.id, week)] = ot

            if e.min_weekly_hours > 0 and len(week_days) >= 5:
                sf = pulp.LpVariable(f"sf_{e.id}_{week}", lowBound=0)
                problem += sf >= e.min_weekly_hours - hours_expr, f"minh_{e.id}_{week}"
                shortfall[(e.id, week)] = sf

    # ---- C4 minimum rest between consecutive days ----
    ordered = sorted(days)
    for e in employees:
        for day, nxt in zip(ordered, ordered[1:], strict=False):
            if (nxt - day).days != 1:
                continue
            for t1 in templates:
                if (e.id, day, t1.id) not in x:
                    continue
                for t2 in templates:
                    if (e.id, nxt, t2.id) not in x:
                        continue
                    if rest_violation(t1, t2, params.min_rest_hours):
                        problem += (
                            x[(e.id, day, t1.id)] + x[(e.id, nxt, t2.id)] <= 1,
                            f"rest_{e.id}_{day}_{t1.id}_{t2.id}",
                        )

    # ---- C5 max consecutive working days ----
    for e in employees:
        window = e.max_consecutive_days
        if window >= len(ordered):
            continue
        for i in range(len(ordered) - window):
            block = ordered[i : i + window + 1]
            if (block[-1] - block[0]).days != window:
                continue
            block_vars = [v for (eid, d, _t), v in x.items() if eid == e.id and d in block]
            if block_vars:
                problem += pulp.lpSum(block_vars) <= window, f"consec_{e.id}_{block[0]}"

    # ---- objective ----
    wage = pulp.lpSum(
        var * shift_cost(employee_by_id[eid], template_by_id[tid])
        for (eid, _d, tid), var in x.items()
    )
    overtime_premium = pulp.lpSum(
        ot * employee_by_id[eid].hourly_rate * (OVERTIME_MULTIPLIER - 1.0)
        for (eid, _w), ot in overtime_hours.items()
    )
    problem += (
        wage
        + overtime_premium
        + params.understaff_penalty * pulp.lpSum(under.values())
        + params.overstaff_penalty * pulp.lpSum(over.values())
        + params.min_hours_penalty * pulp.lpSum(shortfall.values())
    )

    try:
        solver = pulp.PULP_CBC_CMD(msg=False, timeLimit=params.time_limit_seconds)
        problem.solve(solver)
    except pulp.PulpSolverError as exc:  # no CBC binary on this host
        raise SolverUnavailable(str(exc)) from exc

    status = pulp.LpStatus[problem.status].lower()
    if status not in ("optimal", "not solved"):
        return SolveResult(status, "milp", [], 0.0, 0.0, 0.0, [], {"reason": status})

    shifts: list[PlannedShift] = []
    for (eid, day, tid), var in x.items():
        if var.value() is None or var.value() < 0.5:
            continue
        employee = employee_by_id[eid]
        template = template_by_id[tid]
        shifts.append(
            PlannedShift(
                employee_id=eid,
                employee_name=employee.name,
                template_id=tid,
                role=template.role,
                day=day,
                start_hour=template.start_hour,
                end_hour=template.end_hour,
                hours=float(template.hours),
                cost=shift_cost(employee, template),
            )
        )
    shifts.sort(key=lambda s: (s.day, s.start_hour, s.role, s.employee_name))

    coverage, under_hours, over_hours = coverage_report(shifts, requirements)
    labor_cost = round(sum(s.cost for s in shifts), 2)
    premium = round(
        sum(
            (ot.value() or 0.0) * employee_by_id[eid].hourly_rate * (OVERTIME_MULTIPLIER - 1.0)
            for (eid, _w), ot in overtime_hours.items()
        ),
        2,
    )
    return SolveResult(
        status="optimal",
        solver="milp",
        shifts=shifts,
        labor_cost=round(labor_cost + premium, 2),
        understaffed_hours=under_hours,
        overstaffed_hours=over_hours,
        coverage=coverage,
        meta={
            "objective": round(float(pulp.value(problem.objective) or 0.0), 2),
            "overtime_premium": premium,
            "variables": len(x),
            "constraints": int(problem.numConstraints()),
        },
    )
