import hashlib
import json
import uuid

import pytest

from app.models import DeviceBackup
from app.routers.backups import validate_envelope


def compact_sha256(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def backup_record(device_id: uuid.UUID) -> DeviceBackup:
    return DeviceBackup(
        id=uuid.uuid4(),
        owner_id=uuid.uuid4(),
        device_id=device_id,
        storage_key="internal/generated.json",
        sha256="a" * 64,
        size_bytes=100,
        format_version=2,
        status="pending",
    )


def test_validates_complete_backup_envelope() -> None:
    device_id = uuid.uuid4()
    payload = {
        "notebooks": [],
        "libraryState": {"folders": ["Zażółć"]},
        "studyPlannerState": {"sessions": []},
    }
    envelope = {
        "format": "nanotateczki-server-backup",
        "version": 2,
        "deviceId": str(device_id),
        "checksumAlgorithm": "sha256",
        "checksum": compact_sha256(payload),
        "payload": payload,
    }

    validate_envelope(envelope, backup_record(device_id))


def test_rejects_tampering_and_cross_device_finalize() -> None:
    device_id = uuid.uuid4()
    payload = {"notebooks": [], "libraryState": {}, "studyPlannerState": {}}
    envelope = {
        "format": "nanotateczki-server-backup",
        "version": 2,
        "deviceId": str(device_id),
        "checksumAlgorithm": "sha256",
        "checksum": compact_sha256(payload),
        "payload": payload,
    }
    envelope["payload"]["notebooks"].append({"uid": "injected"})

    with pytest.raises(ValueError):
        validate_envelope(envelope, backup_record(device_id))

    envelope["checksum"] = compact_sha256(envelope["payload"])
    with pytest.raises(ValueError):
        validate_envelope(envelope, backup_record(uuid.uuid4()))


def test_rejects_non_object_and_incomplete_backup_payload() -> None:
    record = backup_record(uuid.uuid4())
    with pytest.raises(ValueError):
        validate_envelope([], record)

    payload = {"notebooks": [], "libraryState": {}}
    envelope = {
        "format": "nanotateczki-server-backup",
        "version": 2,
        "deviceId": str(record.device_id),
        "checksumAlgorithm": "sha256",
        "checksum": compact_sha256(payload),
        "payload": payload,
    }
    with pytest.raises(ValueError):
        validate_envelope(envelope, record)


def test_rejects_malformed_or_duplicate_notebooks_in_verified_backup() -> None:
    record = backup_record(uuid.uuid4())
    payload = {
        "notebooks": [{"uid": "n1", "pages": "not-a-list"}],
        "libraryState": {},
        "studyPlannerState": {},
    }
    envelope = {
        "format": "nanotateczki-server-backup",
        "version": 2,
        "deviceId": str(record.device_id),
        "checksumAlgorithm": "sha256",
        "payload": payload,
    }
    envelope["checksum"] = compact_sha256(payload)
    with pytest.raises(ValueError):
        validate_envelope(envelope, record)

    notebook = {
        "uid": "n1",
        "title": "Notes",
        "kind": 0,
        "folder": "Notes",
        "createdAt": "2026-01-01T00:00:00Z",
        "updatedAt": "2026-01-01T00:00:00Z",
        "pages": [],
    }
    payload["notebooks"] = [notebook, dict(notebook)]
    envelope["checksum"] = compact_sha256(payload)
    with pytest.raises(ValueError, match="duplicate notebook UID"):
        validate_envelope(envelope, record)
