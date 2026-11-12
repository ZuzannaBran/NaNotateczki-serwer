"""Stage 9: fail-closed account v2 archive verification and failure modes."""

import asyncio
import base64
import hashlib
import json
import uuid
from types import SimpleNamespace

import pytest

from app.models import DeviceBackup
from app.routers.backups import (
    _validate_verified_v2_history,
    validate_envelope,
)
from app.schemas import BackupCreate

UUID1 = "11111111-1111-4111-8111-111111111111"


def op(document="note-1"):
    return {
        "schemaVersion": 1,
        "operationId": UUID1,
        "actorId": "actor-test",
        "actorSeq": 1,
        "documentId": document,
        "type": "document.create",
        "payload": {"value": {"title": "hello"}},
        "dependencies": [],
        "logicalClock": 1,
    }


def fixture(*, cursor=7, media=False):
    owner, device = uuid.uuid4(), uuid.uuid4()
    entry = op()
    operations = [{"cursor": cursor, "operation": entry}]
    blobs = {}
    if media:
        data = b"valid-pdf-bytes"
        digest = hashlib.sha256(data).hexdigest()
        entry = {
            **entry, "type": "asset.attach", "pageId": "page-1",
            "objectId": "object-1", "payload": {
                "assetId": "file-1", "sha256": digest,
                "sizeBytes": len(data), "mediaType": "application/pdf",
            },
        }
        operations = [{"cursor": cursor, "operation": entry}]
        blobs[digest] = {
            "sizeBytes": len(data),
            "mediaType": "application/pdf",
            "bytesBase64": base64.b64encode(data).decode(),
        }
    record = DeviceBackup(
        id=uuid.uuid4(), owner_id=owner, device_id=device,
        storage_key=f"{owner}/archive.json", sha256="a" * 64,
        size_bytes=200, format_version=3, status="pending",
    )
    payload = {
        "notebooks": [],
        "libraryState": {"folders": []},
        "studyPlannerState": {"sessions": []},
        "operationHistory": operations,
        "assetBlobs": blobs,
    }
    envelope = {
        "format": "nanotateczki-server-backup",
        "version": 3,
        "protocol": "operations-v2",
        "deviceId": str(device),
        "accountId": str(owner),
        "schemaVersion": 8,
        "operationCheckpoint": {f"account:{owner}": cursor},
        "checksumAlgorithm": "sha256",
        "payload": payload,
    }
    return record, envelope


