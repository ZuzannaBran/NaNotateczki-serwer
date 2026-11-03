"""Stage-4 API invariants without a database connection."""
import asyncio
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.operation_protocol import OperationEnvelope
from app.routers.operations import (
    OperationBatch, Principal, apply_one, ensure_actor_binding,
    list_operations, validate_target,
)

OP_ID = "11111111-1111-4111-8111-111111111111"
PARENT_ID = "22222222-2222-4222-8222-222222222222"


def envelope(seq=1, *, operation_id=OP_ID, dependencies=None, doc="doc"):
    return OperationEnvelope.from_dict({
        "schemaVersion": 1, "operationId": operation_id,
        "actorId": "actor", "actorSeq": seq, "documentId": doc,
        "type": "document.create",
        "payload": {"value": {"title": "Test"}},
        "dependencies": dependencies or [], "logicalClock": seq,
    })


class FakeSession:
    def __init__(self, existing=None, checkpoint=None):
        self.existing = existing
        self.checkpoint = checkpoint
        self.writes = []

    async def scalar(self, statement):
        return self.existing

    async def get(self, model, identity):
        return self.checkpoint

    def add(self, item):
        self.writes.append(item)


def principal():
    owner = uuid.uuid4()
    return Principal(owner, "owner", owner)


def test_idempotent_http_replay_returns_same_cursor_without_writes():
    owner = principal()
    operation = envelope()
    existing = SimpleNamespace(
        owner_id=owner.owner_id, envelope=operation.to_dict(), cursor=41,
    )
    checkpoint = SimpleNamespace(
        principal_kind="owner", principal_id=owner.owner_id, share_id=None,
    )
    session = FakeSession(existing, checkpoint)
    ack = asyncio.run(apply_one(session, owner, operation))
    assert ack == {"operationId": OP_ID, "cursor": 41, "status": "duplicate"}
    assert session.writes == []


def test_reused_operation_id_with_different_content_is_conflict():
    owner = principal()
    existing = SimpleNamespace(
        owner_id=owner.owner_id, envelope=envelope().to_dict(), cursor=1,
    )
    checkpoint = SimpleNamespace(
        principal_kind="owner", principal_id=owner.owner_id, share_id=None,
    )
    session = FakeSession(existing, checkpoint)
    altered = envelope(operation_id=OP_ID)
    other = altered.to_dict()
    other["payload"] = {"value": {"title": "Forged"}}
    altered = OperationEnvelope.from_dict(other)
    with pytest.raises(HTTPException) as error:
        asyncio.run(apply_one(session, owner, altered))
    assert error.value.status_code == 409
    assert session.writes == []


def test_missing_predecessor_is_not_committed():
    owner = principal()
    session = FakeSession()
    assert asyncio.run(apply_one(session, owner, envelope(seq=3))) is None
    assert session.writes == []


def test_missing_dependency_is_not_committed():
    owner = principal()
    session = FakeSession()
    op = envelope(dependencies=[PARENT_ID], operation_id=OP_ID)
    assert asyncio.run(apply_one(session, owner, op)) is None
    assert session.writes == []


def test_checkpoint_prevents_sequence_fork():
    owner = principal()
    checkpoint = SimpleNamespace(
        principal_kind="owner", principal_id=owner.owner_id, share_id=None,
        last_seq=1, last_operation_id=uuid.UUID(PARENT_ID),
        last_logical_clock=1,
    )
    session = FakeSession(checkpoint=checkpoint)
    with pytest.raises(HTTPException) as error:
        asyncio.run(apply_one(session, owner, envelope(seq=1)))
    assert error.value.status_code == 409
    assert session.writes == []


def test_actor_binding_isolated_between_owner_and_share():
    owner = principal()
    grant = SimpleNamespace(
        principal_kind="share", principal_id=uuid.uuid4(),
        share_id=uuid.uuid4(),
    )
    with pytest.raises(HTTPException) as error:
        ensure_actor_binding(grant, owner)
    assert error.value.status_code == 403


def test_guest_cannot_write_outside_shared_document():
    guest = Principal(
        owner_id=uuid.uuid4(), kind="share",
        principal_id=uuid.uuid4(), share_id=uuid.uuid4(),
        document_uid="allowed",
    )
    with pytest.raises(HTTPException) as error:
        validate_target(envelope(doc="other"), guest)
    assert error.value.status_code == 403


def test_actor_may_not_reuse_sequence_across_unrelated_documents():
    owner = principal()
    predecessor = SimpleNamespace(
        document_uid="first-document", logical_clock=1,
    )
    checkpoint = SimpleNamespace(
        principal_kind="owner", principal_id=owner.owner_id, share_id=None,
        last_seq=1, last_operation_id=uuid.UUID(PARENT_ID),
        last_logical_clock=1,
    )

    class Session:
        def __init__(self):
            self.calls = 0

        async def scalar(self, statement):
            self.calls += 1
            return predecessor if self.calls == 2 else None

        async def get(self, model, identity):
            return checkpoint

    next_op = envelope(seq=2, doc="second-document",
                       dependencies=[PARENT_ID])
    with pytest.raises(HTTPException) as error:
        asyncio.run(apply_one(Session(), owner, next_op))
    assert error.value.status_code == 409
    assert error.value.detail == "Actor crossed document scope."


def test_dependency_must_be_in_the_same_document():
    owner = principal()
    parent = SimpleNamespace(
        document_uid="private-document", logical_clock=1,
    )

    class Session:
        def __init__(self):
            self.calls = 0

        async def scalar(self, statement):
            self.calls += 1
            return parent if self.calls == 2 else None

        async def get(self, model, identity):
            return None

    op = envelope(dependencies=[PARENT_ID], operation_id=OP_ID)
    with pytest.raises(HTTPException) as error:
        asyncio.run(apply_one(Session(), owner, op))
    assert error.value.status_code == 422
    assert error.value.detail == "Cross-document causal dependency."


def test_batch_limits_and_strict_envelope():
    with pytest.raises(ValidationError):
        OperationBatch(operations=[])
    with pytest.raises(ValidationError):
        OperationBatch(operations=[{}], unknown=True)


def test_cursor_feed_is_paginated_without_skipping_operations():
    class Session:
        async def scalars(self, query):
            rows = [
                SimpleNamespace(cursor=n, envelope={"operationId": str(n)})
                for n in range(1, 27)
            ]
            return SimpleNamespace(all=lambda: rows)

    feed = asyncio.run(list_operations(principal(), 0, Session()))
    assert len(feed["operations"]) == 25
    assert feed["cursor"] == 25
    assert feed["hasMore"] is True


def test_cursor_feed_has_a_payload_byte_budget():
    class Session:
        async def scalars(self, query):
            rows = [
                SimpleNamespace(cursor=n, envelope={"payload": "x" * 1_000_000})
                for n in range(1, 27)
            ]
            return SimpleNamespace(all=lambda: rows)

    feed = asyncio.run(list_operations(principal(), 0, Session()))
    assert len(feed["operations"]) == 8
    assert feed["cursor"] == 8
    assert feed["hasMore"] is True
