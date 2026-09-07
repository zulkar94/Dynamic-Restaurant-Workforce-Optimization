"""Shared FastAPI dependencies: authentication, authorisation, client context."""

from __future__ import annotations

from collections.abc import Callable

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import User
from app.security.tokens import ACCESS, TokenError, decode_token

bearer_scheme = HTTPBearer(auto_error=False)

ROLE_RANK = {"viewer": 0, "manager": 1, "admin": 2}

UNAUTHORIZED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="invalid or expired credentials",
    headers={"WWW-Authenticate": "Bearer"},
)


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64]
    return request.client.host if request.client else "unknown"


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    session: AsyncSession = Depends(get_session),
) -> User:
    if credentials is None or not credentials.credentials:
        raise UNAUTHORIZED
    try:
        payload = decode_token(credentials.credentials, expected_kind=ACCESS)
    except TokenError as exc:
        raise UNAUTHORIZED from exc

    try:
        user_id = int(payload["sub"])
    except (KeyError, ValueError) as exc:
        raise UNAUTHORIZED from exc

    user = await session.scalar(select(User).where(User.id == user_id))
    if user is None or not user.is_active:
        raise UNAUTHORIZED
    return user


def require_role(minimum: str) -> Callable:
    """Dependency factory enforcing a minimum role rank."""
    threshold = ROLE_RANK[minimum]

    async def _guard(user: User = Depends(get_current_user)) -> User:
        if ROLE_RANK.get(user.role, -1) < threshold:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail=f"requires role >= {minimum}"
            )
        return user

    return _guard


require_viewer = require_role("viewer")
require_manager = require_role("manager")
require_admin = require_role("admin")
