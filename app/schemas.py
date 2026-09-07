"""Request/response contracts. Strict: unknown fields rejected."""

from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.security.passwords import validate_password_strength

ROLES = ("server", "cook", "host", "busser", "bartender")
USER_ROLES = ("admin", "manager", "viewer")


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ------------------------------------------------------------------------------ auth
class LoginRequest(Strict):
    email: EmailStr
    password: str = Field(min_length=1, max_length=256)


class RefreshRequest(Strict):
    refresh_token: str = Field(min_length=10, max_length=4096)


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    scheme: str = "bearer"
    expires_in: int


class UserCreate(Strict):
    email: EmailStr
    password: str = Field(min_length=12, max_length=256)
    role: str = "manager"

    @field_validator("password")
    @classmethod
    def _strength(cls, v: str) -> str:
        validate_password_strength(v)
        return v

    @field_validator("role")
    @classmethod
    def _role(cls, v: str) -> str:
        if v not in USER_ROLES:
            raise ValueError(f"role must be one of {USER_ROLES}")
        return v


class UserOut(ORMModel):
    id: int
    email: str
    role: str
    is_active: bool


# ------------------------------------------------------------------------- employees
class AvailabilityIn(Strict):
    weekday: int = Field(ge=0, le=6)
    earliest_hour: int = Field(default=0, ge=0, le=24)
    latest_hour: int = Field(default=24, ge=0, le=24)
    available: bool = True

    @field_validator("latest_hour")
    @classmethod
    def _order(cls, v: int, info) -> int:
        earliest = info.data.get("earliest_hour", 0)
        if v <= earliest:
            raise ValueError("latest_hour must exceed earliest_hour")
        return v


class AvailabilityOut(ORMModel):
    weekday: int
    earliest_hour: int
    latest_hour: int
    available: bool


class TimeOffIn(Strict):
    day: dt.date
    reason: str = Field(default="", max_length=255)


class TimeOffOut(ORMModel):
    day: dt.date
    reason: str


class EmployeeCreate(Strict):
    name: str = Field(min_length=1, max_length=120)
    roles: list[str] = Field(min_length=1)
    hourly_rate: float = Field(gt=0, le=500)
    max_weekly_hours: int = Field(default=40, ge=1, le=60)
    min_weekly_hours: int = Field(default=0, ge=0, le=60)
    max_consecutive_days: int = Field(default=5, ge=1, le=7)
    seniority: int = Field(default=1, ge=1, le=5)
    active: bool = True
    availability: list[AvailabilityIn] = Field(default_factory=list)

    @field_validator("roles")
    @classmethod
    def _roles(cls, v: list[str]) -> list[str]:
        bad = sorted(set(v) - set(ROLES))
        if bad:
            raise ValueError(f"unknown roles {bad}; allowed {ROLES}")
        return sorted(set(v))


class EmployeeUpdate(Strict):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    roles: list[str] | None = None
    hourly_rate: float | None = Field(default=None, gt=0, le=500)
    max_weekly_hours: int | None = Field(default=None, ge=1, le=60)
    min_weekly_hours: int | None = Field(default=None, ge=0, le=60)
    max_consecutive_days: int | None = Field(default=None, ge=1, le=7)
    seniority: int | None = Field(default=None, ge=1, le=5)
    active: bool | None = None

    @field_validator("roles")
    @classmethod
    def _roles(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return v
        return EmployeeCreate._roles(v)


class EmployeeOut(ORMModel):
    id: int
    name: str
    roles: list[str]
    hourly_rate: float
    max_weekly_hours: int
    min_weekly_hours: int
    max_consecutive_days: int
    seniority: int
    active: bool
    availability: list[AvailabilityOut] = []
    time_off: list[TimeOffOut] = []


# --------------------------------------------------------------------------- demand
class SalesIn(Strict):
    day: dt.date
    hour: int = Field(ge=0, le=23)
    covers: float = Field(ge=0, le=10000)
    revenue: float = Field(default=0.0, ge=0, le=1_000_000)
    is_holiday: bool = False
    weather_score: float = Field(default=0.5, ge=0, le=1)


class SalesBulkIn(Strict):
    records: list[SalesIn] = Field(min_length=1, max_length=20000)


class SalesOut(ORMModel):
    day: dt.date
    hour: int
    covers: float
    revenue: float
    is_holiday: bool
    weather_score: float


class ForecastPoint(BaseModel):
    day: dt.date
    hour: int
    covers: float


class ForecastOut(BaseModel):
    model: str
    trained_rows: int
    metrics: dict[str, float]
    points: list[ForecastPoint]


class TrainRequest(Strict):
    model: str = "hist_gbr"
    test_days: int = Field(default=14, ge=1, le=90)


# ------------------------------------------------------------------------- schedule
class ShiftTemplateIn(Strict):
    name: str = Field(min_length=1, max_length=64)
    role: str
    start_hour: int = Field(ge=0, le=23)
    end_hour: int = Field(ge=1, le=24)
    active: bool = True

    @field_validator("role")
    @classmethod
    def _role(cls, v: str) -> str:
        if v not in ROLES:
            raise ValueError(f"role must be one of {ROLES}")
        return v

    @field_validator("end_hour")
    @classmethod
    def _span(cls, v: int, info) -> int:
        start = info.data.get("start_hour", 0)
        if v - start < 3:
            raise ValueError("shift must span >= 3 hours")
        if v - start > 10:
            raise ValueError("shift must span <= 10 hours")
        return v


class ShiftTemplateOut(ORMModel):
    id: int
    name: str
    role: str
    start_hour: int
    end_hour: int
    active: bool


class OptimizeRequest(Strict):
    start_day: dt.date
    days: int = Field(default=7, ge=1, le=21)
    model: str = "hist_gbr"
    solver: str = Field(default="milp", pattern="^(milp|greedy)$")
    understaff_penalty: float = Field(default=90.0, ge=0, le=10000)
    overstaff_penalty: float = Field(default=8.0, ge=0, le=10000)
    min_hours_penalty: float = Field(default=6.0, ge=0, le=10000)
    min_rest_hours: int = Field(default=11, ge=0, le=16)


class AssignmentOut(ORMModel):
    employee_id: int
    employee_name: str
    role: str
    day: dt.date
    start_hour: int
    end_hour: int
    hours: float
    cost: float


class CoverageRow(BaseModel):
    day: dt.date
    hour: int
    role: str
    required: int
    scheduled: int


class ScheduleOut(ORMModel):
    id: int
    start_day: dt.date
    end_day: dt.date
    status: str
    solver: str
    labor_cost: float
    understaffed_hours: float
    overstaffed_hours: float
    forecast_model: str
    meta: dict
    assignments: list[AssignmentOut] = []


class ScheduleDetail(ScheduleOut):
    coverage: list[CoverageRow] = []
