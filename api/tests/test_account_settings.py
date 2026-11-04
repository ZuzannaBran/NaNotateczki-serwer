"""Regression tests for versioned account preferences without PostgreSQL."""

import asyncio
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.models import AccountSettings, User
from app.routers import account_settings as api


class MemorySession:
    def __init__(self):
        self.records = {}
        self.locked = False
        self.commits = 0
        self.protocols = {}
        self.protocol_checks = 0

    async def execute(self, statement, parameters=None):
        if parameters is not None:
            assert self.locked
            assert "FROM account_sync_protocol" in str(statement)
            self.protocol_checks += 1
            return SimpleNamespace(
                scalar_one_or_none=lambda: self.protocols.get(
                    parameters["owner_id"]
                ),
            )
        # A user-row lock must precede all account settings writes.
        assert "FOR UPDATE" in str(statement)
        self.locked = True

    async def get(self, model, identifier):
        assert model is AccountSettings
        assert self.locked
        return self.records.get(identifier)

    def add(self, record):
        assert self.locked
        self.records[(record.owner_id, record.category)] = record

    async def commit(self):
        self.commits += 1


def test_rejects_unknown_and_invalid_settings():
    with pytest.raises(ValueError):
        api.validate_settings("recentColors", [True, 0])
    with pytest.raises(ValueError):
        api.validate_settings("recentColors", [-1])
    with pytest.raises(ValueError):
        api.validate_settings("recentColors", [0] * 13)
    assert api.validate_settings("backupPreferences", {
        "version": 1, "idleSeconds": 15
    })["idleSeconds"] == 15
    assert api.validate_settings("backupPreferences", {
        "version": 1, "idleSeconds": 60
    })["idleSeconds"] == 60
    with pytest.raises(ValueError):
        api.validate_settings("backupPreferences", {
            "version": 1, "idleSeconds": 1
        })
    with pytest.raises(ValueError):
        api.validate_settings("appPreferences", {"refreshToken": "secret"})
    with pytest.raises(ValueError):
        api.validate_settings("editorPreferences", {"password": "secret"})
    with pytest.raises(ValueError):
        api.validate_settings("editorPreferences", {"data": "x" * 140000})
    with pytest.raises(ValidationError):
        api.SettingsWrite(expectedRevision=-1, value={})
    with pytest.raises(ValidationError):
        api.SettingsWrite(expectedRevision=1, value={},
                          unexpected="denied")


def test_account_scoped_revisioned_writes_and_conflicts(monkeypatch):
    account = User(id=uuid.uuid4(), username="account", password_hash="hash")
    other = User(id=uuid.uuid4(), username="other", password_hash="hash")
    session = MemorySession()
    notifications = []

    async def publish(target, message):
        notifications.append((target, message))

    monkeypatch.setattr(api.hub, "publish", publish)

    async def run():
        first = await api.put_account_settings(
            "backupPreferences",
            api.SettingsWrite(
                expectedRevision=0,
                value={"version": 1, "idleSeconds": 900},
            ),
            account,
            session,
        )
        assert first["revision"] == 1
        assert first["value"]["idleSeconds"] == 900

        with pytest.raises(HTTPException) as conflict:
            await api.put_account_settings(
                "backupPreferences",
                api.SettingsWrite(
                    expectedRevision=0,
                    value={"version": 1, "idleSeconds": 30},
                ),
                account,
                session,
            )
        assert conflict.value.status_code == 409
        assert conflict.value.detail["revision"] == 1
        assert conflict.value.detail["value"]["idleSeconds"] == 900

        second = await api.put_account_settings(
            "backupPreferences",
            api.SettingsWrite(
                expectedRevision=1,
                value={"version": 1, "idleSeconds": 300},
            ),
            account,
            session,
        )
        assert second["revision"] == 2

        separate = await api.put_account_settings(
            "backupPreferences",
            api.SettingsWrite(
                expectedRevision=0,
                value={"version": 1, "idleSeconds": 30},
            ),
            other,
            session,
        )
        assert separate["revision"] == 1

    asyncio.run(run())
    assert session.commits == 3
    assert session.records[(account.id, "backupPreferences")].revision == 2
    assert session.records[(other.id, "backupPreferences")].revision == 1
    assert len(notifications) == 3
    assert notifications[0][0] == account.id
    assert notifications[2][0] == other.id


