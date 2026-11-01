"""HTTP and WebSocket contracts for read-only study shares.

These isolated tests use the real FastAPI router and a narrowly scoped
in-memory substitute for its SQLAlchemy queries. They require no PostgreSQL.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.database import database_session
from app.main import app
from app.models import StudyShare, StudyShareGrant, SyncedNotebook, User
from app.realtime_hub import RealtimeHub
from app.routers import study_shares as shares
from app.security import current_user


OWNER_ID = uuid.UUID("d1111111-1111-4111-8111-111111111111")
OTHER_ID = uuid.UUID("d2222222-2222-4222-8222-222222222222")
NOTE_UID = "study-notebook"
PASSWORD = "x"


class MemorySession:
    """Mimic only the SQL statements used by study-share endpoints."""

    def __init__(self):
        self.notebooks = [
            SyncedNotebook(
                id=uuid.uuid4(), owner_id=OWNER_ID, uid=NOTE_UID,
                content={"uid": NOTE_UID, "kind": 0, "title": "Real notebook"},
                revision=1, deleted_at=None,
            )
        ]
        self.shares = {}
        self.grants = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def scalar(self, statement):
        entity = statement.column_descriptions[0]["entity"]
        params = tuple(statement.compile().params.values())
        if entity is SyncedNotebook:
            return next(
                (note for note in self.notebooks
                 if note.owner_id in params and note.uid in params
                 and note.deleted_at is None),
                None,
            )
        if entity is StudyShare:
            return next(
                (share for share in self.shares.values()
                 if share.id in params and share.owner_id in params),
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

    async def commit(self):
        pass


@pytest.fixture
def environment(monkeypatch, tmp_path):
    db = MemorySession()
    owner = User(id=OWNER_ID, username="owner", password_hash="unused")
    principal = {"user": owner}
    app.dependency_overrides[database_session] = lambda: db
    app.dependency_overrides[current_user] = lambda: principal["user"]
    monkeypatch.setattr(
        shares, "frame_path", lambda share_id: tmp_path / f"{share_id}.png"
    )
    monkeypatch.setattr(shares, "hub", RealtimeHub())
    monkeypatch.setattr(shares, "session_factory", lambda: db)
    shares.join_limiter._entries.clear()
    shares.publish_limiter._entries.clear()
    try:
        with TestClient(app) as client:
            yield client, db, principal, tmp_path
    finally:
        app.dependency_overrides.clear()
        shares.join_limiter._entries.clear()
        shares.publish_limiter._entries.clear()


def create(client):
    return client.post("/v1/study-shares", json={
        "documentUid": NOTE_UID, "title": "Spoofed title",
        "password": PASSWORD,
    })


def join(client, share_id, password=PASSWORD):
    return client.post(f"/v1/study-shares/{share_id}/join", json={
        "password": password,
    })


def png(width=2, height=2):
    return (
        shares.PNG_MAGIC + b"\x00\x00\x00\x0dIHDR"
        + width.to_bytes(4, "big") + height.to_bytes(4, "big")
        + b"\x08\x06\x00\x00\x00" + b"\x00\x00\x00\x00"
    )


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
    assert db.notebooks[0].uid == NOTE_UID


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
        db.notebooks[0].content["kind"] = 2
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
        assert client.post("/v1/study-shares", json=body).status_code == 422
    assert not db.shares


def test_password_ticket_and_private_frame(environment):
    client, db, _, _ = environment
    share_id = create(client).json()["id"]
    assert join(client, share_id, "incorrect").status_code == 401
    accepted = join(client, share_id)
    assert accepted.status_code == 200
    ticket = accepted.json()["ticket"]
    assert accepted.json()["title"] == "Real notebook"
    assert len(ticket) >= 40
    stored = {grant.token_hash for grant in db.grants.values()}
    assert shares.token_digest(ticket) in stored
    assert ticket not in stored
    path = f"/v1/study-shares/{share_id}/frame"
    assert client.get(path).status_code == 401
    assert client.get(path, headers={
        "Authorization": "Bearer invalid",
    }).status_code == 401
    assert client.get(path, headers={
        "Authorization": f"Bearer {ticket}",
    }).status_code == 404
    frame = png()
    published = client.put(path, content=frame, headers={
        "Content-Type": "image/png",
    })
    assert published.status_code == 204, published.text
    received = client.get(path, headers={
        "Authorization": f"Bearer {ticket}",
    })
    assert received.status_code == 200
    assert received.content == frame
    assert received.headers["cache-control"] == "no-store"
    assert db.shares[uuid.UUID(share_id)].revision == 1


def test_ticket_cannot_open_another_share(environment):
    client, _, _, _ = environment
    first = create(client).json()["id"]
    second = create(client).json()["id"]
    ticket = join(client, first).json()["ticket"]
    response = client.get(f"/v1/study-shares/{second}/frame", headers={
        "Authorization": f"Bearer {ticket}",
    })
    assert response.status_code == 401


@pytest.mark.parametrize("mime,body,status", [
    ("application/json", b"{}", 415),
    ("image/png", b"not png", 413),
    ("image/png", png(5000, 20), 422),
    ("image/png", png() + b"0" * (2 * 1024 * 1024), 413),
])
def test_bad_frame_never_advances_revision(environment, mime, body, status):
    client, db, _, _ = environment
    share_id = create(client).json()["id"]
    result = client.put(
        f"/v1/study-shares/{share_id}/frame",
        content=body, headers={"Content-Type": mime},
    )
    assert result.status_code == status
    assert db.shares[uuid.UUID(share_id)].revision == 0


def test_other_account_cannot_manage_share(environment):
    client, db, principal, _ = environment
    share_id = create(client).json()["id"]
    principal["user"] = User(
        id=OTHER_ID, username="stranger", password_hash="unused"
    )
    root = f"/v1/study-shares/{share_id}"
    assert client.put(root + "/frame", content=png(), headers={
        "Content-Type": "image/png",
    }).status_code == 404
    assert client.post(root + "/extend", json={}).status_code == 404
    assert client.request("DELETE", root, content=b"").status_code == 404
    assert db.shares[uuid.UUID(share_id)].revoked_at is None


def test_extension_preserves_link_and_renews_existing_grants(environment):
    client, db, _, _ = environment
    share_id = create(client).json()["id"]
    ticket = join(client, share_id).json()["ticket"]
    root = f"/v1/study-shares/{share_id}"
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


def test_expired_share_rejects_join_and_publish(environment):
    client, db, _, _ = environment
    share_id = create(client).json()["id"]
    ticket = join(client, share_id).json()["ticket"]
    db.shares[uuid.UUID(share_id)].expires_at = (
        datetime.now(UTC) - timedelta(seconds=1)
    )
    assert join(client, share_id).status_code == 410
    root = f"/v1/study-shares/{share_id}"
    assert client.get(root + "/frame", headers={
        "Authorization": f"Bearer {ticket}",
    }).status_code in (401, 410)
    assert client.put(root + "/frame", content=png(), headers={
        "Content-Type": "image/png",
    }).status_code == 410


def test_end_revokes_guest_but_preserves_original_document(environment):
    client, db, _, folder = environment
    share_id = create(client).json()["id"]
    ticket = join(client, share_id).json()["ticket"]
    root = f"/v1/study-shares/{share_id}"
    assert client.put(root + "/frame", content=png(), headers={
        "Content-Type": "image/png",
    }).status_code == 204
    assert (folder / f"{share_id}.png").exists()
    assert client.request("DELETE", root, content=b"").status_code == 204
    assert not (folder / f"{share_id}.png").exists()
    assert db.notebooks[0].uid == NOTE_UID
    assert db.shares[uuid.UUID(share_id)].revoked_at is not None
    assert client.get(root + "/frame", headers={
        "Authorization": f"Bearer {ticket}",
    }).status_code == 410
    assert join(client, share_id).status_code == 410


def test_join_rate_limiting(environment):
    client, _, _, _ = environment
    share_id = create(client).json()["id"]
    for _ in range(8):
        assert join(client, share_id, "wrong-password").status_code == 401
    assert join(client, share_id).status_code == 429


def test_websocket_requires_exact_origin_and_ticket(environment):
    client, _, _, _ = environment
    share_id = create(client).json()["id"]
    url = f"/v1/study-shares/{share_id}/events"
    with pytest.raises(WebSocketDisconnect) as rejected:
        with client.websocket_connect(
            url, headers={"origin": "https://evil.test"}
        ):
            pass
    assert rejected.value.code == 4403
    with client.websocket_connect(
        url, headers={"origin": "https://app.example.test"}
    ) as websocket:
        websocket.send_json({"type": "authenticate", "ticket": "invalid"})
        with pytest.raises(WebSocketDisconnect) as rejected:
            websocket.receive_json()
        assert rejected.value.code == 4401


def test_websocket_delivers_new_frame_signal(environment):
    client, _, _, _ = environment
    share_id = create(client).json()["id"]
    ticket = join(client, share_id).json()["ticket"]
    url = f"/v1/study-shares/{share_id}/events"
    with client.websocket_connect(
        url, headers={"origin": "https://app.example.test"}
    ) as websocket:
        websocket.send_json({"type": "authenticate", "ticket": ticket})
        assert websocket.receive_json() == {
            "type": "authenticated", "revision": 0,
        }
        response = client.put(
            f"/v1/study-shares/{share_id}/frame",
            content=png(), headers={"Content-Type": "image/png"},
        )
        assert response.status_code == 204
        assert websocket.receive_json() == {
            "type": "frame_changed", "revision": 1,
        }


def test_websocket_receives_end_event_before_revocation(environment):
    client, _, _, _ = environment
    share_id = create(client).json()["id"]
    ticket = join(client, share_id).json()["ticket"]
    url = f"/v1/study-shares/{share_id}/events"
    with client.websocket_connect(
        url, headers={"origin": "https://app.example.test"}
    ) as websocket:
        websocket.send_json({"type": "authenticate", "ticket": ticket})
        assert websocket.receive_json()["type"] == "authenticated"
        assert client.request(
            "DELETE", f"/v1/study-shares/{share_id}", content=b""
        ).status_code == 204
        assert websocket.receive_json() == {"type": "ended"}


def test_cors_does_not_expose_ticket_to_foreign_browser(environment):
    client, _, _, _ = environment
    share_id = create(client).json()["id"]
    ticket = join(client, share_id).json()["ticket"]
    path = f"/v1/study-shares/{share_id}/frame"
    allowed = client.get(path, headers={
        "Authorization": f"Bearer {ticket}",
        "Origin": "https://app.example.test",
    })
    assert allowed.headers["access-control-allow-origin"] == (
        "https://app.example.test"
    )
    blocked = client.get(path, headers={
        "Authorization": f"Bearer {ticket}", "Origin": "https://evil.test",
    })
    assert "access-control-allow-origin" not in blocked.headers


def test_two_guests_receive_the_same_revision(environment):
    client, _, _, _ = environment
    share_id = create(client).json()["id"]
    first_ticket = join(client, share_id).json()["ticket"]
    second_ticket = join(client, share_id).json()["ticket"]
    url = f"/v1/study-shares/{share_id}/events"
    headers = {"origin": "https://app.example.test"}
    with client.websocket_connect(url, headers=headers) as first:
        first.send_json({"type": "authenticate", "ticket": first_ticket})
        assert first.receive_json()["type"] == "authenticated"
        with client.websocket_connect(url, headers=headers) as second:
            second.send_json({
                "type": "authenticate", "ticket": second_ticket,
            })
            assert second.receive_json()["type"] == "authenticated"
            response = client.put(
                f"/v1/study-shares/{share_id}/frame",
                content=png(), headers={"Content-Type": "image/png"},
            )
            assert response.status_code == 204
            assert first.receive_json() == {
                "type": "frame_changed", "revision": 1,
            }
            assert second.receive_json() == {
                "type": "frame_changed", "revision": 1,
            }


def test_reconnect_reports_current_revision_and_restores_frame(environment):
    client, _, _, _ = environment
    share_id = create(client).json()["id"]
    ticket = join(client, share_id).json()["ticket"]
    url = f"/v1/study-shares/{share_id}/events"
    headers = {"origin": "https://app.example.test"}
    with client.websocket_connect(url, headers=headers) as websocket:
        websocket.send_json({"type": "authenticate", "ticket": ticket})
        assert websocket.receive_json()["revision"] == 0
    for width in (2, 3):
        assert client.put(
            f"/v1/study-shares/{share_id}/frame",
            content=png(width=width),
            headers={"Content-Type": "image/png"},
        ).status_code == 204
    with client.websocket_connect(url, headers=headers) as websocket:
        websocket.send_json({"type": "authenticate", "ticket": ticket})
        assert websocket.receive_json() == {
            "type": "authenticated", "revision": 2,
        }
        restored = client.get(
            f"/v1/study-shares/{share_id}/frame",
            headers={"Authorization": f"Bearer {ticket}"},
        )
        assert restored.status_code == 200
        assert restored.content == png(width=3)


def test_extension_notifies_connected_guests(environment):
    client, db, _, _ = environment
    share_id = create(client).json()["id"]
    ticket = join(client, share_id).json()["ticket"]
    url = f"/v1/study-shares/{share_id}/events"
    with client.websocket_connect(
        url, headers={"origin": "https://app.example.test"}
    ) as websocket:
        websocket.send_json({"type": "authenticate", "ticket": ticket})
        assert websocket.receive_json()["type"] == "authenticated"
        db.shares[uuid.UUID(share_id)].expires_at = (
            datetime.now(UTC) + timedelta(minutes=5)
        )
        response = client.post(
            f"/v1/study-shares/{share_id}/extend", json={}
        )
        assert response.status_code == 200
        event = websocket.receive_json()
        assert event["type"] == "extended"
        assert event["expiresAt"] == response.json()["expiresAt"]
