"""Tombstone reconciliation is explicit and server-ordered, never backup-driven."""

import asyncio
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.models import SyncedNotebook, User
from app.routers import sync
from app.schemas import DeleteWrite, SyncWrite


def notebook(uid="course", title="Edited later"):
    return {
        "uid": uid, "title": title, "kind": 0, "folder": "Notes",
        "createdAt": "2026-11-02T10:00:00Z",
        "updatedAt": "2026-11-02T10:05:00Z",
        "pages": [{
            "id": "page-1", "title": "Page 1",
            "textBlocks": [], "imageBlocks": [], "inkStrokes": [],
        }],
    }


class MemorySession:
    def __init__(self, record):
        self.record = record
        self.events = []
        self.commits = 0

    async def scalar(self, statement):
        # No active shared session remains after deletion.
        return None

    async def execute(self, statement):
        return SimpleNamespace(scalar_one_or_none=lambda: self.record)

    def add(self, event):
        self.events.append(event)

    async def flush(self):
        pass

    async def commit(self):
        self.commits += 1


def request():
    return Request({
        "type": "http", "method": "PUT", "path": "/v1/sync/notebooks/course",
        "headers": [(b"content-length", b"1000")],
    })


@pytest.fixture
def environment(monkeypatch):
    owner = User(
        id=uuid.uuid4(), username="account", password_hash="unused",
    )
    record = SyncedNotebook(
        id=uuid.uuid4(), owner_id=owner.id, uid="course",
        content=None, revision=2, deleted_at=datetime.now(UTC),
    )
    session = MemorySession(record)

    async def skip_lock(*args, **kwargs):
        pass

    async def empty_idempotency(*args, **kwargs):
        return None

    async def no_broadcast(*args, **kwargs):
        pass

    monkeypatch.setattr(sync, "lock_owner_events", skip_lock)
    monkeypatch.setattr(sync, "idempotent_response", empty_idempotency)
    monkeypatch.setattr(sync.hub, "publish", no_broadcast)
    return owner, record, session


def test_stale_backup_cannot_implicitly_revive_deleted_uid(environment):
    owner, record, session = environment
    with pytest.raises(HTTPException) as failure:
        asyncio.run(sync.put_notebook(
            "course",
            SyncWrite(expectedRevision=2, notebook=notebook()),
            request(), "a" * 36, owner, session,
        ))
    assert failure.value.status_code == 409
    assert record.deleted_at is not None
    assert session.commits == 0


def test_legacy_restore_deleted_flag_cannot_revive_tombstone(environment):
    owner, record, session = environment
    with pytest.raises(HTTPException) as failure:
        asyncio.run(sync.put_notebook(
            "course",
            SyncWrite(
                expectedRevision=2, restoreDeleted=True, notebook=notebook(),
            ),
            request(), "b" * 36, owner, session,
        ))
    assert failure.value.status_code == 409
    assert record.uid == "course"
    assert record.deleted_at is not None
    assert record.content is None
    assert session.commits == 0


def test_concurrent_delete_must_first_observe_latest_edit(environment):
    owner, record, session = environment
    record.deleted_at = None
    record.content = notebook("course", "Latest edit")
    record.revision = 3
    with pytest.raises(HTTPException) as failure:
        asyncio.run(sync.delete_notebook(
            "course", DeleteWrite(expectedRevision=2),
            request(), "c" * 36, owner, session,
        ))
    assert failure.value.status_code == 409
    assert record.deleted_at is None
    result = asyncio.run(sync.delete_notebook(
        "course", DeleteWrite(expectedRevision=3),
        request(), "d" * 36, owner, session,
    ))
    assert result["revision"] == 4
    assert record.content is None
    assert record.deleted_at is not None
