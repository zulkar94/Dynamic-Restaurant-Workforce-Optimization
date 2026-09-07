#!/usr/bin/env python3
"""Generate a realistic demo dataset: staff roster + hourly sales history.

Usage
    python scripts/seed_demo.py --days 180 --employees 28
    python scripts/seed_demo.py --export data/sales.json   # file only, no DB write

Demand model: weekday base x lunch/dinner peaks x weekend uplift x holiday spike
x weather modifier x growth trend x Poisson-like noise. Deterministic under --seed.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import math
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings  # noqa: E402
from app.db import dispose_db, get_sessionmaker, init_db  # noqa: E402
from app.models import Availability, Employee, SalesRecord  # noqa: E402
from app.services.seed import bootstrap  # noqa: E402

ROLE_MIX = [
    ("server", 10, (13.0, 19.0)),
    ("cook", 7, (16.0, 24.0)),
    ("host", 3, (12.5, 15.0)),
    ("busser", 4, (12.0, 14.0)),
    ("bartender", 3, (15.0, 21.0)),
]

WEEKDAY_BASE = {0: 0.78, 1: 0.82, 2: 0.9, 3: 1.0, 4: 1.35, 5: 1.5, 6: 1.1}
HOUR_SHAPE = {
    10: 0.15,
    11: 0.35,
    12: 0.95,
    13: 1.0,
    14: 0.55,
    15: 0.3,
    16: 0.35,
    17: 0.6,
    18: 0.9,
    19: 1.15,
    20: 1.2,
    21: 0.85,
    22: 0.45,
}


def generate_sales(
    start: dt.date, days: int, peak_covers: float, rng: random.Random, service_hours: range
) -> list[dict]:
    holidays = {start + dt.timedelta(days=d) for d in range(days) if d % 47 == 0}
    records: list[dict] = []
    for offset in range(days):
        day = start + dt.timedelta(days=offset)
        trend = 1.0 + 0.0008 * offset
        weather = round(min(1.0, max(0.0, rng.gauss(0.6, 0.22))), 2)
        holiday = day in holidays
        day_factor = WEEKDAY_BASE[day.weekday()] * trend
        day_factor *= 1.35 if holiday else 1.0
        day_factor *= 0.85 + 0.3 * weather
        for hour in service_hours:
            shape = HOUR_SHAPE.get(hour, 0.3)
            mean = peak_covers * day_factor * shape
            noise = rng.gauss(0, math.sqrt(max(mean, 1.0)) * 0.6)
            covers = max(0.0, round(mean + noise, 1))
            records.append(
                {
                    "day": day.isoformat(),
                    "hour": hour,
                    "covers": covers,
                    "revenue": round(covers * rng.uniform(26.0, 41.0), 2),
                    "is_holiday": holiday,
                    "weather_score": weather,
                }
            )
    return records


def generate_employees(count: int, rng: random.Random) -> list[dict]:
    people: list[dict] = []
    index = 1
    pool = [(role, share) for role, share, _rate in ROLE_MIX]
    total_share = sum(share for _r, share in pool)
    for role, share, (low, high) in ROLE_MIX:
        n = max(1, round(count * share / total_share))
        for _ in range(n):
            roles = [role]
            if rng.random() < 0.25:
                extra = rng.choice([r for r, _s, _x in ROLE_MIX if r != role])
                roles.append(extra)
            full_time = rng.random() < 0.6
            availability = []
            for weekday in range(7):
                if rng.random() < 0.12:  # recurring day off
                    continue
                if rng.random() < 0.3:
                    availability.append(
                        {"weekday": weekday, "earliest_hour": 15, "latest_hour": 24}
                    )
                else:
                    availability.append({"weekday": weekday, "earliest_hour": 8, "latest_hour": 24})
            people.append(
                {
                    "name": f"{role.capitalize()} {index:02d}",
                    "roles": sorted(set(roles)),
                    "hourly_rate": round(rng.uniform(low, high), 2),
                    "max_weekly_hours": 40 if full_time else 24,
                    "min_weekly_hours": 24 if full_time else 8,
                    "max_consecutive_days": 5 if full_time else 4,
                    "seniority": rng.randint(1, 5),
                    "availability": availability,
                }
            )
            index += 1
    return people


async def load_into_db(sales: list[dict], staff: list[dict], reset: bool) -> None:
    await init_db()
    maker = get_sessionmaker()
    async with maker() as session:
        await bootstrap(session)
        if reset:
            from sqlalchemy import delete

            await session.execute(delete(SalesRecord))
            await session.execute(delete(Employee))
            await session.commit()

        session.add_all(
            SalesRecord(
                day=dt.date.fromisoformat(r["day"]),
                hour=r["hour"],
                covers=r["covers"],
                revenue=r["revenue"],
                is_holiday=r["is_holiday"],
                weather_score=r["weather_score"],
            )
            for r in sales
        )
        for person in staff:
            employee = Employee(
                name=person["name"],
                roles=person["roles"],
                hourly_rate=person["hourly_rate"],
                max_weekly_hours=person["max_weekly_hours"],
                min_weekly_hours=person["min_weekly_hours"],
                max_consecutive_days=person["max_consecutive_days"],
                seniority=person["seniority"],
            )
            employee.availability = [Availability(**a) for a in person["availability"]]
            session.add(employee)
        await session.commit()
    await dispose_db()


def main() -> int:
    parser = argparse.ArgumentParser(description="seed demo data")
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument("--employees", type=int, default=28)
    parser.add_argument("--peak-covers", type=float, default=70.0)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--export", type=str, default="")
    parser.add_argument("--reset", action="store_true", help="wipe employees and sales first")
    args = parser.parse_args()

    settings = get_settings()
    rng = random.Random(args.seed)
    end = dt.date.today()
    start = end - dt.timedelta(days=args.days)
    sales = generate_sales(start, args.days, args.peak_covers, rng, settings.service_hours)
    staff = generate_employees(args.employees, rng)

    if args.export:
        target = Path(args.export)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({"sales": sales, "employees": staff}, indent=2))
        print(f"exported {len(sales)} sales rows, {len(staff)} employees -> {target}")
        return 0

    asyncio.run(load_into_db(sales, staff, args.reset))
    print(
        f"seeded {len(sales)} sales rows ({start} .. {end}), {len(staff)} employees "
        f"into {settings.database_url}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
