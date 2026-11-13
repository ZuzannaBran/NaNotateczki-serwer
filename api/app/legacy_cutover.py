"""Offline account cutover. Run as an administrative role during maintenance.

No v1 rows are modified. The receipt, operation feed and protocol fence are
published in one commit. Unreferenced, verified media files from a failed run
are safe to reuse on retry; the transaction never publishes partial receipts.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from .account_protocol import account_protocol, legacy_cutover_receipt
from .asset_store import asset_path, store_upload, valid_verified_file
from .legacy_operation_plan import (
    Asset, LegacyConversionError, collect_ids, library_plan, mapping, notebook_plan,
)
from .models import (
    AccountSettings, ChangeEvent, DocumentOperation, DocumentState,
    StudyShare, SyncedNotebook, User, VerifiedAsset,
)
from .operation_projection import apply_operation
from .operation_protocol import MAX_SAFE_INTEGER
from .routers.operations import (
    Principal, apply_one, ensure_projection_budget, require_operations_v2_write,
)
from .owner_lock import lock_owner_events
from .settings import get_settings


async def document_rows(session: AsyncSession, owner_id: uuid.UUID):
    # Don't buffer all account snapshots on a low-memory server.
    last = ""
    while True:
        row = await session.scalar(
            select(SyncedNotebook).where(
                SyncedNotebook.owner_id == owner_id, SyncedNotebook.uid > last,
            ).order_by(SyncedNotebook.uid).limit(1)
        )
        if row is None:
            break
        last = row.uid
        yield row


async def history_ids(session: AsyncSession, owner_id: uuid.UUID, uid: str):
    pages = set()
    objects = {}
    result = await session.stream_scalars(
        select(ChangeEvent.content).where(
            ChangeEvent.owner_id == owner_id,
            ChangeEvent.entity_uid == uid,
            ChangeEvent.content.is_not(None),
        ).order_by(ChangeEvent.id).execution_options(yield_per=1)
    )
    try:
        async for snapshot in result:
            old_pages, old_objects = collect_ids(mapping(snapshot, "legacy event"))
            pages.update(old_pages)
            objects.update(old_objects)
    finally:
        await result.close()
    return pages, objects


class AssetStream:
    def __init__(self, data: bytes):
        self.data = data

    async def stream(self):
        yield self.data


async def persist_asset(session: AsyncSession, owner_id: uuid.UUID, asset: Asset):
    settings = get_settings()
    digest = asset.digest
    known = await session.get(VerifiedAsset, (owner_id, digest))
    if known is not None and (
        known.size_bytes != len(asset.data) or known.media_type != asset.media_type
    ):
        raise LegacyConversionError("Asset receipt does not match migration source.")
    path = asset_path(settings.data_path, owner_id, digest)
    if not valid_verified_file(path, digest, len(asset.data)):
        await store_upload(
            AssetStream(asset.data), settings.data_path, owner_id,
            digest, settings.maximum_asset_bytes,
        )
    if known is None:
        session.add(VerifiedAsset(
            owner_id=owner_id, sha256=digest, size_bytes=len(asset.data),
            media_type=asset.media_type,
        ))
        await session.flush()


async def migrate_account(
    session: AsyncSession,
    owner_id: uuid.UUID,
    *,
    apply: bool = False,
    expected_cursor: int | None = None,
) -> dict:
    """Caller must own the transaction and commit only on success."""
    if apply:
        if expected_cursor is None:
            raise LegacyConversionError("The inspected v1 cursor is required.")
        require_operations_v2_write(owner_id)
    await lock_owner_events(session, owner_id)
    if await session.scalar(
        select(User.id).where(User.id == owner_id).with_for_update()
    ) is None:
        raise LegacyConversionError("Account does not exist.")
    receipt = await legacy_cutover_receipt(session, owner_id)
    protocol = await account_protocol(session, owner_id)
    if receipt is not None and protocol == "v2":
        if apply and receipt["legacy_cursor"] != expected_cursor:
            raise LegacyConversionError("Cutover receipt differs from inspected cursor.")
        return {"status": "already-activated", **receipt}
    if receipt is not None or protocol != "v1":
        raise LegacyConversionError("Account already has a different protocol activation.")
    for model in (DocumentOperation, DocumentState):
        if await session.scalar(select(model.owner_id).where(model.owner_id == owner_id).limit(1)):
            raise LegacyConversionError("Account already has independent v2 data.")
    active_share = await session.scalar(
        select(StudyShare.id).where(
            StudyShare.owner_id == owner_id,
            StudyShare.revoked_at.is_(None),
            StudyShare.expires_at > datetime.now(UTC),
        ).limit(1)
    )
    if active_share is not None:
        raise LegacyConversionError("Finish active Shares and flush guest outboxes first.")
    legacy_cursor = await session.scalar(
        select(func.coalesce(func.max(ChangeEvent.id), 0)).where(
            ChangeEvent.owner_id == owner_id
        )
    )
    if legacy_cursor > MAX_SAFE_INTEGER or (apply and legacy_cursor != expected_cursor):
        raise LegacyConversionError("Legacy cursor changed; inspect and flush devices again.")
    # No cursor for another transaction can become visible before these.
    if apply:
        await session.execute(text("SELECT pg_advisory_xact_lock(681246731)"))
    principal = Principal(owner_id, "owner", owner_id)
    total = 0
    operation_cursor = 0
    pages_seen: set[str] = set()
    objects_seen: set[str] = set()
    max_asset = get_settings().maximum_asset_bytes
    max_projection = get_settings().maximum_sync_bytes

    async def process(steps):
        nonlocal operation_cursor
        current = None
        tombstones = set()
        verified = set()
        iterator = iter(steps)
        step = next(iterator, None)
        while step is not None:
            following = next(iterator, None)
            if isinstance(step, Asset):
                if len(step.data) > max_asset:
                    raise LegacyConversionError("Legacy asset exceeds configured size limit.")
                if apply:
                    await persist_asset(session, owner_id, step)
                else:
                    verified.add(step.digest)
                step = following
                continue
            initial = None
            value = step.payload.get("value")
            if step.kind == "object.create" and value.get("objectType") == "image":
                if (following is None or isinstance(following, Asset) or
                        following.kind != "asset.attach" or
                        following.object_id != step.object_id or
                        following.payload.get("assetId") != value["assetId"]):
                    raise LegacyConversionError("Image creation lacks an atomic attachment.")
                initial = value["assetId"]
            if apply:
                receipt = await apply_one(
                    session, principal, step, converting_legacy=True,
                    initial_image_receipt=initial,
                )
                if receipt is None or receipt["status"] != "applied":
                    raise LegacyConversionError("Non-contiguous migration journal.")
                operation_cursor = receipt["cursor"]
                if operation_cursor > MAX_SAFE_INTEGER:
                    raise LegacyConversionError("Operation cursor exceeds client precision.")
            else:
                current, tombstone = apply_operation(
                    current, step, tombstones, verified_assets=frozenset(verified),
                    initial_image_receipt=initial,
                )
                if tombstone is not None:
                    tombstones.add(tombstone)
                ensure_projection_budget(current, max_projection)
            step = following

    async for row in document_rows(session, owner_id):
        snapshot = row.content
        if row.deleted_at is None:
            snapshot = mapping(snapshot, "legacy notebook")
            pages, objects = collect_ids(snapshot)
            if pages_seen.intersection(pages) or objects_seen.intersection(objects):
                raise LegacyConversionError("Legacy IDs collide in the account projection.")
            pages_seen.update(pages)
            objects_seen.update(objects)
            old_pages, old_objects = await history_ids(session, owner_id, row.uid)
        else:
            snapshot = None
            old_pages, old_objects = set(), {}
        await process(notebook_plan(
            owner_id, row.uid, snapshot, historical_pages=old_pages,
            historical_objects=old_objects,
        ))
        total += 1
    # V1 change-only UIDs must not disappear from the cutover.
    if await session.scalar(
        select(ChangeEvent.id).where(
            ChangeEvent.owner_id == owner_id,
            ~select(SyncedNotebook.id).where(
                SyncedNotebook.owner_id == owner_id,
                SyncedNotebook.uid == ChangeEvent.entity_uid,
            ).exists(),
        ).limit(1)
    ):
        raise LegacyConversionError("Legacy event refers to a missing notebook row.")
    library = await session.get(AccountSettings, (owner_id, "libraryState"))
    if library is not None:
        await process(library_plan(owner_id, mapping(library.content, "library state")))
    values = {
        "owner_id": owner_id, "legacy_cursor": legacy_cursor,
        "operation_cursor": operation_cursor, "converted_documents": total,
    }
    if apply:
        await session.execute(text("""
            INSERT INTO legacy_account_cutovers
                (owner_id, legacy_cursor, operation_cursor, converted_documents)
            VALUES (:owner_id, :legacy_cursor, :operation_cursor, :converted_documents)
        """), values)
        await session.execute(text("""
            INSERT INTO account_sync_protocol (owner_id, protocol)
            VALUES (:owner_id, 'v2')
        """), {"owner_id": owner_id})
    return {
        "status": "activated" if apply else "validated",
        **{key: value for key, value in values.items() if key != "owner_id"},
    }


async def run(args):
    from .database import engine, session_factory

    try:
        async with session_factory.begin() as session:
            return await migrate_account(
                session, args.owner, apply=args.apply,
                expected_cursor=args.expect_v1_cursor,
            )
    finally:
        await engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("owner", type=uuid.UUID)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expect-v1-cursor", type=int)
    parser.add_argument("--all-v1-devices-flushed", action="store_true")
    args = parser.parse_args()
    if args.apply and not args.all_v1_devices_flushed:
        parser.error("--apply requires confirmed flush of every v1 device and guest")
    try:
        report = asyncio.run(run(args))
    except (ValueError, HTTPException) as error:
        parser.exit(1, f"Cutover refused: {getattr(error, 'detail', error)}\n")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
