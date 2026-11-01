"""Private, deduplicated feedback and diagnostic snapshots for maintainers."""

import asyncio
import hashlib
import json
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..models import User
from ..rate_limit import InMemoryRateLimiter
from ..security import current_user
from ..settings import get_settings

router = APIRouter(prefix="/v1/reports", tags=["reports"])
_write_lock = asyncio.Lock()
_feedback_limiter = InMemoryRateLimiter(attempts=10, window_seconds=3600)
SystemName = Literal["Linux", "Web", "Android", "iOS", "macOS", "Windows", "Other"]


class FeedbackPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    system: SystemName
    issue: str = Field(min_length=5, max_length=5000)

    @field_validator("issue")
    @classmethod
    def nonblank_issue(cls, value: str) -> str:
        value = value.strip()
        if len(value) < 5:
            raise ValueError("Describe the problem in at least 5 characters.")
        return value


class DiagnosticsPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    deviceId: uuid.UUID
    system: SystemName
    errors: str = Field(max_length=32000)
    dataIntegrity: str = Field(max_length=32000)
    optimization: str = Field(max_length=32000)


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError("Invalid reports index.")
    return value


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _store_feedback(base: Path, payload: FeedbackPayload) -> str:
    path = base / "feedback" / "reports.json"
    records = _read_json(path)
    digest = hashlib.sha256(
        json.dumps(
            {"system": payload.system, "issue": payload.issue},
            ensure_ascii=False, sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    now = datetime.now(UTC).isoformat()
    previous = records.get(digest, {})
    records[digest] = {
        "id": digest,
        "system": payload.system,
        "issue": payload.issue,
        "firstReportedAt": previous.get("firstReportedAt", now),
        "lastReportedAt": now,
        "occurrences": previous.get("occurrences", 0) + 1,
    }
    # Bound disk usage under repeated anonymous submissions.
    if len(records) > 2000:
        oldest = min(records, key=lambda key: records[key]["lastReportedAt"])
        del records[oldest]
    _write_json(path, records)
    return digest


def _store_diagnostics(
    base: Path, owner_id: uuid.UUID, payload: DiagnosticsPayload
) -> bool:
    path = base / "diagnostics" / str(owner_id) / f"{payload.deviceId}.json"
    current = _read_json(path)
    snapshot = payload.model_dump(mode="json")
    digest = hashlib.sha256(
        json.dumps(snapshot, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    if current.get("contentHash") == digest:
        return False
    _write_json(path, {
        "accountId": str(owner_id),
        "receivedAt": datetime.now(UTC).isoformat(),
        "contentHash": digest,
        **snapshot,
    })
    return True


@router.post("/feedback", status_code=201)
async def feedback(body: FeedbackPayload, request: Request) -> dict:
    client = request.client.host if request.client else "unknown"
    await _feedback_limiter.check(f"feedback:{client}")
    async with _write_lock:
        report_id = await asyncio.to_thread(
            _store_feedback, get_settings().data_path, body
        )
    return {"reportId": report_id}


@router.put("/diagnostics")
async def diagnostics(
    body: DiagnosticsPayload,
    user: User = Depends(current_user),
) -> dict:
    async with _write_lock:
        changed = await asyncio.to_thread(
            _store_diagnostics, get_settings().data_path, user.id, body
        )
    return {"stored": True, "changed": changed}
