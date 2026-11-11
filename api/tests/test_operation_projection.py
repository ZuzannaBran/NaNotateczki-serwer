"""Stage-4 projection regression tests: convergence, deletes and validation."""
from copy import deepcopy
from itertools import permutations

import pytest

from app.operation_projection import (
    ProjectionError, apply_operation, ordered_objects,
)
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


def test_stale_source_delete_removes_moved_object_and_blocks_recreation():
    state, deleted = seeded()
    state = apply(state, op(3, "page.create", target="destination"), deleted)
    state = apply(state, op(4, "stroke.create", page="p", target="moved"),
                  deleted)
    state = apply(state, op(
        5, "object.move", page="p", target="moved",
        props={"toPageId": "destination", "sortIndex": 1},
    ), deleted)
    assert "moved" in state["pages"]["destination"]["objects"]

    # The deleting device still sees the old source page.
    state = apply(state, op(6, "object.delete", page="p", target="moved"),
                  deleted)
    assert all(
        "moved" not in page["objects"] for page in state["pages"].values()
    )
    assert ("object", "p", "moved") in deleted

    # Stale edits, late moves and creations must not resurrect the UID.
    state = apply(
        state, op(7, "object.set", page="destination", target="moved"), deleted
    )
    state = apply(state, op(
        8, "object.move", page="p", target="moved",
        props={"toPageId": "destination", "sortIndex": 2},
    ), deleted)
    assert all(
        "moved" not in page["objects"] for page in state["pages"].values()
    )
    with pytest.raises(ProjectionError, match="deleted"):
        apply(state, op(
            9, "stroke.create", page="destination", target="moved",
        ), deleted)


def test_delete_after_source_page_removed_still_reclaims_moved_uid():
    state, deleted = seeded()
    state = apply(state, op(3, "page.create", target="destination"), deleted)
    state = apply(state, op(4, "stroke.create", page="p", target="moved"),
                  deleted)
    state = apply(state, op(
        5, "object.move", page="p", target="moved",
        props={"toPageId": "destination", "sortIndex": 1},
    ), deleted)
    state = apply(state, op(6, "page.delete", target="p"), deleted)
    state = apply(state, op(7, "object.delete", page="p", target="moved"),
                  deleted)
    assert "moved" not in state["pages"]["destination"]["objects"]


def test_delete_before_concurrent_move_stays_deleted():
    state, deleted = seeded()
    state = apply(state, op(3, "page.create", target="other"), deleted)
    state = apply(state, op(4, "stroke.create", page="p", target="uid"),
                  deleted)
    state = apply(state, op(5, "object.delete", page="p", target="uid"),
                  deleted)
    state = apply(state, op(
        6, "object.move", page="p", target="uid",
        props={"toPageId": "other", "sortIndex": 0},
    ), deleted)
    assert all("uid" not in page["objects"] for page in state["pages"].values())


def test_object_uid_cannot_be_created_twice_across_pages():
    state, deleted = seeded()
    state = apply(state, op(3, "page.create", target="other"), deleted)
    state = apply(state, op(4, "stroke.create", page="p", target="uid"),
                  deleted)
    with pytest.raises(ProjectionError, match="already used"):
        apply(state, op(5, "stroke.create", page="other", target="uid"),
              deleted)



@pytest.mark.parametrize("target", ["document", "page", "object"])
@pytest.mark.parametrize("rank", [
    [], [1, "actor", 2], [1, "actor", True, "op"],
    ["1", "actor", 2, "op"], [1, "actor", 2, []],
])
def test_corrupt_scalar_version_never_silently_repairs(target, rank):
    state, deleted = seeded()
    if target == "object":
        state = apply(
            state, op(3, "object.create", page="p", target="a"), deleted,
        )
        node = state["pages"]["p"]["objects"]["a"]
        change = op(4, "object.set", page="p", target="a",
                    props={"properties": {"fresh": True}})
    elif target == "page":
        node = state["pages"]["p"]
        change = op(4, "page.set", target="p",
                    props={"properties": {"title": "New"}})
    else:
        node = state
        change = op(4, "document.set",
                    props={"properties": {"title": "New"}})
    node["versions"]["unrelated"] = rank
    before = deepcopy(state)
    with pytest.raises(ProjectionError, match="Corrupt persisted versions"):
        apply_operation(state, change, deleted)
    assert state == before


