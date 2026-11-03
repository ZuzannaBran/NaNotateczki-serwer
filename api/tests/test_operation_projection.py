"""Stage-4 projection regression tests: convergence, deletes and validation."""
from copy import deepcopy
from itertools import permutations

import pytest

from app.operation_projection import ProjectionError, apply_operation
from app.operation_protocol import OperationEnvelope


def op(
    n: int, kind: str, *, doc="doc", page=None, target=None,
    props=None, actor="device-a", clock=None, deps=None,
):
    envelope = {
        "schemaVersion": 1,
        "operationId": str(__import__("uuid").UUID(int=n)),
        "actorId": actor,
        "actorSeq": n,
        "documentId": doc,
        "type": kind,
        "payload": props if props is not None else (
            {} if kind.endswith(".delete") else
            {"value": {}} if kind.endswith(".create") else
            {"properties": {"x": n}}
        ),
        "dependencies": deps or [],
        "logicalClock": clock or n,
    }
    if page is not None:
        envelope["pageId"] = page
    if target is not None:
        envelope["objectId"] = target
    return OperationEnvelope.from_dict(envelope)


def apply(state, operation, deleted):
    state, marker = apply_operation(deepcopy(state), operation, deleted)
    if marker is not None:
        deleted.add(marker)
    return state


def seeded():
    deleted = set()
    state = apply(None, op(1, "document.create"), deleted)
    state = apply(state, op(2, "page.create", target="p"), deleted)
    return state, deleted


def test_independent_creates_and_delete_never_resurrect():
    state, deleted = seeded()
    state = apply(state, op(3, "stroke.create", page="p", target="a",
                            props={"value": {"points": [1]}}), deleted)
    state = apply(state, op(4, "stroke.create", page="p", target="b",
                            props={"value": {"points": [2]}}), deleted)
    state = apply(state, op(5, "object.delete", page="p", target="a"), deleted)
    assert "a" not in state["pages"]["p"]["objects"]
    assert "b" in state["pages"]["p"]["objects"]
    state = apply(state, op(6, "object.set", page="p", target="a"), deleted)
    assert "a" not in state["pages"]["p"]["objects"]
    with pytest.raises(ProjectionError, match="deleted"):
        apply(state, op(7, "stroke.create", page="p", target="a"), deleted)


def test_scalar_property_resolution_is_order_independent():
    original, deleted = seeded()
    original = apply(original, op(3, "object.create", page="p", target="a",
                                  props={"value": {"color": "white", "x": 0}}),
                     deleted)
    left = op(4, "object.set", page="p", target="a", actor="a",
              clock=9, props={"properties": {"x": 10, "color": "red"}})
    right = op(5, "object.set", page="p", target="a", actor="z",
               clock=9, props={"properties": {"color": "blue"}})
    for order in permutations((left, right)):
        state = deepcopy(original)
        for event in order:
            state = apply(state, event, deleted)
        fields = state["pages"]["p"]["objects"]["a"]["properties"]
        assert fields == {"color": "blue", "x": 10}


def test_document_tombstone_overrides_late_edits():
    state, deleted = seeded()
    state = apply(state, op(3, "document.delete"), deleted)
    assert state is None
    assert ("document", "", "") in deleted
    assert apply(state, op(4, "document.set"), deleted) is None
    with pytest.raises(ProjectionError, match="already used"):
        apply(state, op(5, "document.create"), deleted)


def test_page_tombstone_suppresses_child_and_cannot_reuse_uid():
    state, deleted = seeded()
    state = apply(state, op(3, "page.delete", target="p"), deleted)
    assert state["pages"] == {}
    state = apply(state, op(4, "object.set", page="p", target="z"), deleted)
    assert state["pages"] == {}
    with pytest.raises(ProjectionError, match="deleted"):
        apply(state, op(5, "page.create", target="p"), deleted)


def test_folder_tombstone_cannot_be_recreated():
    deleted = set()
    state = apply(None, op(1, "folder.create", doc="@library", target="f",
                           props={"value": {"name": "First"}}), deleted)
    state = apply(state, op(2, "folder.set", doc="@library", target="f",
                            props={"properties": {"name": "Second"}}), deleted)
    assert state["folders"]["f"]["properties"]["name"] == "Second"
    state = apply(state, op(3, "folder.delete", doc="@library", target="f"),
                  deleted)
    with pytest.raises(ProjectionError, match="deleted"):
        apply(state, op(4, "folder.create", doc="@library", target="f"), deleted)


def test_reject_unimplemented_rich_text_and_unverified_assets():
    state, deleted = seeded()
    for kind, payload in (
        ("text.insert", {"afterAtomId": None, "atoms": []}),
        ("asset.attach", {"assetId": "a", "sha256": "a" * 64}),
    ):
        with pytest.raises(ProjectionError, match="not enabled"):
            apply(state, op(3, kind, page="p", target="t", props=payload),
                  deleted)


def test_reject_modified_delete_payload_and_missing_properties():
    state, deleted = seeded()
    with pytest.raises(ProjectionError, match="empty payload"):
        apply(state, op(3, "page.delete", target="p",
                        props={"properties": {"x": 1}}), deleted)
    with pytest.raises(ProjectionError, match="properties"):
        apply(state, op(4, "page.set", target="p",
                        props={"properties": {}}), deleted)



def test_reject_extra_payload_fields_in_v2_projection():
    state, deleted = seeded()
    with pytest.raises(ProjectionError, match="exactly one properties"):
        apply(
            state,
            op(3, "page.set", target="p",
               props={"properties": {"x": 4}, "ignored": "unvalidated"}),
            deleted,
        )


def test_reject_identity_changes_in_v2_projection():
    state, deleted = seeded()
    for field in ("id", "uid"):
        with pytest.raises(ProjectionError, match="Stable IDs"):
            apply(
                state,
                op(3, "page.set", target="p",
                   props={"properties": {field: "forged-id"}}),
                deleted,
            )


def test_empty_creation_is_valid_but_property_update_requires_fields():
    state, deleted = seeded()
    state = apply(
        state,
        op(3, "object.create", page="p", target="blank",
           props={"value": {}}),
        deleted,
    )
    assert "blank" in state["pages"]["p"]["objects"]
    with pytest.raises(ProjectionError, match="properties"):
        apply(
            state,
            op(4, "object.set", page="p", target="blank",
               props={"properties": {}}),
            deleted,
        )
