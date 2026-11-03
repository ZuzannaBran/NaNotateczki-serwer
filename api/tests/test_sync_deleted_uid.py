"""A legacy client cannot restore an account notebook tombstone."""

import asyncio
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.routers import sync
from app.schemas import SyncWrite


def test_legacy_restore_flag_cannot_resurrect_deleted_notebook(monkeypatch):
    uid = "deleted-document"
    record = SimpleNamespace(
        uid=uid,
        revision=4,
        deleted_at=datetime.now(UTC),
        content=None,
    )

    class DeletedDocumentSession:
        async def scalar(self, statement):
            return None

        async def execute(self, statement):
            return SimpleNamespace(scalar_one_or_none=lambda: record)

    async def no_op(*args):
        return None

    monkeypatch.setattr(sync, "request_size_guard", no_op)
    monkeypatch.setattr(sync, "lock_owner_events", no_op)
    monkeypatch.setattr(sync, "idempotent_response", no_op)
    payload = {
        "uid": uid,
        "title": "Stale local version",
        "kind": 0,
        "folder": "",
        "createdAt": "2026-11-02T00:00:00Z",
        "updatedAt": "2026-11-02T00:00:00Z",
        "pages": [],
    }
    request = SimpleNamespace(headers={"content-length": "100"})
    principal = SimpleNamespace(id=uuid.uuid4())

    for restore in (False, True):
        with pytest.raises(HTTPException) as failure:
            asyncio.run(
                sync.put_notebook(
                    uid,
                    SyncWrite(
                        expectedRevision=record.revision,
                        notebook=payload,
                        restoreDeleted=restore,
                    ),
                    request,
                    "legacy-device-retry-key",
                    principal,
                    DeletedDocumentSession(),
                )
            )
        assert failure.value.status_code == 409
        assert failure.value.detail == {
            "uid": uid,
            "revision": record.revision,
            "deleted": True,
            "notebook": None,
        }
