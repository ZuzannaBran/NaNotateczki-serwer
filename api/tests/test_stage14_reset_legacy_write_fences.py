"""Legacy write endpoints must fail closed for a reset v2 account.

No database or network connection is used by these unit tests.
"""
import asyncio
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.routers import account_settings, backups, planner, study_shares


class Session:
    def __init__(self):
        self.gets = 0
        self.commits = 0

    async def execute(self, *_args, **_kwargs):
        return None

    async def get(self, *_args, **_kwargs):
        self.gets += 1
        raise AssertionError("A protected database record was accessed")

    async def commit(self):
        self.commits += 1
        raise AssertionError("A rejected legacy request committed")


async def reject(*_args):
    raise HTTPException(status_code=409, detail="v2 account")


def test_old_settings_writer_cannot_restore_reset_account(monkeypatch):
    monkeypatch.setattr(account_settings, "require_legacy_protocol", reject)
    db = Session()
    owner = SimpleNamespace(id=uuid.uuid4())
    body = account_settings.SettingsWrite(
        expectedRevision=0, value={"darkMode": True},
    )
    with pytest.raises(HTTPException) as error:
        asyncio.run(account_settings.put_account_settings(
            "appPreferences", body, owner, db,
        ))
    assert error.value.status_code == 409
    assert db.gets == 0 and db.commits == 0


def test_old_planner_writer_cannot_restore_deleted_test_entries(monkeypatch):
    monkeypatch.setattr(planner, "require_legacy_protocol", reject)

    async def locked(*_args):
        return None

    monkeypatch.setattr(planner, "lock_owner_events", locked)
    db = Session()
    owner = SimpleNamespace(id=uuid.uuid4())
    body = planner.PlannerWrite(
        modifiedAt=datetime.now(UTC), deleted=True, entry=None,
    )
    with pytest.raises(HTTPException) as error:
        asyncio.run(planner.put_entry("test-entry", body, owner, db))
    assert error.value.status_code == 409
    assert db.gets == 0 and db.commits == 0


def test_old_share_creation_cannot_recreate_test_share(monkeypatch):
    monkeypatch.setattr(study_shares, "require_legacy_protocol", reject)

    async def locked(*_args):
        return None

    monkeypatch.setattr(study_shares, "lock_owner_events", locked)
    db = Session()
    owner = SimpleNamespace(id=uuid.uuid4())
    body = study_shares.ShareCreate(
        documentUid="test-document", title="Test", password="test",
    )
    with pytest.raises(HTTPException) as error:
        asyncio.run(study_shares.create_share(body, owner, db))
    assert error.value.status_code == 409
    assert db.gets == 0 and db.commits == 0


def test_old_backup_create_cannot_write_after_reset(monkeypatch):
    monkeypatch.setattr(backups, "require_legacy_protocol", reject)
    monkeypatch.setattr(
        backups, "get_settings",
        lambda: SimpleNamespace(maximum_backup_bytes=1024 * 1024),
    )
    db = Session()
    owner = SimpleNamespace(id=uuid.uuid4())
    body = SimpleNamespace(sizeBytes=24, deviceId=uuid.uuid4())
    with pytest.raises(HTTPException) as error:
        asyncio.run(backups.create_backup(body, owner, db))
    assert error.value.status_code == 409
    assert db.gets == 0 and db.commits == 0


def test_old_backup_upload_and_finalize_cannot_write_after_reset(
    monkeypatch,
):
    monkeypatch.setattr(backups, "require_legacy_protocol", reject)

    async def pending(*_args, **_kwargs):
        return SimpleNamespace(status="pending")

    monkeypatch.setattr(backups, "owned_backup", pending)

    async def must_not_finalize(*_args):
        raise AssertionError("Backup finalized despite protocol fence")

    monkeypatch.setattr(backups, "_finalize_backup", must_not_finalize)
    db = Session()
    owner = SimpleNamespace(id=uuid.uuid4())
    backup_id = uuid.uuid4()
    with pytest.raises(HTTPException) as error:
        asyncio.run(backups.upload_content(
            backup_id, None, "checksum", owner, db,
        ))
    assert error.value.status_code == 409

    with pytest.raises(HTTPException) as error:
        asyncio.run(backups.finalize_backup(backup_id, owner, db))
    assert error.value.status_code == 409
    assert db.commits == 0
