"""Pluggable model registry. Add a model by editing this dict only."""

from __future__ import annotations

from collections.abc import Callable

from sklearn.ensemble import HistGradientBoostingRegressor, RandomForestRegressor
from sklearn.linear_model import Ridge
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler


def _ridge() -> Pipeline:
    return Pipeline([("scale", StandardScaler()), ("model", Ridge(alpha=1.0, random_state=None))])


def _random_forest() -> RandomForestRegressor:
    return RandomForestRegressor(n_estimators=300, min_samples_leaf=2, n_jobs=-1, random_state=42)


def _hist_gbr() -> HistGradientBoostingRegressor:
    return HistGradientBoostingRegressor(
        max_iter=400, learning_rate=0.06, max_depth=6, l2_regularization=1.0, random_state=42
    )


MODEL_REGISTRY: dict[str, Callable[[], object]] = {
    "ridge": _ridge,
    "random_forest": _random_forest,
    "hist_gbr": _hist_gbr,
}


def build_model(name: str):
    if name not in MODEL_REGISTRY:
        raise KeyError(f"unknown model '{name}'; available {sorted(MODEL_REGISTRY)}")
    return MODEL_REGISTRY[name]()
