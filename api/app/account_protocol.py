"""Persisted readiness for the only live account writer, sync v2.

Absent markers identify archived accounts that still need offline conversion.
Fresh accounts get a marker in the PostgreSQL insertion transaction.
"""
import uuid

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def account_protocol(session: AsyncSession, owner_id: uuid.UUID) -> str:
    result = await session.execute(
        text(
            "SELECT protocol FROM account_sync_protocol "
            "WHERE owner_id = :owner_id"
        ),
        {"owner_id": owner_id},
    )
    # Keep the historical mode label for the offline importer and old clients.
    return result.scalar_one_or_none() or "v1"


async def require_v2_protocol(
    session: AsyncSession, owner_id: uuid.UUID,
) -> None:
    if await account_protocol(session, owner_id) != "v2":
        raise HTTPException(
            status_code=409,
            detail="Account has not completed operation protocol cutover.",
        )

async def legacy_cutover_receipt(
    session: AsyncSession, owner_id: uuid.UUID,
) -> dict | None:
    result = await session.execute(
        text("""SELECT legacy_cursor, operation_cursor, converted_documents
                FROM legacy_account_cutovers WHERE owner_id = :owner_id"""),
        {"owner_id": owner_id},
    )
    row = result.mappings().one_or_none()
    return dict(row) if row is not None else None


async def require_v2_activation_ready(
    session: AsyncSession, owner_id: uuid.UUID,
) -> dict | None:
    """Block v2 startup unless historical state has a durable receipt."""
    await require_v2_protocol(session, owner_id)
    receipt = await legacy_cutover_receipt(session, owner_id)
    if receipt is not None:
        return receipt
    result = await session.execute(
        text("""
            SELECT (
                EXISTS (SELECT 1 FROM synced_notebooks
                        WHERE owner_id = :owner_id)
                OR EXISTS (SELECT 1 FROM change_events
                           WHERE owner_id = :owner_id)
                OR EXISTS (SELECT 1 FROM idempotency_records
                           WHERE owner_id = :owner_id)
                OR EXISTS (SELECT 1 FROM device_backups
                           WHERE owner_id = :owner_id AND format_version <> 3)
                OR EXISTS (SELECT 1 FROM account_settings
                           WHERE owner_id = :owner_id
                             AND category = 'libraryState')
                OR EXISTS (SELECT 1 FROM study_shares
                           WHERE owner_id = :owner_id
                             AND COALESCE(edit_clock ->> '_protocol', '')
                                 <> 'v2')
            )
        """),
        {"owner_id": owner_id},
    )
    if result.scalar_one():
        raise HTTPException(
            status_code=409,
            detail="Legacy account data has not been converted to v2.",
        )
