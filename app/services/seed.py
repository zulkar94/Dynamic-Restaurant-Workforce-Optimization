"""Idempotent bootstrap: admin user and default shift templates."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import ShiftTemplate, User
from app.security import hash_password

DEFAULT_TEMPLATES = [
    ("Opening Server", "server", 10, 18),
    ("Closing Server", "server", 15, 23),
    ("Mid Server", "server", 12, 20),
    ("Opening Cook", "cook", 10, 18),
    ("Closing Cook", "cook", 15, 23),
    ("Host Day", "host", 10, 17),
    ("Host Evening", "host", 16, 23),
    ("Busser Evening", "busser", 17, 23),
    ("Bartender Evening", "bartender", 16, 23),
]


async def ensure_admin(session: AsyncSession) -> None:
    settings = get_settings()
    exists = await session.scalar(select(func.count()).select_from(User))
    if exists:
        return
    session.add(
        User(
            email=settings.admin_email.lower(),
            password_hash=hash_password(settings.admin_password),
            role="admin",
        )
    )
    await session.commit()


async def ensure_templates(session: AsyncSession) -> None:
    exists = await session.scalar(select(func.count()).select_from(ShiftTemplate))
    if exists:
        return
    session.add_all(
        ShiftTemplate(name=name, role=role, start_hour=start, end_hour=end)
        for name, role, start, end in DEFAULT_TEMPLATES
    )
    await session.commit()


async def bootstrap(session: AsyncSession) -> None:
    await ensure_admin(session)
    await ensure_templates(session)
