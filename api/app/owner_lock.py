import hashlib
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def lock_owner_events(session: AsyncSession, owner_id: uuid.UUID) -> None:
    # PostgreSQL sequences are assigned before commit. Serializing writes for
    # one account prevents a later event from making an uncommitted one invisible
    # to a client that advances its change cursor.
    key = int.from_bytes(hashlib.sha256(owner_id.bytes).digest()[:8], "big", signed=True)
    await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})