@pytest.mark.parametrize("bad", [
    [], {"good": [1, "actor", False, "op"]},
    {"bad": [1, "actor", 2, None]},
])
def test_asset_version_corruption_never_overwrites_receipts(bad):
    state, deleted = seeded()
    state = apply(
        state, op(3, "object.create", page="p", target="image",
                  props={"value": {"objectType": "image"}}), deleted,
    )
    node = state["pages"]["p"]["objects"]["image"]
    node["assetVersions"] = bad
    before = deepcopy(state)
    asset = op(
        4, "asset.attach", page="p", target="image",
        props={"assetId": "asset-a", "sha256": "a" * 64,
               "sizeBytes": 12, "mediaType": "image/png"},
    )
    with pytest.raises(
        ProjectionError, match="Corrupt persisted assetVersions"
    ):
        apply_operation(state, asset, deleted,
                        verified_assets=frozenset({"a" * 64}))
    assert state == before


@pytest.mark.parametrize("bad", [
    [], [1, "actor", 2], [1, "actor", True, "op"],
])
def test_corrupt_move_rank_does_not_relocate_objects(bad):
    state, deleted = seeded()
    state = apply(state, op(3, "page.create", target="second"), deleted)
    state = apply(
        state, op(4, "object.create", page="p", target="a"), deleted,
    )
    first = op(
        5, "object.move", page="p", target="a",
        props={"toPageId": "second", "sortIndex": 0},
    )
    state = apply(state, first, deleted)
    state["pages"]["second"]["objects"]["a"]["lastMove"]["rank"] = bad
    before = deepcopy(state)
    second = op(
        6, "object.move", page="second", target="a",
        props={"toPageId": "p", "sortIndex": 1},
    )
    with pytest.raises(ProjectionError, match="Corrupt persisted move"):
        apply_operation(state, second, deleted)
    assert state == before



def test_failed_folder_cycle_keeps_original_graph_and_versions():
    deleted = set()
    state = apply(
        None, op(500, "folder.create", doc="@library", target="root",
                 props={"value": {"name": "Root", "parentId": None}}),
        deleted,
    )
    state = apply(
        state, op(501, "folder.create", doc="@library", target="child",
                  props={"value": {"name": "Child", "parentId": "root"}}),
        deleted,
    )
    before = deepcopy(state)
    bad_move = op(
        502, "folder.move", doc="@library", target="root",
        props={"properties": {"parentId": "child"}},
    )
    with pytest.raises(ProjectionError, match="cycle"):
        apply_operation(state, bad_move, deleted)
    assert state == before
    assert state["folders"]["root"]["properties"]["parentId"] is None
    assert state["folders"]["root"]["versions"] == before[
        "folders"
    ]["root"]["versions"]

    good_move = op(
        503, "folder.move", doc="@library", target="child",
        props={"properties": {"parentId": None}},
    )
    state, _ = apply_operation(state, good_move, deleted)
    assert state["folders"]["child"]["properties"]["parentId"] is None


def test_failed_folder_create_cannot_leave_ghost_or_parent_reference():
    deleted = set()
    state = apply(
        None, op(510, "folder.create", doc="@library", target="root",
                 props={"value": {"name": "Root", "parentId": None}}),
        deleted,
    )
    before = deepcopy(state)
    bad_create = op(
        511, "folder.create", doc="@library", target="ghost",
        props={"value": {"name": "Ghost", "parentId": "missing"}},
    )
    with pytest.raises(ProjectionError, match="does not exist"):
        apply_operation(state, bad_create, deleted)
    assert state == before
    assert "ghost" not in state["folders"]


@pytest.mark.parametrize("value", [
    {"title": "Missing owner"}, {"plannerUid": ""},
    {"plannerUid": 17}, {"plannerUid": None},
])
def test_invalid_task_create_has_no_partial_identity(value):
    deleted = set()
    state = apply(
        None, op(520, "task.create", doc="@library", target="existing",
                 props={"value": {"plannerUid": "p1"}}), deleted,
    )
    before = deepcopy(state)
    bad_create = op(
        521, "task.create", doc="@library", target="invalid",
        props={"value": value},
    )
    with pytest.raises(ProjectionError, match="plannerUid"):
        apply_operation(state, bad_create, deleted)
    assert state == before
    assert "invalid" not in state["tasks"]


@pytest.mark.parametrize("value", ["", 0, None])
def test_invalid_task_move_preserves_old_planner(value):
    deleted = set()
    state = apply(
        None, op(530, "task.create", doc="@library", target="task",
                 props={"value": {"plannerUid": "before"}}), deleted,
    )
    before = deepcopy(state)
    invalid = op(
        531, "task.move", doc="@library", target="task",
        props={"properties": {"plannerUid": value}},
    )
    with pytest.raises(ProjectionError, match="plannerUid"):
        apply_operation(state, invalid, deleted)
    assert state == before


