"""Concurrent backup finalization must not exhaust one API worker's memory."""

import asyncio
import uuid
from types import SimpleNamespace

from app.routers import backups


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

    async def exercise():
        return await asyncio.gather(
            backups.finalize_backup(
                uuid.uuid4(), SimpleNamespace(id=uuid.uuid4()), object()
            ),
            backups.finalize_backup(
                uuid.uuid4(), SimpleNamespace(id=uuid.uuid4()), object()
            ),
        )

    results = asyncio.run(exercise())
    assert results == [
        {"status": "verified"},
        {"status": "verified"},
    ]
    assert maximum_active == 1
