"""SQLAlchemy 2.0 declarative models."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class Base(DeclarativeBase):
    pass


# --------------------------------------------------------------------------- auth
class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(16), default="manager")  # admin|manager|viewer
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RefreshToken(Base):
    """One row per issued refresh token. Rotation revokes whole family on reuse."""

    __tablename__ = "refresh_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    jti: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    family: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    used: Mapped[bool] = mapped_column(Boolean, default=False)
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    actor: Mapped[str] = mapped_column(String(255), default="anonymous")
    action: Mapped[str] = mapped_column(String(64), index=True)
    detail: Mapped[str] = mapped_column(String(1024), default="")
    ip: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ------------------------------------------------------------------------ workforce
class Employee(Base):
    __tablename__ = "employees"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), index=True)
    roles: Mapped[list[str]] = mapped_column(JSON, default=list)  # e.g. ["server","host"]
    hourly_rate: Mapped[float] = mapped_column(Float, default=15.0)
    max_weekly_hours: Mapped[int] = mapped_column(Integer, default=40)
    min_weekly_hours: Mapped[int] = mapped_column(Integer, default=0)
    max_consecutive_days: Mapped[int] = mapped_column(Integer, default=5)
    seniority: Mapped[int] = mapped_column(Integer, default=1)  # 1..5, tie-break preference
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    availability: Mapped[list[Availability]] = relationship(
        back_populates="employee", cascade="all, delete-orphan", lazy="selectin"
    )
    time_off: Mapped[list[TimeOff]] = relationship(
        back_populates="employee", cascade="all, delete-orphan", lazy="selectin"
    )


class Availability(Base):
    """Weekly recurring availability window, per weekday (0=Mon .. 6=Sun)."""

    __tablename__ = "availability"
    __table_args__ = (UniqueConstraint("employee_id", "weekday", name="uq_avail_emp_day"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), index=True
    )
    weekday: Mapped[int] = mapped_column(Integer)
    earliest_hour: Mapped[int] = mapped_column(Integer, default=0)
    latest_hour: Mapped[int] = mapped_column(Integer, default=24)
    available: Mapped[bool] = mapped_column(Boolean, default=True)

    employee: Mapped[Employee] = relationship(back_populates="availability")


class TimeOff(Base):
    __tablename__ = "time_off"
    __table_args__ = (UniqueConstraint("employee_id", "day", name="uq_timeoff_emp_day"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), index=True
    )
    day: Mapped[dt.date] = mapped_column(Date)
    reason: Mapped[str] = mapped_column(String(255), default="")

    employee: Mapped[Employee] = relationship(back_populates="time_off")


class ShiftTemplate(Base):
    __tablename__ = "shift_templates"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    role: Mapped[str] = mapped_column(String(32), index=True)
    start_hour: Mapped[int] = mapped_column(Integer)
    end_hour: Mapped[int] = mapped_column(Integer)
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    @property
    def hours(self) -> int:
        return self.end_hour - self.start_hour

    def covers(self, hour: int) -> bool:
        return self.start_hour <= hour < self.end_hour


# ---------------------------------------------------------------------------- demand
class SalesRecord(Base):
    __tablename__ = "sales_records"
    __table_args__ = (
        UniqueConstraint("day", "hour", name="uq_sales_day_hour"),
        Index("ix_sales_day", "day"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    day: Mapped[dt.date] = mapped_column(Date)
    hour: Mapped[int] = mapped_column(Integer)
    covers: Mapped[float] = mapped_column(Float, default=0.0)
    revenue: Mapped[float] = mapped_column(Float, default=0.0)
    is_holiday: Mapped[bool] = mapped_column(Boolean, default=False)
    weather_score: Mapped[float] = mapped_column(Float, default=0.5)  # 0 bad .. 1 good


# -------------------------------------------------------------------------- schedule
class Schedule(Base):
    __tablename__ = "schedules"

    id: Mapped[int] = mapped_column(primary_key=True)
    start_day: Mapped[dt.date] = mapped_column(Date, index=True)
    end_day: Mapped[dt.date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(16), default="draft")  # draft|published|archived
    solver: Mapped[str] = mapped_column(String(16), default="milp")
    labor_cost: Mapped[float] = mapped_column(Float, default=0.0)
    understaffed_hours: Mapped[float] = mapped_column(Float, default=0.0)
    overstaffed_hours: Mapped[float] = mapped_column(Float, default=0.0)
    forecast_model: Mapped[str] = mapped_column(String(32), default="hist_gbr")
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    created_by: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    assignments: Mapped[list[Assignment]] = relationship(
        back_populates="schedule", cascade="all, delete-orphan", lazy="selectin"
    )


class Assignment(Base):
    __tablename__ = "assignments"
    __table_args__ = (
        UniqueConstraint("schedule_id", "employee_id", "day", name="uq_assign_one_shift_per_day"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    schedule_id: Mapped[int] = mapped_column(
        ForeignKey("schedules.id", ondelete="CASCADE"), index=True
    )
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id", ondelete="CASCADE"))
    employee_name: Mapped[str] = mapped_column(String(120), default="")
    template_id: Mapped[int] = mapped_column(ForeignKey("shift_templates.id", ondelete="CASCADE"))
    role: Mapped[str] = mapped_column(String(32))
    day: Mapped[dt.date] = mapped_column(Date, index=True)
    start_hour: Mapped[int] = mapped_column(Integer)
    end_hour: Mapped[int] = mapped_column(Integer)
    hours: Mapped[float] = mapped_column(Float)
    cost: Mapped[float] = mapped_column(Float)

    schedule: Mapped[Schedule] = relationship(back_populates="assignments")