@pytest.mark.parametrize("rank", [
    [0, "actor", 1, "00000000-0000-4000-8000-000000000001"],
    [-1, "actor", 1, "00000000-0000-4000-8000-000000000001"],
    [9007199254740992, "actor", 1, "00000000-0000-4000-8000-000000000001"],
    [1, "", 1, "00000000-0000-4000-8000-000000000001"],
    [1, "actor", 0, "00000000-0000-4000-8000-000000000001"],
    [1, "actor", 9007199254740992, "00000000-0000-4000-8000-000000000001"],
    [1, "actor", 1, "not-a-uuid"],
])
def test_out_of_contract_scalar_ranks_fail_closed(rank):
    state, deleted = seeded()
    state["versions"]["created"] = rank
    before = deepcopy(state)
    with pytest.raises(ProjectionError, match="Corrupt persisted versions"):
        apply_operation(
            state, op(540, "document.set",
                      props={"properties": {"title": "Late"}}),
            deleted,
        )
    assert state == before


def test_folder_and_task_existing_corrupt_versions_preserve_original():
    deleted = set()
    state = apply(
        None, op(560, "folder.create", doc="@library", target="f",
                 props={"value": {"parentId": None}}), deleted,
    )
    state = apply(
        state, op(561, "task.create", doc="@library", target="t",
                  props={"value": {"plannerUid": "p"}}), deleted,
    )
    state["folders"]["f"]["versions"]["bad"] = [0, "", 0, ""]
    state["tasks"]["t"]["versions"]["bad"] = [0, "", 0, ""]
    before = deepcopy(state)
    with pytest.raises(ProjectionError, match="Corrupt persisted versions"):
        apply_operation(
            state, op(562, "folder.set", doc="@library", target="f",
                      props={"properties": {"name": "Updated"}}), deleted,
        )
    assert state == before
    with pytest.raises(ProjectionError, match="Corrupt persisted versions"):
        apply_operation(
            state, op(563, "task.set", doc="@library", target="t",
                      props={"properties": {"completed": True}}), deleted,
        )
    assert state == before



def test_failed_move_with_corrupt_scalar_versions_keeps_old_location():
    state, deleted = seeded()
    state = apply(state, op(600, "page.create", target="destination"), deleted)
    state = apply(
        state, op(601, "object.create", page="p", target="ink",
                  props={"value": {"objectType": "stroke", "sortIndex": 0}}),
        deleted,
    )
    node = state["pages"]["p"]["objects"]["ink"]
    node["versions"]["corrupt"] = [0, "", 0, ""]
    before = deepcopy(state)
    move = op(
        602, "object.move", page="p", target="ink",
        props={"toPageId": "destination", "sortIndex": 1},
    )
    with pytest.raises(ProjectionError, match="Corrupt persisted versions"):
        apply_operation(state, move, deleted)
    assert state == before
    assert "ink" in state["pages"]["p"]["objects"]
    assert "ink" not in state["pages"]["destination"]["objects"]


def test_image_create_with_unattached_asset_leaves_no_ghost_object():
    state, deleted = seeded()
    before = deepcopy(state)
    invalid = op(
        610, "object.create", page="p", target="image-ghost",
        props={"value": {
            "objectType": "image", "assetId": "missing-attachment",
        }},
    )
    with pytest.raises(ProjectionError, match="verified attachment"):
        apply_operation(state, invalid, deleted)
    assert state == before
    assert "image-ghost" not in state["pages"]["p"]["objects"]


def test_successful_folder_move_replaces_only_changed_node():
    deleted = set()
    state = apply(
        None, op(620, "folder.create", doc="@library", target="root",
                 props={"value": {"parentId": None}}), deleted,
    )
    state = apply(
        state, op(621, "folder.create", doc="@library", target="child",
                  props={"value": {"parentId": "root"}}), deleted,
    )
    old_node = state["folders"]["child"]
    new_state, _ = apply_operation(
        state, op(622, "folder.move", doc="@library", target="child",
                  props={"properties": {"parentId": None}}),
        deleted,
    )
    assert old_node["properties"]["parentId"] == "root"
    assert new_state["folders"]["child"]["properties"]["parentId"] is None



