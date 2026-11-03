"""Stage-4 transactional operation API, independent of legacy snapshot sync."""
from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import database_session
from ..models import (
    ActorCheckpoint, DocumentOperation, DocumentState, ObjectTombstone,
    StudyShare, StudyShareGrant, SyncedNotebook, User,
)
from ..operation_ordering import precedence
from ..operation_projection import ProjectionError, apply_operation
from ..operation_protocol import (
    InvalidOperation, LIBRARY_DOCUMENT_ID, OperationEnvelope,
)
from ..realtime_hub import hub
from ..security import decode_access_token_claims, token_digest
from ..settings import get_settings
from .study_shares import ensure_active
from .sync import lock_owner_events

log = logging.getLogger(__name__)
router = APIRouter(prefix="/v2", tags=["operations"])
MAX_BATCH = 50
MAX_PAGE = 25
MAX_PAGE_BYTES = 8 * 1024 * 1024
_ID = re.compile(r"^[^\x00-\x1f\x7f]{1,128}$")


class OperationBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operations: list[dict] = Field(min_length=1, max_length=MAX_BATCH)


@dataclass(frozen=True)
class Principal:
    owner_id: uuid.UUID
    kind: str
    principal_id: uuid.UUID
    share_id: uuid.UUID | None = None
    document_uid: str | None = None


async def authorize(
    session: AsyncSession, authorization: str | None,
    share_id: uuid.UUID | None,
) -> Principal:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Bearer token required.")
    token = authorization[7:]
    if share_id is not None:
        if not 40 <= len(token) <= 256:
            raise HTTPException(status_code=401, detail="Invalid share ticket.")
        grant = await session.scalar(
            select(StudyShareGrant).where(
                StudyShareGrant.share_id == share_id,
                StudyShareGrant.token_hash == token_digest(token),
            )
        )
        share = await session.get(StudyShare, share_id)
        if grant is None or share is None:
            raise HTTPException(status_code=401, detail="Invalid share ticket.")
        # A formerly valid, expired ticket is a terminal session state, not
        # an authentication retry. Keep the opaque token binding check first.
        if grant.expires_at <= datetime.now(UTC):
            raise HTTPException(status_code=410, detail="Share ticket expired.")
        ensure_active(share)
        return Principal(
            share.owner_id, "share", grant.id, share.id, share.document_uid
        )
    user_id, _, auth_version = decode_access_token_claims(token)
    user = await session.get(User, user_id)
    if user is None or user.auth_version != auth_version:
        raise HTTPException(status_code=401, detail="Session has expired.")
    return Principal(user.id, "owner", user.id)


async def check_share_lock(session: AsyncSession, principal: Principal) -> None:
    if principal.share_id is None:
        return
    share = await session.scalar(
        select(StudyShare).where(
            StudyShare.id == principal.share_id,
            StudyShare.owner_id == principal.owner_id,
            StudyShare.document_uid == principal.document_uid,
        ).with_for_update()
    )
    if share is None:
        raise HTTPException(status_code=410, detail="Share was removed.")
    ensure_active(share)
    grant = await session.get(StudyShareGrant, principal.principal_id)
    if (
        grant is None or grant.share_id != principal.share_id
        or grant.expires_at <= datetime.now(UTC)
    ):
        raise HTTPException(status_code=401, detail="Share ticket expired.")


def validate_target(op: OperationEnvelope, principal: Principal) -> None:
    for value in (op.actor_id, op.document_id, op.object_id, op.page_id):
        if value is not None and not _ID.fullmatch(value):
            raise HTTPException(status_code=422, detail="Invalid target ID.")
    if principal.share_id is not None and (
        op.document_id != principal.document_uid
        or op.document_id == LIBRARY_DOCUMENT_ID
    ):
        raise HTTPException(status_code=403, detail="Share scope violation.")
    if op.reverts_operation_id is not None and (
        op.reverts_operation_id not in op.dependencies
    ):
        raise HTTPException(status_code=422, detail="Missing inverse dependency.")


