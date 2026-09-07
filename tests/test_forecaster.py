"""Demand model behaviour and leakage guards."""

from __future__ import annotations

import datetime as dt

import numpy as np
import pytest

from app.ml.features import FEATURE_NAMES, Slot, build_row, build_training_matrix
from app.ml.forecaster import DemandForecaster
from app.ml.registry import MODEL_REGISTRY, build_model

SERVICE = range(10, 23)


def test_registry_builds_every_model():
    for name in MODEL_REGISTRY:
        assert hasattr(build_model(name), "fit")
    with pytest.raises(KeyError):
        build_model("does_not_exist")


def test_feature_row_width_matches_names():
    row = build_row(Slot(dt.date(2026, 3, 2), 12), {}, default=10.0)
    assert len(row) == len(FEATURE_NAMES)


def test_training_matrix_skips_lag_warmup(sales_history):
    x, y, slots = build_training_matrix(sales_history, SERVICE)
    first_day = min(r["day"] for r in sales_history)
    assert x.shape[0] == y.shape[0] == len(slots)
    assert min(s.day for s in slots) >= first_day + dt.timedelta(days=14)
    assert not np.isnan(x).any()


def test_short_history_falls_back_to_seasonal_naive():
    records = [
        {"day": dt.date(2026, 3, 2) + dt.timedelta(days=d), "hour": h, "covers": 20.0}
        for d in range(5)
        for h in SERVICE
    ]
    forecaster = DemandForecaster("hist_gbr", SERVICE).fit(records)
    assert forecaster.fallback is True
    result = forecaster.predict_range(dt.date(2026, 3, 9), 2)
    assert result.model == "seasonal_naive"
    assert len(result.points) == 2 * len(SERVICE)


def test_trained_model_tracks_weekly_pattern(sales_history):
    forecaster = DemandForecaster("hist_gbr", SERVICE).fit(sales_history, test_days=14)
    assert forecaster.fallback is False
    assert forecaster.metrics["mae"] < 8.0
    assert forecaster.metrics["r2"] > 0.7

    start = max(r["day"] for r in sales_history) + dt.timedelta(days=1)
    result = forecaster.predict_range(start, 7)
    assert len(result.points) == 7 * len(SERVICE)
    assert all(covers >= 0 for _d, _h, covers in result.points)

    by_hour: dict[int, list[float]] = {}
    for _day, hour, covers in result.points:
        by_hour.setdefault(hour, []).append(covers)
    lunch = float(np.mean(by_hour[12]))
    lull = float(np.mean(by_hour[16]))
    assert lunch > lull, "model lost the lunch peak"


def test_predictions_are_deterministic(sales_history):
    start = max(r["day"] for r in sales_history) + dt.timedelta(days=1)
    a = DemandForecaster("hist_gbr", SERVICE).fit(sales_history).predict_range(start, 3)
    b = DemandForecaster("hist_gbr", SERVICE).fit(sales_history).predict_range(start, 3)
    assert a.points == b.points


def test_model_artifact_roundtrip(sales_history, tmp_path):
    forecaster = DemandForecaster("ridge", SERVICE).fit(sales_history)
    path = forecaster.save(tmp_path)
    assert path.exists() and path.stat().st_size > 0
