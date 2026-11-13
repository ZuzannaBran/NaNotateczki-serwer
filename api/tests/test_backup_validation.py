import asyncio
import hashlib
import json
import uuid
from types import SimpleNamespace

import pytest

from app.models import DeviceBackup
from app.routers.backups import list_v2_backups, original_payload_json, validate_envelope


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

def test_accepts_dart_json_number_and_string_representation() -> None:
    record = backup_record(uuid.uuid4())
    payload_json = (
        b'{"notebooks":[],"libraryState":{"label":"\\u0141odz"},'
        b'"studyPlannerState":{"value":1e-7,"nested":{"payload":"a}b"}}}'
    )
    digest = hashlib.sha256(payload_json).hexdigest()
    envelope = {
        "format": "nanotateczki-server-backup",
        "version": 2,
        "deviceId": str(record.device_id),
        "checksumAlgorithm": "sha256",
        "checksum": digest,
    }
    prefix = json.dumps(envelope, separators=(",", ":"))[:-1].encode()
    raw = prefix + b',"payload":' + payload_json + b"}"

    assert bytes(original_payload_json(raw)) == payload_json
    with pytest.raises(ValueError, match="payload checksum mismatch"):
        validate_envelope(json.loads(raw), record)
    validate_envelope(json.loads(raw), record, original_json=raw)


def test_rejects_tampering_and_malformed_notebooks_with_wire_checksum() -> None:
    record = backup_record(uuid.uuid4())
    payload_json = (
        b'{"notebooks":[],"libraryState":{},"studyPlannerState":{"value":1e-7}}'
    )
    envelope = {
        "format": "nanotateczki-server-backup",
        "version": 2,
        "deviceId": str(record.device_id),
        "checksumAlgorithm": "sha256",
        "checksum": hashlib.sha256(payload_json).hexdigest(),
    }
    prefix = json.dumps(envelope, separators=(",", ":"))[:-1].encode()
    tampered = prefix + b',"payload":' + payload_json.replace(b"1e-7", b"2e-7") + b"}"
    with pytest.raises(ValueError, match="payload checksum mismatch"):
        validate_envelope(json.loads(tampered), record, original_json=tampered)

    malformed = b'{"notebooks":[{"uid":"bad"}],"libraryState":{},"studyPlannerState":{}}'
    bad_raw = prefix + b',"payload":' + malformed + b"}"
    with pytest.raises(ValueError, match="notebook payload is incomplete"):
        validate_envelope(json.loads(bad_raw), record, original_json=bad_raw)



def test_account_preferences_are_optional_but_validated() -> None:
    record = backup_record(uuid.uuid4())
    payload = {
        "notebooks": [],
        "libraryState": {},
        "studyPlannerState": {},
        "appPreferences": {"darkMode": True},
        "editorPreferences": {},
        "recentColors": [4278190080],
        "backupPreferences": {"version": 1, "idleSeconds": 900},
    }
    envelope = {
        "format": "nanotateczki-server-backup",
        "version": 2,
        "deviceId": str(record.device_id),
        "checksumAlgorithm": "sha256",
        "payload": payload,
    }
    envelope["checksum"] = compact_sha256(payload)
    validate_envelope(envelope, record)

    payload["recentColors"] = [True]
    envelope["checksum"] = compact_sha256(payload)
    with pytest.raises(ValueError):
        validate_envelope(envelope, record)

    payload["recentColors"] = []
    payload["backupPreferences"]["idleSeconds"] = 1
    envelope["checksum"] = compact_sha256(payload)
    with pytest.raises(ValueError):
        validate_envelope(envelope, record)


def test_latest_backups_are_sorted_by_verification_time() -> None:
    class EmptyResult:
        def scalars(self):
            return []

    class InspectSession:
        async def execute(self, statement, parameters=None):
            if parameters is not None:
                return SimpleNamespace(scalar_one_or_none=lambda: "v2")
            sql = str(statement)
            assert "verified_at DESC" in sql
            assert sql.index("verified_at DESC") < sql.index("created_at DESC")
            return EmptyResult()

    result = asyncio.run(
        list_v2_backups(
            user=SimpleNamespace(id=uuid.uuid4()),
            session=InspectSession(),
        )
    )
    assert result == {"backups": []}


def test_checkpoint_metadata_binds_backup_to_owner() -> None:
    record = backup_record(uuid.uuid4())
    payload = {"notebooks": [], "libraryState": {}, "studyPlannerState": {}}
    envelope = {
        "format": "nanotateczki-server-backup",
        "version": 2,
        "deviceId": str(record.device_id),
        "accountId": str(record.owner_id),
        "schemaVersion": 5,
        "operationCheckpoint": {"account": 42},
        "checksumAlgorithm": "sha256",
        "checksum": compact_sha256(payload),
        "payload": payload,
    }
    validate_envelope(envelope, record)

    envelope["accountId"] = str(uuid.uuid4())
    with pytest.raises(ValueError, match="account"):
        validate_envelope(envelope, record)
    envelope["accountId"] = str(record.owner_id)

    for checkpoint in ({"account": -1}, {"account": True}, {"": 1}, []):
        envelope["operationCheckpoint"] = checkpoint
        with pytest.raises(ValueError, match="checkpoint"):
            validate_envelope(envelope, record)
    envelope["operationCheckpoint"] = {"account": 42}

    envelope["schemaVersion"] = True
    with pytest.raises(ValueError, match="schema version"):
        validate_envelope(envelope, record)
    del envelope["schemaVersion"]
    with pytest.raises(ValueError, match="incomplete"):
        validate_envelope(envelope, record)