def ensure_projection_budget(projected: dict | None, maximum_bytes: int) -> None:
    if projected is None:
        return
    encoded = json.dumps(
        projected, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    if len(encoded) > maximum_bytes:
        raise HTTPException(status_code=413, detail="Document is too large.")


def ensure_actor_binding(
    checkpoint: ActorCheckpoint | None, principal: Principal,
) -> None:
    if checkpoint is not None and (
        checkpoint.principal_kind != principal.kind
        or checkpoint.principal_id != principal.principal_id
        or checkpoint.share_id != principal.share_id
    ):
        raise HTTPException(status_code=403, detail="Actor identity is bound.")


async def apply_one(
    session: AsyncSession, principal: Principal, op: OperationEnvelope,
) -> dict | None:
    """Apply when causal prerequisites exist, else return None for retry."""
    owner_id = principal.owner_id
    existing = await session.scalar(
        select(DocumentOperation).where(DocumentOperation.operation_id ==
                                        uuid.UUID(op.operation_id))
    )
    checkpoint = await session.get(ActorCheckpoint, (owner_id, op.actor_id))
    ensure_actor_binding(checkpoint, principal)
    if existing is not None:
        if existing.owner_id != owner_id or existing.envelope != op.to_dict():
            raise HTTPException(status_code=409, detail="Operation ID reused.")
        return {"operationId": op.operation_id, "cursor": existing.cursor,
                "status": "duplicate"}

    if checkpoint is None:
        if op.actor_seq != 1:
            return None
    elif op.actor_seq != checkpoint.last_seq + 1:
        if op.actor_seq <= checkpoint.last_seq:
            raise HTTPException(status_code=409, detail="Actor sequence fork.")
        return None
    if checkpoint is not None and (
        checkpoint.last_operation_id not in [
            uuid.UUID(item) for item in op.dependencies
        ] or op.logical_clock <= checkpoint.last_logical_clock
    ):
        raise HTTPException(status_code=409, detail="Invalid actor causality.")

    if checkpoint is not None:
        predecessor = await session.scalar(
            select(DocumentOperation).where(
                DocumentOperation.owner_id == owner_id,
                DocumentOperation.operation_id == checkpoint.last_operation_id,
            )
        )
        if predecessor is None or predecessor.document_uid != op.document_id:
            raise HTTPException(
                status_code=409, detail="Actor crossed document scope."
            )

    for dependency in op.dependencies:
        parent = await session.scalar(
            select(DocumentOperation).where(
                DocumentOperation.owner_id == owner_id,
                DocumentOperation.operation_id == uuid.UUID(dependency),
            )
        )
        if parent is None:
            return None
        if parent.document_uid != op.document_id:
            raise HTTPException(
                status_code=422, detail="Cross-document causal dependency."
            )
        if op.logical_clock <= parent.logical_clock:
            raise HTTPException(status_code=422, detail="Invalid logical clock.")

    # Prevent dual writers: stage-11 migration must explicitly convert v1
    # notebook state before any operation edits that UID.
    if op.document_id != LIBRARY_DOCUMENT_ID:
        legacy = await session.scalar(
            select(SyncedNotebook.id).where(
                SyncedNotebook.owner_id == owner_id,
                SyncedNotebook.uid == op.document_id,
            )
        )
        if legacy is not None:
            raise HTTPException(
                status_code=409,
                detail="Legacy document requires stage-11 migration.",
            )

    state = await session.get(DocumentState, (owner_id, op.document_id))
    tombstone_rows = await session.scalars(
        select(ObjectTombstone).where(
            ObjectTombstone.owner_id == owner_id,
            ObjectTombstone.document_uid == op.document_id,
        )
    )
    tombstones = {
        (row.target_kind, row.page_id, row.object_id)
        for row in tombstone_rows
    }
    # Copy from JSONB state so a failure never leaves an in-memory partial
    # change attached to a pending ORM instance.
    import copy

    before = copy.deepcopy(state.content) if state is not None else None
    try:
        projected, tombstone = apply_operation(before, op, tombstones)
    except ProjectionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if projected is None and state is None and op.kind != "document.delete":
        raise HTTPException(status_code=409, detail="Document is not initialized.")
    ensure_projection_budget(projected, get_settings().maximum_sync_bytes)

    row = DocumentOperation(
        owner_id=owner_id, operation_id=uuid.UUID(op.operation_id),
        actor_id=op.actor_id, actor_seq=op.actor_seq,
        document_uid=op.document_id, op_kind=op.kind,
        logical_clock=op.logical_clock, envelope=op.to_dict(),
    )
    session.add(row)
    await session.flush()
    now = datetime.now(UTC)
    if state is None:
        state = DocumentState(
            owner_id=owner_id, document_uid=op.document_id,
            revision=1, content=projected,
            deleted_at=now if projected is None else None,
        )
        session.add(state)
    else:
        state.revision += 1
        state.content = projected
        if op.kind == "document.delete":
            state.deleted_at = now
        state.updated_at = now

    if tombstone is not None:
        target_kind, page_id, object_id = tombstone
        session.add(ObjectTombstone(
            owner_id=owner_id, document_uid=op.document_id,
            target_kind=target_kind, page_id=page_id, object_id=object_id,
            deleted_by=uuid.UUID(op.operation_id),
        ))
    if checkpoint is None:
        session.add(ActorCheckpoint(
            owner_id=owner_id, actor_id=op.actor_id,
            principal_kind=principal.kind, principal_id=principal.principal_id,
            share_id=principal.share_id, last_seq=op.actor_seq,
            last_operation_id=uuid.UUID(op.operation_id),
            last_logical_clock=op.logical_clock,
        ))
    else:
        checkpoint.last_seq = op.actor_seq
        checkpoint.last_operation_id = uuid.UUID(op.operation_id)
        checkpoint.last_logical_clock = op.logical_clock
    await session.flush()
    return {"operationId": op.operation_id, "cursor": row.cursor,
            "status": "applied"}


@router.post("/operations/batch")
async def post_batch(
    body: OperationBatch,
    authorization: str | None = Header(default=None),
    share_id: uuid.UUID | None = Header(default=None, alias="X-Share-Id"),
    session: AsyncSession = Depends(database_session),
) -> dict:
    principal = await authorize(session, authorization, share_id)
    operations: list[OperationEnvelope] = []
    seen: set[str] = set()
    for raw in body.operations:
        try:
            op = OperationEnvelope.from_dict(raw)
        except InvalidOperation as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        validate_target(op, principal)
        if op.operation_id in seen:
            raise HTTPException(status_code=422, detail="Duplicate in batch.")
        seen.add(op.operation_id)
        operations.append(op)

    # Serializes account commits with v1 sync and revocations, so a later
    # cursor cannot become visible before an earlier transaction commits.
    await lock_owner_events(session, principal.owner_id)
    # The global lock linearizes newly allocated v2 event cursors across
    # accounts; a rolled-back transaction cannot expose an earlier gap.
    await session.execute(text("SELECT pg_advisory_xact_lock(681246731)"))
    await check_share_lock(session, principal)
    pending = {op.operation_id: op for op in operations}
    acknowledgements: dict[str, dict] = {}
    changed_docs: set[str] = set()
    while pending:
        progressed = False
        for op in sorted(pending.values(), key=precedence):
            ack = await apply_one(session, principal, op)
            if ack is None:
                continue
            acknowledgements[op.operation_id] = ack
            if ack["status"] == "applied":
                changed_docs.add(op.document_id)
            del pending[op.operation_id]
            progressed = True
        if not progressed:
            # No partial commit. The durable client outbox resends the same
            # IDs once a predecessor/dependency is available.
            raise HTTPException(
                status_code=409,
                detail={"code": "CAUSAL_GAP",
                        "operationIds": sorted(pending)},
            )
    await session.commit()
    ordered = [acknowledgements[op.operation_id] for op in operations]
    cursor = max(item["cursor"] for item in ordered)
    if changed_docs:
        try:
            await hub.publish(principal.owner_id, {
                "type": "operations_changed", "cursor": cursor,
            })
            shares = await session.scalars(
                select(StudyShare).where(
                    StudyShare.owner_id == principal.owner_id,
                    StudyShare.document_uid.in_(changed_docs),
                    StudyShare.revoked_at.is_(None),
                    StudyShare.expires_at > datetime.now(UTC),
                )
            )
            for share in shares:
                await hub.publish(share.id, {
                    "type": "operations_changed", "cursor": cursor,
                })
        except Exception:
            log.exception("Operation notification failed after commit")
    return {"cursor": cursor, "acknowledgements": ordered}


async def list_operations(
    principal: Principal, after: int, session: AsyncSession,
) -> dict:
    query = select(DocumentOperation).where(
        DocumentOperation.owner_id == principal.owner_id,
        DocumentOperation.cursor > after,
    )
    if principal.document_uid is not None:
        query = query.where(
            DocumentOperation.document_uid == principal.document_uid
        )
    items = list((await session.scalars(
        query.order_by(DocumentOperation.cursor).limit(MAX_PAGE + 1)
    )).all())
    page = []
    size_bytes = 0
    for row in items:
        encoded_size = len(json.dumps(
            row.envelope, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8"))
        if page and (
            len(page) >= MAX_PAGE or size_bytes + encoded_size > MAX_PAGE_BYTES
        ):
            break
        page.append(row)
        size_bytes += encoded_size
    return {"cursor": page[-1].cursor if page else after,
            "hasMore": len(items) > len(page),
            "operations": [
                {"cursor": row.cursor, "operation": row.envelope}
                for row in page
            ]}


@router.get("/operations")
async def get_operations(
    after: int = Query(default=0, ge=0),
    authorization: str | None = Header(default=None),
    share_id: uuid.UUID | None = Header(default=None, alias="X-Share-Id"),
    session: AsyncSession = Depends(database_session),
) -> dict:
    principal = await authorize(session, authorization, share_id)
    return await list_operations(principal, after, session)


@router.get("/documents/{document_id}/snapshot")
async def get_snapshot(
    document_id: str,
    authorization: str | None = Header(default=None),
    share_id: uuid.UUID | None = Header(default=None, alias="X-Share-Id"),
    session: AsyncSession = Depends(database_session),
) -> dict:
    principal = await authorize(session, authorization, share_id)
    if not _ID.fullmatch(document_id):
        raise HTTPException(status_code=422, detail="Invalid document ID.")
    if principal.document_uid is not None and document_id != principal.document_uid:
        raise HTTPException(status_code=403, detail="Share scope violation.")
    # Both projection and its frontier must come from ONE MVCC statement.
    # Otherwise a commit between two SELECTs could make a client skip edits.
    frontier = select(
        func.coalesce(func.max(DocumentOperation.cursor), 0)
    ).where(
        DocumentOperation.owner_id == principal.owner_id,
        DocumentOperation.document_uid == document_id,
    ).scalar_subquery()
    row = (await session.execute(
        select(DocumentState, frontier).where(
            DocumentState.owner_id == principal.owner_id,
            DocumentState.document_uid == document_id,
        )
    )).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Snapshot not found.")
    state, cursor = row
    return {"documentId": document_id, "revision": state.revision,
            "cursor": int(cursor or 0), "deleted": state.deleted_at is not None,
            "state": state.content}


@router.get("/sync/checkpoint")
async def get_checkpoint(
    authorization: str | None = Header(default=None),
    share_id: uuid.UUID | None = Header(default=None, alias="X-Share-Id"),
    session: AsyncSession = Depends(database_session),
) -> dict:
    principal = await authorize(session, authorization, share_id)
    query = select(func.coalesce(func.max(DocumentOperation.cursor), 0)).where(
        DocumentOperation.owner_id == principal.owner_id,
    )
    if principal.document_uid is not None:
        query = query.where(
            DocumentOperation.document_uid == principal.document_uid
        )
    cursor = await session.scalar(query)
    actors_query = select(ActorCheckpoint).where(
        ActorCheckpoint.owner_id == principal.owner_id
    )
    if principal.share_id is not None:
        actors_query = actors_query.where(
            ActorCheckpoint.share_id == principal.share_id
        )
    actors = await session.scalars(
        actors_query.order_by(ActorCheckpoint.actor_id).limit(1000)
    )
    return {"cursor": int(cursor or 0), "actors": [
        {"actorId": row.actor_id, "actorSeq": row.last_seq,
         "lastOperationId": str(row.last_operation_id),
         "logicalClock": row.last_logical_clock}
        for row in actors
    ]}