def test_independent_moves_converge_with_equal_destination_sort_index():
    original, deleted = seeded()
    original = apply(
        original, op(700, "page.create", target="destination"), deleted,
    )
    for n, identity in ((701, "ink-a"), (702, "ink-b")):
        original = apply(
            original, op(
                n, "stroke.create", page="p", target=identity,
                props={"value": {"sortIndex": 0}},
            ), deleted,
        )
    first = op(
        703, "object.move", page="p", target="ink-a",
        actor="moving-a", clock=705,
        props={"toPageId": "destination", "sortIndex": 0},
    )
    second = op(
        704, "object.move", page="p", target="ink-b",
        actor="moving-b", clock=705,
        props={"toPageId": "destination", "sortIndex": 0},
    )
    for order in permutations((first, second)):
        state = deepcopy(original)
        for move in order:
            state = apply(state, move, deleted)
        assert state["pages"]["p"]["objects"] == {}
        objects = state["pages"]["destination"]["objects"]
        assert set(objects) == {"ink-a", "ink-b"}
        assert [
            item_id
            for item_id, node in ordered_objects(state["pages"]["destination"])
        ] == ["ink-a", "ink-b"]
        assert all(
            node["lastMove"]["actorId"] == (
                "moving-a" if item_id == "ink-a" else "moving-b"
            )
            for item_id, node in objects.items()
        )



def test_concurrent_same_field_metadata_converges_by_rank():
    seed, deleted = seeded()
    low = op(
        810, "document.set", actor="device-a", clock=12,
        props={"properties": {"title": "Offline"}},
    )
    high = op(
        811, "document.set", actor="device-z", clock=13,
        props={"properties": {"title": "Remote"}},
    )
    for order in permutations((low, high)):
        state = deepcopy(seed)
        for event in order:
            state = apply(state, event, deleted)
        assert state["properties"]["title"] == "Remote"
        assert state["versions"]["title"] == [
            high.logical_clock, high.actor_id, high.actor_seq,
            high.operation_id,
        ]
    page_low = op(
        812, "page.set", target="p", actor="device-a", clock=18,
        props={"properties": {"isBookmarked": False}},
    )
    page_high = op(
        813, "page.set", target="p", actor="device-z", clock=18,
        props={"properties": {"isBookmarked": True}},
    )
    for order in permutations((page_low, page_high)):
        state = deepcopy(seed)
        for event in order:
            state = apply(state, event, deleted)
        assert state["pages"]["p"]["properties"]["isBookmarked"] is True


def test_concurrent_same_object_scalar_converges_in_both_orders():
    seed, deleted = seeded()
    seed = apply(
        seed, op(820, "object.create", page="p", target="stroke",
                 props={"value": {"colorValue": 0, "width": 2.0}}), deleted,
    )
    low = op(
        821, "object.set", page="p", target="stroke",
        actor="device-b", clock=900,
        props={"properties": {"width": 3.0}},
    )
    high = op(
        822, "object.set", page="p", target="stroke",
        actor="device-z", clock=900,
        props={"properties": {"width": 5.0}},
    )
    for order in permutations((low, high)):
        state = deepcopy(seed)
        for event in order:
            state = apply(state, event, deleted)
        node = state["pages"]["p"]["objects"]["stroke"]
        assert node["properties"] == {"colorValue": 0, "width": 5.0}
        assert node["versions"]["width"] == [
            high.logical_clock, high.actor_id, high.actor_seq,
            high.operation_id,
        ]


def test_creation_rank_prevents_stale_object_scalar_overwrite():
    seed, deleted = seeded()
    seed = apply(
        seed, op(840, "object.create", page="p", target="original",
                 props={"value": {"colorValue": 7, "width": 2.0}}), deleted,
    )
    old_revision = deepcopy(seed)
    stale = op(
        841, "object.set", page="p", target="original",
        actor="offline-old-device", clock=100,
        props={"properties": {"width": 99.0}},
    )
    result = apply(seed, stale, deleted)
    assert result == old_revision
    node = result["pages"]["p"]["objects"]["original"]
    assert node["properties"]["width"] == 2.0
    assert node["versions"]["width"] == [
        840, "device-a", 840, str(__import__("uuid").UUID(int=840)),
    ]


def test_ranked_object_reorder_survives_lower_rank_move():
    seed, deleted = seeded()
    seed = apply(
        seed, op(825, "page.create", target="destination", clock=30,
                 props={"value": {"title": "Destination"}}), deleted,
    )
    seed = apply(
        seed, op(826, "stroke.create", page="p", target="stroke", clock=35,
                 props={"value": {"sortIndex": 0}}), deleted,
    )
    for move_first in (False, True):
        state = deepcopy(seed)
        move = op(
            828, "object.move", page="p", target="stroke",
            actor="move-actor", clock=50,
            props={"toPageId": "destination", "sortIndex": 2},
        )
        reorder = op(
            827, "object.reorder",
            page="destination" if move_first else "p",
            target="stroke", actor="reorder-actor", clock=60,
            props={"properties": {"sortIndex": 9}},
        )
        for event in ((move, reorder) if move_first else (reorder, move)):
            state = apply(state, event, deleted)
        node = state["pages"]["destination"]["objects"]["stroke"]
        assert node["properties"]["sortIndex"] == 9
        assert node["versions"]["sortIndex"][0] == 60
        assert "stroke" not in state["pages"]["p"]["objects"]


