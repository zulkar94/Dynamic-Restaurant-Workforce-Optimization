"""Append-only audit trail. Never persist credentials or tokens."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditLog

REDACTED = "[redacted]"
_SENSITIVE = ("password", "token", "secret", "authorization")


def _scrub(detail: str) -> str:
    lowered = detail.lower()
    if any(word in lowered for word in _SENSITIVE):
        return REDACTED
    return detail[:1024]


async def record(
    session: AsyncSession, *, actor: str, action: str, detail: str = "", ip: str = ""
) -> None:
    session.add(AuditLog(actor=actor[:255], action=action[:64], detail=_scrub(detail), ip=ip[:64]))
    await session.commit()
