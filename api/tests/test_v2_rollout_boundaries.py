"""Paused and unlisted accounts cannot mutate auxiliary v2 stores."""
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.database import database_session
from app.main import app
from app.security import current_user
from app.settings import get_settings


OWNER = uuid.UUID("11111111-2222-4333-8444-555555555555")
SHARE = uuid.UUID("22222222-2222-4333-8444-555555555555")
BACKUP = uuid.UUID("33333333-2222-4333-8444-555555555555")


class NoWriteSession:
    async def get(self, model, key):
        if key == SHARE:
            return SimpleNamespace(
                owner_id=OWNER, edit_clock={"_protocol": "v2"},
                revoked_at=None,
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
        raise AssertionError("Unexpected database access before rollout gate")

    async def execute(self, *args, **kwargs):
        raise AssertionError("Database accessed before rollout gate")

    async def scalar(self, *args, **kwargs):
        raise AssertionError("Database accessed before rollout gate")

    async def commit(self):
        raise AssertionError("Unlisted account must never commit")


@pytest.fixture(params=["disabled", "canary"])
def blocked_client(request, monkeypatch):
    monkeypatch.setenv("OPERATIONS_V2_WRITE_MODE", request.param)
    monkeypatch.setenv("OPERATIONS_V2_CANARY_OWNER_IDS", str(uuid.uuid4()))
    get_settings.cache_clear()
    previous = dict(app.dependency_overrides)
    app.dependency_overrides[current_user] = lambda: SimpleNamespace(id=OWNER)
    app.dependency_overrides[database_session] = lambda: NoWriteSession()
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            yield client
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)
        get_settings.cache_clear()


@pytest.mark.parametrize(("method", "path", "body"), [
    ("PUT", "/v2/account-settings/appPreferences",
     {"expectedRevision": 0, "value": {"darkMode": True}}),
    ("POST", "/v2/backups", {
        "deviceId": str(BACKUP), "sizeBytes": 1,
        "sha256": "0" * 64, "formatVersion": 3,
    }),
    ("PUT", f"/v2/backups/{BACKUP}/content", {}),
    ("POST", f"/v2/backups/{BACKUP}/finalize", {}),
    ("POST", "/v2/study-shares",
     {"documentUid": "doc", "title": "Study", "password": "secret"}),
    ("PUT", f"/v2/study-shares/{SHARE}/appearance",
     {"darkMode": True, "backgroundStyle": 0, "backgroundSpacing": 24}),
    ("POST", f"/v2/study-shares/{SHARE}/extend", {}),
    ("POST", f"/v2/study-shares/{SHARE}/join", {"password": "secret"}),
])
def test_auxiliary_v2_writes_obey_the_account_rollout_gate(
    blocked_client, method, path, body,
):
    response = blocked_client.request(
        method, path, json=body, headers={"X-Content-SHA256": "0" * 64},
    )
    assert response.status_code == 403, response.text
    assert "not enabled" in response.json()["detail"]


def test_pausing_writes_does_not_prevent_share_revocation(blocked_client):
    share = SimpleNamespace(edit_clock={"_protocol": "v2"}, revoked_at=None)

    class RevocationSession:
        committed = False

        async def scalar(self, query):
            return share

        async def commit(self):
            self.committed = True

    session = RevocationSession()
    app.dependency_overrides[database_session] = lambda: session
    response = blocked_client.delete(f"/v2/study-shares/{SHARE}")
    assert response.status_code == 204
    assert session.committed
    assert share.revoked_at is not None