def test_conflicting_settings_require_no_automatic_overwrite(monkeypatch):
    account = User(id=uuid.uuid4(), username="account", password_hash="hash")
    session = MemorySession()
    session.records[(account.id, "recentColors")] = AccountSettings(
        owner_id=account.id, category="recentColors", revision=7,
        content=[4278190080],
    )

    async def no_publish(*args, **kwargs):
        pytest.fail("Conflict must not broadcast a change.")

    monkeypatch.setattr(api.hub, "publish", no_publish)
    with pytest.raises(HTTPException) as conflict:
        asyncio.run(api.put_account_settings(
            "recentColors",
            api.SettingsWrite(expectedRevision=3, value=[4294967295]),
            account,
            session,
        ))
    assert conflict.value.status_code == 409
    assert session.commits == 0
    assert session.records[(account.id, "recentColors")].content == [4278190080]



def test_read_settings_is_scoped_to_authenticated_owner():
    first = uuid.uuid4()
    second = uuid.uuid4()
    records = [
        AccountSettings(owner_id=first, category="appPreferences",
                        revision=3, content={"darkMode": True}),
        AccountSettings(owner_id=second, category="appPreferences",
                        revision=8, content={"darkMode": False}),
    ]

    class FakeRows:
        def __init__(self, matching):
            self.matching = matching

        def scalars(self):
            return iter(self.matching)

    class ReadSession:
        async def execute(self, statement):
            parameters = statement.compile().params.values()
            ids = [value for value in parameters if isinstance(value, uuid.UUID)]
            assert len(ids) == 1
            return FakeRows([row for row in records if row.owner_id == ids[0]])

    result = asyncio.run(api.get_account_settings(
        User(id=first, username="user", password_hash="hash"),
        ReadSession(),
    ))
    assert result == {
        "settings": {
            "appPreferences": {
                "revision": 3,
                "value": {"darkMode": True},
            },
        },
    }


def test_library_state_category_has_postgresql_migration():
    from pathlib import Path

    migrations = Path(__file__).resolve().parents[2] / "migrations"
    original = (migrations / "0007_account_settings.sql").read_text()
    upgrade = (migrations / "0009_account_settings_library_state.sql").read_text()
    assert "libraryState" not in original
    assert "DROP CONSTRAINT account_settings_category_check" in upgrade
    assert "ADD CONSTRAINT account_settings_category_check" in upgrade
    assert "'libraryState'" in upgrade
    assert "libraryState" in api.Category.__args__
    assert api.validate_settings("libraryState", {
        "folders": ["Notes"],
        "covers": {},
        "plannerTasks": {},
        "recentCoverColors": [],
    })["folders"] == ["Notes"]


def test_v2_account_cannot_write_legacy_account_settings(monkeypatch):
    account = User(id=uuid.uuid4(), username="v2", password_hash="hash")
    session = MemorySession()
    session.protocols[account.id] = "v2"

    async def no_publish(*args, **kwargs):
        pytest.fail("Blocked v1 write must not publish.")

    monkeypatch.setattr(api.hub, "publish", no_publish)
    with pytest.raises(HTTPException) as error:
        asyncio.run(api.put_account_settings(
            "backupPreferences",
            api.SettingsWrite(
                expectedRevision=0,
                value={"version": 1, "idleSeconds": 30},
            ),
            account,
            session,
        ))
    assert error.value.status_code == 409
    assert session.protocol_checks == 1
    assert session.records == {}
    assert session.commits == 0
