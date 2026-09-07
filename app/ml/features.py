"""Feature engineering for hourly cover-count demand.

No target leakage: every feature at timestamp t uses only data strictly before t
(lags >= 168h) or exogenous calendar/weather facts known at planning time.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import numpy as np

FEATURE_NAMES = [
    "hour",
    "weekday",
    "is_weekend",
    "is_holiday",
    "weather_score",
    "hour_sin",
    "hour_cos",
    "dow_sin",
    "dow_cos",
    "lag_168",
    "lag_336",
    "roll4_same_slot",
    "roll4_day_total",
]

WEEK = 168  # hours


@dataclass(frozen=True, order=True)
class Slot:
    day: dt.date
    hour: int

    @property
    def key(self) -> tuple[dt.date, int]:
        return (self.day, self.hour)


def _shift(day: dt.date, hour: int, hours_back: int) -> tuple[dt.date, int]:
    stamp = dt.datetime.combine(day, dt.time(hour)) - dt.timedelta(hours=hours_back)
    return stamp.date(), stamp.hour


def build_row(
    slot: Slot,
    history: dict[tuple[dt.date, int], float],
    *,
    is_holiday: bool = False,
    weather_score: float = 0.5,
    default: float = 0.0,
) -> list[float]:
    """One feature vector. `history` maps (day, hour) -> covers, actuals or predictions."""
    day, hour = slot.day, slot.hour
    weekday = day.weekday()
    lag1 = history.get(_shift(day, hour, WEEK), default)
    lag2 = history.get(_shift(day, hour, 2 * WEEK), default)

    same_slot = [
        history[_shift(day, hour, WEEK * k)]
        for k in range(1, 5)
        if _shift(day, hour, WEEK * k) in history
    ]
    roll_slot = float(np.mean(same_slot)) if same_slot else default

    day_totals: list[float] = []
    for k in range(1, 5):
        past_day = day - dt.timedelta(days=7 * k)
        vals = [v for (d, _h), v in history.items() if d == past_day]
        if vals:
            day_totals.append(float(np.sum(vals)))
    roll_day = float(np.mean(day_totals)) if day_totals else default * 12

    return [
        float(hour),
        float(weekday),
        1.0 if weekday >= 5 else 0.0,
        1.0 if is_holiday else 0.0,
        float(weather_score),
        float(np.sin(2 * np.pi * hour / 24)),
        float(np.cos(2 * np.pi * hour / 24)),
        float(np.sin(2 * np.pi * weekday / 7)),
        float(np.cos(2 * np.pi * weekday / 7)),
        float(lag1),
        float(lag2),
        roll_slot,
        roll_day,
    ]


def build_training_matrix(
    records: list[dict],
    service_hours: range,
) -> tuple[np.ndarray, np.ndarray, list[Slot]]:
    """records: dicts with day, hour, covers, is_holiday, weather_score. Sorted internally."""
    history = {(r["day"], r["hour"]): float(r["covers"]) for r in records}
    rows: list[list[float]] = []
    targets: list[float] = []
    slots: list[Slot] = []
    ordered = sorted(records, key=lambda r: (r["day"], r["hour"]))
    if not ordered:
        return np.empty((0, len(FEATURE_NAMES))), np.empty((0,)), []
    first_day = ordered[0]["day"]
    warmup_cutoff = first_day + dt.timedelta(days=14)  # need 2 weeks of lags
    mean_covers = float(np.mean([r["covers"] for r in ordered])) if ordered else 0.0

    for r in ordered:
        if r["day"] < warmup_cutoff or r["hour"] not in service_hours:
            continue
        slot = Slot(r["day"], r["hour"])
        # exclude the target's own value from lookups: lags are >= 168h back, safe.
        rows.append(
            build_row(
                slot,
                history,
                is_holiday=bool(r.get("is_holiday", False)),
                weather_score=float(r.get("weather_score", 0.5)),
                default=mean_covers,
            )
        )
        targets.append(float(r["covers"]))
        slots.append(slot)
    return np.asarray(rows, dtype=float), np.asarray(targets, dtype=float), slots
