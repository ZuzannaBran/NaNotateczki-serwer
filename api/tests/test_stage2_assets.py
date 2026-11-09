"""Verified asset bytes and deterministic attach/detach projection."""
from __future__ import annotations

import asyncio
import hashlib
import tempfile
import uuid
from copy import deepcopy
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.asset_store import (
    asset_path, content_type, store_upload, valid_verified_file,
)
from app.operation_projection import ProjectionError, apply_operation
from app.operation_protocol import OperationEnvelope
from app.text_sequence import render_delta


OWNER = uuid.UUID("11111111-1111-4111-8111-111111111111")
PNG = b"\x89PNG\r\n\x1a\n" + b"verified-test-payload"
DIGEST = hashlib.sha256(PNG).hexdigest()


class FakeRequest:
    def __init__(self, data: bytes):
        self.data = data

    async def stream(self):
        yield self.data[:5]
        yield self.data[5:]


def operation(n: int, kind: str, payload: dict, *, page=None, obj=None,
              reverts=None):
    return OperationEnvelope.from_dict({
        "schemaVersion": 1,
        "operationId": str(uuid.UUID(int=n)),
        "actorId": "asset-test", "actorSeq": n,
        "documentId": "asset-document", "type": kind,
        "payload": payload,
        "dependencies": [reverts] if reverts is not None else [],
        "logicalClock": n,
        **({"pageId": page} if page is not None else {}),
        **({"objectId": obj} if obj is not None else {}),
        **({"revertsOperationId": reverts} if reverts is not None else {}),
    })


def seeded():
    state, _ = apply_operation(
        None, operation(1, "document.create", {"value": {}}), set()
    )
    state, _ = apply_operation(
        state, operation(2, "page.create", {"value": {}}, obj="page"), set()
    )
    state, _ = apply_operation(
        state, operation(3, "object.create", {"value": {
            "objectType": "image",
        }}, page="page", obj="image"), set()
    )
    return state


def attach_payload():
    return {
        "assetId": "primary", "sha256": DIGEST,
        "sizeBytes": len(PNG), "mediaType": "image/png",
    }


def test_upload_is_content_addressed_and_fsynced():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        size, media = asyncio.run(store_upload(
            FakeRequest(PNG), root, OWNER, DIGEST, 1024
        ))
        target = asset_path(root, OWNER, DIGEST)
        assert (size, media) == (len(PNG), "image/png")
        assert target.read_bytes() == PNG
        assert valid_verified_file(target, DIGEST, size)
        target.write_bytes(b"corrupt" + PNG)
        assert not valid_verified_file(target, DIGEST, size)


def test_wrong_checksum_and_too_large_upload_leave_no_final_asset():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        wrong = "1" * 64
        with pytest.raises(HTTPException, match=""):
            asyncio.run(store_upload(FakeRequest(PNG), root, OWNER, wrong, 1024))
        assert not asset_path(root, OWNER, wrong).exists()
        with pytest.raises(HTTPException) as exc:
            asyncio.run(store_upload(FakeRequest(PNG), root, OWNER, DIGEST, 4))
        assert exc.value.status_code == 413
        assert not asset_path(root, OWNER, DIGEST).exists()


def test_reject_invalid_media_type_and_symlink():
    with pytest.raises(HTTPException) as exc:
        content_type(b"<svg>not a supported image</svg>")
    assert exc.value.status_code == 415
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        target = asset_path(root, OWNER, DIGEST)
        target.parent.mkdir(parents=True)
        source = root / "source.png"
        source.write_bytes(PNG)
        target.symlink_to(source)
        assert not valid_verified_file(target, DIGEST, len(PNG))


def test_unverified_attach_fails_closed():
    state = seeded()
    change = operation(
        4, "asset.attach", attach_payload(), page="page", obj="image"
    )
    with pytest.raises(ProjectionError, match="not verified"):
        apply_operation(state, change, set())


def test_verified_attach_and_detach_and_z_order_converge():
    state = seeded()
    attach = operation(
        4, "asset.attach", attach_payload(), page="page", obj="image"
    )
    state, tombstone = apply_operation(
        state, attach, set(), verified_assets=frozenset({DIGEST})
    )
    assert tombstone is None
    node = state["pages"]["page"]["objects"]["image"]
    assert node["assets"]["primary"]["sha256"] == DIGEST
    detach = operation(
        5, "asset.detach", {"assetId": "primary"},
        page="page", obj="image",
    )
    state, _ = apply_operation(state, detach, set())
    assert node["assets"] == {}  # shared original node was mutated
    assert state["pages"]["page"]["objects"]["image"]["assets"] == {}
    state, _ = apply_operation(
        state, attach, set(), verified_assets=frozenset({DIGEST})
    )
    assert state["pages"]["page"]["objects"]["image"]["assets"] == {}



