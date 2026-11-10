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
    ensure_projection_budget, list_operations, validate_batch_receipts,
    validate_target,
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


def test_300_dependency_frontier_uses_one_query_and_keeps_missing_gap():
    owner = principal()
    dependencies = [
        f"{index:08x}-1111-4111-8111-111111111111"
        for index in range(300)
    ]

    class Session(FakeSession):
        def __init__(self):
            super().__init__()
            self.parent_queries = 0

        async def scalars(self, statement):
            self.parent_queries += 1
            return [
                SimpleNamespace(
                    operation_id=uuid.UUID(parent),
                    document_uid="doc",
                    logical_clock=0,
                )
                for parent in dependencies[:-1]
            ]

    session = Session()
    operation = envelope(dependencies=dependencies)
    assert asyncio.run(apply_one(session, owner, operation)) is None
    assert session.parent_queries == 1
    assert session.writes == []


def test_bulk_dependency_lookup_rejects_cross_document_parent():
    owner = principal()
    dependencies = [
        f"{index:08x}-1111-4111-8111-111111111111"
        for index in range(300)
    ]

    class Session(FakeSession):
        def __init__(self):
            super().__init__()
            self.parent_queries = 0

        async def scalars(self, statement):
            self.parent_queries += 1
            return [
                SimpleNamespace(
                    operation_id=uuid.UUID(parent),
                    document_uid="foreign" if index == 299 else "doc",
                    logical_clock=0,
                )
                for index, parent in enumerate(dependencies)
            ]

    session = Session()
    operation = envelope(dependencies=dependencies)
    with pytest.raises(HTTPException) as error:
        asyncio.run(apply_one(session, owner, operation))
    assert error.value.status_code == 422
    assert error.value.detail == "Cross-document causal dependency."
    assert session.parent_queries == 1
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



def test_projection_size_is_checked_before_any_database_write():
    ensure_projection_budget({"text": "small"}, 64)
    with pytest.raises(HTTPException) as error:
        ensure_projection_budget({"text": "é" * 30}, 50)
    assert error.value.status_code == 413
    assert error.value.detail == "Document is too large."



def test_ack_batch_contract_preserves_request_order_and_actor_causality():
    first = envelope()
    second = envelope(seq=2, operation_id=PARENT_ID, dependencies=[OP_ID])
    receipts = {
        second.operation_id: {
            "operationId": second.operation_id,
            "cursor": 24,
            "status": "applied",
        },
        first.operation_id: {
            "operationId": first.operation_id,
            "cursor": 23,
            "status": "duplicate",
        },
    }
    result = validate_batch_receipts([first, second], receipts)
    assert [item["operationId"] for item in result] == [
        first.operation_id, second.operation_id,
    ]
    assert [item["cursor"] for item in result] == [23, 24]


@pytest.mark.parametrize("fault", [
    "duplicate_cursor", "reversed_cursor", "unsafe_integer",
    "wrong_operation", "invalid_status", "missing_entry", "bool_cursor",
])
def test_invalid_ack_batch_is_rejected_before_database_commit(fault):
    first = envelope()
    second = envelope(seq=2, operation_id=PARENT_ID, dependencies=[OP_ID])
    receipts = {
        first.operation_id: {
            "operationId": first.operation_id,
            "cursor": 23,
            "status": "duplicate",
        },
        second.operation_id: {
            "operationId": second.operation_id,
            "cursor": 24,
            "status": "applied",
        },
    }
    if fault == "duplicate_cursor":
        receipts[second.operation_id]["cursor"] = 23
    elif fault == "reversed_cursor":
        receipts[first.operation_id]["cursor"] = 25
    elif fault == "unsafe_integer":
        receipts[second.operation_id]["cursor"] = 9007199254740992
    elif fault == "wrong_operation":
        receipts[second.operation_id]["operationId"] = OP_ID
    elif fault == "invalid_status":
        receipts[second.operation_id]["status"] = "pending"
    elif fault == "missing_entry":
        del receipts[second.operation_id]
    elif fault == "bool_cursor":
        receipts[second.operation_id]["cursor"] = True
    with pytest.raises(HTTPException) as error:
        validate_batch_receipts([first, second], receipts)
    assert error.value.status_code == 500


def test_ack_batch_tracks_actor_cursor_order_independently():
    first = envelope()
    another = envelope(operation_id=PARENT_ID).to_dict()
    another["actorId"] = "independent"
    peer = OperationEnvelope.from_dict(another)
    receipts = {
        first.operation_id: {
            "operationId": first.operation_id,
            "cursor": 34,
            "status": "applied",
        },
        peer.operation_id: {
            "operationId": peer.operation_id,
            "cursor": 33,
            "status": "applied",
        },
    }
    result = validate_batch_receipts([first, peer], receipts)
    assert [item["cursor"] for item in result] == [34, 33]



