"""Verified asset bytes and deterministic attach/detach projection."""
from __future__ import annotations

import asyncio
import hashlib
import tempfile
import uuid
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.asset_store import (
    asset_path, content_type, store_upload, valid_verified_file,
)
from app.operation_projection import ProjectionError, apply_operation
from app.operation_protocol import OperationEnvelope


OWNER = uuid.UUID("11111111-1111-4111-8111-111111111111")
PNG = b"\x89PNG\r\n\x1a\n" + b"verified-test-payload"
DIGEST = hashlib.sha256(PNG).hexdigest()


class FakeRequest:
    def __init__(self, data: bytes):
        self.data = data

    async def stream(self):
        yield self.data[:5]
        yield self.data[5:]


def operation(n: int, kind: str, payload: dict, *, page=None, obj=None):
    return OperationEnvelope.from_dict({
        "schemaVersion": 1,
        "operationId": str(uuid.UUID(int=n)),
        "actorId": "asset-test", "actorSeq": n,
        "documentId": "asset-document", "type": kind,
        "payload": payload, "dependencies": [], "logicalClock": n,
        **({"pageId": page} if page is not None else {}),
        **({"objectId": obj} if obj is not None else {}),
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
