"""Persisted readiness for the only supported account protocol."""
import uuid

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def account_protocol(session: AsyncSession, owner_id: uuid.UUID) -> str:
    result = await session.execute(
        text("SELECT protocol FROM account_sync_protocol WHERE owner_id = :owner_id"),
        {"owner_id": owner_id},
    )
    return result.scalar_one_or_none() or "unsupported"


async def require_v2_protocol(session: AsyncSession, owner_id: uuid.UUID) -> None:
    if await account_protocol(session, owner_id) != "v2":
        raise HTTPException(status_code=409, detail="Account does not support protocol v2.")


async def require_v2_activation_ready(session: AsyncSession, owner_id: uuid.UUID) -> None:
    await require_v2_protocol(session, owner_id)
