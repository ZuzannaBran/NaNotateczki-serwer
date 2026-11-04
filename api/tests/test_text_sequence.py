"""Stage-6 share text regression tests for deterministic operation replay."""
from __future__ import annotations

import copy
import uuid

import pytest

from app.operation_projection import ProjectionError, apply_operation
from app.operation_protocol import OperationEnvelope
from app.text_sequence import TextSequenceError, render_text


DOC = "doc-stage-six"
PAGE = "page-stage-six"
OBJECT = "text-stage-six"
A = "00000000-0000-4000-8000-000000000001"
B = "00000000-0000-4000-8000-000000000002"
C = "00000000-0000-4000-8000-000000000003"


def operation(kind: str, payload: dict, *, actor: str = "alice",
              clock: int = 1, seq: int = 1,
              object_id: str | None = OBJECT,
              page_id: str | None = PAGE) -> OperationEnvelope:
    return OperationEnvelope.from_dict({
        "schemaVersion": 1,
        "operationId": str(uuid.uuid4()), "actorId": actor,
        "actorSeq": seq, "documentId": DOC,
        **({"pageId": page_id} if page_id is not None else {}),
        **({"objectId": object_id} if object_id is not None else {}),
        "type": kind, "payload": payload,
        "logicalClock": clock, "dependencies": [],
    })


def initial_state() -> dict:
    doc = operation("document.create", {"value": {"title": "Untitled"}},
                    object_id=None, page_id=None)
    page = operation("page.create", {"value": {"title": "Page"}},
                     object_id=PAGE, page_id=None)
    text = operation("object.create", {"value": {
        "objectType": "text", "text": "", "width": 200,
    }})
    state, _ = apply_operation(None, doc, set())
    state, _ = apply_operation(state, page, set())
    state, _ = apply_operation(state, text, set())
    return state


def node(state: dict) -> dict:
    return state["pages"][PAGE]["objects"][OBJECT]


def test_concurrent_insertions_converge_with_reverse_arrival() -> None:
    first = operation("text.insert", {"afterAtomId": None,
                                     "atoms": [{"id": A, "text": "A"}]},
                      actor="alice", clock=10)
    second = operation("text.insert", {"afterAtomId": None,
                                      "atoms": [{"id": B, "text": "B"}]},
                       actor="bob", clock=10)
    one, two = initial_state(), initial_state()
    for change in (first, second):
        one, _ = apply_operation(one, change, set())
    for change in (second, first):
        two, _ = apply_operation(two, change, set())
    assert node(one)["textAtoms"] == node(two)["textAtoms"]
    assert node(one)["properties"]["text"] == node(two)["properties"]["text"]
    assert node(one)["properties"]["text"] == "BA"


def test_later_insert_at_caret_precedes_existing_successor() -> None:
    state = initial_state()
    first = operation("text.insert", {
        "afterAtomId": None,
        "atoms": [{"id": A, "text": "A"}, {"id": B, "text": "B"}],
    }, clock=4)
    caret = operation("text.insert", {
        "afterAtomId": A, "atoms": [{"id": C, "text": "X"}],
    }, clock=5)
    state, _ = apply_operation(state, first, set())
    state, _ = apply_operation(state, caret, set())
    assert node(state)["properties"]["text"] == "AXB"


def test_delete_retains_anchor_for_delayed_insert() -> None:
    state = initial_state()
    insert = operation("text.insert", {"afterAtomId": None,
                                      "atoms": [{"id": A, "text": "A"}]})
    delete = operation("text.delete", {"atomIds": [A]}, clock=3)
    follow = operation("text.insert", {"afterAtomId": A,
                                      "atoms": [{"id": B, "text": "B"}]},
                       clock=4)
    for change in (insert, delete, follow):
        state, _ = apply_operation(state, change, set())
    assert node(state)["properties"]["text"] == "B"
    assert node(state)["textAtoms"][A]["deleted"] is True


def test_text_and_transforms_update_independent_properties() -> None:
    state = initial_state()
    edits = [
        operation("text.insert", {"afterAtomId": None,
                                  "atoms": [{"id": A, "text": "hi"}]},
                  clock=4),
        operation("object.transform", {"properties": {"dx": 22}}, clock=5),
    ]
    for change in edits:
        state, _ = apply_operation(state, change, set())
    assert node(state)["properties"]["dx"] == 22
    assert node(state)["properties"]["text"] == "hi"


def test_text_cannot_be_overwritten_by_stale_property_set() -> None:
    state = initial_state()
    state, _ = apply_operation(
        state, operation("text.insert", {"afterAtomId": None,
                                        "atoms": [{"id": A, "text": "hi"}]}),
        set(),
    )
    with pytest.raises(ProjectionError, match="cannot be replaced"):
        apply_operation(
            state, operation("object.set", {"properties": {"text": "old"}}),
            set(),
        )


@pytest.mark.parametrize("payload", [
    {"afterAtomId": None, "atoms": [{"id": A, "text": "x"},
                                    {"id": A, "text": "y"}]},
    {"afterAtomId": None, "atoms": [{"id": [], "text": "x"}]},
    {"afterAtomId": C, "atoms": [{"id": A, "text": "x"}]},
    {"afterAtomId": None, "atoms": [{"id": A, "text": ""}]},
    {"afterAtomId": None, "atoms": [{"id": A, "text": "x", "extra": 1}]},
])
def test_malformed_insert_rolls_back(payload: dict) -> None:
    state = initial_state()
    before = copy.deepcopy(state)
    with pytest.raises(ProjectionError):
        apply_operation(state, operation("text.insert", payload), set())
    assert state == before


