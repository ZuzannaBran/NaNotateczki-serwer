"""Stage 12: real FastAPI/PostgreSQL/WebSocket operation tests.

Run only against the disposable database created by scripts/run-stage12-real.sh.
No fake database, dependency overrides, or mocked WebSocket hub are used.
"""
import asyncio
import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.main import app
from app.security import create_access_token, token_digest
from app.settings import get_settings


def _require_disposable_database():
    if os.environ.get("NN_STAGE12_REAL_DB") != "1":
        pytest.skip("Run scripts/run-stage12-real.sh for isolated PostgreSQL")
    url = urlsplit(get_settings().database_url.replace("+asyncpg", ""))
    if (
        url.hostname not in {"localhost", "127.0.0.1"}
        or url.path != "/nanotateczki_stage12_test"
        or url.username != "nano_app"
    ):
        pytest.fail("Refusing to run Stage 12 against a non-disposable DB")
    fixture_url = os.environ.get("NN_STAGE12_FIXTURE_DATABASE_URL")
    if not fixture_url:
        pytest.fail("Missing isolated Stage 12 fixture database URL")
    fixture = urlsplit(fixture_url.replace("+asyncpg", ""))
    if (
        fixture.hostname != url.hostname
        or fixture.port != url.port
        or fixture.path != url.path
        or fixture.username != "stage12_admin"
    ):
        pytest.fail("Stage 12 fixture database must match the disposable DB")


async def _execute_sql(sql, parameters):
    _require_disposable_database()
    engine = create_async_engine(os.environ["NN_STAGE12_FIXTURE_DATABASE_URL"])
    try:
        async with engine.begin() as connection:
            await connection.execute(text(sql), parameters)
    finally:
        await engine.dispose()


def sql(sql_string, **parameters):
    asyncio.run(_execute_sql(sql_string, parameters))


@pytest.fixture(scope="module")
def client():
    _require_disposable_database()
    with TestClient(app) as api:
        ready = api.get("/ready")
        assert ready.status_code == 200, ready.text
        yield api


def identity(*, v2=True):
    owner_id = uuid.uuid4()
    sql(
        "INSERT INTO users (id, username, password_hash, auth_version) "
        "VALUES (:id, :username, :password_hash, 1)",
        id=owner_id, username="stage12_" + owner_id.hex[:16],
        password_hash="stage12-test-only-not-a-login",
    )
    # New accounts start in v1 unless this disposable fixture opts into v2.
    if v2:
        sql(
            "INSERT INTO account_sync_protocol (owner_id, protocol) "
            "VALUES (:owner_id, 'v2')",
            owner_id=owner_id,
        )
    token, _ = create_access_token(owner_id, 1)
    return owner_id, token


def headers(token, share_id=None):
    result = {"Authorization": "Bearer " + token}
    if share_id is not None:
        result["X-Share-Id"] = str(share_id)
    return result


@dataclass
class Actor:
    document: str
    name: str
    sequence: int = 0
    clock: int = 0
    previous: str | None = None

    def next(self, kind, *, payload, page=None, target=None, after=()):
        self.sequence += 1
        self.clock += 1
        operation_id = str(uuid.uuid4())
        dependencies = list(dict.fromkeys([
            *([self.previous] if self.previous else []), *after,
        ]))
        operation = {
            "schemaVersion": 1,
            "operationId": operation_id,
            "actorId": self.name,
            "actorSeq": self.sequence,
            "documentId": self.document,
            "type": kind,
            "payload": payload,
            "dependencies": dependencies,
            "logicalClock": self.clock,
        }
        if page is not None:
            operation["pageId"] = page
        if target is not None:
            operation["objectId"] = target
        self.previous = operation_id
        return operation


def submit(client, token, operations, share_id=None, status=200):
    response = client.post(
        "/v2/operations/batch", headers=headers(token, share_id),
        json={"operations": operations},
    )
    assert response.status_code == status, response.text
    return response.json()


def snapshot(client, token, document):
    response = client.get(
        f"/v2/documents/{document}/snapshot", headers=headers(token),
    )
    assert response.status_code == 200, response.text
    return response.json()


