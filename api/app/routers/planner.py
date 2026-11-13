"""Per-account planner reconciliation with durable tombstones."""

import json
from datetime import UTC, datetime, timedelta

from fastapi import Depends, Query
from pydantic import BaseModel, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import database_session
from ..models import PlannerEntry, User
from ..security import current_user


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