def test_delete_unknown_atom_rejected_and_preserves_state() -> None:
    state = initial_state()
    before = copy.deepcopy(state)
    with pytest.raises(ProjectionError):
        apply_operation(
            state, operation("text.delete", {"atomIds": [A]}), set(),
        )
    assert state == before


def test_text_graph_rejects_cycles() -> None:
    atoms = {
        A: {"after": B, "text": "a", "deleted": False, "order": [1, "a", 1, A, 0]},
        B: {"after": A, "text": "b", "deleted": False, "order": [2, "b", 1, B, 0]},
    }
    with pytest.raises(TextSequenceError, match="Unreachable"):
        render_text(atoms)



def test_object_type_cannot_be_changed_by_property_set() -> None:
    state = initial_state()
    with pytest.raises(ProjectionError, match="Identity"):
        apply_operation(
            state, operation("object.set", {
                "properties": {"objectType": "image"},
            }), set(),
        )


def test_nontext_objects_reject_crdt_text_operations() -> None:
    state = initial_state()
    node(state)["properties"]["objectType"] = "image"
    with pytest.raises(ProjectionError, match="text object"):
        apply_operation(
            state, operation("text.insert", {
                "afterAtomId": None,
                "atoms": [{"id": A, "text": "bad"}],
            }), set(),
        )


def test_existing_plain_text_requires_explicit_migration() -> None:
    state = initial_state()
    node(state)["properties"]["text"] = "legacy"
    with pytest.raises(ProjectionError, match="explicit migration"):
        apply_operation(
            state, operation("text.insert", {"afterAtomId": None,
                                            "atoms": [{"id": A, "text": "x"}]}),
            set(),
        )


def test_quill_embed_requires_verified_attachment_and_round_trips() -> None:
    state = initial_state()
    embed = operation("text.insert", {
        "afterAtomId": None,
        "atoms": [{"id": A, "text": "\ufffc",
                   "embed": {"image": "asset:photo-1"},
                   "attributes": {"width": 120}}],
    })
    before = copy.deepcopy(state)
    with pytest.raises(ProjectionError, match="matching attached asset"):
        apply_operation(state, embed, set())
    assert state == before

    attachment = operation("asset.attach", {
        "assetId": "photo-1", "sha256": "a" * 64,
        "sizeBytes": 100, "mediaType": "image/png",
    })
    with pytest.raises(ProjectionError, match="not verified"):
        apply_operation(state, attachment, set())
    state, _ = apply_operation(
        state, attachment, set(), verified_assets=frozenset({"a" * 64}),
    )
    state, _ = apply_operation(state, embed, set())
    assert node(state)["properties"]["text"] == "\ufffc"
    assert node(state)["properties"]["delta"] == [{
        "insert": {"image": "asset:photo-1"},
        "attributes": {"width": 120},
    }]
    with pytest.raises(ProjectionError, match="embedded asset"):
        apply_operation(
            state, operation("asset.detach", {"assetId": "photo-1"},
                             clock=10), set(),
        )
    state, _ = apply_operation(
        state, operation("text.delete", {"atomIds": [A]}, clock=11), set(),
    )
    state, _ = apply_operation(
        state, operation("asset.detach", {"assetId": "photo-1"},
                         clock=12), set(),
    )
    assert node(state).get("assets", {}) == {}
    assert node(state)["properties"]["delta"] == []


@pytest.mark.parametrize("embed,text", [
    ({"image": "https://example.com/x.png"}, "\ufffc"),
    ({"image": "asset:photo", "video": "asset:other"}, "\ufffc"),
    ({"video": "asset:photo"}, "\ufffc"),
    ({"image": "asset:photo"}, "x"),
    ({"image": "asset:photo\\n"}, "\ufffc"),
])
def test_quill_embeds_fail_closed(embed: dict, text: str) -> None:
    state = initial_state()
    before = copy.deepcopy(state)
    with pytest.raises(ProjectionError):
        apply_operation(
            state, operation("text.insert", {
                "afterAtomId": None,
                "atoms": [{"id": A, "text": text, "embed": embed}],
            }), set(),
        )
    assert state == before


def test_pdf_embed_rejects_an_image_attachment() -> None:
    state = initial_state()
    state, _ = apply_operation(state, operation("asset.attach", {
        "assetId": "file-1", "sha256": "b" * 64,
        "sizeBytes": 100, "mediaType": "image/png",
    }), set(), verified_assets=frozenset({"b" * 64}))
    with pytest.raises(ProjectionError, match="matching attached asset"):
        apply_operation(state, operation("text.insert", {
            "afterAtomId": None,
            "atoms": [{"id": A, "text": "\ufffc",
                       "embed": {"pdf": "asset:file-1"}}],
        }), set())


@pytest.mark.parametrize("bad_id", ["bad\\nasset", "bad\\x7fasset", ""])
def test_asset_ids_reject_controls_and_empty_values(bad_id: str) -> None:
    state = initial_state()
    for kind, payload in [
        ("asset.attach", {
            "assetId": bad_id, "sha256": "a" * 64,
            "sizeBytes": 10, "mediaType": "image/png",
        }),
        ("asset.detach", {"assetId": bad_id}),
    ]:
        before = copy.deepcopy(state)
        with pytest.raises(ProjectionError, match="Invalid asset"):
            apply_operation(
                state, operation(kind, payload), set(),
                verified_assets=frozenset({"a" * 64}),
            )
        assert state == before
