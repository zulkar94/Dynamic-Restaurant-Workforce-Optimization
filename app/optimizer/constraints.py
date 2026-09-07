"""Domain types, labour rules, and demand -> headcount translation."""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field

# covers one staff member of this role can serve per hour, plus hard floors/ceilings
ROLE_POLICY: dict[str, dict[str, int]] = {
    "server": {"covers_per_staff": 12, "min_on_duty": 1, "max_on_duty": 12},
    "cook": {"covers_per_staff": 18, "min_on_duty": 1, "max_on_duty": 8},
    "host": {"covers_per_staff": 60, "min_on_duty": 1, "max_on_duty": 3},
    "busser": {"covers_per_staff": 35, "min_on_duty": 0, "max_on_duty": 5},
    "bartender": {"covers_per_staff": 45, "min_on_duty": 0, "max_on_duty": 4},
}

OVERTIME_THRESHOLD_HOURS = 40
OVERTIME_MULTIPLIER = 1.5


@dataclass(frozen=True)
class EmployeeSpec:
    id: int
    name: str
    roles: tuple[str, ...]
    hourly_rate: float
    max_weekly_hours: int
    min_weekly_hours: int
    max_consecutive_days: int
    seniority: int = 1
    # weekday -> (earliest_hour, latest_hour); missing weekday means unavailable
    availability: dict[int, tuple[int, int]] = field(default_factory=dict)
    days_off: frozenset[dt.date] = frozenset()

    def can_work(self, day: dt.date, start_hour: int, end_hour: int) -> bool:
        if day in self.days_off:
            return False
        window = self.availability.get(day.weekday())
        if window is None:
            return False
        earliest, latest = window
        return earliest <= start_hour and end_hour <= latest


@dataclass(frozen=True)
class TemplateSpec:
    id: int
    name: str
    role: str
    start_hour: int
    end_hour: int

    @property
    def hours(self) -> int:
        return self.end_hour - self.start_hour

    def covers_hour(self, hour: int) -> bool:
        return self.start_hour <= hour < self.end_hour


@dataclass
class SolveParams:
    understaff_penalty: float = 90.0
    overstaff_penalty: float = 8.0
    min_hours_penalty: float = 6.0
    min_rest_hours: int = 11
    time_limit_seconds: int = 30


@dataclass
class PlannedShift:
    employee_id: int
    employee_name: str
    template_id: int
    role: str
    day: dt.date
    start_hour: int
    end_hour: int
    hours: float
    cost: float


@dataclass
class SolveResult:
    status: str
    solver: str
    shifts: list[PlannedShift]
    labor_cost: float
    understaffed_hours: float
    overstaffed_hours: float
    coverage: list[dict]
    meta: dict


def required_headcount(covers: float, role: str) -> int:
    policy = ROLE_POLICY.get(role)
    if policy is None:
        raise KeyError(f"no staffing policy for role '{role}'")
    raw = math.ceil(max(0.0, covers) / policy["covers_per_staff"])
    return int(min(max(raw, policy["min_on_duty"]), policy["max_on_duty"]))


def demand_to_requirements(
    forecast: dict[tuple[dt.date, int], float],
    roles: set[str],
) -> dict[tuple[dt.date, int, str], int]:
    """Translate hourly covers into per-role headcount targets."""
    return {
        (day, hour, role): required_headcount(covers, role)
        for (day, hour), covers in forecast.items()
        for role in roles
        if role in ROLE_POLICY
    }


def shift_cost(employee: EmployeeSpec, template: TemplateSpec) -> float:
    """Base cost. Overtime premium handled separately by the solver's weekly term."""
    return round(employee.hourly_rate * template.hours, 4)


def rest_violation(first: TemplateSpec, second: TemplateSpec, min_rest_hours: int) -> bool:
    """True when `second` (next calendar day) starts before the rest window closes."""
    gap = (24 - first.end_hour) + second.start_hour
    return gap < min_rest_hours


def coverage_report(
    shifts: list[PlannedShift],
    requirements: dict[tuple[dt.date, int, str], int],
) -> tuple[list[dict], float, float]:
    scheduled: dict[tuple[dt.date, int, str], int] = {}
    for shift in shifts:
        for hour in range(shift.start_hour, shift.end_hour):
            key = (shift.day, hour, shift.role)
            scheduled[key] = scheduled.get(key, 0) + 1

    rows: list[dict] = []
    under = 0.0
    over = 0.0
    for key in sorted(set(requirements) | set(scheduled)):
        day, hour, role = key
        need = requirements.get(key, 0)
        have = scheduled.get(key, 0)
        under += max(0, need - have)
        over += max(0, have - need)
        rows.append({"day": day, "hour": hour, "role": role, "required": need, "scheduled": have})
    return rows, float(under), float(over)