def initial_document(client, token, document=None):
    document = document or "stage12-" + uuid.uuid4().hex
    page = "page-" + uuid.uuid4().hex
    actor = Actor(document, "seed-" + uuid.uuid4().hex)
    created = actor.next("document.create", payload={"value": {"title": "Stage 12"}})
    added_page = actor.next(
        "page.create", target=page, payload={"value": {"name": "First"}},
    )
    submit(client, token, [added_page, created])  # Deliberately reordered.
    return document, page, created, added_page


def feed(client, token):
    cursor = 0
    result = []
    while True:
        response = client.get(
            "/v2/operations", params={"after": cursor},
            headers=headers(token),
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        entries = payload["operations"]
        assert all(item["cursor"] > cursor for item in entries)
        assert [x["cursor"] for x in entries] == sorted(
            x["cursor"] for x in entries
        )
        result.extend(entries)
        if not payload["hasMore"]:
            return result
        assert entries, "A paginated feed must advance its cursor"
        cursor = payload["cursor"]


def test_300_independent_parents_commit_over_real_http_and_postgresql(client):
    """A bounded full frontier is applied without dropping any parent ID."""
    _, token = identity()
    document = "frontier-" + uuid.uuid4().hex
    creator = Actor(document, "frontier-create-" + uuid.uuid4().hex)
    created = creator.next(
        "document.create", payload={"value": {"title": "base"}},
    )
    submit(client, token, [created])

    parents = []
    for index in range(300):
        actor = Actor(document, f"frontier-peer-{index}", clock=1)
        parents.append(actor.next(
            "document.set",
            payload={"properties": {"title": f"peer-{index}"}},
            after=[created["operationId"]],
        ))
    for index in range(0, len(parents), 50):
        submitted = submit(client, token, parents[index:index + 50])
        assert len(submitted["acknowledgements"]) == 50
        assert all(
            receipt["status"] == "applied"
            for receipt in submitted["acknowledgements"]
        )

    merger = Actor(document, "frontier-merge", clock=2)
    merged = merger.next(
        "document.set", payload={"properties": {"title": "merged"}},
        after=[entry["operationId"] for entry in parents],
    )
    assert len(merged["dependencies"]) == 300
    received = submit(client, token, [merged])
    assert received["acknowledgements"][0]["status"] == "applied"
    state = snapshot(client, token, document)
    assert state["revision"] == 302
    assert state["state"]["properties"]["title"] == "merged"
    before_cursor = state["cursor"]

    replay = submit(client, token, [merged])
    assert replay["acknowledgements"][0]["status"] == "duplicate"
    assert snapshot(client, token, document)["revision"] == 302
    assert snapshot(client, token, document)["cursor"] == before_cursor

    compact = Actor(document, "frontier-cursor-" + uuid.uuid4().hex, clock=302)
    compacted = compact.next(
        "document.set", payload={"properties": {"title": "compacted"}},
    )
    compacted["causalCursor"] = before_cursor
    assert compacted["dependencies"] == []
    receipt = submit(client, token, [compacted])
    assert receipt["acknowledgements"][0]["status"] == "applied"
    assert snapshot(client, token, document)["revision"] == 303
    assert snapshot(client, token, document)["state"]["properties"]["title"] == "compacted"


def test_global_cursor_tracks_commit_order_of_competing_transactions():
    """Real PostgreSQL serializes the v2 cursor before either commit."""
    _require_disposable_database()
    owner_id, _ = identity()

    async def exercise():
        engine = create_async_engine(
            os.environ["NN_STAGE12_FIXTURE_DATABASE_URL"],
        )
        first_id, second_id = uuid.uuid4(), uuid.uuid4()
        first = await engine.connect()
        second = await engine.connect()
        first_transaction = await first.begin()
        second_transaction = await second.begin()

        async def insert(connection, operation_id, actor_id):
            await connection.execute(text(
                "INSERT INTO document_operations "
                "(owner_id, operation_id, actor_id, actor_seq, "
                "document_uid, op_kind, logical_clock, envelope) "
                "VALUES (:owner, :operation, :actor, 1, 'cursor-test', "
                "'document.create', 1, CAST(:envelope AS jsonb))"
            ), {
                "owner": owner_id,
                "operation": operation_id,
                "actor": actor_id,
                "envelope": '{"schemaVersion":1}',
            })

        try:
            await first.execute(
                text("SELECT pg_advisory_xact_lock(681246731)")
            )
            await insert(first, first_id, "cursor-first")
            started = asyncio.Event()

            async def competing_commit():
                started.set()
                await second.execute(
                    text("SELECT pg_advisory_xact_lock(681246731)")
                )
                await insert(second, second_id, "cursor-second")
                await second_transaction.commit()

            competing = asyncio.create_task(competing_commit())
            await started.wait()
            await asyncio.sleep(0.05)
            assert not competing.done(), "Second writer bypassed cursor lock"
            await first_transaction.commit()
            await asyncio.wait_for(competing, timeout=10)

            async with engine.connect() as check:
                records = (await check.execute(text(
                    "SELECT operation_id, cursor "
                    "FROM document_operations "
                    "WHERE operation_id IN (:first, :second) "
                    "ORDER BY cursor"
                ), {"first": first_id, "second": second_id})).all()
                assert len(records) == 2
                assert [row.operation_id for row in records] == [
                    first_id, second_id,
                ]
                assert records[0].cursor < records[1].cursor
        finally:
            if first_transaction.is_active:
                await first_transaction.rollback()
            if second_transaction.is_active:
                await second_transaction.rollback()
            await first.close()
            await second.close()
            await engine.dispose()

    asyncio.run(exercise())


def test_persistent_protocol_fence_rejects_cross_writers(client):
    _, v1_token = identity(v2=False)
    legacy_doc = "stage12-legacy-" + uuid.uuid4().hex
    operation = Actor(legacy_doc, "before-cutover").next(
        "document.create", payload={"value": {"title": "Legacy"}},
    )
    blocked = client.post(
        "/v2/operations/batch", headers=headers(v1_token),
        json={"operations": [operation]},
    )
    assert blocked.status_code == 409, blocked.text
    assert "cutover" in str(blocked.json()).lower()

    _, v2_token = identity()
    uid = "stage12-unrelated-" + uuid.uuid4().hex
    old_notebook = {
        "uid": uid, "title": "Stale snapshot", "kind": 0,
        "folder": "", "createdAt": "2026-11-04T10:00:00Z",
        "updatedAt": "2026-11-04T10:00:00Z", "pages": [],
    }
    response = client.put(
        f"/v1/sync/notebooks/{uid}",
        headers={**headers(v2_token), "Idempotency-Key": uuid.uuid4().hex},
        json={"expectedRevision": 0, "notebook": old_notebook},
    )
    assert response.status_code == 409, response.text
    assert "operation API v2" in str(response.json())


def test_v2_planner_and_backup_use_activated_account(client):
    _, v1_token = identity(v2=False)
    owner_id, token = identity()
    for access, expected in ((v1_token, 409), (token, 200)):
        response = client.get("/v2/activation/readiness", headers=headers(access))
        assert response.status_code == expected, response.text

    entry_id = "event-" + uuid.uuid4().hex
    mutation = {
        "operationId": str(uuid.uuid4()), "expectedRevision": 0,
        "entry": {"id": entry_id, "kind": "event", "title": "Offline replay",
                  "scheduledAt": "2026-11-13T11:00:00Z"},
    }
    for access, expected in ((v1_token, 409), (token, 200)):
        response = client.put(f"/v2/planner/entries/{entry_id}",
                              headers=headers(access), json=mutation)
        assert response.status_code == expected, response.text
    planner = client.get("/v2/planner/entries", headers=headers(token)).json()
    assert planner["entries"][0]["revision"] == 1
    assert planner["entries"][0]["id"] == entry_id

    document, _, _, _ = initial_document(client, token)
    history = feed(client, token)
    cursor = history[-1]["cursor"]
    device_id = str(uuid.uuid4())
    payload = {
        "notebooks": [], "operationHistory": history, "assetBlobs": {},
        "libraryState": {}, "studyPlannerState": {"sessions": []},
        "appPreferences": {}, "editorPreferences": {},
        "recentColors": [], "backupPreferences": {},
    }
    def compact(value):
        return json.dumps(value, separators=(",", ":")).encode()

    archive = compact({
        "format": "nanotateczki-server-backup", "version": 3,
        "protocol": "operations-v2", "schemaVersion": 8,
        "accountId": str(owner_id), "deviceId": device_id,
        "operationCheckpoint": {f"account:{owner_id}": cursor},
        "checksumAlgorithm": "sha256",
        "checksum": hashlib.sha256(compact(payload)).hexdigest(),
        "createdAt": datetime.now(UTC).isoformat(), "payload": payload,
    })
    digest = hashlib.sha256(archive).hexdigest()
    created = client.post("/v2/backups", headers=headers(token), json={
        "deviceId": device_id, "formatVersion": 3,
        "sizeBytes": len(archive), "sha256": digest,
    })
    assert created.status_code == 201, created.text
    backup_id = created.json()["backupId"]
    uploaded = client.put(f"/v2/backups/{backup_id}/content", headers={
        **headers(token), "X-Content-SHA256": digest,
        "Content-Type": "application/octet-stream",
    }, content=archive)
    assert uploaded.status_code == 204, uploaded.text
    assert client.get("/v2/backups", headers=headers(token)).json()["backups"] == []

    # A newer committed tombstone does not invalidate a verified older prefix.
    deleter = Actor(document, "backup-delete-" + uuid.uuid4().hex, clock=2)
    submit(client, token, [deleter.next(
        "document.delete", payload={}, after=[history[-1]["operation"]["operationId"]],
    )])
    finalized = client.post(f"/v2/backups/{backup_id}/finalize", headers=headers(token))
    assert finalized.status_code == 200, finalized.text
    assert finalized.json()["status"] == "verified"
    download = client.get(f"/v2/backups/{backup_id}/download", headers=headers(token))
    assert download.status_code == 200 and download.content == archive
    assert feed(client, token)[-1]["operation"]["type"] == "document.delete"
    _, other = identity()
    assert client.get(f"/v2/backups/{backup_id}/download",
                      headers=headers(other)).status_code == 404


def test_1000_inserts_1000_deletes_and_idempotent_retries(client):
    _, token = identity()
    document, page, created, previous = initial_document(client, token)
    actor = Actor(document, "stress-" + uuid.uuid4().hex, clock=2)
    changes = []
    for n in range(1000):
        changes.append(actor.next(
            "stroke.create", target=f"ink-{n}", page=page,
            payload={"value": {"points": [n, n + 1]}},
            after=[previous["operationId"]] if n == 0 else (),
        ))
    for n in range(1000):
        changes.append(actor.next(
            "object.delete", target=f"ink-{n}", page=page, payload={},
        ))
    for index in range(0, len(changes), 50):
        batch = changes[index:index + 50]
        response = submit(client, token, batch)
        assert len(response["acknowledgements"]) == len(batch)
        assert all(
            ack["status"] == "applied"
            for ack in response["acknowledgements"]
        )
    final = snapshot(client, token, document)
    assert final["revision"] == 2002
    assert final["state"]["pages"][page]["objects"] == {}
    before = final["cursor"]
    repeated = submit(client, token, changes[-50:])
    assert all(ack["status"] == "duplicate"
               for ack in repeated["acknowledgements"])
    assert snapshot(client, token, document)["revision"] == 2002
    assert snapshot(client, token, document)["cursor"] == before
    entries = feed(client, token)
    ids = [item["operation"]["operationId"] for item in entries]
    assert len(ids) == 2002 == len(set(ids))
    assert created["operationId"] in ids


def test_atomic_rollback_reordered_packets_and_forged_replay(client):
    _, token = identity()
    document, page, _, last = initial_document(client, token)
    actor = Actor(document, "atomic-" + uuid.uuid4().hex, clock=2)
    valid = actor.next(
        "object.create", target="valid", page=page,
        payload={"value": {"x": 1}}, after=[last["operationId"]],
    )
    invalid = actor.next(
        "object.create", target="bad", page="no-such-page",
        payload={"value": {"x": 2}},
    )
    before = snapshot(client, token, document)
    submit(client, token, [valid, invalid], status=422)
    assert snapshot(client, token, document) == before

    # Retry the first operation with its original ID after the rollback.
    submit(client, token, [valid])
    assert set(snapshot(client, token, document)["state"]["pages"][page]
               ["objects"]) == {"valid"}
    forged = dict(valid, payload={"value": {"x": 999}})
    submit(client, token, [forged], status=409)
    duplicate = submit(client, token, [valid])
    assert duplicate["acknowledgements"][0]["status"] == "duplicate"

    # A missing predecessor must not partially commit the batch.
    gap = dict(invalid, dependencies=[str(uuid.uuid4())])
    submit(client, token, [gap], status=409)
    assert snapshot(client, token, document)["revision"] == 3


def test_account_isolation_same_document_id_and_feed(client):
    _, token_a = identity()
    _, token_b = identity()
    shared_uid = "same-uid-" + uuid.uuid4().hex
    initial_document(client, token_a, shared_uid)
    missing = client.get(
        f"/v2/documents/{shared_uid}/snapshot", headers=headers(token_b),
    )
    assert missing.status_code == 404
    assert feed(client, token_b) == []
    initial_document(client, token_b, shared_uid)
    assert snapshot(client, token_a, shared_uid)["revision"] == 2
    assert snapshot(client, token_b, shared_uid)["revision"] == 2
    assert len(feed(client, token_a)) == 2
    assert len(feed(client, token_b)) == 2


def test_concurrent_plain_text_is_deterministic(client):
    _, token = identity()
    results = []
    for reversed_delivery in (False, True):
        document, page, _, page_op = initial_document(client, token)
        setup = Actor(document, "text-seed-" + uuid.uuid4().hex, clock=2)
        text_id = "text-" + uuid.uuid4().hex
        created = setup.next(
            "object.create", target=text_id, page=page,
            payload={"value": {"objectType": "text", "text": ""}},
            after=[page_op["operationId"]],
        )
        submit(client, token, [created])
        left = Actor(document, "alice-" + document, clock=3).next(
            "text.insert", target=text_id, page=page,
            payload={"afterAtomId": None, "atoms": [
                {"id": str(uuid.uuid4()), "text": "A"},
            ]}, after=[created["operationId"]],
        )
        right = Actor(document, "bob-" + document, clock=3).next(
            "text.insert", target=text_id, page=page,
            payload={"afterAtomId": None, "atoms": [
                {"id": str(uuid.uuid4()), "text": "B"},
            ]}, after=[created["operationId"]],
        )
        for operation in ([right, left] if reversed_delivery else [left, right]):
            submit(client, token, [operation])
        state = snapshot(client, token, document)["state"]
        results.append(state["pages"][page]["objects"][text_id]
                       ["properties"]["text"])
    assert results[0] == results[1]
    assert sorted(results[0]) == ["A", "B"]


def _share_for(owner, document):
    share_id = uuid.uuid4()
    grant_id = uuid.uuid4()
    ticket = "stage12-" + uuid.uuid4().hex + uuid.uuid4().hex
    expiry = datetime.now(UTC) + timedelta(hours=1)
    sql(
        "INSERT INTO study_shares "
        "(id, owner_id, document_uid, title, password_hash, "
        "expires_at, revoked_at, revision, edit_clock) "
        "VALUES (:id, :owner, :document, 'Stage 12', 'test-only', "
        ":expiry, NULL, 0, jsonb_build_object('_protocol', 'v2'))",
        id=share_id, owner=owner, document=document, expiry=expiry,
    )
    sql(
        "INSERT INTO study_share_grants "
        "(id, share_id, token_hash, expires_at) "
        "VALUES (:id, :share, :digest, :expiry)",
        id=grant_id, share=share_id, digest=token_digest(ticket),
        expiry=expiry,
    )
    return share_id, ticket


def test_real_websocket_share_delivery_and_revocation(client):
    owner, token = identity()
    document, page, _, initial_page = initial_document(client, token)
    share_id, ticket = _share_for(owner, document)
    guest_a = Actor(document, "guest-a-" + uuid.uuid4().hex, clock=2)
    guest_b = Actor(document, "guest-b-" + uuid.uuid4().hex, clock=2)
    a = guest_a.next(
        "stroke.create", target="alpha", page=page,
        payload={"value": {"points": [1]}},
        after=[initial_page["operationId"]],
    )
    b = guest_b.next(
        "stroke.create", target="beta", page=page,
        payload={"value": {"points": [2]}},
        after=[initial_page["operationId"]],
    )
    origin = str(get_settings().app_origin).rstrip("/")
    with client.websocket_connect(
        "/v2/realtime", headers={"Origin": origin},
    ) as owner_ws, client.websocket_connect(
        "/v2/realtime", headers={"Origin": origin},
    ) as guest_ws:
        owner_ws.send_json({"type": "authenticate", "token": token})
        guest_ws.send_json({
            "type": "authenticate", "token": ticket, "shareId": str(share_id),
        })
        assert owner_ws.receive_json()["type"] == "authenticated"
        assert guest_ws.receive_json()["type"] == "authenticated"
        submit(client, ticket, [b], share_id=share_id)
        assert owner_ws.receive_json()["type"] == "operations_changed"
        assert guest_ws.receive_json()["type"] == "operations_changed"
        submit(client, ticket, [a], share_id=share_id)
        assert owner_ws.receive_json()["type"] == "operations_changed"
        assert guest_ws.receive_json()["type"] == "operations_changed"
        # Confirm server-side shutdown before TestClient closes transports.
        # Closing both live sessions concurrently could cancel the portal
        # future even after all data assertions had already passed.
        owner_ws.send_json({"type": "unsubscribe"})
        guest_ws.send_json({"type": "unsubscribe"})
        assert owner_ws.receive_json() == {"type": "unsubscribed"}
        assert guest_ws.receive_json() == {"type": "unsubscribed"}
    objects = snapshot(client, token, document)["state"]["pages"][page]["objects"]
    assert set(objects) == {"alpha", "beta"}

    deletion = guest_a.next(
        "object.delete", page=page, target="beta", payload={},
    )
    transform = guest_b.next(
        "object.transform", page=page, target="beta",
        payload={"properties": {"dx": 99}},
    )
    submit(client, ticket, [deletion], share_id=share_id)
    submit(client, ticket, [transform], share_id=share_id)
    objects = snapshot(client, token, document)["state"]["pages"][page]["objects"]
    assert "beta" not in objects and "alpha" in objects

    sql(
        "UPDATE study_shares SET revoked_at = :now WHERE id = :id",
        id=share_id, now=datetime.now(UTC),
    )
    response = client.get(
        "/v2/operations", headers=headers(ticket, share_id),
    )
    assert response.status_code == 410
    retry = guest_a.next(
        "object.delete", page=page, target="alpha", payload={},
    )
    submit(client, ticket, [retry], share_id=share_id, status=410)
    assert "alpha" in snapshot(client, token, document)["state"][
        "pages"][page]["objects"]


def test_page_delete_wins_over_late_write_and_undo_redo_is_logged(client):
    _, token = identity()
    document, page, _, initial_page = initial_document(client, token)
    actor = Actor(document, "undo-" + uuid.uuid4().hex, clock=2)
    created = actor.next(
        "stroke.create", target="old-stroke", page=page,
        payload={"value": {"x": 0}},
        after=[initial_page["operationId"]],
    )
    moved = actor.next(
        "object.transform", target="old-stroke", page=page,
        payload={"properties": {"x": 30}},
    )
    undo = actor.next(
        "object.transform", target="old-stroke", page=page,
        payload={"properties": {"x": 0}},
    )
    undo["revertsOperationId"] = moved["operationId"]
    redo = actor.next(
        "object.transform", target="old-stroke", page=page,
        payload={"properties": {"x": 30}},
    )
    redo["revertsOperationId"] = undo["operationId"]
    for item in (created, moved, undo, redo):
        submit(client, token, [item])
    state = snapshot(client, token, document)["state"]
    assert state["pages"][page]["objects"]["old-stroke"]["properties"]["x"] == 30

    removed = actor.next("page.delete", target=page, payload={})
    submit(client, token, [removed])
    other = Actor(document, "late-" + uuid.uuid4().hex, clock=8)
    attempted = other.next(
        "stroke.create", target="new-stroke", page=page,
        payload={"value": {"x": 123}}, after=[removed["operationId"]],
    )
    submit(client, token, [attempted], status=422)
    state = snapshot(client, token, document)["state"]
    assert state["pages"] == {}
    assert state is not None
    assert len(feed(client, token)) == 7


def test_socket_reconnect_recovers_missed_feed(client):
    _, token = identity()
    document, page, _, last = initial_document(client, token)
    checkpoint = snapshot(client, token, document)["cursor"]
    actor = Actor(document, "reconnect-" + uuid.uuid4().hex, clock=2)
    first = actor.next(
        "stroke.create", target="offline-1", page=page,
        payload={"value": {"x": 1}}, after=[last["operationId"]],
    )
    # No subscribed WebSocket during this commit.
    submit(client, token, [first])
    resumed = client.get(
        "/v2/operations", params={"after": checkpoint},
        headers=headers(token),
    )
    assert resumed.status_code == 200, resumed.text
    assert [item["operation"]["operationId"]
            for item in resumed.json()["operations"]] == [first["operationId"]]

    origin = str(get_settings().app_origin).rstrip("/")
    with client.websocket_connect(
        "/v2/realtime", headers={"Origin": origin},
    ) as websocket:
        websocket.send_json({"type": "authenticate", "token": token})
        assert websocket.receive_json()["type"] == "authenticated"
        second = actor.next(
            "stroke.create", target="offline-2", page=page,
            payload={"value": {"x": 2}},
        )
        submit(client, token, [second])
        assert websocket.receive_json()["type"] == "operations_changed"
    state = snapshot(client, token, document)["state"]["pages"][page]
    assert set(state["objects"]) == {"offline-1", "offline-2"}


def test_stale_page_delete_after_move_removes_visible_object(client):
    _, token = identity()
    document, source, _, seed = initial_document(client, token)
    destination = "target-" + uuid.uuid4().hex
    creator = Actor(document, "create-" + uuid.uuid4().hex, clock=2)
    new_page = creator.next(
        "page.create", target=destination, payload={"value": {"name": "Next"}},
        after=[seed["operationId"]],
    )
    submit(client, token, [new_page])
    object_id = "moved-" + uuid.uuid4().hex
    created = creator.next(
        "stroke.create", target=object_id, page=source,
        payload={"value": {"x": 2}},
    )
    submit(client, token, [created])
    mover = Actor(document, "move-" + uuid.uuid4().hex, clock=5)
    moved = mover.next(
        "object.move", target=object_id, page=source,
        payload={"toPageId": destination, "sortIndex": 0},
        after=[created["operationId"]],
    )
    submit(client, token, [moved])
    deleter = Actor(document, "delete-" + uuid.uuid4().hex, clock=6)
    deleted = deleter.next(
        "object.delete", target=object_id, page=source,
        payload={}, after=[created["operationId"]],
    )
    submit(client, token, [deleted])
    state = snapshot(client, token, document)["state"]
    assert all(
        object_id not in page["objects"] for page in state["pages"].values()
    )

    # A move issued without observing the deletion must remain a no-op.
    stale = mover.next(
        "object.move", target=object_id, page=source,
        payload={"toPageId": destination, "sortIndex": 1},
    )
    submit(client, token, [stale])
    creator.clock = deleted["logicalClock"]
    recycled = creator.next(
        "stroke.create", target=object_id, page=destination,
        payload={"value": {"x": 7}},
        after=[deleted["operationId"]],
    )
    submit(client, token, [recycled], status=422)
    state = snapshot(client, token, document)["state"]
    assert all(
        object_id not in page["objects"] for page in state["pages"].values()
    )