def test_replacement_requires_verified_attach_and_detach_after_switch():
    state = seeded()
    state, _ = apply_operation(
        state, operation(
            4, "asset.attach", attach_payload(), page="page", obj="image",
        ), set(), verified_assets=frozenset({DIGEST})
    )
    bad = operation(
        5, "object.set", {"properties": {"assetId": "unverified"}},
        page="page", obj="image",
    )
    with pytest.raises(ProjectionError, match="verified attachment"):
        apply_operation(state, bad, set())
    state, _ = apply_operation(
        state, operation(
            6, "object.set", {"properties": {"assetId": "primary"}},
            page="page", obj="image",
        ), set()
    )
    with pytest.raises(ProjectionError, match="active image"):
        apply_operation(state, operation(
            7, "asset.detach", {"assetId": "primary"},
            page="page", obj="image",
        ), set())
    second_digest = "b" * 64
    new_payload = {
        "assetId": "replacement", "sha256": second_digest,
        "sizeBytes": 11, "mediaType": "image/png",
    }
    state, _ = apply_operation(
        state, operation(
            8, "asset.attach", new_payload, page="page", obj="image",
        ), set(), verified_assets=frozenset({second_digest})
    )
    state, _ = apply_operation(
        state, operation(
            9, "object.set", {"properties": {"assetId": "replacement"}},
            page="page", obj="image",
        ), set()
    )
    state, _ = apply_operation(
        state, operation(
            10, "asset.detach", {"assetId": "primary"},
            page="page", obj="image",
        ), set()
    )
    node = state["pages"]["page"]["objects"]["image"]
    assert node["properties"]["assetId"] == "replacement"
    assert set(node["assets"]) == {"replacement"}


def test_asset_id_cannot_be_rebound_after_detach():
    state = seeded()
    payload = attach_payload()
    state, _ = apply_operation(
        state, operation(4, "asset.attach", payload, page="page", obj="image"),
        set(), verified_assets=frozenset({DIGEST}),
    )
    state, _ = apply_operation(
        state, operation(5, "asset.detach", {"assetId": "primary"},
                         page="page", obj="image"), set(),
    )
    assert state["assetReceipts"]["primary"]["sha256"] == DIGEST
    with pytest.raises(ProjectionError, match="already bound"):
        apply_operation(
            deepcopy(state),
            operation(6, "asset.attach",
                      {**payload, "sha256": "b" * 64},
                      page="page", obj="image"),
            set(), verified_assets=frozenset({"b" * 64}),
        )
    state, _ = apply_operation(
        state, operation(7, "asset.attach", payload,
                         page="page", obj="image"),
        set(), verified_assets=frozenset({DIGEST}),
    )
    assert state["pages"]["page"]["objects"]["image"]["assets"][
        "primary"
    ]["sha256"] == DIGEST


def test_asset_receipt_survives_owner_deletion_and_inverse():
    state = seeded()
    payload = attach_payload()
    state, _ = apply_operation(
        state, operation(4, "asset.attach", payload, page="page", obj="image"),
        set(), verified_assets=frozenset({DIGEST}),
    )
    state, marker = apply_operation(
        state, operation(5, "object.delete", {}, page="page", obj="image"),
        set(),
    )
    assert marker == ("object", "page", "image")
    state, _ = apply_operation(
        state, operation(6, "object.create",
                         {"value": {"objectType": "text"}},
                         page="page", obj="restored"), {marker},
    )
    with pytest.raises(ProjectionError, match="already bound"):
        apply_operation(
            deepcopy(state),
            operation(7, "asset.attach",
                      {**payload, "sha256": "b" * 64},
                      page="page", obj="restored"),
            {marker}, verified_assets=frozenset({"b" * 64}),
        )
    state, _ = apply_operation(
        state, operation(8, "asset.attach", payload,
                         page="page", obj="restored"),
        {marker}, verified_assets=frozenset({DIGEST}),
    )
    assert state["assetReceipts"]["primary"]["sha256"] == DIGEST


def test_missing_historical_receipt_fails_closed():
    state = seeded()
    payload = attach_payload()
    state, _ = apply_operation(
        state, operation(4, "asset.attach", payload, page="page", obj="image"),
        set(), verified_assets=frozenset({DIGEST}),
    )
    state, _ = apply_operation(
        state, operation(5, "asset.detach", {"assetId": "primary"},
                         page="page", obj="image"), set(),
    )
    state.pop("assetReceipts")
    with pytest.raises(ProjectionError, match="no durable receipt"):
        apply_operation(
            deepcopy(state),
            operation(6, "asset.attach", payload,
                      page="page", obj="image"),
            set(), verified_assets=frozenset({DIGEST}),
        )