def sign(envelope):
    envelope["checksum"] = hashlib.sha256(
        json.dumps(
            envelope["payload"], ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def test_v2_schema_and_empty_signed_archive():
    assert BackupCreate(
        deviceId=uuid.uuid4(), sha256="a" * 64,
        sizeBytes=400, formatVersion=3,
    ).formatVersion == 3
    record, envelope = fixture()
    sign(envelope)
    validate_envelope(envelope, record)
    envelope["operationCheckpoint"] = {f"account:{record.owner_id}": 0}
    envelope["payload"]["operationHistory"] = []
    sign(envelope)
    validate_envelope(envelope, record)


def test_rejects_missing_prefix_and_wrong_account():
    record, envelope = fixture()
    for mutation in (
        lambda e: e["payload"]["operationHistory"].clear(),
        lambda e: e["payload"]["operationHistory"][0].update(cursor=8),
        lambda e: e["operationCheckpoint"].update({"account:foreign": 7}),
        lambda e: e.update(accountId=str(uuid.uuid4())),
        lambda e: e.update(protocol="snapshot-v1"),
    ):
        import copy
        mutated = copy.deepcopy(envelope)
        mutation(mutated)
        sign(mutated)
        with pytest.raises((ValueError, KeyError)):
            validate_envelope(mutated, record)


def test_corrupted_media_and_wrong_receipt_rejected():
    record, envelope = fixture(media=True)
    sign(envelope)
    validate_envelope(envelope, record)
    digest = next(iter(envelope["payload"]["assetBlobs"]))
    blob = envelope["payload"]["assetBlobs"][digest]
    blob["bytesBase64"] = base64.b64encode(b"bad-image-data").decode()
    sign(envelope)
    with pytest.raises(ValueError, match="corrupt"):
        validate_envelope(envelope, record)
    blob["bytesBase64"] = "this-is-not-base64"
    sign(envelope)
    with pytest.raises(ValueError, match="encoding"):
        validate_envelope(envelope, record)
    blob["bytesBase64"] = base64.b64encode(b"valid-pdf-bytes").decode()
    blob["sizeBytes"] += 1
    sign(envelope)
    with pytest.raises(ValueError, match="receipt"):
        validate_envelope(envelope, record)


def test_server_refuses_forged_or_omitted_journal_history():
    record, envelope = fixture(cursor=11)
    sign(envelope)
    validate_envelope(envelope, record)

    class FakeSession:
        async def stream_scalars(self, statement):
            assert "document_operations" in str(statement).lower()
            async def rows():
                yield SimpleNamespace(cursor=11, envelope=op())
            return rows()

    asyncio.run(_validate_verified_v2_history(
        envelope, record, FakeSession(),
    ))
    envelope["payload"]["operationHistory"][0]["operation"]["payload"] = {
        "value": {"title": "forged"},
    }
    sign(envelope)
    validate_envelope(envelope, record)
    with pytest.raises(ValueError, match="committed journal"):
        asyncio.run(_validate_verified_v2_history(
            envelope, record, FakeSession(),
        ))


def test_old_format_does_not_accept_operational_archive():
    record, envelope = fixture()
    record.format_version = 2
    sign(envelope)
    with pytest.raises(ValueError, match="unsupported envelope"):
        validate_envelope(envelope, record)
def test_content_addressed_references_are_not_reserialized(monkeypatch):
    from app.routers import backups

    record, envelope = fixture(media=True)
    digest = next(iter(envelope["payload"]["assetBlobs"]))
    previous = envelope["payload"]["assetBlobs"][digest]
    envelope["payload"]["assetBlobs"][digest] = {
        "sizeBytes": previous["sizeBytes"],
        "mediaType": previous["mediaType"],
        "storage": "verified-v2",
    }
    sign(envelope)
    validate_envelope(envelope, record)
    assert "bytesBase64" not in json.dumps(envelope)

    class FakeSession:
        async def stream_scalars(self, statement):
            async def rows():
                yield SimpleNamespace(
                    cursor=7,
                    envelope=envelope["payload"]["operationHistory"][0]["operation"],
                )
            return rows()

        async def get(self, model, identity):
            assert identity == (record.owner_id, digest)
            return SimpleNamespace(
                size_bytes=previous["sizeBytes"],
                media_type=previous["mediaType"],
            )

    monkeypatch.setattr(backups, "valid_verified_file", lambda *args: True)
    asyncio.run(_validate_verified_v2_history(
        envelope, record, FakeSession(),
    ))
    monkeypatch.setattr(backups, "valid_verified_file", lambda *args: False)
    with pytest.raises(ValueError, match="not durable"):
        asyncio.run(_validate_verified_v2_history(
            envelope, record, FakeSession(),
        ))


def test_verified_reference_needs_no_embedded_base64_but_inline_bytes_do():
    record, envelope = fixture(media=True)
    digest = next(iter(envelope["payload"]["assetBlobs"]))
    stored = envelope["payload"]["assetBlobs"][digest]
    envelope["payload"]["assetBlobs"][digest] = {
        "sizeBytes": stored["sizeBytes"],
        "mediaType": stored["mediaType"],
        "storage": "verified-v2",
    }
    sign(envelope)
    validate_envelope(envelope, record)

    for malformed in (None, [], {"unexpected": True}, 42):
        envelope["payload"]["assetBlobs"][digest] = {
            "sizeBytes": stored["sizeBytes"],
            "mediaType": stored["mediaType"],
            "bytesBase64": malformed,
        }
        sign(envelope)
        with pytest.raises(ValueError, match="base64"):
            validate_envelope(envelope, record)


def test_wrong_backup_format_is_rejected_after_protocol_fence(monkeypatch):
    import asyncio

    from fastapi import HTTPException

    from app.routers import backups

    called = []

    async def fence(session, owner_id, *, v2):
        called.append(v2)
        raise HTTPException(status_code=409, detail="wrong protocol")

    monkeypatch.setattr(backups, "_require_backup_protocol", fence)

    class Session:
        async def execute(self, statement):
            return None

    async def rejected():
        return await backups._create_backup(
            SimpleNamespace(sizeBytes=10, formatVersion=9),
            SimpleNamespace(id=uuid.uuid4()), Session(), v2=False,
        )

    with pytest.raises(HTTPException) as rejected_result:
        asyncio.run(rejected())
    assert rejected_result.value.status_code == 409
    assert called == [False]


def test_reference_asset_with_modified_owner_or_mime_is_rejected():
    record, envelope = fixture(media=True)
    digest = next(iter(envelope["payload"]["assetBlobs"]))
    record_blob = envelope["payload"]["assetBlobs"][digest]
    record_blob.clear()
    record_blob.update({
        "sizeBytes": 15,
        "mediaType": "image/png",
        "storage": "verified-v2",
    })
    sign(envelope)
    with pytest.raises(ValueError, match="receipt"):
        validate_envelope(envelope, record)