@pytest.mark.parametrize("cursor", [-1, 9007199254740992, True, "1"])
def test_invalid_feed_cursor_is_refused_before_database_read(cursor):
    class Session:
        async def scalars(self, query):
            raise AssertionError("Invalid cursor must never query the journal.")

    with pytest.raises(HTTPException) as error:
        asyncio.run(list_operations(principal(), cursor, Session()))
    assert error.value.status_code == 422
    assert error.value.detail == "Invalid feed cursor."


def test_safe_upper_feed_cursor_returns_empty_feed():
    class Session:
        async def scalars(self, query):
            return SimpleNamespace(all=lambda: [])

    result = asyncio.run(
        list_operations(principal(), 9007199254740991, Session())
    )
    assert result == {
        "cursor": 9007199254740991,
        "hasMore": False,
        "operations": [],
    }


def test_compact_cursor_does_not_bypass_missing_actor_predecessor():
    owner = principal()
    previous = uuid.UUID(PARENT_ID)
    checkpoint = SimpleNamespace(
        principal_kind="owner", principal_id=owner.owner_id, share_id=None,
        last_seq=1, last_operation_id=previous, last_logical_clock=1,
    )

    class Session(FakeSession):
        def __init__(self):
            super().__init__(checkpoint=checkpoint)
            self.calls = 0

        async def scalar(self, statement):
            self.calls += 1
            if self.calls == 1:
                return None
            return SimpleNamespace(
                document_uid="doc", cursor=51, logical_clock=1,
            )

    session = Session()
    operation = OperationEnvelope.from_dict({
        **envelope(seq=2).to_dict(),
        "dependencies": [], "causalCursor": 50,
    })
    with pytest.raises(HTTPException, match="") as error:
        asyncio.run(apply_one(session, owner, operation))
    assert error.value.status_code == 409
    assert session.writes == []


def test_share_compact_cursor_cannot_claim_private_owner_history():
    guest = Principal(
        owner_id=uuid.uuid4(), kind="share",
        principal_id=uuid.uuid4(), share_id=uuid.uuid4(),
        document_uid="shared-doc",
    )

    class Session(FakeSession):
        def __init__(self):
            super().__init__()
            self.calls = 0

        async def scalar(self, statement):
            self.calls += 1
            # No duplicate, owner global max, share visible max.
            return (None, 600, 400)[self.calls - 1]

    operation = OperationEnvelope.from_dict({
        **envelope(doc="shared-doc").to_dict(),
        "causalCursor": 500,
    })
    session = Session()
    assert asyncio.run(apply_one(session, guest, operation)) is None
    assert session.calls == 3
    assert session.writes == []


def test_compact_cursor_rejects_nonexistent_committed_number():
    owner = principal()
    operation = OperationEnvelope.from_dict({
        **envelope().to_dict(), "causalCursor": 50,
    })

    class Session(FakeSession):
        def __init__(self):
            super().__init__()
            self.calls = 0

        async def scalar(self, statement):
            self.calls += 1
            # Duplicate check, global max, exact cursor lookup.
            return (None, 90, None)[self.calls - 1]

    session = Session()
    with pytest.raises(HTTPException) as error:
        asyncio.run(apply_one(session, owner, operation))
    assert error.value.status_code == 422
    assert "Unknown compact causal cursor" in error.value.detail
    assert session.calls == 3
    assert session.writes == []


def test_guest_compact_cursor_requires_exact_visible_document_position():
    guest = Principal(
        owner_id=uuid.uuid4(), kind="share",
        principal_id=uuid.uuid4(), share_id=uuid.uuid4(),
        document_uid="shared-doc",
    )
    operation = OperationEnvelope.from_dict({
        **envelope(doc="shared-doc").to_dict(), "causalCursor": 50,
    })

    class Session(FakeSession):
        def __init__(self):
            super().__init__()
            self.calls = 0

        async def scalar(self, statement):
            self.calls += 1
            # A private owner cursor is below the latest visible cursor,
            # but has no matching row in the shared-document scope.
            return (None, 90, 80, None)[self.calls - 1]

    session = Session()
    with pytest.raises(HTTPException) as error:
        asyncio.run(apply_one(session, guest, operation))
    assert error.value.status_code == 422
    assert session.calls == 4
    assert session.writes == []

