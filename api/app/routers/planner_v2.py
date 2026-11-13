"""Revision-based, idempotent calendar writes independent of client clocks."""

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from ..account_protocol import require_v2_protocol
from ..database import database_session
from ..models import PlannerEntry, PlannerEntryMutation, User
from ..realtime_hub import hub
from ..security import current_user
from .operations import require_operations_v2_write
from .planner import PlannerWrite, list_entries, record_json
from .sync import lock_owner_events

router = APIRouter(prefix="/v2/planner", tags=["planner-v2"])


class PlannerRevisionWrite(BaseModel):
    operationId: uuid.UUID
    expectedRevision: int = Field(ge=0)
    deleted: bool = False
    entry: dict | None = None

    @model_validator(mode="after")
    def validate_entry(self):
        # Reuse the exact content constraints of the legacy endpoint.
        PlannerWrite(
            modifiedAt=datetime.now(UTC),
            deleted=self.deleted,
            entry=self.entry,
        )
        return self


@router.get("/entries")
async def get_entries(
    after: str = Query(default="", max_length=128),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(database_session),
) -> dict:
    await require_v2_protocol(session, user.id)
    return await list_entries(after=after, user=user, session=session)


@router.put("/entries/{uid}")
async def put_entry(
    uid: str,
    body: PlannerRevisionWrite,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(database_session),
) -> dict:
    if (
        not uid or len(uid) > 128
        or (body.entry is not None and body.entry.get("id") != uid)
    ):
        raise HTTPException(status_code=422, detail="Planner entry ID mismatch")

    payload = body.model_dump(mode="json")
    fingerprint = hashlib.sha256(
        json.dumps({"uid": uid, **payload}, sort_keys=True,
                   separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()

    # Serializes mutations on this account, including legacy v1 requests.
    await lock_owner_events(session, user.id)
    await require_v2_protocol(session, user.id)
    require_operations_v2_write(user.id)
    receipt = await session.get(
        PlannerEntryMutation, (user.id, body.operationId)
    )
    if receipt is not None:
        if receipt.payload_hash != fingerprint or receipt.uid != uid:
            raise HTTPException(
                status_code=409, detail="Operation ID reused with new payload"
            )
        return receipt.result

    record = await session.get(PlannerEntry, (user.id, uid))
    current_revision = (record.revision or 1) if record is not None else 0
    if record is not None and record.deleted:
        if not body.deleted:
            raise HTTPException(
                status_code=409,
                detail={"reason": "deleted", "current": record_json(record)},
            )
        # Idempotent deletion with a different operation ID.
        result = record_json(record)
    elif (
        body.expectedRevision != current_revision
        and not body.deleted
    ):
        raise HTTPException(
            status_code=409,
            detail={
                "reason": "stale_revision",
                "current": record_json(record) if record else None,
            },
        )
    else:
        # Concurrent deletion is allowed to supersede an unseen edit.
        now = datetime.now(UTC)
        previous = record.modified_at if record is not None else None
        modified = max(
            now,
            previous.astimezone(UTC) + timedelta(microseconds=1)
            if previous is not None else now,
        )
        if record is None:
            record = PlannerEntry(
                owner_id=user.id, uid=uid, content=None,
                modified_at=modified, deleted=True, revision=1,
            )
            session.add(record)
        else:
            record.revision = current_revision + 1
        record.deleted = body.deleted
        record.content = None if body.deleted else body.entry
        record.modified_at = modified
        result = record_json(record)

    session.add(
        PlannerEntryMutation(
            owner_id=user.id, operation_id=body.operationId, uid=uid,
            payload_hash=fingerprint, result=result,
        )
    )
    await session.commit()
    if record is not None and result["revision"] != current_revision:
        await hub.publish(user.id, {"type": "planner_changed", "id": uid})
    return result
