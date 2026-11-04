"""A v1 snapshot must not overwrite a v2-operated document."""
import asyncio
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.routers import sync
from app.schemas import DeleteWrite, SyncWrite


@pytest.mark.parametrize("method", ["put", "delete"])
def test_legacy_writes_reject_uid_with_v2_state(monkeypatch, method):
    async def no_op(*args):
        return None

    monkeypatch.setattr(sync, "request_size_guard", no_op)
    monkeypatch.setattr(sync, "lock_owner_events", no_op)
    monkeypatch.setattr(sync, "require_legacy_protocol", no_op)
    monkeypatch.setattr(sync, "idempotent_response", no_op)

    class ExistingV2:
        async def scalar(self, query):
            return 1  # DocumentState.revision; must reject before any write.

        async def commit(self):
            raise AssertionError("No v1 commit allowed")

    uid = "v2-document"
    user = SimpleNamespace(id=uuid.uuid4())
    request = SimpleNamespace(headers={"content-length": "100"})
    session = ExistingV2()
    if method == "put":
        notebook = {
            "uid": uid, "title": "Stale",
            "kind": 0, "folder": "",
            "createdAt": "2026-11-03T10:00:00Z",
            "updatedAt": "2026-11-03T10:00:00Z",
            "pages": [],
        }
        coroutine = sync.put_notebook(
            uid, SyncWrite(expectedRevision=1, notebook=notebook),
            request, "legacy-retry-1234567", user, session,
        )
    else:
        coroutine = sync.delete_notebook(
            uid, DeleteWrite(expectedRevision=1),
            request, "legacy-retry-1234567", user, session,
        )
    with pytest.raises(HTTPException) as error:
        asyncio.run(coroutine)
    assert error.value.status_code == 409
    assert "operation API v2" in error.value.detail
