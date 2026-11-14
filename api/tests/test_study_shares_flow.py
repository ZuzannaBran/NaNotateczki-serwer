"""HTTP and WebSocket contracts for editable study shares.

These isolated tests use the real FastAPI router and a narrowly scoped
in-memory substitute for its SQLAlchemy queries. They require no PostgreSQL.
"""

import copy
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.database import database_session
from app.main import app
from app.models import (
    DocumentState, StudyShare, StudyShareGrant, User,
)
from app.realtime_hub import RealtimeHub
from app.routers import study_shares as shares
from app.security import current_user
from app.settings import get_settings


OWNER_ID = uuid.UUID("d1111111-1111-4111-8111-111111111111")
OTHER_ID = uuid.UUID("d2222222-2222-4222-8222-222222222222")
NOTE_UID = "study-notebook"
PASSWORD = "x"
APP_ORIGIN = str(get_settings().app_origin).rstrip("/")


class MemorySession:
    """Mimic only the SQL statements used by study-share endpoints."""

    def __init__(self):
        self.notebooks = [
            DocumentState(
                owner_id=OWNER_ID, document_uid=NOTE_UID,
                content={"properties": {"kindIndex": 0, "title": "Real notebook"}},
                revision=1, deleted_at=None,
            )
        ]
        self.shares = {}
        self.grants = {}
        self.events = []
        self.history = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def scalar(self, statement):
        entity = statement.column_descriptions[0]["entity"]
        params = tuple(statement.compile().params.values())
        if entity is DocumentState:
            return next(
                (note for note in self.notebooks
                 if note.owner_id in params and note.document_uid in params
                 and note.deleted_at is None),
                None,
            )
        if entity is StudyShare:
            # Owner lifecycle calls must still find expired/revoked sessions.
            if any(share.id in params for share in self.shares.values()):
                return next(
                    (share for share in self.shares.values()
                     if share.id in params
                     and (share.owner_id in params or len(params) == 1)),
                    None,
                )
            return next(
                (share for share in self.shares.values()
                 if share.owner_id in params
                 and share.document_uid in params
                 and share.revoked_at is None
                 and share.expires_at > datetime.now(UTC)),
                None,
            )
        if entity is StudyShareGrant:
            return next(
                (grant for grant in self.grants.values()
                 if grant.share_id in params and grant.token_hash in params
                 and grant.expires_at > datetime.now(UTC)),
                None,
            )
        raise AssertionError(f"Unexpected SELECT on {entity}")

    async def get(self, model, identifier):
        if model is StudyShare:
            return self.shares.get(identifier)
        raise AssertionError(f"Unexpected GET on {model}")

    def add(self, model):
        if isinstance(model, StudyShare):
            self.shares[model.id] = model
            for note in self.notebooks:
                if note.owner_id == model.owner_id and note.document_uid == model.document_uid:
                    self.history[(note.owner_id, note.document_uid, note.revision)] = (
                        copy.deepcopy(note.content)
                    )
        elif isinstance(model, StudyShareGrant):
            self.grants[model.id] = model
        else:
            raise AssertionError(f"Unexpected INSERT on {type(model)}")

    async def execute(self, statement):
        values = statement.compile().params
        assert statement.table.name == "study_share_grants"
        for grant in self.grants.values():
            if grant.share_id in values.values():
                grant.expires_at = values["expires_at"]

    async def flush(self):
        pass

    async def commit(self):
        for note in self.notebooks:
            if note.content is not None:
                self.history[(note.owner_id, note.document_uid, note.revision)] = (
                    copy.deepcopy(note.content)
                )


@pytest.fixture
def environment(monkeypatch, tmp_path):
    db = MemorySession()
    owner = User(id=OWNER_ID, username="owner", password_hash="unused")
    principal = {"user": owner}
    app.dependency_overrides[database_session] = lambda: db
    app.dependency_overrides[current_user] = lambda: principal["user"]
    monkeypatch.setattr(shares, "hub", RealtimeHub())
    monkeypatch.setattr(shares, "session_factory", lambda: db)
    async def no_advisory_lock(*args):
        return None
    monkeypatch.setattr(shares, "lock_owner_events", no_advisory_lock)
    monkeypatch.setattr(shares, "require_v2_protocol", no_advisory_lock)
    shares.join_limiter._entries.clear()
    try:
        with TestClient(app) as client:
            yield client, db, principal, tmp_path
    finally:
        app.dependency_overrides.clear()
        shares.join_limiter._entries.clear()


