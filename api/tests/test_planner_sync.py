"""Planner sync contract: per-entry LWW, tombstones and account isolation."""

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.database import database_session
from app.main import app
from app.models import PlannerEntry, User
from app.routers import planner
from app.security import current_user


class MemorySession:
    def __init__(self):
        self.records = {}
        self.commits = 0
        self.protocols = {}
        self.protocol_checks = 0

    async def get(self, model, key):
        assert model is PlannerEntry
        return self.records.get(key)

    async def execute(self, statement, parameters=None):
        if parameters is not None:
            assert "FROM account_sync_protocol" in str(statement)
            self.protocol_checks += 1
            return SimpleNamespace(
                scalar_one_or_none=lambda: self.protocols.get(
                    parameters["owner_id"]
                ),
            )
        params = statement.compile().params
        owner = next(x for x in params.values() if isinstance(x, uuid.UUID))
        after = next((x for x in params.values() if isinstance(x, str)), "")
        rows = sorted(
            (r for (user, uid), r in self.records.items()
             if user == owner and uid > after),
            key=lambda r: r.uid,
        )
        return SimpleNamespace(scalars=lambda: iter(rows[:201]))

    def add(self, record):
        self.records[(record.owner_id, record.uid)] = record

    async def commit(self):
        self.commits += 1


@pytest.fixture
def environment(monkeypatch):
    db = MemorySession()
    owner = User(
        id=uuid.UUID("d1111111-1111-4111-8111-111111111111"),
        username="owner", password_hash="unused",
    )
    principal = {"user": owner}
    app.dependency_overrides[database_session] = lambda: db
    app.dependency_overrides[current_user] = lambda: principal["user"]

    async def lock(*args):
        return None

    async def publish(*args):
        return None

    monkeypatch.setattr(planner, "lock_owner_events", lock)
    monkeypatch.setattr(planner.hub, "publish", publish)
    try:
        with TestClient(app) as client:
            yield client, db, principal
    finally:
        app.dependency_overrides.clear()


def value(uid, title):
    return {
        "id": uid, "kind": "event", "title": title,
        "scheduledAt": "2026-11-02T12:00:00Z", "minutes": 30,
    }


def write(client, uid, stamp, title=None):
    return client.put(f"/v1/planner/entries/{uid}", json={
        "modifiedAt": stamp.isoformat(), "deleted": title is None,
        "entry": None if title is None else value(uid, title),
    })


def test_later_edit_wins_and_old_retry_cannot_overwrite(environment):
    client, db, _ = environment
    base = datetime.now(UTC) - timedelta(hours=1)
    assert write(client, "e1", base, "Original").status_code == 200
    assert write(client, "e1", base + timedelta(seconds=2), "Later").json()[
        "entry"
    ]["title"] == "Later"
    result = write(client, "e1", base + timedelta(seconds=1), "Stale")
    assert result.json()["entry"]["title"] == "Later"
    assert db.commits == 2


def test_delete_is_tombstone_and_old_offline_edit_cannot_resurrect(environment):
    client, _, _ = environment
    base = datetime.now(UTC) - timedelta(hours=1)
    write(client, "e1", base, "Original")
    assert write(client, "e1", base + timedelta(seconds=2)).json()["deleted"]
    assert write(client, "e1", base + timedelta(seconds=1), "Old").json()["deleted"]
    result = client.get("/v1/planner/entries").json()
    assert result["entries"][0]["deleted"] is True


def test_deletion_wins_over_clock_skew_and_stale_resurrection(environment):
    client, db, _ = environment
    base = datetime.now(UTC) - timedelta(hours=2)
    assert write(client, "shared", base, "Original").status_code == 200

    # Device B edits with its clock two hours ahead, before A deletes.
    ahead = base + timedelta(hours=3)
    assert write(client, "shared", ahead, "Unsynced B").status_code == 200
    deleted = write(client, "shared", base + timedelta(minutes=1))
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] is True
    assert datetime.fromisoformat(deleted.json()["modifiedAt"]) > ahead

    commits_after_delete = db.commits
    # Stale PUTs and even newer timestamps must not resurrect a tombstone.
    stale = write(client, "shared", ahead + timedelta(hours=1), "Stale B")
    assert stale.status_code == 200
    assert stale.json()["deleted"] is True
    assert db.commits == commits_after_delete
    assert client.get("/v1/planner/entries").json()["entries"][0]["deleted"]


def test_accounts_have_independent_entries(environment):
    client, _, principal = environment
    stamp = datetime.now(UTC)
    write(client, "entry", stamp, "Owner")
    principal["user"] = User(
        id=uuid.uuid4(), username="second", password_hash="unused"
    )
    assert client.get("/v1/planner/entries").json()["entries"] == []
    write(client, "entry", stamp, "Second")
    assert client.get("/v1/planner/entries").json()["entries"][0][
        "entry"
    ]["title"] == "Second"


def test_invalid_timestamp_and_cross_id_are_rejected(environment):
    client, _, _ = environment
    assert client.put("/v1/planner/entries/e", json={
        "modifiedAt": "2026-11-02T12:00:00", "deleted": False,
        "entry": value("e", "Title"),
    }).status_code == 422
    assert client.put("/v1/planner/entries/e", json={
        "modifiedAt": datetime.now(UTC).isoformat(), "deleted": False,
        "entry": value("other", "Title"),
    }).status_code == 422


def test_paginated_bootstrap_includes_all_entries(environment):
    client, db, principal = environment
    for index in range(215):
        uid = f"item-{index:04}"
        db.add(PlannerEntry(
            owner_id=principal["user"].id,
            uid=uid,
            content=value(uid, "Event"),
            modified_at=datetime.now(UTC),
            deleted=False,
        ))
    first = client.get("/v1/planner/entries").json()
    assert len(first["entries"]) == 200
    assert first["next"] == "item-0199"
    second = client.get(
        "/v1/planner/entries", params={"after": first["next"]}
    ).json()
    assert len(second["entries"]) == 15
    assert second["next"] is None


def test_v2_account_cannot_write_legacy_planner_entries(environment):
    client, db, principal = environment
    db.protocols[principal["user"].id] = "v2"
    stamp = datetime.now(UTC)
    response = write(client, "blocked-entry", stamp, "Not allowed")
    assert response.status_code == 409
    assert db.protocol_checks == 1
    assert db.records == {}
    assert db.commits == 0
