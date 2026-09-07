"""JWT issue/verify. Access + rotating refresh with family revocation."""

from __future__ import annotations

import datetime as dt
import secrets
import uuid

import jwt

from app.config import get_settings

ACCESS = "access"
REFRESH = "refresh"


class TokenError(Exception):
    """Raised on malformed, expired, or wrong-kind token."""


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def new_family() -> str:
    return secrets.token_urlsafe(24)


def _encode(payload: dict) -> str:
    settings = get_settings()
    return jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)


def make_access_token(*, subject: str, role: str) -> tuple[str, int]:
    settings = get_settings()
    ttl = dt.timedelta(minutes=settings.access_ttl_minutes)
    now = _now()
    payload = {
        "sub": subject,
        "role": role,
        "kind": ACCESS,
        "jti": uuid.uuid4().hex,
        "iat": int(now.timestamp()),
        "exp": int((now + ttl).timestamp()),
        "iss": "rwo",
    }
    return _encode(payload), int(ttl.total_seconds())


def make_refresh_token(*, subject: str, family: str) -> tuple[str, str, dt.datetime]:
    """Return (token, jti, expires_at)."""
    settings = get_settings()
    now = _now()
    expires_at = now + dt.timedelta(days=settings.refresh_ttl_days)
    jti = uuid.uuid4().hex
    payload = {
        "sub": subject,
        "kind": REFRESH,
        "family": family,
        "jti": jti,
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
        "iss": "rwo",
    }
    return _encode(payload), jti, expires_at


def decode_token(token: str, *, expected_kind: str) -> dict:
    settings = get_settings()
    try:
        payload = jwt.decode(
            token,
            settings.secret_key,
            algorithms=[settings.jwt_algorithm],
            issuer="rwo",
            options={"require": ["exp", "iat", "sub", "jti"]},
        )
    except jwt.PyJWTError as exc:  # expired, bad signature, bad issuer
        raise TokenError(str(exc)) from exc
    if payload.get("kind") != expected_kind:
        raise TokenError("unexpected token kind")
    return payload
