"""Sales ingestion, history and demand forecasting."""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_session
from app.deps import client_ip, require_manager, require_viewer
from app.ml.registry import MODEL_REGISTRY
from app.models import SalesRecord, User
from app.schemas import ForecastOut, ForecastPoint, SalesBulkIn, SalesOut, TrainRequest
from app.services import audit, planner

router = APIRouter(tags=["demand"])


def _validate_model(name: str) -> str:
    if name not in MODEL_REGISTRY:
        raise HTTPException(
            status_code=422, detail=f"unknown model; available {sorted(MODEL_REGISTRY)}"
        )
    return name


@router.post("/demand/ingest", status_code=202)
async def ingest_sales(
    payload: SalesBulkIn,
    request: Request,
    actor: User = Depends(require_manager),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Upsert hourly sales. Idempotent on (day, hour)."""
    rows = [r.model_dump() for r in payload.records]
    stmt = sqlite_insert(SalesRecord).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=[SalesRecord.day, SalesRecord.hour],
        set_={
            "covers": stmt.excluded.covers,
            "revenue": stmt.excluded.revenue,
            "is_holiday": stmt.excluded.is_holiday,
            "weather_score": stmt.excluded.weather_score,
        },
    )
    await session.execute(stmt)
    await session.commit()
    await audit.record(
        session,
        actor=actor.email,
        action="demand_ingest",
        detail=f"rows={len(rows)}",
        ip=client_ip(request),
    )
    return {"ingested": len(rows)}


@router.get("/demand/history", response_model=list[SalesOut])
async def sales_history(
    start: dt.date | None = None,
    end: dt.date | None = None,
    limit: int = Query(default=2000, ge=1, le=20000),
    _user: User = Depends(require_viewer),
    session: AsyncSession = Depends(get_session),
) -> list[SalesRecord]:
    stmt = select(SalesRecord).order_by(SalesRecord.day.desc(), SalesRecord.hour).limit(limit)
    if start:
        stmt = stmt.where(SalesRecord.day >= start)
    if end:
        stmt = stmt.where(SalesRecord.day <= end)
    return list((await session.execute(stmt)).scalars().all())


@router.get("/models")
async def list_models(_user: User = Depends(require_viewer)) -> dict:
    return {"models": sorted(MODEL_REGISTRY), "default": "hist_gbr"}


@router.post("/forecast/train")
async def train(
    payload: TrainRequest,
    _actor: User = Depends(require_manager),
    session: AsyncSession = Depends(get_session),
) -> dict:
    settings = get_settings()
    model = _validate_model(payload.model)
    forecaster = await planner.train_forecaster(session, model, payload.test_days)
    artifact = None
    if not forecaster.fallback:
        artifact = str(forecaster.save(settings.artifacts_dir))
    return {
        "model": forecaster.model_name,
        "fallback": forecaster.fallback,
        "trained_rows": forecaster.trained_rows,
        "metrics": forecaster.metrics,
        "artifact": artifact,
    }


@router.get("/forecast", response_model=ForecastOut)
async def forecast(
    start_day: dt.date,
    days: int = Query(default=7, ge=1, le=21),
    model: str = "hist_gbr",
    _user: User = Depends(require_viewer),
    session: AsyncSession = Depends(get_session),
) -> ForecastOut:
    settings = get_settings()
    if days > settings.horizon_days_max:
        raise HTTPException(
            status_code=422, detail=f"horizon exceeds {settings.horizon_days_max} days"
        )
    result = await planner.forecast_range(session, _validate_model(model), start_day, days)
    return ForecastOut(
        model=result.model,
        trained_rows=result.trained_rows,
        metrics=result.metrics,
        points=[ForecastPoint(day=d, hour=h, covers=v) for d, h, v in result.points],
    )
