"""A legacy guest may not write a snapshot into an activated v2 owner."""
import asyncio
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.routers import study_shares


def test_share_legacy_writer_respects_account_protocol(monkeypatch):
    owner = uuid.uuid4()
    session_id = uuid.uuid4()
    calls = []

    async def authorized_share(*args):
        calls.append("share")
        return SimpleNamespace(owner_id=owner)

    async def lock_owner(*args):
        calls.append("lock")

    async def reject_v2(session, owner_id):
        assert owner_id == owner
        calls.append("protocol")
        raise HTTPException(
            status_code=409,
            detail="Account uses operation API v2; update the client.",
        )

    monkeypatch.setattr(study_shares, "guest_share", authorized_share)
    monkeypatch.setattr(study_shares, "lock_owner_events", lock_owner)
    monkeypatch.setattr(
        study_shares, "require_legacy_protocol", reject_v2,
    )

    class NoWritesSession:
        async def scalar(self, query):
            raise AssertionError("No legacy notebook query after v2 cutover")
        async def commit(self):
            raise AssertionError("No legacy commit after v2 cutover")

    notebook = {
        "uid": "any", "title": "Snapshot", "kind": 0, "folder": "",
        "createdAt": "2026-11-04T10:00:00Z",
        "updatedAt": "2026-11-04T10:00:00Z", "pages": [],
    }
    body = study_shares.ShareDocumentWrite(
        baseRevision=1, baseNotebook=notebook, notebook=notebook,
    )
    request = SimpleNamespace(headers={"content-length": "100"})
    with pytest.raises(HTTPException) as error:
        asyncio.run(study_shares.edit_shared_document(
            session_id, body, request, "guest-ticket", NoWritesSession(),
        ))
    assert error.value.status_code == 409
    assert calls == ["share", "lock", "protocol"]