def create(client):
    return client.post("/v2/study-shares", json={
        "documentUid": NOTE_UID, "title": "Spoofed title",
        "password": PASSWORD,
    })


def join(client, share_id, password=PASSWORD):
    return client.post(f"/v2/study-shares/{share_id}/join", json={
        "password": password,
    })


def test_create_uses_cloud_owner_and_hashes_password(environment):
    client, db, _, _ = environment
    result = create(client)
    assert result.status_code == 201, result.text
    share = db.shares[uuid.UUID(result.json()["id"])]
    assert share.owner_id == OWNER_ID
    assert share.document_uid == NOTE_UID
    assert share.title == "Real notebook"
    assert share.password_hash != PASSWORD
    assert shares.verify_password(PASSWORD, share.password_hash)
    remaining = share.expires_at - datetime.now(UTC)
    assert timedelta(hours=3, minutes=59) <= remaining <= timedelta(hours=4)
    assert db.notebooks[0].document_uid == NOTE_UID


@pytest.mark.parametrize("case,status", [
    ("missing", 409), ("foreign", 409), ("deleted", 409),
    ("planner", 422),
])
def test_cannot_share_unsynced_foreign_deleted_or_planner(
    environment, case, status,
):
    client, db, _, _ = environment
    if case == "missing":
        db.notebooks.clear()
    elif case == "foreign":
        db.notebooks[0].owner_id = OTHER_ID
    elif case == "deleted":
        db.notebooks[0].deleted_at = datetime.now(UTC)
    else:
        db.notebooks[0].content["properties"]["kindIndex"] = 2
    response = create(client)
    assert response.status_code == status
    assert not db.shares


def test_create_input_validation(environment):
    client, db, _, _ = environment
    for body in [
        {"documentUid": NOTE_UID, "title": "t", "password": ""},
        {"documentUid": "", "title": "t", "password": PASSWORD},
        {"documentUid": NOTE_UID, "title": "", "password": PASSWORD},
    ]:
        assert client.post("/v2/study-shares", json=body).status_code == 422
    assert not db.shares


def test_other_account_cannot_manage_share(environment):
    client, db, principal, _ = environment
    share_id = create(client).json()["id"]
    principal["user"] = User(
        id=OTHER_ID, username="stranger", password_hash="unused"
    )
    root = f"/v2/study-shares/{share_id}"
    assert client.put(root + "/appearance", json={"darkMode": True,
        "backgroundStyle": 1, "backgroundSpacing": 24}).status_code == 404
    assert client.post(root + "/extend", json={}).status_code == 404
    assert client.request("DELETE", root, content=b"").status_code == 404
    assert db.shares[uuid.UUID(share_id)].revoked_at is None


def test_extension_preserves_link_and_renews_existing_grants(environment):
    client, db, _, _ = environment
    share_id = create(client).json()["id"]
    ticket = join(client, share_id).json()["ticket"]
    root = f"/v2/study-shares/{share_id}"
    assert client.post(root + "/extend", json={}).status_code == 409
    share = db.shares[uuid.UUID(share_id)]
    original = share.expires_at
    share.expires_at = datetime.now(UTC) + timedelta(minutes=4)
    response = client.post(root + "/extend", json={})
    assert response.status_code == 200, response.text
    assert share.expires_at > original
    assert all(g.expires_at == share.expires_at for g in db.grants.values())
    assert shares.token_digest(ticket) in {
        grant.token_hash for grant in db.grants.values()
    }
    assert share.id == uuid.UUID(share_id)


def test_join_rate_limiting(environment):
    client, _, _, _ = environment
    share_id = create(client).json()["id"]
    for _ in range(8):
        assert join(client, share_id, "wrong-password").status_code == 401
    assert join(client, share_id).status_code == 429


def test_websocket_requires_exact_origin_and_ticket(environment):
    client, _, _, _ = environment
    share_id = create(client).json()["id"]
    url = f"/v2/study-shares/{share_id}/events"
    with pytest.raises(WebSocketDisconnect) as rejected:
        with client.websocket_connect(
            url, headers={"origin": "https://evil.test"}
        ):
            pass
    assert rejected.value.code == 4403
    with client.websocket_connect(
        url, headers={"origin": APP_ORIGIN}
    ) as websocket:
        websocket.send_json({"type": "authenticate", "ticket": "invalid"})
        with pytest.raises(WebSocketDisconnect) as rejected:
            websocket.receive_json()
        assert rejected.value.code == 4401


