"""Concurrent backup finalization must not exhaust one API worker's memory."""

import asyncio
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.routers import backups


class MemorySession:
    def __init__(self, protocol="v1"):
        self.protocol = protocol
        self.checks = 0

    async def execute(self, statement, parameters):
        assert "FROM account_sync_protocol" in str(statement)
        assert isinstance(parameters["owner_id"], uuid.UUID)
        self.checks += 1
        return SimpleNamespace(scalar_one_or_none=lambda: self.protocol)


def test_large_finalizations_are_serialized(monkeypatch):
    active = 0
    maximum_active = 0

    async def fake_finalize(*args):
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        await asyncio.sleep(0.015)
        active -= 1
        return {"status": "verified"}

    monkeypatch.setattr(backups, "_finalize_backup", fake_finalize)

    session = MemorySession()

    async def exercise():
        return await asyncio.gather(
            backups.finalize_backup(
                uuid.uuid4(), SimpleNamespace(id=uuid.uuid4()), session
            ),
            backups.finalize_backup(
                uuid.uuid4(), SimpleNamespace(id=uuid.uuid4()), session
            ),
        )

    results = asyncio.run(exercise())
    assert results == [
        {"status": "verified"},
        {"status": "verified"},
    ]
    assert maximum_active == 1
    assert session.checks == 2


def test_v2_backup_finalize_rejected_before_finalization(monkeypatch):
    async def should_not_finalize(*args):
        pytest.fail("Legacy finalization must not run on a v2 account.")

    monkeypatch.setattr(backups, "_finalize_backup", should_not_finalize)
    session = MemorySession(protocol="v2")
    with pytest.raises(HTTPException) as error:
        asyncio.run(backups.finalize_backup(
            uuid.uuid4(), SimpleNamespace(id=uuid.uuid4()), session
        ))
    assert error.value.status_code == 409
    assert session.checks == 1
