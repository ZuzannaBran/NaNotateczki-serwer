"""Stage 2 causal ordering and convergence without a database fixture."""

from itertools import permutations

import pytest

from app.operation_ordering import (
    CausalOperationBuffer,
    OperationOrderError,
    OperationPropertyRegister,
    precedence,
)
from app.operation_protocol import OperationEnvelope

A1 = "11111111-1111-4111-8111-111111111111"
A2 = "22222222-2222-4222-8222-222222222222"
B1 = "33333333-3333-4333-8333-333333333333"
B2 = "44444444-4444-4444-8444-444444444444"


def op(
    operation_id=A1, actor="A", sequence=1, clock=1,
    kind="object.set", entity="stroke", payload=None, deps=None,
):
    return OperationEnvelope.from_dict({
        "schemaVersion": 1,
        "operationId": operation_id,
        "actorId": actor,
        "actorSeq": sequence,
        "documentId": "notebook",
        "pageId": "page",
        "objectId": entity,
        "type": kind,
        "payload": payload if payload is not None else {
            "properties": {"color": "blue"}
        },
        "dependencies": deps or [],
        "logicalClock": clock,
    })


def test_out_of_order_predecessor_and_cross_actor_dependency():
    queue = CausalOperationBuffer()
    first = op()
    second = op(A2, "A", 2, 2, deps=[A1])
    after = op(B1, "B", 1, 3, deps=[A2])
    assert queue.ingest(after) == []
    assert queue.ingest(second) == []
    assert queue.missing_dependencies(after) == [A2]
    assert queue.ingest(first) == [first, second, after]
    assert not queue.pending


def test_retransmit_and_fork_are_not_replayed():
    queue = CausalOperationBuffer()
    first = op()
    assert queue.ingest(first) == [first]
    assert queue.ingest(first) == []
    with pytest.raises(OperationOrderError):
        queue.ingest(op(A1, payload={"properties": {"color": "red"}}))
    with pytest.raises(OperationOrderError):
        queue.ingest(op(B1, actor="A"))


def test_dependency_clock_must_advance():
    queue = CausalOperationBuffer()
    queue.ingest(op(A1, clock=20))
    with pytest.raises(OperationOrderError):
        queue.ingest(op(B1, actor="B", clock=19, deps=[A1]))


def test_all_arrival_permutations_reach_identical_register_state():
    changes = [
        op(A1, kind="object.create", entity="one",
           payload={"value": {"color": "blue", "x": 0}}),
        op(A2, "A", 2, 2, entity="one",
           payload={"properties": {"x": 12}}, deps=[A1]),
        op(B1, "B", 1, 2, entity="one",
           payload={"properties": {"color": "red"}}, deps=[A1]),
        op(B2, "B", 2, 3, kind="object.create", entity="two",
           payload={"value": {"color": "green"}}, deps=[B1]),
    ]
    for order in permutations(changes):
        queue = CausalOperationBuffer()
        projection = OperationPropertyRegister()
        for incoming in order:
            for accepted in queue.ingest(incoming):
                projection.apply(accepted)
        assert not queue.pending
        assert projection.fields_for(changes[0]) == {"color": "red", "x": 12}
        assert projection.fields_for(changes[-1]) == {"color": "green"}


def test_delete_is_permanent_even_if_stale_or_future_edits_arrive():
    creation = op(A1, kind="object.create", payload={"value": {"x": 1}})
    update = op(B1, "B", 1, 50, payload={"properties": {"x": 20}})
    deletion = op(A2, "A", 2, 2, kind="object.delete",
                  payload={}, deps=[A1])
    projection = OperationPropertyRegister()
    for operation in (deletion, update, creation):
        projection.apply(operation)
    assert projection.fields_for(creation) is None


def test_same_field_tiebreak_is_independent_of_arrival_order():
    left = op(A1, "A", 1, 9)
    right = op(B1, "B", 1, 9, payload={"properties": {"color": "red"}})
    assert precedence(right) > precedence(left)
    for order in ((left, right), (right, left)):
        projection = OperationPropertyRegister()
        for operation in order:
            projection.apply(operation)
        assert projection.fields_for(left) == {"color": "red"}


def test_snapshot_frontier_and_queue_limit():
    queue = CausalOperationBuffer(actor_sequences={"A": 2}, maximum_pending=1)
    with pytest.raises(OperationOrderError):
        queue.ingest(op(A1))
    queue.ingest(op(A2, "A", 4, 4))
    with pytest.raises(OperationOrderError):
        queue.ingest(op(B2, "B", 2, 4))


def test_text_atoms_cannot_fall_through_to_scalar_lww():
    insertion = op(A1, kind="text.insert", payload={
        "afterAtomId": None,
        "atoms": [{"id": B1, "text": "x"}],
    })
    with pytest.raises(NotImplementedError):
        OperationPropertyRegister().apply(insertion)
