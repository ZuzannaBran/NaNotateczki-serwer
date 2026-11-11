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
    ChangeEvent, DocumentState, StudyShare, StudyShareGrant,
    SyncedNotebook, User,
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
            SyncedNotebook(
                id=uuid.uuid4(), owner_id=OWNER_ID, uid=NOTE_UID,
                content={"uid": NOTE_UID, "kind": 0, "title": "Real notebook"},
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
        if entity is SyncedNotebook:
            return next(
                (note for note in self.notebooks
                 if note.owner_id in params and note.uid in params
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
        if entity is ChangeEvent:
            return next((
                copy.deepcopy(snapshot)
                for (owner, uid, revision), snapshot in self.history.items()
                if owner in params and uid in params and revision in params
            ), None)
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
                if note.owner_id == model.owner_id and note.uid == model.document_uid:
                    self.history[(note.owner_id, note.uid, note.revision)] = (
                        copy.deepcopy(note.content)
                    )
        elif isinstance(model, StudyShareGrant):
            self.grants[model.id] = model
        elif isinstance(model, ChangeEvent):
            model.id = len(self.events) + 1
            self.events.append(model)
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
                self.history[(note.owner_id, note.uid, note.revision)] = (
                    copy.deepcopy(note.content)
                )


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
    async def no_advisory_lock(*args):
        return None
    monkeypatch.setattr(shares, "lock_owner_events", no_advisory_lock)
    monkeypatch.setattr(shares, "require_legacy_protocol", no_advisory_lock)
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
    client, db, _, _ = environment
    first = create(client).json()["id"]
    db.notebooks.append(SyncedNotebook(
        id=uuid.uuid4(), owner_id=OWNER_ID, uid="other-notebook",
        content={"uid": "other-notebook", "kind": 0, "title": "Other"},
        revision=1, deleted_at=None,
    ))
    second_response = client.post("/v1/study-shares", json={
        "documentUid": "other-notebook", "title": "Other",
        "password": PASSWORD,
    })
    assert second_response.status_code == 201
    second = second_response.json()["id"]
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


def test_chunked_delete_without_length_cannot_end_share(environment):
    client, db, _, _ = environment
    share_id = create(client).json()["id"]
    root = f"/v1/study-shares/{share_id}"
    response = client.request(
        "DELETE", root, content=b"",
        headers={"Transfer-Encoding": "chunked"},
    )
    assert response.status_code == 411
    assert db.shares[uuid.UUID(share_id)].revoked_at is None
    assert client.request("DELETE", root, content=b"").status_code == 204


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
    url = f"/v1/study-shares/{share_id}/events"

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
        f"/v1/study-shares/{share_id}/events",
        headers={"origin": APP_ORIGIN},
    ) as websocket:
        websocket.send_json({"type": "authenticate", "ticket": ticket})
        assert websocket.receive_json()["type"] == "authenticated"
        assert order[:2] == ["subscribe", "authorize"]


def test_websocket_delivers_new_frame_signal(environment):
    client, _, _, _ = environment
    share_id = create(client).json()["id"]
    ticket = join(client, share_id).json()["ticket"]
    url = f"/v1/study-shares/{share_id}/events"
    with client.websocket_connect(
        url, headers={"origin": APP_ORIGIN}
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
        url, headers={"origin": APP_ORIGIN}
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
    root = f"/v1/study-shares/{share_id}"
    headers = {"Authorization": f"Bearer {ticket}"}
    original = client.get(root + "/document", headers=headers)
    assert original.status_code == 200
    initial_revision = original.json()["revision"]
    appearance = {
        "darkMode": True,
        "backgroundStyle": 2,
        "backgroundSpacing": 24.0,
    }
    assert client.put(root + "/appearance", json=appearance).status_code == 200
    received = client.get(root + "/document", headers=headers)
    assert received.status_code == 200
    assert received.json()["appearance"] == appearance
    assert received.json()["revision"] == initial_revision
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


def test_two_guests_receive_the_same_revision(environment):
    client, _, _, _ = environment
    share_id = create(client).json()["id"]
    first_ticket = join(client, share_id).json()["ticket"]
    second_ticket = join(client, share_id).json()["ticket"]
    url = f"/v1/study-shares/{share_id}/events"
    headers = {"origin": APP_ORIGIN}
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
    headers = {"origin": APP_ORIGIN}
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
        url, headers={"origin": APP_ORIGIN}
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


def _editable_notebook():
    return {
        "uid": NOTE_UID,
        "title": "Real notebook",
        "kind": 0,
        "folder": "Studies",
        "createdAt": "2026-11-01T11:00:00+00:00",
        "updatedAt": "2026-11-01T12:00:00+00:00",
        "pages": [{
            "id": "page-1",
            "title": "Page 1",
            "isBookmarked": False,
            "inkStrokes": [],
            "textBlocks": [],
            "imageBlocks": [],
        }],
    }


def test_guest_can_edit_original_and_is_revoked_on_end(environment):
    client, db, _, _ = environment
    original = _editable_notebook()
    db.notebooks[0].content = original
    share_id = create(client).json()["id"]
    url = f"/v1/study-shares/{share_id}/document"

    assert client.get(url).status_code == 401
    ticket = join(client, share_id).json()["ticket"]
    bearer = {"Authorization": f"Bearer {ticket}"}
    before = client.get(url, headers=bearer)
    assert before.status_code == 200, before.text
    assert before.json()["notebook"] == original

    import copy
    edited = copy.deepcopy(original)
    edited["pages"][0]["inkStrokes"].append({
        "id": "stroke-1", "points": [{"x": 2, "y": 4}],
    })
    response = client.put(url, headers=bearer, json={
        "baseRevision": 1, "baseNotebook": original,
        "notebook": edited,
    })
    assert response.status_code == 200, response.text
    assert db.notebooks[0].content["pages"][0]["inkStrokes"][0]["id"] == "stroke-1"
    assert db.notebooks[0].content["folder"] == "Studies"
    assert db.notebooks[0].revision == 2
    assert len(db.events) == 1

    assert client.request(
        "DELETE", f"/v1/study-shares/{share_id}", content=b""
    ).status_code == 204
    assert client.get(url, headers=bearer).status_code == 410
    assert client.put(url, headers=bearer, json={
        "baseRevision": 2, "baseNotebook": edited, "notebook": edited,
    }).status_code == 410
    assert db.notebooks[0].content["uid"] == NOTE_UID


def test_merge_preserves_new_stroke_layer_order():
    base = _editable_notebook()
    edited = copy.deepcopy(base)
    edited["pages"][0]["inkStrokes"] = [
        {"id": "z-first", "points": [{"x": 1, "y": 1}]},
        {"id": "a-second", "points": [{"x": 2, "y": 2}]},
    ]
    merged = shares.merge_share_document(base, edited, base)
    assert [stroke["id"] for stroke in merged["pages"][0]["inkStrokes"]] == [
        "z-first", "a-second",
    ]


def test_guests_merge_independent_objects_without_lost_writes(environment):
    import copy
    client, db, _, _ = environment
    original = _editable_notebook()
    db.notebooks[0].content = original
    share_id = create(client).json()["id"]
    url = f"/v1/study-shares/{share_id}/document"
    a = join(client, share_id).json()["ticket"]
    b = join(client, share_id).json()["ticket"]

    def add(stroke_id):
        candidate = copy.deepcopy(original)
        candidate["pages"][0]["inkStrokes"] = [{"id": stroke_id}]
        return candidate

    for ticket, stroke in [(a, "a"), (b, "b")]:
        result = client.put(url, headers={
            "Authorization": f"Bearer {ticket}",
        }, json={
            "baseRevision": 1, "baseNotebook": original,
            "notebook": add(stroke),
        })
        assert result.status_code == 200, result.text
    ids = {ink["id"] for ink in db.notebooks[0].content["pages"][0]["inkStrokes"]}
    assert ids == {"a", "b"}
    assert db.notebooks[0].revision == 3


def test_guest_cannot_change_original_location_or_owner_fields(environment):
    import copy
    client, db, _, _ = environment
    original = _editable_notebook()
    db.notebooks[0].content = original
    share_id = create(client).json()["id"]
    ticket = join(client, share_id).json()["ticket"]
    for key, value in [("uid", "other"), ("folder", "Other"),
                       ("kind", 1), ("title", "Changed")]:
        edited = copy.deepcopy(original)
        edited[key] = value
        result = client.put(
            f"/v1/study-shares/{share_id}/document",
            headers={"Authorization": f"Bearer {ticket}"},
            json={
                "baseRevision": 1, "baseNotebook": original,
                "notebook": edited,
            },
        )
        assert result.status_code == 409, result.text
    assert db.notebooks[0].revision == 1


def test_guest_changes_to_same_object_are_not_silently_overwritten(environment):
    import copy
    client, db, _, _ = environment
    base = _editable_notebook()
    base["pages"][0]["textBlocks"] = [{"id": "text", "text": "original"}]
    db.notebooks[0].content = base
    share_id = create(client).json()["id"]
    token = join(client, share_id).json()["ticket"]
    url = f"/v1/study-shares/{share_id}/document"
    first = copy.deepcopy(base)
    first["pages"][0]["textBlocks"][0]["text"] = "A"
    second = copy.deepcopy(base)
    second["pages"][0]["textBlocks"][0]["text"] = "B"
    for doc, code in [(first, 200), (second, 200)]:
        response = client.put(url, headers={
            "Authorization": f"Bearer {token}",
        }, json={
            "baseRevision": 1, "baseNotebook": base, "notebook": doc,
        })
        assert response.status_code == code, response.text
    assert db.notebooks[0].content["pages"][0]["textBlocks"][0]["text"] == "A"


def test_server_arrival_time_chooses_first_write_then_larger_tie():
    import copy
    from app.routers.study_shares import merge_share_document

    base = _editable_notebook()
    base["pages"][0]["textBlocks"] = [{"id": "text-1", "text": "original"}]
    first = copy.deepcopy(base)
    first["pages"][0]["textBlocks"][0]["text"] = "first"
    second = copy.deepcopy(base)
    second["pages"][0]["textBlocks"][0]["text"] = "second-longer"
    t = datetime(2026, 11, 1, 12, 0, 0, 100, tzinfo=UTC)
    clock = {}

    saved = merge_share_document(base, first, base, clock, t)
    assert saved["pages"][0]["textBlocks"][0]["text"] == "first"

    later = merge_share_document(
        base, second, saved, clock, t + timedelta(microseconds=1)
    )
    assert later["pages"][0]["textBlocks"][0]["text"] == "first"

    tied = merge_share_document(base, second, saved, clock, t)
    assert tied["pages"][0]["textBlocks"][0]["text"] == "second-longer"

    older = merge_share_document(
        base, first, tied, clock, t - timedelta(microseconds=1)
    )
    assert older["pages"][0]["textBlocks"][0]["text"] == "first"


def test_caller_clock_cannot_choose_conflict_winner():
    from app.routers.study_shares import merge_share_document

    base = _editable_notebook()
    assert "edit_clock" not in base
    assert "receivedAt" not in base
    result = merge_share_document(base, base, base, {}, datetime.now(UTC))
    assert result["pages"] == base["pages"]


def test_owner_change_feed_contains_merged_guest_strokes(monkeypatch):
    """Every event must contain the committed merge, not the stale request."""
    import asyncio
    import copy
    from types import SimpleNamespace

    from starlette.requests import Request

    from app.routers import sync
    from app.schemas import SyncWrite

    original = _editable_notebook()
    guest = copy.deepcopy(original)
    guest["pages"][0]["inkStrokes"] = [{"id": "guest"}]
    owner_edit = copy.deepcopy(original)
    owner_edit["pages"][0]["inkStrokes"] = [{"id": "owner"}]
    record = SyncedNotebook(
        id=uuid.uuid4(), owner_id=OWNER_ID, uid=NOTE_UID,
        content=guest, revision=2, deleted_at=None,
    )
    share = StudyShare(
        id=uuid.uuid4(), owner_id=OWNER_ID, document_uid=NOTE_UID,
        title="Shared", password_hash="unused",
        revision=0, edit_clock={},
        expires_at=datetime.now(UTC) + timedelta(hours=4),
    )
    events = []

    class OwnerSession:
        async def scalar(self, statement):
            entity = statement.column_descriptions[0]["entity"]
            if entity is DocumentState:
                return None
            return original if entity is ChangeEvent else share

        async def execute(self, _statement):
            return SimpleNamespace(scalar_one_or_none=lambda: record)

        async def get(self, _model, _identifier):
            return None

        def add(self, item):
            if isinstance(item, ChangeEvent):
                item.id = len(events) + 1
                events.append(item)

        async def flush(self):
            pass

        async def commit(self):
            pass

    async def no_op(*_args, **_kwargs):
        pass

    monkeypatch.setattr(sync, "lock_owner_events", no_op)
    monkeypatch.setattr(sync, "require_legacy_protocol", no_op)
    monkeypatch.setattr(sync, "hub", SimpleNamespace(publish=no_op))
    response = asyncio.run(sync.put_notebook(
        NOTE_UID,
        SyncWrite(
            expectedRevision=1,
            baseNotebook=original,
            notebook=owner_edit,
        ),
        Request({
            "type": "http",
            "headers": [(b"content-length", b"4096")],
        }),
        "test-idempotency-01234567",
        User(id=OWNER_ID, username="owner", password_hash="unused"),
        OwnerSession(),
    ))
    assert response["revision"] == 3
    assert len(events) == 1
    assert events[0].content == record.content
    assert {stroke["id"] for stroke in events[0].content["pages"][0]["inkStrokes"]} == {
        "guest", "owner",
    }


def test_guest_rejects_fabricated_base_snapshot(environment):
    """A forged common ancestor cannot overwrite already committed content."""
    import copy

    client, db, _, _ = environment
    original = _editable_notebook()
    db.notebooks[0].content = original
    share_id = create(client).json()["id"]
    ticket = join(client, share_id).json()["ticket"]
    fabricated = copy.deepcopy(original)
    fabricated["pages"][0]["inkStrokes"] = [{"id": "invented-before"}]
    modified = copy.deepcopy(fabricated)
    modified["pages"][0]["inkStrokes"] = [{"id": "invented-after"}]
    response = client.put(
        f"/v1/study-shares/{share_id}/document",
        headers={"Authorization": f"Bearer {ticket}"},
        json={
            "baseRevision": 1,
            "baseNotebook": fabricated,
            "notebook": modified,
        },
    )
    assert response.status_code == 409
    assert db.notebooks[0].revision == 1
    assert db.notebooks[0].content == original



def test_cannot_open_two_active_shares_for_same_document(environment):
    client, db, _, _ = environment
    db.notebooks[0].content = _editable_notebook()
    first = create(client)
    assert first.status_code == 201, first.text
    second = create(client)
    assert second.status_code == 409, second.text
    assert len(db.shares) == 1



def test_deleting_shared_notebook_revokes_guest_access(monkeypatch):
    import asyncio
    import copy
    from types import SimpleNamespace

    from starlette.requests import Request

    from app.routers import sync
    from app.schemas import DeleteWrite

    original = copy.deepcopy(_editable_notebook())
    note = SyncedNotebook(
        id=uuid.uuid4(), owner_id=OWNER_ID, uid=NOTE_UID,
        content=original, revision=1, deleted_at=None,
    )
    share = StudyShare(
        id=uuid.uuid4(), owner_id=OWNER_ID, document_uid=NOTE_UID,
        title="Shared", password_hash="unused",
        revision=0, edit_clock={},
        expires_at=datetime.now(UTC) + timedelta(hours=4),
    )
    notifications = []

    class Session:
        async def scalar(self, query):
            if query.column_descriptions[0]["entity"] is DocumentState:
                return None
            return share

        async def execute(self, _query):
            return SimpleNamespace(scalar_one_or_none=lambda: note)

        async def get(self, _model, _key):
            return None

        def add(self, item):
            if isinstance(item, ChangeEvent):
                item.id = 17

        async def flush(self):
            pass

        async def commit(self):
            pass

    async def no_lock(*_args):
        pass

    async def publish(key, message):
        notifications.append((key, message))

    monkeypatch.setattr(sync, "lock_owner_events", no_lock)
    monkeypatch.setattr(sync, "require_legacy_protocol", no_lock)
    monkeypatch.setattr(sync, "hub", SimpleNamespace(publish=publish))
    asyncio.run(sync.delete_notebook(
        NOTE_UID,
        DeleteWrite(expectedRevision=1),
        Request({
            "type": "http",
            "headers": [(b"content-length", b"4096")],
        }),
        "delete-idempotency-12345",
        User(id=OWNER_ID, username="owner", password_hash="unused"),
        Session(),
    ))
    assert note.deleted_at is not None
    assert share.revoked_at is not None
    assert (share.id, {"type": "ended"}) in notifications



def test_three_simultaneous_conflicts_keep_largest_then_ignore_late_write():
    """Three contenders within one microsecond follow one stable winner."""
    import copy
    from app.routers.study_shares import merge_share_document

    base = _editable_notebook()
    base["pages"][0]["textBlocks"] = [{"id": "text", "text": "x"}]

    def change(text):
        candidate = copy.deepcopy(base)
        candidate["pages"][0]["textBlocks"][0]["text"] = text
        return candidate

    t = datetime(2026, 11, 1, 12, 0, 0, 500, tzinfo=UTC)
    clock = {}
    first = merge_share_document(base, change("ab"), base, clock, t)
    medium = merge_share_document(base, change("abcde"), first, clock, t)
    biggest = merge_share_document(base, change("abcdefghijkl"), medium, clock, t)
    assert biggest["pages"][0]["textBlocks"][0]["text"] == "abcdefghijkl"

    later = merge_share_document(
        base, change("z" * 100), biggest, clock, t + timedelta(microseconds=1)
    )
    assert later["pages"][0]["textBlocks"][0]["text"] == "abcdefghijkl"
    assert clock["page/page-1/textBlocks/text"]["at"] == t.isoformat()


def test_duplicate_retried_edit_is_noop_without_new_clock():
    """A retried request after a lost HTTP response must be idempotent."""
    import copy
    from app.routers.study_shares import merge_share_document

    base = _editable_notebook()
    edited = copy.deepcopy(base)
    edited["pages"][0]["inkStrokes"] = [{"id": "same-stroke"}]
    t = datetime(2026, 11, 1, 12, 0, 0, 1, tzinfo=UTC)
    clock = {}
    accepted = merge_share_document(base, edited, base, clock, t)
    first_clock = copy.deepcopy(clock)
    repeated = merge_share_document(
        base, edited, accepted, clock, t + timedelta(seconds=5)
    )
    assert repeated == accepted
    assert clock == first_clock
    assert len(repeated["pages"][0]["inkStrokes"]) == 1



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
    assert client.post(path, json=body).status_code == 409
    assert client.delete(f"/v1/study-shares/{share_id}").status_code == 204
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
