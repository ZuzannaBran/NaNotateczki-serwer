import hashlib
import json
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import database_session
from ..models import ChangeEvent, IdempotencyRecord, StudyShare, SyncedNotebook, User
from ..realtime_hub import hub
from ..schemas import DeleteWrite, SyncWrite
from ..security import current_user
from ..settings import get_settings


router = APIRouter(prefix="/v1/sync", tags=["sync"])


async def lock_owner_events(session: AsyncSession, owner_id: uuid.UUID) -> None:
    # PostgreSQL sequences are assigned before commit. Serializing writes for
    # one account prevents a later event from making an uncommitted one invisible
    # to a client that advances its change cursor.
    key = int.from_bytes(hashlib.sha256(owner_id.bytes).digest()[:8], "big", signed=True)
    await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})


def record_json(record: SyncedNotebook) -> dict:
    return {
        "uid": record.uid,
        "revision": record.revision,
        "deleted": record.deleted_at is not None,
        "notebook": None if record.deleted_at is not None else record.content,
    }


def event_json(event: ChangeEvent) -> dict:
    return {
        "uid": event.entity_uid,
        "revision": event.revision,
        "deleted": event.deleted,
        "notebook": None if event.deleted else event.content,
    }


async def request_size_guard(request: Request) -> None:
    raw = request.headers.get("content-length")
    if raw is None or not raw.isdigit():
        raise HTTPException(status_code=411, detail="Content-Length is required.")
    if int(raw) > get_settings().maximum_sync_bytes:
        raise HTTPException(status_code=413, detail="Sync payload is too large.")


async def idempotent_response(
    session: AsyncSession, owner_id: uuid.UUID, key: str, request_hash: str
) -> dict | None:
    record = await session.get(IdempotencyRecord, (owner_id, key))
    if record is None:
        return None
    if record.request_hash != request_hash:
        raise HTTPException(status_code=409, detail="Idempotency key was reused.")
    return record.response


async def conflict(record: SyncedNotebook | None, uid: str) -> None:
    if record is None:
        detail = {"uid": uid, "revision": 0, "deleted": True, "notebook": None}
    else:
        detail = record_json(record)
    raise HTTPException(status_code=409, detail=detail)


@router.get("/bootstrap")
async def bootstrap(
    user: User = Depends(current_user),
    session: AsyncSession = Depends(database_session),
) -> dict:
    # Read the cursor first. A concurrent write may then appear both in the
    # snapshot and changes feed, which is harmless because revisions dedupe it.
    # Reading it second could permanently skip a write absent from the snapshot.
    cursor = await session.scalar(
        select(func.coalesce(func.max(ChangeEvent.id), 0)).where(
            ChangeEvent.owner_id == user.id
        )
    )
    records = await session.execute(
        select(SyncedNotebook)
        .where(SyncedNotebook.owner_id == user.id)
        .order_by(SyncedNotebook.uid)
    )
    return {
        "cursor": int(cursor or 0),
        "notebooks": [record_json(item) for item in records.scalars()],
    }


@router.get("/changes")
async def changes(
    after: int = Query(ge=0),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(database_session),
) -> dict:
    result = await session.execute(
        select(ChangeEvent)
        .where(ChangeEvent.owner_id == user.id, ChangeEvent.id > after)
        .order_by(ChangeEvent.id)
        .limit(500)
    )
    events = list(result.scalars())
    cursor = events[-1].id if events else after
    return {"cursor": cursor, "changes": [event_json(item) for item in events]}


