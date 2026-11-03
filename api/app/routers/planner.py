"""Per-account planner reconciliation with durable tombstones and LWW clocks."""

import json
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import database_session
from ..models import PlannerEntry, User
from ..realtime_hub import hub
from ..security import current_user
from .sync import lock_owner_events


router = APIRouter(prefix="/v1/planner", tags=["planner"])
MAX_ENTRY_BYTES = 65536


def record_json(record: PlannerEntry) -> dict:
    return {
        "id": record.uid,
        "modifiedAt": record.modified_at.astimezone(UTC).isoformat(),
        "deleted": record.deleted,
        "revision": record.revision or 1,
        "entry": None if record.deleted else record.content,
    }


class PlannerWrite(BaseModel):
    modifiedAt: datetime
    deleted: bool = False
    entry: dict | None = None

    @field_validator("modifiedAt")
    @classmethod
    def timezone_required(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("modifiedAt must include a timezone")
        if value > datetime.now(UTC) + timedelta(days=1):
            raise ValueError("modifiedAt is too far in the future")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_entry(self):
        if self.deleted != (self.entry is None):
            raise ValueError("Deleted entries must have no content")
        if self.entry is not None:
            if (self.entry.get("kind") not in ("session", "event")
                    or not isinstance(self.entry.get("title"), str)
                    or not isinstance(self.entry.get("scheduledAt"), str)):
                raise ValueError("Invalid planner entry")
            size = len(json.dumps(self.entry, ensure_ascii=False).encode())
            if size > MAX_ENTRY_BYTES:
                raise ValueError("Planner entry is too large")
        return self


@router.get("/entries")
async def list_entries(
    after: str = Query(default="", max_length=128),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(database_session),
) -> dict:
    rows = await session.execute(
        select(PlannerEntry)
        .where(PlannerEntry.owner_id == user.id, PlannerEntry.uid > after)
        .order_by(PlannerEntry.uid)
        .limit(201)
    )
    records = list(rows.scalars())
    page = records[:200]
    return {
        "entries": [record_json(record) for record in page],
        "next": page[-1].uid if len(records) > 200 else None,
    }


@router.put("/entries/{uid}")
async def put_entry(
    uid: str,
    body: PlannerWrite,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(database_session),
) -> dict:
    if (not uid or len(uid) > 128
            or (body.entry is not None and body.entry.get("id") != uid)):
        raise HTTPException(status_code=422, detail="Planner entry ID mismatch")
    await lock_owner_events(session, user.id)
    record = await session.get(PlannerEntry, (user.id, uid))
    created = record is None
    if record is not None:
        # An acknowledged deletion is permanent for this UID. A stale device
        # must create a new entry with a new UID rather than resurrect this one.
        if record.deleted:
            return record_json(record)
        if not body.deleted and body.modifiedAt <= record.modified_at:
            return record_json(record)
        # Deletions win against edits made on devices with clocks ahead.
        # Preserve a monotonic timestamp for all readers and retries.
        modified_at = (
            max(body.modifiedAt, record.modified_at + timedelta(microseconds=1))
            if body.deleted else body.modifiedAt
        )
    else:
        modified_at = body.modifiedAt
        record = PlannerEntry(
            owner_id=user.id, uid=uid, content=None,
            modified_at=modified_at, deleted=True, revision=1,
        )
        session.add(record)
    if not created:
        record.revision = (record.revision or 1) + 1
    record.modified_at = modified_at
    record.content = None if body.deleted else body.entry
    record.deleted = body.deleted
    await session.commit()
    await hub.publish(user.id, {"type": "planner_changed", "id": uid})
    return record_json(record)
