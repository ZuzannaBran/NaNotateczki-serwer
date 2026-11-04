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


def test_text_atoms_are_applied_but_unverified_assets_remain_disabled():
    state, deleted = seeded()
    state = apply(state, op(3, "object.create", page="p", target="t",
                            props={"value": {"objectType": "text", "text": ""}}),
                  deleted)
    atom_id = "00000000-0000-4000-8000-000000000001"
    state = apply(state, op(4, "text.insert", page="p", target="t",
                            props={"afterAtomId": None, "atoms": [
                                {"id": atom_id, "text": "Hello"}
                            ]}), deleted)
    assert state["pages"]["p"]["objects"]["t"]["properties"]["text"] == "Hello"

    with pytest.raises(ProjectionError, match="not verified"):
        apply(state, op(5, "asset.attach", page="p", target="t",
                        props={"assetId": "a", "sha256": "a" * 64,
                               "sizeBytes": 4, "mediaType": "image/png"}), deleted)


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
        with pytest.raises(ProjectionError, match="Identity"):
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


def test_folder_moves_reject_cycles_and_orphan_parents():
    deleted = set()
    state = apply(
        None, op(301, "folder.create", doc="@library", target="root",
                 props={"value": {"name": "Root", "parentId": None}}), deleted
    )
    state = apply(
        state, op(302, "folder.create", doc="@library", target="child",
                  props={"value": {"name": "Child", "parentId": "root"}}),
        deleted,
    )
    state = apply(
        state, op(303, "folder.create", doc="@library", target="leaf",
                  props={"value": {"name": "Leaf", "parentId": "child"}}),
        deleted,
    )

    with pytest.raises(ProjectionError, match="cycle"):
        apply(state, op(304, "folder.move", doc="@library", target="root",
                        props={"properties": {"parentId": "leaf"}}), deleted)
    with pytest.raises(ProjectionError, match="does not exist"):
        apply(state, op(305, "folder.move", doc="@library", target="child",
                        props={"properties": {"parentId": "missing"}}), deleted)
    with pytest.raises(ProjectionError, match="has children"):
        apply(state, op(306, "folder.delete", doc="@library", target="root"),
              deleted)

    # Valid reparenting changes references without creating another folder.
    moved = apply(
        state, op(307, "folder.move", doc="@library", target="leaf",
                  props={"properties": {"parentId": "root"}}), deleted
    )
    assert set(moved["folders"]) == {"root", "child", "leaf"}
    assert moved["folders"]["leaf"]["properties"]["parentId"] == "root"


def test_task_level_create_edit_move_delete_and_no_resurrection():
    deleted = set()
    state = apply(
        None,
        op(410, "task.create", doc="@library", target="task-1",
           props={"value": {"plannerUid": "p1", "title": "Exercise"}}),
        deleted,
    )
    state = apply(
        state,
        op(411, "task.create", doc="@library", target="task-2",
           props={"value": {"plannerUid": "p1", "title": "Reading"}}),
        deleted,
    )
    state = apply(
        state,
        op(412, "task.set", doc="@library", target="task-1",
           props={"properties": {"completed": True}}),
        deleted,
    )
    assert state["tasks"]["task-1"]["properties"]["completed"] is True
    state = apply(
        state,
        op(413, "task.move", doc="@library", target="task-1",
           props={"properties": {"plannerUid": "p2"}}),
        deleted,
    )
    assert state["tasks"]["task-1"]["properties"]["plannerUid"] == "p2"
    state = apply(
        state,
        op(414, "task.delete", doc="@library", target="task-1"),
        deleted,
    )
    assert "task-1" not in state["tasks"]
    assert "task-2" in state["tasks"]
    state = apply(
        state,
        op(415, "task.set", doc="@library", target="task-1",
           props={"properties": {"title": "Stale"}}),
        deleted,
    )
    assert "task-1" not in state["tasks"]
    with pytest.raises(ProjectionError, match="deleted"):
        apply(
            state, op(416, "task.create", doc="@library", target="task-1",
                      props={"value": {"plannerUid": "p2"}}), deleted,
        )


def test_task_requires_planner_and_rejects_unverified_identity():
    deleted = set()
    with pytest.raises(ProjectionError, match="plannerUid"):
        apply(None, op(420, "task.create", doc="@library", target="bad"),
              deleted)
    with pytest.raises(ProjectionError, match="plannerUid"):
        apply(
            None, op(421, "task.create", doc="@library", target="bad",
                     props={"value": {"plannerUid": 123}}), deleted
        )
