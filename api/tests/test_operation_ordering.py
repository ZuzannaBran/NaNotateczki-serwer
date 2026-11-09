"""Stage 2 causal ordering and convergence without a database fixture."""

from itertools import permutations

import pytest

from app.operation_ordering import (
    CausalOperationBuffer,
    OperationActorClock,
    OperationCheckpoint,
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
    original = CausalOperationBuffer()
    original.ingest(op(A1))
    original.ingest(op(A2, "A", 2, 2, deps=[A1]))
    checkpoint = OperationCheckpoint.from_dict(
        original.export_checkpoint().to_dict()
    )
    queue = CausalOperationBuffer.from_checkpoint(
        checkpoint, maximum_pending=1
    )
    with pytest.raises(OperationOrderError):
        queue.ingest(op(B2, "A", 1, 1))
    assert queue.ingest(op(B1, "A", 4, 4)) == []
    with pytest.raises(OperationOrderError):
        queue.ingest(op(B2, "B", 2, 4))


def test_text_atoms_cannot_fall_through_to_scalar_lww():
    insertion = op(A1, kind="text.insert", payload={
        "afterAtomId": None,
        "atoms": [{"id": B1, "text": "x"}],
    })
    with pytest.raises(NotImplementedError):
        OperationPropertyRegister().apply(insertion)


def test_cycle_is_rejected_without_applying_any_operation():
    queue = CausalOperationBuffer()
    first = op(A1, "A", 1, 2, deps=[B1])
    second = op(B1, "B", 1, 2, deps=[A1])
    assert queue.ingest(first) == []
    with pytest.raises(OperationOrderError):
        queue.ingest(second)
    assert len(queue.pending) == 1
    assert not queue.accepted


def test_actor_predecessor_must_be_explicitly_in_dependencies():
    queue = CausalOperationBuffer()
    queue.ingest(op(A1))
    with pytest.raises(OperationOrderError):
        queue.ingest(op(A2, "A", 2, 2))


def test_bad_buffered_dependent_is_quarantined_without_losing_parent():
    queue = CausalOperationBuffer()
    bad = op(B1, "B", 1, 1, deps=[A1])
    assert queue.ingest(bad) == []
    parent = op(A1, "A", 1, 2)
    assert queue.ingest(parent) == [parent]
    assert queue.accepted[A1] == parent
    assert B1 not in queue.pending
    assert B1 in queue.rejected
    # The invalid operation no longer reserves its actor sequence.
    corrected = op(B2, "B", 1, 3, deps=[A1])
    assert queue.ingest(corrected) == [corrected]
    with pytest.raises(OperationOrderError):
        queue.ingest(bad)


def test_bad_incoming_operation_does_not_mutate_checkpoint():
    queue = CausalOperationBuffer()
    first = op(A1, "A", 1, 7)
    assert queue.ingest(first) == [first]
    before = queue.export_checkpoint().to_dict()
    with pytest.raises(OperationOrderError):
        queue.ingest(op(A2, "A", 2, 7, deps=[A1]))
    assert queue.export_checkpoint().to_dict() == before
    assert not queue.pending
    assert not queue.rejected
    assert queue.ingest(op(A2, "A", 2, 8, deps=[A1]))


def test_recovered_checkpoint_rejects_forged_actor_and_dependency_clocks():
    queue = CausalOperationBuffer()
    first = op(A1, "A", 1, 15)
    queue.ingest(first)
    restored = CausalOperationBuffer.from_checkpoint(
        OperationCheckpoint.from_dict(
            queue.export_checkpoint().to_dict()
        )
    )
    with pytest.raises(OperationOrderError):
        restored.ingest(op(A2, "A", 2, 16))
    with pytest.raises(OperationOrderError):
        restored.ingest(op(A2, "A", 2, 15, deps=[A1]))
    with pytest.raises(OperationOrderError):
        restored.ingest(op(B1, "B", 1, 14, deps=[A1]))
    assert restored.ingest(op(B1, "B", 1, 16, deps=[A1]))


def test_recovered_checkpoint_rejects_changed_duplicate_payload():
    queue = CausalOperationBuffer()
    original = op(A1)
    queue.ingest(original)
    restored = CausalOperationBuffer.from_checkpoint(
        queue.export_checkpoint()
    )
    assert restored.ingest(original) == []
    with pytest.raises(OperationOrderError):
        restored.ingest(
            op(A1, payload={"properties": {"color": "forged"}})
        )


def test_checkpoint_rejects_incomplete_history_and_pending_outbox():
    queue = CausalOperationBuffer()
    queue.ingest(op(A2, "A", 2, 2, deps=[A1]))
    with pytest.raises(OperationOrderError):
        queue.export_checkpoint()
    with pytest.raises(ValueError):
        CausalOperationBuffer(actor_sequences={"A": 1})
    with pytest.raises(ValueError):
        CausalOperationBuffer.from_checkpoint(
            OperationCheckpoint((op(A2, "A", 2, 2, deps=[A1]),))
        )


def test_actor_clock_carries_observed_remote_dependencies_across_restart():
    actor = OperationActorClock("local")
    remote = op(B1, "remote", 1, 20)
    actor.observe(remote)
    first = actor.next(
        document_id="notebook", kind="object.set", page_id="page",
        object_id="stroke", payload={"properties": {"x": 1}},
    )
    assert first.logical_clock == 21
    assert B1 in first.dependencies
    resumed = OperationActorClock.from_checkpoint(actor.to_checkpoint())
    second = resumed.next(
        document_id="notebook", kind="object.set", page_id="page",
        object_id="stroke", payload={"properties": {"x": 2}},
    )
    assert {B1, first.operation_id}.issubset(second.dependencies)


def test_actor_clock_undo_includes_inverse_and_ignores_other_documents():
    actor = OperationActorClock("local")
    remote = op(B1, "remote", 1, 3)
    other = OperationEnvelope.from_dict({
        **remote.to_dict(), "operationId": B2,
        "documentId": "private-document",
    })
    actor.observe(other)
    first = actor.next(
        document_id="notebook", kind="object.set", page_id="page",
        object_id="stroke", payload={"properties": {"x": 1}},
        reverts_operation_id=remote.operation_id,
    )
    assert remote.operation_id in first.dependencies
    assert B2 not in first.dependencies


def test_actor_clock_preserves_300_independent_parents_across_restart():
    actor = OperationActorClock("local")
    ids = []
    for index in range(300):
        operation_id = f"{index:08x}-1111-4111-8111-111111111111"
        ids.append(operation_id)
        actor.observe(op(operation_id, f"remote-{index}", 1, 2))
    restored = OperationActorClock.from_checkpoint(actor.to_checkpoint())
    operation = restored.next(
        document_id="notebook", kind="object.set", page_id="page",
        object_id="stroke", payload={"properties": {"x": 1}},
    )
    assert len(operation.dependencies) == 300
    assert set(operation.dependencies) == set(ids)
    assert OperationEnvelope.from_dict(operation.to_dict()) == operation
    assert restored.actor_seq == 1


def test_actor_clock_does_not_advance_on_dependency_byte_limit():
    from app.operation_protocol import MAX_DEPENDENCY_BYTES

    actor = OperationActorClock("local")
    for index in range(MAX_DEPENDENCY_BYTES // 39 + 1):
        remote = op(
            f"{index:08x}-1111-4111-8111-111111111111",
            f"remote-{index}", 1, 2,
        )
        actor.observe(remote)
    before = actor.to_checkpoint()
    with pytest.raises(ValueError):
        actor.next(
            document_id="notebook", kind="object.set", page_id="page",
            object_id="stroke", payload={"properties": {"x": 1}},
        )
    assert actor.to_checkpoint() == before


def test_rejected_id_survives_checkpoint_and_cannot_be_reused():
    queue = CausalOperationBuffer()
    bad = op(B1, "B", 1, 1, deps=[A1])
    queue.ingest(bad)
    queue.ingest(op(A1, "A", 1, 2))
    restored = CausalOperationBuffer.from_checkpoint(
        OperationCheckpoint.from_dict(queue.export_checkpoint().to_dict())
    )
    assert B1 in restored.rejected
    with pytest.raises(OperationOrderError):
        restored.ingest(op(B1, "B", 1, 3, deps=[A1]))
    assert restored.ingest(op(B2, "B", 1, 3, deps=[A1]))



def test_actor_clock_binds_document_across_checkpoint_restart():
    actor = OperationActorClock("local")
    first = actor.next(
        document_id="notebook", kind="object.set", page_id="page",
        object_id="stroke", payload={"properties": {"x": 1}},
    )
    assert actor.document_id == "notebook"
    checkpoint = actor.to_checkpoint()
    assert checkpoint["documentId"] == "notebook"
    resumed = OperationActorClock.from_checkpoint(checkpoint)
    with pytest.raises(ValueError, match="document scope"):
        resumed.next(
            document_id="private-document", kind="object.set", page_id="page",
            object_id="stroke", payload={"properties": {"x": 2}},
        )
    foreign = OperationEnvelope.from_dict({
        **op(B1, actor="foreign", clock=25).to_dict(),
        "documentId": "private-document",
    })
    resumed.observe(foreign)
    second = resumed.next(
        document_id="notebook", kind="object.set", page_id="page",
        object_id="stroke", payload={"properties": {"x": 2}},
    )
    assert second.dependencies == (first.operation_id,)


def test_actor_clock_rejects_unscoped_old_nonempty_checkpoint():
    actor = OperationActorClock("local")
    actor.next(
        document_id="notebook", kind="object.set", page_id="page",
        object_id="stroke", payload={"properties": {"x": 1}},
    )
    checkpoint = actor.to_checkpoint()
    del checkpoint["documentId"]
    with pytest.raises(ValueError, match="Invalid actor checkpoint"):
        OperationActorClock.from_checkpoint(checkpoint)


def test_causal_buffer_rejects_cross_document_dependencies_and_actor_moves():
    first = op(A1)
    queue = CausalOperationBuffer()
    assert queue.ingest(first) == [first]
    wrong_dependency = OperationEnvelope.from_dict({
        **op(B1, actor="B", clock=2, deps=[A1]).to_dict(),
        "documentId": "another-document",
    })
    with pytest.raises(OperationOrderError, match="Cross-document"):
        queue.ingest(wrong_dependency)
    wrong_actor = OperationEnvelope.from_dict({
        **op(A2, actor="A", sequence=2, clock=2, deps=[A1]).to_dict(),
        "documentId": "another-document",
    })
    with pytest.raises(OperationOrderError, match="actor causality"):
        queue.ingest(wrong_actor)
    assert queue.actor_sequences == {"A": 1}
    assert not queue.pending



def test_actor_clock_prunes_transitive_observed_frontier():
    actor = OperationActorClock("local")
    previous = None
    for index in range(260):
        operation_id = f"{index + 1000:08x}-1111-4111-8111-111111111111"
        remote = OperationEnvelope.from_dict({
            "schemaVersion": 1,
            "operationId": operation_id,
            "actorId": f"remote-{index}",
            "actorSeq": 1,
            "documentId": "notebook",
            "pageId": "page",
            "objectId": "stroke",
            "type": "object.set",
            "payload": {"properties": {"x": index}},
            "dependencies": [previous] if previous else [],
            "logicalClock": index + 1,
        })
        actor.observe(remote)
        previous = operation_id
    next_operation = actor.next(
        document_id="notebook",
        kind="object.set", page_id="page", object_id="stroke",
        payload={"properties": {"x": 999}},
    )
    assert next_operation.dependencies == (previous,)
    assert next_operation.logical_clock == 261