def test_malformed_attach_or_detach_rejected():
    state = seeded()
    for kind, payload in [
        ("asset.attach", {"assetId": "p", "sha256": DIGEST}),
        ("asset.attach", {**attach_payload(), "mediaType": "text/html"}),
        ("asset.attach", {**attach_payload(), "sizeBytes": -1}),
        ("asset.detach", {"assetId": "primary", "junk": True}),
    ]:
        with pytest.raises(ProjectionError):
            apply_operation(
                state, operation(6, kind, payload, page="page", obj="image"),
                set(), verified_assets=frozenset({DIGEST}),
            )


def test_verified_asset_identity_can_be_shared_by_two_owners():
    state = seeded()
    state, _ = apply_operation(
        state, operation(4, "object.create",
                         {"value": {"objectType": "text"}},
                         page="page", obj="text-other"), set(),
    )
    for sequence, owner in ((5, "image"), (6, "text-other")):
        state, _ = apply_operation(
            state, operation(sequence, "asset.attach", attach_payload(),
                             page="page", obj=owner),
            set(), verified_assets=frozenset({DIGEST}),
        )
    for owner in ("image", "text-other"):
        assert state["pages"]["page"]["objects"][owner]["assets"][
            "primary"
        ]["sha256"] == DIGEST
    assert state["assetReceipts"]["primary"]["sha256"] == DIGEST


@pytest.mark.parametrize("changes", [
    {"sha256": "b" * 64},
    {"sizeBytes": len(PNG) + 1},
    {"mediaType": "application/pdf"},
])
def test_shared_asset_identity_rejects_conflicting_receipts(changes):
    state = seeded()
    state, _ = apply_operation(
        state, operation(4, "object.create",
                         {"value": {"objectType": "text"}},
                         page="page", obj="text-other"), set(),
    )
    state, _ = apply_operation(
        state, operation(5, "asset.attach", attach_payload(),
                         page="page", obj="image"),
        set(), verified_assets=frozenset({DIGEST}),
    )
    before = deepcopy(state)
    with pytest.raises(ProjectionError, match="already bound"):
        apply_operation(
            state, operation(6, "asset.attach",
                             {**attach_payload(), **changes},
                             page="page", obj="text-other"),
            set(), verified_assets=frozenset({DIGEST, "b" * 64}),
        )
    assert state == before


def test_rejected_legacy_alias_does_not_insert_an_empty_receipt_ledger():
    state = seeded()
    state, _ = apply_operation(
        state, operation(4, "object.create",
                         {"value": {"objectType": "text"}},
                         page="page", obj="text-other"), set(),
    )
    state, _ = apply_operation(
        state, operation(5, "asset.attach", attach_payload(),
                         page="page", obj="image"),
        set(), verified_assets=frozenset({DIGEST}),
    )
    # A v2 snapshot that predates the document-wide receipt ledger must
    # not acquire a partial ledger when an incompatible attach fails.
    state.pop("assetReceipts")
    before = deepcopy(state)
    with pytest.raises(ProjectionError, match="already bound"):
        apply_operation(
            state, operation(6, "asset.attach",
                             {**attach_payload(), "sha256": "b" * 64},
                             page="page", obj="text-other"),
            set(), verified_assets=frozenset({"b" * 64}),
        )
    assert state == before
    assert "assetReceipts" not in state


