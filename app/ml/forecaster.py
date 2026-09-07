"""Hourly demand forecaster.

Falls back to seasonal-naive (same weekday/hour mean over trailing weeks) when
history is too short to fit a supervised model. Prediction beyond one week is
recursive: predicted values feed later lags.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field
from pathlib import Path

import joblib
import numpy as np

from app.ml.features import FEATURE_NAMES, Slot, build_row, build_training_matrix
from app.ml.registry import build_model

MIN_TRAIN_ROWS = 120


@dataclass
class ForecastResult:
    model: str
    trained_rows: int
    metrics: dict[str, float]
    points: list[tuple[dt.date, int, float]] = field(default_factory=list)

    def as_map(self) -> dict[tuple[dt.date, int], float]:
        return {(d, h): v for d, h, v in self.points}


def _metrics(y_true: np.ndarray, y_pred: np.ndarray) -> dict[str, float]:
    if y_true.size == 0:
        return {"mae": 0.0, "rmse": 0.0, "mape": 0.0, "r2": 0.0}
    err = y_pred - y_true
    mae = float(np.mean(np.abs(err)))
    rmse = float(math.sqrt(float(np.mean(err**2))))
    denom = np.where(np.abs(y_true) < 1e-6, np.nan, np.abs(y_true))
    mape = float(np.nanmean(np.abs(err) / denom) * 100.0) if np.any(~np.isnan(denom)) else 0.0
    var = float(np.var(y_true))
    r2 = float(1.0 - (np.mean(err**2) / var)) if var > 1e-9 else 0.0
    return {
        "mae": round(mae, 3),
        "rmse": round(rmse, 3),
        "mape": round(mape, 2),
        "r2": round(r2, 3),
    }


class DemandForecaster:
    def __init__(self, model_name: str = "hist_gbr", service_hours: range = range(10, 23)) -> None:
        self.model_name = model_name
        self.service_hours = service_hours
        self.model = None
        self.fallback = False
        self.metrics: dict[str, float] = {}
        self.trained_rows = 0
        self.history: dict[tuple[dt.date, int], float] = {}
        self.mean_covers = 0.0

    # ----------------------------------------------------------------- training
    def fit(self, records: list[dict], test_days: int = 14) -> DemandForecaster:
        self.history = {(r["day"], r["hour"]): float(r["covers"]) for r in records}
        self.mean_covers = float(np.mean([r["covers"] for r in records])) if records else 0.0
        x, y, slots = build_training_matrix(records, self.service_hours)
        self.trained_rows = int(x.shape[0])

        if self.trained_rows < MIN_TRAIN_ROWS:
            self.fallback = True
            self.model = None
            self.metrics = {"mae": 0.0, "rmse": 0.0, "mape": 0.0, "r2": 0.0, "fallback": 1.0}
            return self

        cutoff = max(s.day for s in slots) - dt.timedelta(days=test_days)
        train_mask = np.array([s.day <= cutoff for s in slots])
        if train_mask.sum() < MIN_TRAIN_ROWS // 2 or (~train_mask).sum() == 0:
            train_mask = np.ones(len(slots), dtype=bool)

        model = build_model(self.model_name)
        model.fit(x[train_mask], y[train_mask])
        holdout = ~train_mask
        if holdout.sum() > 0:
            self.metrics = _metrics(y[holdout], np.asarray(model.predict(x[holdout])))
        else:
            self.metrics = _metrics(y[train_mask], np.asarray(model.predict(x[train_mask])))

        # refit on full history for deployment
        model_full = build_model(self.model_name)
        model_full.fit(x, y)
        self.model = model_full
        self.fallback = False
        return self

    # --------------------------------------------------------------- prediction
    def _seasonal_naive(self, slot: Slot) -> float:
        vals = []
        for k in range(1, 5):
            key = (slot.day - dt.timedelta(days=7 * k), slot.hour)
            if key in self.history:
                vals.append(self.history[key])
        if vals:
            return float(np.mean(vals))
        same = [v for (d, h), v in self.history.items() if h == slot.hour]
        return float(np.mean(same)) if same else self.mean_covers

    def predict_range(
        self,
        start_day: dt.date,
        days: int,
        *,
        holidays: set[dt.date] | None = None,
        weather: dict[dt.date, float] | None = None,
    ) -> ForecastResult:
        holidays = holidays or set()
        weather = weather or {}
        working = dict(self.history)
        points: list[tuple[dt.date, int, float]] = []

        for offset in range(days):
            day = start_day + dt.timedelta(days=offset)
            for hour in self.service_hours:
                slot = Slot(day, hour)
                if self.fallback or self.model is None:
                    value = self._seasonal_naive(slot)
                else:
                    row = build_row(
                        slot,
                        working,
                        is_holiday=day in holidays,
                        weather_score=weather.get(day, 0.5),
                        default=self.mean_covers,
                    )
                    value = float(self.model.predict(np.asarray([row], dtype=float))[0])
                value = max(0.0, value)
                working[(day, hour)] = value
                points.append((day, hour, round(value, 2)))

        return ForecastResult(
            model="seasonal_naive" if self.fallback else self.model_name,
            trained_rows=self.trained_rows,
            metrics=self.metrics,
            points=points,
        )

    # ------------------------------------------------------------- persistence
    def save(self, directory: str | Path) -> Path:
        path = Path(directory)
        path.mkdir(parents=True, exist_ok=True)
        target = path / f"forecaster_{self.model_name}.joblib"
        joblib.dump(
            {
                "model": self.model,
                "model_name": self.model_name,
                "metrics": self.metrics,
                "trained_rows": self.trained_rows,
                "features": FEATURE_NAMES,
            },
            target,
        )
        return target