def test_native_websocket_requires_ticket_without_origin(environment):
    client, _, _, _ = environment
    share_id = create(client).json()["id"]
    ticket = join(client, share_id).json()["ticket"]
    url = f"/v2/study-shares/{share_id}/events"

    with client.websocket_connect(url) as websocket:
        websocket.send_json({"type": "authenticate", "ticket": ticket})
        assert websocket.receive_json() == {
            "type": "authenticated", "revision": 0,
        }

    with client.websocket_connect(url) as websocket:
        websocket.send_json({"type": "authenticate", "ticket": "invalid"})
        with pytest.raises(WebSocketDisconnect) as rejected:
            websocket.receive_json()
        assert rejected.value.code == 4401


def test_guest_subscribes_before_authorization_to_catch_revocation(
    environment, monkeypatch,
):
    client, _, _, _ = environment
    share_id = create(client).json()["id"]
    ticket = join(client, share_id).json()["ticket"]
    order = []
    original_subscribe = shares.hub.subscribe
    original_authorize = shares.guest_share

    async def subscribe(*args, **kwargs):
        order.append("subscribe")
        return await original_subscribe(*args, **kwargs)

    async def authorize(*args, **kwargs):
        order.append("authorize")
        return await original_authorize(*args, **kwargs)

    monkeypatch.setattr(shares.hub, "subscribe", subscribe)
    monkeypatch.setattr(shares, "guest_share", authorize)
    with client.websocket_connect(
        f"/v2/study-shares/{share_id}/events",
        headers={"origin": APP_ORIGIN},
    ) as websocket:
        websocket.send_json({"type": "authenticate", "ticket": ticket})
        assert websocket.receive_json()["type"] == "authenticated"
        assert order[:2] == ["subscribe", "authorize"]


def test_websocket_receives_end_event_before_revocation(environment):
    client, _, _, _ = environment
    share_id = create(client).json()["id"]
    ticket = join(client, share_id).json()["ticket"]
    url = f"/v2/study-shares/{share_id}/events"
    with client.websocket_connect(
        url, headers={"origin": APP_ORIGIN}
    ) as websocket:
        websocket.send_json({"type": "authenticate", "ticket": ticket})
        assert websocket.receive_json()["type"] == "authenticated"
        assert client.request(
            "DELETE", f"/v2/study-shares/{share_id}", content=b""
        ).status_code == 204
        assert websocket.receive_json() == {"type": "ended"}


def test_cors_does_not_expose_ticket_to_foreign_browser(environment):
    client, _, _, _ = environment
    share_id = create(client).json()["id"]
    ticket = join(client, share_id).json()["ticket"]
    path = f"/v2/study-shares/{share_id}/appearance"
    allowed = client.get(path, headers={
        "Authorization": f"Bearer {ticket}",
        "Origin": APP_ORIGIN,
    })
    assert allowed.headers["access-control-allow-origin"] == (
        APP_ORIGIN
    )
    blocked = client.get(path, headers={
        "Authorization": f"Bearer {ticket}", "Origin": "https://evil.test",
    })
    assert "access-control-allow-origin" not in blocked.headers


def test_host_appearance_updates_without_changing_document_revision(
    environment,
):
    client, db, principal, _ = environment
    share_id = create(client).json()["id"]
    ticket = join(client, share_id).json()["ticket"]
    root = f"/v2/study-shares/{share_id}"
    headers = {"Authorization": f"Bearer {ticket}"}
    original = client.get(root + "/appearance", headers=headers)
    assert original.status_code == 200
    initial_revision = db.shares[uuid.UUID(share_id)].revision
    appearance = {
        "darkMode": True,
        "backgroundStyle": 2,
        "backgroundSpacing": 24.0,
    }
    assert client.put(root + "/appearance", json=appearance).status_code == 200
    received = client.get(root + "/appearance", headers=headers)
    assert received.status_code == 200
    assert received.json()["appearance"] == appearance
    assert db.shares[uuid.UUID(share_id)].revision == initial_revision
    assert client.put(root + "/appearance", json=appearance).status_code == 200
    for invalid in [
        {**appearance, "backgroundStyle": 4},
        {**appearance, "backgroundSpacing": 2},
        {**appearance, "darkMode": "invalid"},
    ]:
        assert client.put(root + "/appearance", json=invalid).status_code == 422
    principal["user"] = User(
        id=OTHER_ID, username="other", password_hash="unused"
    )
    assert client.put(root + "/appearance", json=appearance).status_code == 404