def test_concurrent_format_same_atom_same_attribute_is_ranked():
    original, deleted = seeded()
    original = apply(
        original, op(
            830, "object.create", page="p", target="text",
            props={"value": {"objectType": "text"}},
        ), deleted,
    )
    atom_id = "00000000-0000-4000-8000-0000000008f1"
    original = apply(
        original, op(
            831, "text.insert", page="p", target="text",
            props={"afterAtomId": None, "atoms": [
                {"id": atom_id, "text": "Hello"}
            ]},
        ), deleted,
    )
    low = op(
        832, "text.format", page="p", target="text",
        actor="device-a", clock=900,
        props={"atomIds": [atom_id], "attributes": {"bold": True}},
    )
    high = op(
        833, "text.format", page="p", target="text",
        actor="device-z", clock=901,
        props={"atomIds": [atom_id], "attributes": {"bold": None}},
    )
    for order in permutations((low, high)):
        state = deepcopy(original)
        for event in order:
            state = apply(state, event, deleted)
        atom = state["pages"]["p"]["objects"]["text"]["textAtoms"][atom_id]
        assert "bold" not in atom["attributes"]
        assert atom["attributeVersions"]["bold"] == [
            high.logical_clock, high.actor_id, high.actor_seq,
            high.operation_id,
        ]



def test_concurrent_stroke_point_register_converges_in_both_orders():
    original, deleted = seeded()
    original = apply(
        original, op(
            910, "stroke.create", page="p", target="stroke",
            props={"value": {"points": [[1, 2, 0.5]], "width": 2.0}},
        ), deleted,
    )
    low = op(
        911, "object.transform", page="p", target="stroke",
        actor="tablet-a", clock=1000,
        props={"properties": {"points": [[10, 20, 0.5]]}},
    )
    high = op(
        912, "object.transform", page="p", target="stroke",
        actor="tablet-b", clock=1001,
        props={"properties": {
            "points": [[30, 40, 0.8], [31, 41, 0.9]],
        }},
    )
    for order in permutations((low, high)):
        state = deepcopy(original)
        for event in order:
            state = apply(state, event, deleted)
        node = state["pages"]["p"]["objects"]["stroke"]
        assert node["properties"]["points"] == [
            [30, 40, 0.8], [31, 41, 0.9],
        ]
        assert node["properties"]["width"] == 2.0
        assert node["versions"]["points"] == [
            high.logical_clock, high.actor_id, high.actor_seq,
            high.operation_id,
        ]



def test_concurrent_stale_page_edits_rebase_after_move():
    state, deleted = seeded()
    state = apply(state, op(100, "page.create", target="new"), deleted)
    state = apply(
        state,
        op(101, "stroke.create", page="p", target="ink",
           props={"value": {"colorValue": 1, "sortIndex": 0}}),
        deleted,
    )
    moved = op(
        102, "object.move", page="p", target="ink", actor="device-move",
        props={"toPageId": "new", "sortIndex": 3}, clock=200,
    )
    style = op(
        103, "object.set", page="p", target="ink", actor="device-style",
        props={"properties": {"colorValue": 2}}, clock=201,
    )
    for events in [(moved, style), (style, moved)]:
        result = deepcopy(state)
        for event in events:
            result = apply(result, event, deleted)
        assert "ink" not in result["pages"]["p"]["objects"]
        ink = result["pages"]["new"]["objects"]["ink"]
        assert ink["properties"]["colorValue"] == 2
        assert ink["properties"]["sortIndex"] == 3


def test_stale_source_from_same_actor_does_not_get_silently_rebased():
    state, deleted = seeded()
    state = apply(state, op(110, "page.create", target="new"), deleted)
    state = apply(state, op(111, "stroke.create", page="p", target="ink"), deleted)
    state = apply(
        state, op(112, "object.move", page="p", target="ink",
                  props={"toPageId": "new", "sortIndex": 0},
                  actor="device-a"), deleted,
    )
    with pytest.raises(ProjectionError, match="Causal object source"):
        apply(
            state, op(113, "object.set", page="p", target="ink",
                      actor="device-a",
                      props={"properties": {"colorValue": 2}}), deleted,
        )
