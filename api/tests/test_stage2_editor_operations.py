"""Stage-2 regressions for Quill CRDT and cross-page object movement."""
from __future__ import annotations

from copy import deepcopy
from uuid import UUID

import pytest

from app.operation_projection import (
    ProjectionError, apply_operation, ordered_objects,
)
from app.operation_protocol import OperationEnvelope


A = "00000000-0000-4000-8000-000000000001"
B = "00000000-0000-4000-8000-000000000002"


def op(n: int, kind: str, payload: dict, *, page=None, obj=None,
       actor="a", clock=None, reverts=None) -> OperationEnvelope:
    return OperationEnvelope.from_dict({
        "schemaVersion": 1,
        "operationId": str(UUID(int=n)),
        "actorId": actor, "actorSeq": n,
        "documentId": "stage2-doc", "type": kind,
        "payload": payload, "dependencies": [reverts] if reverts else [],
        "logicalClock": clock if clock is not None else n,
        **({"pageId": page} if page is not None else {}),
        **({"objectId": obj} if obj is not None else {}),
        **({"revertsOperationId": reverts} if reverts else {}),
    })


def apply(state: dict | None, operation: OperationEnvelope) -> dict:
    state, marker = apply_operation(deepcopy(state), operation, set())
    assert marker is None
    return state


def seeded_text() -> dict:
    state = apply(None, op(1, "document.create", {"value": {}}))
    state = apply(state, op(2, "page.create", {"value": {}}, obj="p"))
    state = apply(state, op(3, "object.create", {
        "value": {"objectType": "text", "text": ""}
    }, page="p", obj="text"))
    return state


def text_node(state: dict) -> dict:
    return state["pages"]["p"]["objects"]["text"]


def test_quill_attributes_survive_insertion_and_delta_replay():
    state = seeded_text()
    insert = op(4, "text.insert", {
        "afterAtomId": None,
        "atoms": [{"id": A, "text": "H", "attributes": {"bold": True}},
                  {"id": B, "text": "i"}],
    }, page="p", obj="text")
    state = apply(state, insert)
    assert text_node(state)["properties"]["text"] == "Hi"
    assert text_node(state)["properties"]["delta"] == [
        {"insert": "H", "attributes": {"bold": True}},
        {"insert": "i"},
    ]


def test_concurrent_quill_styles_converge_per_attribute():
    initial = apply(seeded_text(), op(4, "text.insert", {
        "afterAtomId": None, "atoms": [{"id": A, "text": "X"}],
    }, page="p", obj="text"))
    bold = op(5, "text.format", {
        "atomIds": [A], "attributes": {"bold": True, "color": "red"},
    }, page="p", obj="text", actor="alice", clock=11)
    color = op(6, "text.format", {
        "atomIds": [A], "attributes": {"color": "blue"},
    }, page="p", obj="text", actor="zed", clock=11)
    left = apply(apply(initial, bold), color)
    right = apply(apply(initial, color), bold)
    assert text_node(left)["textAtoms"] == text_node(right)["textAtoms"]
    assert text_node(left)["properties"]["delta"] == [
        {"insert": "X", "attributes": {"bold": True, "color": "blue"}},
    ]


def test_quill_style_removal_is_durable_without_deleting_atom():
    state = apply(seeded_text(), op(4, "text.insert", {
        "afterAtomId": None,
        "atoms": [{"id": A, "text": "X", "attributes": {"italic": True}}],
    }, page="p", obj="text"))
    state = apply(state, op(5, "text.format", {
        "atomIds": [A], "attributes": {"italic": None},
    }, page="p", obj="text"))
    assert text_node(state)["properties"]["delta"] == [{"insert": "X"}]
    assert text_node(state)["textAtoms"][A]["attributes"] == {}


@pytest.mark.parametrize("payload", [
    {"afterAtomId": None, "atoms": [
        {"id": A, "text": "X", "attributes": {"bold": []}}]},
    {"afterAtomId": None, "atoms": [
        {"id": A, "text": "X", "attributes": {"color": None}}]},
    {"atomIds": [A], "attributes": {"color": {"unsafe": True}}},
    {"atomIds": [A], "attributes": {}},
])
def test_invalid_quill_attributes_fail_without_projection_changes(payload):
    state = seeded_text()
    before = deepcopy(state)
    kind = "text.format" if "atomIds" in payload else "text.insert"
    with pytest.raises(ProjectionError):
        apply(state, op(4, kind, payload, page="p", obj="text"))
    assert state == before


def test_object_moves_between_pages_without_losing_properties():
    state = seeded_text()
    state = apply(state, op(4, "page.create", {"value": {}}, obj="p2"))
    state = apply(state, op(5, "stroke.create", {
        "value": {"points": [1, 2], "sortIndex": 0, "toolIndex": 3},
    }, page="p", obj="ink"))
    state = apply(state, op(6, "object.move", {
        "toPageId": "p2", "sortIndex": 2,
    }, page="p", obj="ink"))
    assert "ink" not in state["pages"]["p"]["objects"]
    moved = state["pages"]["p2"]["objects"]["ink"]["properties"]
    assert moved["points"] == [1, 2]
    assert moved["sortIndex"] == 2
    assert [uid for uid, _ in ordered_objects(state["pages"]["p2"])] == [
        "ink"
    ]


def test_invalid_move_preserves_source_and_destination():
    state = seeded_text()
    before = deepcopy(state)
    with pytest.raises(ProjectionError, match="Destination page"):
        apply(state, op(4, "object.move", {
            "toPageId": "missing", "sortIndex": 0,
        }, page="p", obj="text"))
    assert state == before


def test_undo_is_independent_compensating_operation():
    state = seeded_text()
    before = op(4, "object.set", {
        "properties": {"dx": 20},
    }, page="p", obj="text")
    state = apply(state, before)
    undo = op(5, "object.set", {
        "properties": {"dx": 0},
    }, page="p", obj="text", reverts=before.operation_id)
    state = apply(state, undo)
    assert text_node(state)["properties"]["dx"] == 0
    assert undo.operation_id != before.operation_id
    assert undo.reverts_operation_id == before.operation_id
