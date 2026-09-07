"""Solver correctness: hard constraints must never be violated."""

from __future__ import annotations

import datetime as dt

import pytest

from app.optimizer import (
    EmployeeSpec,
    SolveParams,
    TemplateSpec,
    demand_to_requirements,
    required_headcount,
    solve,
)

DAY0 = dt.date(2026, 3, 2)  # Monday
ALWAYS = dict.fromkeys(range(7), (0, 24))


def make_employees(count: int, role: str = "server", rate: float = 15.0) -> list[EmployeeSpec]:
    return [
        EmployeeSpec(
            id=i,
            name=f"E{i}",
            roles=(role,),
            hourly_rate=rate + i * 0.1,
            max_weekly_hours=40,
            min_weekly_hours=0,
            max_consecutive_days=5,
            availability=dict(ALWAYS),
        )
        for i in range(1, count + 1)
    ]


TEMPLATES = [
    TemplateSpec(1, "Open", "server", 10, 18),
    TemplateSpec(2, "Close", "server", 15, 23),
]


def flat_demand(days: int, covers: float) -> dict[tuple[dt.date, int], float]:
    return {(DAY0 + dt.timedelta(days=d), h): covers for d in range(days) for h in range(10, 23)}


@pytest.mark.parametrize("solver", ["milp", "greedy"])
def test_coverage_met_when_capacity_is_ample(solver):
    employees = make_employees(20)
    requirements = demand_to_requirements(flat_demand(3, 12.0), {"server"})
    days = [DAY0 + dt.timedelta(days=d) for d in range(3)]
    result = solve(solver, employees, TEMPLATES, requirements, days, SolveParams())
    assert result.shifts
    assert result.understaffed_hours == 0.0


@pytest.mark.parametrize("solver", ["milp", "greedy"])
def test_one_shift_per_employee_per_day(solver):
    employees = make_employees(12)
    requirements = demand_to_requirements(flat_demand(4, 24.0), {"server"})
    days = [DAY0 + dt.timedelta(days=d) for d in range(4)]
    result = solve(solver, employees, TEMPLATES, requirements, days, SolveParams())
    seen = set()
    for shift in result.shifts:
        key = (shift.employee_id, shift.day)
        assert key not in seen, "employee double-booked on one day"
        seen.add(key)


@pytest.mark.parametrize("solver", ["milp", "greedy"])
def test_weekly_hour_cap_respected(solver):
    employees = [EmployeeSpec(1, "Solo", ("server",), 15.0, 16, 0, 7, availability=dict(ALWAYS))]
    requirements = demand_to_requirements(flat_demand(7, 12.0), {"server"})
    days = [DAY0 + dt.timedelta(days=d) for d in range(7)]
    result = solve(solver, employees, TEMPLATES, requirements, days, SolveParams())
    assert sum(s.hours for s in result.shifts) <= 16


@pytest.mark.parametrize("solver", ["milp", "greedy"])
def test_min_rest_blocks_close_then_open(solver):
    """Close ends 23:00, Open starts 10:00 -> 11h gap. With 12h rest it is illegal."""
    employees = make_employees(3)
    requirements = demand_to_requirements(flat_demand(2, 12.0), {"server"})
    days = [DAY0, DAY0 + dt.timedelta(days=1)]
    result = solve(solver, employees, TEMPLATES, requirements, days, SolveParams(min_rest_hours=12))
    by_employee: dict[int, dict[dt.date, tuple[int, int]]] = {}
    for shift in result.shifts:
        by_employee.setdefault(shift.employee_id, {})[shift.day] = (
            shift.start_hour,
            shift.end_hour,
        )
    for schedule in by_employee.values():
        first = schedule.get(DAY0)
        second = schedule.get(DAY0 + dt.timedelta(days=1))
        if first and second:
            assert (24 - first[1]) + second[0] >= 12


@pytest.mark.parametrize("solver", ["milp", "greedy"])
def test_time_off_and_availability_are_hard(solver):
    blocked_day = DAY0 + dt.timedelta(days=1)
    employees = [
        EmployeeSpec(
            1,
            "Evenings",
            ("server",),
            14.0,
            40,
            0,
            5,
            availability=dict.fromkeys(range(7), (15, 24)),
        ),
        EmployeeSpec(
            2,
            "OffTuesday",
            ("server",),
            14.0,
            40,
            0,
            5,
            availability=dict(ALWAYS),
            days_off=frozenset({blocked_day}),
        ),
    ]
    requirements = demand_to_requirements(flat_demand(3, 24.0), {"server"})
    days = [DAY0 + dt.timedelta(days=d) for d in range(3)]
    result = solve(solver, employees, TEMPLATES, requirements, days, SolveParams())
    for shift in result.shifts:
        if shift.employee_id == 1:
            assert shift.start_hour >= 15
        if shift.employee_id == 2:
            assert shift.day != blocked_day


@pytest.mark.parametrize("solver", ["milp", "greedy"])
def test_role_qualification_enforced(solver):
    employees = [EmployeeSpec(1, "CookOnly", ("cook",), 18.0, 40, 0, 5, availability=dict(ALWAYS))]
    requirements = demand_to_requirements(flat_demand(2, 12.0), {"server"})
    days = [DAY0, DAY0 + dt.timedelta(days=1)]
    result = solve(solver, employees, TEMPLATES, requirements, days, SolveParams())
    assert result.shifts == []


def test_milp_is_not_more_expensive_than_greedy():
    employees = make_employees(14)
    requirements = demand_to_requirements(flat_demand(5, 20.0), {"server"})
    days = [DAY0 + dt.timedelta(days=d) for d in range(5)]
    milp = solve("milp", employees, TEMPLATES, requirements, days, SolveParams())
    greedy = solve("greedy", employees, TEMPLATES, requirements, days, SolveParams())
    milp_penalty = milp.labor_cost + 90 * milp.understaffed_hours
    greedy_penalty = greedy.labor_cost + 90 * greedy.understaffed_hours
    assert milp_penalty <= greedy_penalty + 1e-6


def test_required_headcount_policy_bounds():
    assert required_headcount(0, "server") == 1  # min_on_duty floor
    assert required_headcount(24, "server") == 2
    assert required_headcount(100000, "server") == 12  # max_on_duty ceiling
    assert required_headcount(0, "busser") == 0


def test_unknown_solver_rejected():
    with pytest.raises(KeyError):
        solve("annealing", [], [], {}, [], SolveParams())