def test_extension_notifies_connected_guests(environment):
    client, db, _, _ = environment
    share_id = create(client).json()["id"]
    ticket = join(client, share_id).json()["ticket"]
    url = f"/v2/study-shares/{share_id}/events"
    with client.websocket_connect(
        url, headers={"origin": APP_ORIGIN}
    ) as websocket:
        websocket.send_json({"type": "authenticate", "ticket": ticket})
        assert websocket.receive_json()["type"] == "authenticated"
        db.shares[uuid.UUID(share_id)].expires_at = (
            datetime.now(UTC) + timedelta(minutes=5)
        )
        response = client.post(
            f"/v2/study-shares/{share_id}/extend", json={}
        )
        assert response.status_code == 200
        event = websocket.receive_json()
        assert event["type"] == "extended"
        assert event["expiresAt"] == response.json()["expiresAt"]


def test_cannot_open_two_active_shares_for_same_document(environment):
    client, db, _, _ = environment
    first = create(client)
    assert first.status_code == 201, first.text
    second = create(client)
    assert second.status_code == 409, second.text
    assert len(db.shares) == 1


def test_v2_share_creation_is_fenced_to_committed_document(
    environment, monkeypatch,
):
    client, db, _, _ = environment

    async def approved_v2(*args):
        return None

    monkeypatch.setattr(shares, "require_v2_protocol", approved_v2)
    original_scalar = db.scalar
    state = DocumentState(
        owner_id=OWNER_ID,
        document_uid=NOTE_UID,
        revision=2,
        content={
            "properties": {
                "kindIndex": 0,
                "title": "Committed operational notebook",
            },
            "pages": {},
        },
        deleted_at=None,
    )

    async def v2_scalar(statement):
        if statement.column_descriptions[0]["entity"] is DocumentState:
            return state
        return await original_scalar(statement)

    monkeypatch.setattr(db, "scalar", v2_scalar)
    path = "/v2/study-shares"
    body = {
        "documentUid": NOTE_UID, "title": "Spoofed",
        "password": PASSWORD,
    }
    result = client.post(path, json=body)
    assert result.status_code == 201, result.text
    share_id = result.json()["id"]
    share = db.shares[uuid.UUID(share_id)]
    assert share.title == "Committed operational notebook"
    assert share.edit_clock["_protocol"] == "v2"
    assert join(client, share_id).json()["protocol"] == "v2"
    assert join(client, share_id).json()["documentUid"] == NOTE_UID
    ticket = join(client, share_id).json()["ticket"]
    legacy_read = client.get(
        f"/v1/study-shares/{share_id}/document",
        headers={"Authorization": f"Bearer {ticket}"},
    )
    assert legacy_read.status_code == 404
    assert client.post(path, json=body).status_code == 409
    assert client.delete(f"/v2/study-shares/{share_id}").status_code == 204
    assert share.revoked_at is not None


@pytest.mark.parametrize("mode,expected", [
    ("missing", 409),
    ("deleted", 409),
    ("planner", 422),
    ("invalid_kind", 422),
    ("invalid_properties", 409),
])
def test_v2_share_refuses_uncommitted_or_unsupported_document(
    environment, monkeypatch, mode, expected,
):
    client, db, _, _ = environment

    async def approved_v2(*args):
        return None

    monkeypatch.setattr(shares, "require_v2_protocol", approved_v2)
    original_scalar = db.scalar
    state = DocumentState(
        owner_id=OWNER_ID,
        document_uid=NOTE_UID,
        revision=2,
        content={"properties": {"kindIndex": 0, "title": "Safe"}},
        deleted_at=None,
    )
    if mode == "deleted":
        state.deleted_at = datetime.now(UTC)
    elif mode == "planner":
        state.content["properties"]["kindIndex"] = 2
    elif mode == "invalid_kind":
        state.content["properties"]["kindIndex"] = True
    elif mode == "invalid_properties":
        state.content["properties"] = []

    async def v2_scalar(statement):
        if statement.column_descriptions[0]["entity"] is DocumentState:
            return None if mode == "missing" or state.deleted_at else state
        return await original_scalar(statement)

    monkeypatch.setattr(db, "scalar", v2_scalar)
    result = client.post("/v2/study-shares", json={
        "documentUid": NOTE_UID, "title": "Safe", "password": PASSWORD,
    })
    assert result.status_code == expected, result.text
    assert db.shares == {}
