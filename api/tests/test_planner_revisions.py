"""Regression coverage for CAS, stale writes, deletion and lost ACK."""

import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.database import database_session
from app.main import app
from app.models import PlannerEntry, PlannerEntryMutation, User
from app.routers import planner, planner_v2
from app.security import current_user
from app.settings import get_settings


class MemorySession:
    def __init__(self):
        self.entries = {}
        self.receipts = {}
        self.commits = 0
        self.protocol = "v2"

    async def get(self, model, key):
        if model is PlannerEntryMutation:
            return self.receipts.get(key)
        assert model is PlannerEntry
        return self.entries.get(key)

    async def execute(self, statement, parameters=None):
        if "account_sync_protocol" in str(statement):
            return SimpleNamespace(scalar_one_or_none=lambda: self.protocol)
        params = statement.compile().params
        owner = next(x for x in params.values() if isinstance(x, uuid.UUID))
        after = next((x for x in params.values() if isinstance(x, str)), "")
        records = sorted(
            (r for (user, uid), r in self.entries.items()
             if user == owner and uid > after),
            key=lambda r: r.uid,
        )
        return SimpleNamespace(scalars=lambda: iter(records[:201]))

    def add(self, row):
        if isinstance(row, PlannerEntryMutation):
            self.receipts[(row.owner_id, row.operation_id)] = row
        else:
            self.entries[(row.owner_id, row.uid)] = row

    async def commit(self):
        self.commits += 1


@pytest.fixture
def environment(monkeypatch):
    db = MemorySession()
    principal = {"user": User(
        id=uuid.uuid4(), username="first", password_hash="unused"
    )}
    app.dependency_overrides[database_session] = lambda: db
    app.dependency_overrides[current_user] = lambda: principal["user"]

    async def noop(*args):
        return None

    monkeypatch.setattr(planner, "lock_owner_events", noop)
    monkeypatch.setattr(planner_v2, "lock_owner_events", noop)
    monkeypatch.setattr(
        planner_v2, "require_operations_v2_write", lambda owner: None,
    )
    monkeypatch.setattr(planner.hub, "publish", noop)
    monkeypatch.setattr(planner_v2.hub, "publish", noop)
    try:
        with TestClient(app) as client:
            yield client, db, principal
    finally:
        app.dependency_overrides.clear()


def payload(uid, expected=0, title="First", deleted=False, op=None):
    return {
        "operationId": op or str(uuid.uuid4()),
        "expectedRevision": expected,
        "deleted": deleted,
        "entry": None if deleted else {
            "id": uid, "kind": "event", "title": title,
            "scheduledAt": "2026-11-04T11:00:00Z",
        },
    }


def put(client, uid, body):
    return client.put(f"/v2/planner/entries/{uid}", json=body)


def test_revisions_cas_and_clock_independence(environment):
    client, _, _ = environment
    created = put(client, "s", payload("s")).json()
    assert created["revision"] == 1
    edited = put(client, "s", payload("s", expected=1, title="Edit")).json()
    assert edited["revision"] == 2
    assert edited["entry"]["title"] == "Edit"
    stale = put(client, "s", payload("s", expected=1, title="Old"))
    assert stale.status_code == 409
    assert stale.json()["detail"]["current"]["revision"] == 2


def test_lost_ack_deduplicates_by_operation_id(environment):
    client, db, _ = environment
    body = payload("s")
    first = put(client, "s", body)
    commits = db.commits
    again = put(client, "s", body)
    assert again.json() == first.json()
    assert db.commits == commits
    modified = payload("s", op=body["operationId"], title="Other")
    assert put(client, "s", modified).status_code == 409


def test_delete_wins_over_concurrent_edit_and_cannot_resurrect(environment):
    client, _, _ = environment
    put(client, "s", payload("s"))
    put(client, "s", payload("s", expected=1, title="Edited"))
    deleted = put(client, "s", payload("s", expected=1, deleted=True))
    assert deleted.status_code == 200
    assert deleted.json()["revision"] == 3
    assert deleted.json()["deleted"]
    assert put(client, "s", payload("s", expected=3)).status_code == 409
    rows = client.get("/v2/planner/entries").json()["entries"]
    assert len(rows) == 1 and rows[0]["deleted"]


def test_separate_account_uid_and_receipts(environment):
    client, _, principal = environment
    operation = str(uuid.uuid4())
    body = payload("s", op=operation)
    assert put(client, "s", body).json()["revision"] == 1
    principal["user"] = User(
        id=uuid.uuid4(), username="second", password_hash="unused"
    )
    assert client.get("/v2/planner/entries").json()["entries"] == []
    assert put(client, "s", body).json()["revision"] == 1


def test_empty_delete_is_tombstone_not_missing_record(environment):
    client, _, _ = environment
    deleted = put(client, "gone", payload("gone", deleted=True))
    assert deleted.json()["revision"] == 1
    assert put(client, "gone", payload("gone")).status_code == 409


def test_legacy_account_cannot_mutate_planner_via_v2(environment):
    client, db, _ = environment
    db.protocol = None
    assert client.get("/v2/planner/entries").status_code == 409
    assert put(client, "s", payload("s")).status_code == 409
    assert not db.entries and not db.receipts and db.commits == 0


def test_canary_allowlist_also_applies_to_planner(environment, monkeypatch):
    from app.routers.operations import require_operations_v2_write

    client, db, _ = environment
    monkeypatch.setattr(
        planner_v2, "require_operations_v2_write", require_operations_v2_write,
    )
    monkeypatch.setenv("OPERATIONS_V2_WRITE_MODE", "disabled")
    get_settings.cache_clear()
    try:
        assert put(client, "s", payload("s")).status_code == 403
        assert not db.entries and not db.receipts and db.commits == 0
    finally:
        get_settings.cache_clear()
