"""Authentication endpoints.

Login is constant-response on failure (no user enumeration) and lockout-protected.
Refresh tokens rotate on every use; replay of a consumed token revokes the whole
token family, which kills a stolen-token session.
"""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_session
from app.deps import client_ip, get_current_user, require_admin
from app.models import RefreshToken, User
from app.schemas import LoginRequest, RefreshRequest, TokenPair, UserCreate, UserOut
from app.security import (
    hash_password,
    make_access_token,
    make_refresh_token,
    needs_rehash,
    new_family,
    verify_password,
)
from app.security.passwords import hash_password as _hash
from app.security.tokens import REFRESH, TokenError, decode_token
from app.services import audit

router = APIRouter(prefix="/auth", tags=["auth"])

INVALID_CREDENTIALS = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid credentials"
)
# constant-time-ish decoy so a missing user costs the same as a wrong password
_DECOY_HASH = _hash("decoy-password-not-in-use-000")


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _aware(value: dt.datetime | None) -> dt.datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=dt.UTC)


async def _issue_pair(session: AsyncSession, user: User, family: str | None = None) -> TokenPair:
    family = family or new_family()
    access, expires_in = make_access_token(subject=str(user.id), role=user.role)
    refresh, jti, expires_at = make_refresh_token(subject=str(user.id), family=family)
    session.add(RefreshToken(jti=jti, family=family, user_id=user.id, expires_at=expires_at))
    await session.commit()
    return TokenPair(access_token=access, refresh_token=refresh, expires_in=expires_in)


@router.post("/login", response_model=TokenPair)
async def login(
    payload: LoginRequest, request: Request, session: AsyncSession = Depends(get_session)
) -> TokenPair:
    settings = get_settings()
    user = await session.scalar(select(User).where(User.email == payload.email.lower()))

    if user is None:
        verify_password(_DECOY_HASH, payload.password)
        raise INVALID_CREDENTIALS

    locked_until = _aware(user.locked_until)
    if locked_until and locked_until > _utcnow():
        raise HTTPException(status_code=status.HTTP_423_LOCKED, detail="account temporarily locked")

    if not user.is_active or not verify_password(user.password_hash, payload.password):
        user.failed_logins += 1
        if user.failed_logins >= settings.max_login_failures:
            user.locked_until = _utcnow() + dt.timedelta(seconds=settings.lockout_seconds)
            user.failed_logins = 0
        await session.commit()
        await audit.record(
            session, actor=payload.email, action="login_failed", ip=client_ip(request)
        )
        raise INVALID_CREDENTIALS

    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(payload.password)
    user.failed_logins = 0
    user.locked_until = None
    await session.commit()

    await audit.record(session, actor=user.email, action="login", ip=client_ip(request))
    return await _issue_pair(session, user)


@router.post("/refresh", response_model=TokenPair)
async def refresh_tokens(
    payload: RefreshRequest, request: Request, session: AsyncSession = Depends(get_session)
) -> TokenPair:
    try:
        claims = decode_token(payload.refresh_token, expected_kind=REFRESH)
    except TokenError as exc:
        raise INVALID_CREDENTIALS from exc

    stored = await session.scalar(select(RefreshToken).where(RefreshToken.jti == claims["jti"]))
    if stored is None or stored.revoked:
        # replay or unknown token: burn the family
        family = claims.get("family")
        if family:
            await session.execute(
                update(RefreshToken).where(RefreshToken.family == family).values(revoked=True)
            )
            await session.commit()
        await audit.record(
            session, actor=claims.get("sub", "?"), action="refresh_replay", ip=client_ip(request)
        )
        raise INVALID_CREDENTIALS

    if stored.used:
        await session.execute(
            update(RefreshToken).where(RefreshToken.family == stored.family).values(revoked=True)
        )
        await session.commit()
        await audit.record(
            session, actor=str(stored.user_id), action="refresh_reuse", ip=client_ip(request)
        )
        raise INVALID_CREDENTIALS

    expires_at = _aware(stored.expires_at)
    if expires_at is None or expires_at <= _utcnow():
        raise INVALID_CREDENTIALS

    user = await session.scalar(select(User).where(User.id == stored.user_id))
    if user is None or not user.is_active:
        raise INVALID_CREDENTIALS

    stored.used = True
    stored.revoked = True
    await session.commit()
    return await _issue_pair(session, user, family=stored.family)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    payload: RefreshRequest, request: Request, session: AsyncSession = Depends(get_session)
) -> None:
    try:
        claims = decode_token(payload.refresh_token, expected_kind=REFRESH)
    except TokenError:
        return None  # idempotent
    await session.execute(
        update(RefreshToken)
        .where(RefreshToken.family == claims.get("family", ""))
        .values(revoked=True)
    )
    await session.commit()
    await audit.record(
        session, actor=claims.get("sub", "?"), action="logout", ip=client_ip(request)
    )
    return None


@router.get("/me", response_model=UserOut)
async def me(user: User = Depends(get_current_user)) -> User:
    return user


@router.post("/users", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def create_user(
    payload: UserCreate,
    request: Request,
    actor: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> User:
    email = payload.email.lower()
    if await session.scalar(select(User).where(User.email == email)):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="email already registered")
    user = User(email=email, password_hash=hash_password(payload.password), role=payload.role)
    session.add(user)
    await session.commit()
    await session.refresh(user)
    await audit.record(
        session,
        actor=actor.email,
        action="user_created",
        detail=f"{email} role={payload.role}",
        ip=client_ip(request),
    )
    return user