@router.put("/notebooks/{uid}")
async def put_notebook(
    uid: str,
    body: SyncWrite,
    request: Request,
    idempotency_key: str = Header(min_length=16, max_length=64, alias="Idempotency-Key"),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(database_session),
) -> dict:
    await request_size_guard(request)
    if len(uid) > 128 or body.notebook.get("uid") != uid:
        raise HTTPException(status_code=422, detail="Notebook UID mismatch.")
    encoded = json.dumps(body.model_dump(), sort_keys=True, separators=(",", ":")).encode()
    request_hash = hashlib.sha256(encoded).hexdigest()
    received_at = datetime.now(UTC)
    await lock_owner_events(session, user.id)
    previous = await idempotent_response(session, user.id, idempotency_key, request_hash)
    if previous is not None:
        return previous
    # Shared edits use one ordering rule for owners and invited guests.
    # Keep lock order: owner, share, notebook.
    active_share = await session.scalar(
        select(StudyShare).where(
            StudyShare.owner_id == user.id,
            StudyShare.document_uid == uid,
            StudyShare.revoked_at.is_(None),
            StudyShare.expires_at > datetime.now(UTC),
        ).order_by(StudyShare.created_at.desc()).limit(1).with_for_update()
    )
    result = await session.execute(
        select(SyncedNotebook)
        .where(SyncedNotebook.owner_id == user.id, SyncedNotebook.uid == uid)
        .with_for_update()
    )
    record = result.scalar_one_or_none()
    merged_notebook = body.notebook
    if record is None:
        if body.expectedRevision != 0:
            await conflict(None, uid)
        record = SyncedNotebook(
            id=uuid.uuid4(),
            owner_id=user.id,
            uid=uid,
            content=merged_notebook,
            revision=1,
        )
        session.add(record)
    else:
        if active_share is not None and body.baseNotebook is not None:
            from .study_shares import merge_share_document

            if body.baseNotebook.get("uid") != uid:
                raise HTTPException(status_code=409, detail="Invalid base notebook.")
            clock = dict(active_share.edit_clock or {})
            merged_notebook = merge_share_document(
                body.baseNotebook, body.notebook, record.content,
                clock, received_at,
            )
            active_share.edit_clock = clock
        elif record.revision != body.expectedRevision:
            await conflict(record, uid)
        record.revision += 1
        record.content = merged_notebook
        record.deleted_at = None
        record.updated_at = datetime.now(UTC)
    await session.flush()
    event = ChangeEvent(
        owner_id=user.id,
        entity_uid=uid,
        revision=record.revision,
        deleted=False,
        content=body.notebook,
    )
    session.add(event)
    await session.flush()
    response = {"revision": record.revision, "cursor": event.id}
    if active_share is not None:
        response["notebook"] = merged_notebook
    session.add(
        IdempotencyRecord(
            owner_id=user.id,
            key=idempotency_key,
            request_hash=request_hash,
            response=response,
        )
    )
    await session.commit()
    await hub.publish(user.id, {"type": "notebook_changed", "cursor": event.id})
    if active_share is not None:
        await hub.publish(active_share.id, {
            "type": "document_changed", "revision": record.revision,
        })
    return response


@router.delete("/notebooks/{uid}")
async def delete_notebook(
    uid: str,
    body: DeleteWrite,
    request: Request,
    idempotency_key: str = Header(min_length=16, max_length=64, alias="Idempotency-Key"),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(database_session),
) -> dict:
    await request_size_guard(request)
    encoded = json.dumps(body.model_dump(), sort_keys=True, separators=(",", ":")).encode()
    request_hash = hashlib.sha256(encoded).hexdigest()
    await lock_owner_events(session, user.id)
    previous = await idempotent_response(session, user.id, idempotency_key, request_hash)
    if previous is not None:
        return previous
    result = await session.execute(
        select(SyncedNotebook)
        .where(SyncedNotebook.owner_id == user.id, SyncedNotebook.uid == uid)
        .with_for_update()
    )
    record = result.scalar_one_or_none()
    if record is None or record.revision != body.expectedRevision:
        await conflict(record, uid)
    record.revision += 1
    record.deleted_at = datetime.now(UTC)
    record.content = None
    event = ChangeEvent(
        owner_id=user.id,
        entity_uid=uid,
        revision=record.revision,
        deleted=True,
        content=None,
    )
    session.add(event)
    await session.flush()
    response = {"revision": record.revision, "cursor": event.id}
    session.add(
        IdempotencyRecord(
            owner_id=user.id,
            key=idempotency_key,
            request_hash=request_hash,
            response=response,
        )
    )
    await session.commit()
    await hub.publish(user.id, {"type": "notebook_deleted", "cursor": event.id})
    return response