@pytest.mark.parametrize("embed_type", ["image", "pdf"])
@pytest.mark.parametrize("deleted_target", ["embed", "object", "page"])
def test_quill_inverse_reuses_verified_asset_without_reviving_deleted_ids(
    embed_type, deleted_target,
):
    state = seeded()
    tombstones = set()
    receipt = {
        **attach_payload(),
        "mediaType": "application/pdf" if embed_type == "pdf" else "image/png",
    }

    def commit(change, *, verified=False):
        nonlocal state
        state, marker = apply_operation(
            deepcopy(state), change, tombstones,
            verified_assets=frozenset({DIGEST}) if verified else frozenset(),
        )
        if marker is not None:
            tombstones.add(marker)

    commit(operation(4, "page.create", {"value": {}}, obj="other-page"))
    commit(operation(5, "object.create", {"value": {"objectType": "text"}},
                     page="page", obj="text"))
    commit(operation(6, "asset.attach", receipt, page="page", obj="text"),
           verified=True)
    atoms = [
        {"id": str(uuid.UUID(int=2000)), "text": "T",
         "attributes": {"bold": True}},
        *[{"id": str(uuid.UUID(int=n)), "text": "\ufffc",
           "embed": {embed_type: "asset:primary"}} for n in (2001, 2002)],
        {"id": str(uuid.UUID(int=2003)), "text": "\n"},
    ]
    commit(operation(7, "text.insert", {"afterAtomId": None, "atoms": atoms},
                     page="page", obj="text"))
    original = render_delta(state["pages"]["page"]["objects"]["text"][
        "textAtoms"
    ])
    if deleted_target == "embed":
        commit(operation(8, "text.delete", {
            "atomIds": [atom["id"] for atom in atoms[1:3]],
        }, page="page", obj="text"))
        deleted = operation(9, "asset.detach", {"assetId": "primary"},
                            page="page", obj="text")
    elif deleted_target == "object":
        deleted = operation(8, "object.delete", {}, page="page", obj="text")
    else:
        deleted = operation(8, "page.delete", {}, obj="page")
    commit(deleted)
    page = "restored-page" if deleted_target == "page" else "page"
    obj = "text" if deleted_target == "embed" else "restored-text"
    if deleted_target == "page":
        commit(operation(10, "page.create", {"value": {}}, obj=page,
                         reverts=deleted.operation_id))
    if deleted_target != "embed":
        commit(operation(11, "object.create", {"value": {"objectType": "text"}},
                         page=page, obj=obj, reverts=deleted.operation_id))
    attach = operation(12, "asset.attach", receipt, page=page, obj=obj,
                       reverts=deleted.operation_id)
    # A previous upload/owner does not bypass verification of this attachment.
    with pytest.raises(ProjectionError, match="not verified"):
        commit(attach)
    commit(attach, verified=True)
    restored_atoms = [
        {**atom, "id": str(uuid.UUID(int=uuid.UUID(atom["id"]).int + 1000))}
        for atom in (atoms[1:3] if deleted_target == "embed" else atoms)
    ]
    restored = operation(13, "text.insert", {
        "afterAtomId": atoms[0]["id"] if deleted_target == "embed" else None,
        "atoms": restored_atoms,
    }, page=page, obj=obj, reverts=deleted.operation_id)
    commit(restored)
    node = state["pages"][page]["objects"][obj]
    assert render_delta(node["textAtoms"]) == original
    assert node["assets"] == {"primary": {
        key: receipt[key] for key in ("sha256", "sizeBytes", "mediaType")
    }}
    assert restored.reverts_operation_id == deleted.operation_id
    assert deleted.operation_id in restored.dependencies
    if deleted_target == "embed":
        commit(operation(14, "text.delete", {
            "atomIds": [atom["id"] for atom in restored_atoms],
        }, page=page, obj=obj, reverts=restored.operation_id))
        commit(operation(15, "asset.detach", {"assetId": "primary"},
                         page=page, obj=obj, reverts=restored.operation_id))
        assert state["pages"][page]["objects"][obj]["assets"] == {}
    elif deleted_target == "object":
        assert ("object", "page", "text") in tombstones
        commit(operation(14, "object.delete", {}, page=page, obj=obj,
                         reverts=restored.operation_id))
        assert obj not in state["pages"][page]["objects"]
        with pytest.raises(ProjectionError, match="deleted"):
            commit(operation(15, "object.create", {"value": {"objectType": "text"}},
                             page="page", obj="text"))
    else:
        assert ("page", "", "page") in tombstones
        commit(operation(14, "page.delete", {}, obj=page,
                         reverts=restored.operation_id))
        assert page not in state["pages"]
        with pytest.raises(ProjectionError, match="deleted"):
            commit(operation(15, "page.create", {"value": {}}, obj="page"))



@pytest.mark.parametrize("corrupt", [
    {"assetVersions": {"other": [1, "actor", True, "operation"]}},
    {"assetVersions": ["invalid"]},
    {"assets": ["not-a-map"]},
])
def test_corrupt_other_owner_does_not_create_receipt(corrupt):
    state = seeded()
    state, _ = apply_operation(
        state, operation(
            4, "object.create", {"value": {"objectType": "text"}},
            page="page", obj="other",
        ), set(),
    )
    state["pages"]["page"]["objects"]["other"].update(corrupt)
    before = deepcopy(state)
    with pytest.raises(ProjectionError, match="Corrupt persisted"):
        apply_operation(
            state, operation(
                5, "asset.attach", attach_payload(), page="page", obj="image",
            ), set(), verified_assets=frozenset({DIGEST}),
        )
    assert state == before
    assert "assetReceipts" not in state
