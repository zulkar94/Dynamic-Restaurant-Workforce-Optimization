from fastapi import APIRouter

from app.api import auth, demand, health, schedule, workforce

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(auth.router)
api_router.include_router(workforce.router)
api_router.include_router(demand.router)
api_router.include_router(schedule.router)

__all__ = ["api_router", "health"]
